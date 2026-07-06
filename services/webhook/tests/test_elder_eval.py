"""Pure tests for the Elder replay eval harness (#361 slice 2, #537).

NO LLM, NO network - these run in the normal CI test suite. They feed
SYNTHETIC ledger rows + replay results into the pure corpus/scoring/runner core
and assert per-class catch-rate, noise-rate, the out-of-taxonomy
exclusion, baseline-regression detection, and the prompt-sha CI gate.
The live replay (real backends over real PR diffs) is the on-demand
`benchmark.elder-eval.yml` job, not these tests.
"""

from __future__ import annotations

import hashlib

from elder_eval.corpus import (
    build_cases,
    expected_elder_classes,
    normalize_class,
)
from elder_eval.gate import (
    BASELINE_PATH,
    compute_prompt_sha,
    load_baseline,
    merge_baseline,
)
from elder_eval.runner import classes_for_findings, diff_to_hunks
from elder_eval.scoring import (
    CaseReplay,
    compare_to_baseline,
    score,
    to_baseline_dict,
)
from ledger import LedgerRow
from llm_client import Finding


def _row(
    pr: int,
    finding_class: str,
    verdict: str = "fixed",
    reviewer: str = "codex",
    severity: str = "HIGH",
    repo: str = "githumps/grug",
) -> LedgerRow:
    return LedgerRow(
        repo=repo,
        pr=pr,
        reviewer=reviewer,
        severity=severity,
        finding_class=finding_class,
        finding=f"synthetic {finding_class} finding",
        verdict=verdict,
    )


# --- class normalization + taxonomy bridge ---------------------------------


def test_normalize_class_kebabs_labels():
    assert normalize_class("silent failure") == "silent-failure"
    assert normalize_class("Test Coverage") == "test-coverage"
    assert normalize_class("silent-failure") == "silent-failure"


def test_expected_elder_classes_identity_and_aliases():
    # Identity: ledger class that IS an Elder class (modulo kebab).
    assert expected_elder_classes("silent-failure") == frozenset({"silent-failure"})
    assert expected_elder_classes("correctness") == frozenset({"correctness"})
    # Aliases: ledger vocabulary -> Elder vocabulary.
    assert expected_elder_classes("test-gap") == frozenset(
        {"test-coverage", "test-fidelity"}
    )
    assert expected_elder_classes("security-scope") == frozenset({"security"})


def test_expected_elder_classes_out_of_taxonomy_is_empty():
    # Elder has no way to express these - they must be EXCLUDED from the
    # denominator, never scored as misses.
    assert expected_elder_classes("doc-truth") == frozenset()
    assert expected_elder_classes("iac-hygiene") == frozenset()


# --- corpus construction ----------------------------------------------------


def test_build_cases_groups_by_repo_pr_and_splits_verdicts():
    rows = [
        _row(100, "silent-failure", verdict="fixed"),
        _row(100, "correctness", verdict="declined"),
        _row(100, "correctness", verdict="false-positive", reviewer="lore-bot"),
        _row(200, "test-gap", verdict="fixed"),
    ]
    cases = build_cases(rows)
    assert [c.pr for c in cases] == [100, 200]
    c100 = cases[0]
    # Accepted classes (fixed + declined) land in expected_classes.
    assert set(c100.expected_classes) == {"silent-failure", "correctness"}
    # correctness has an accepted row on this case, so the FP row does NOT
    # make it fp-only.
    assert "correctness" not in c100.fp_only_classes


def test_build_cases_fp_only_class_feeds_noise():
    rows = [
        _row(300, "silent-failure", verdict="false-positive"),
        _row(300, "correctness", verdict="fixed"),
    ]
    (case,) = build_cases(rows)
    # silent-failure on PR 300 is known ONLY as a false positive - a replay
    # emission there is noise. Stored in ELDER-normalized form.
    assert "silent-failure" in case.fp_only_classes
    assert set(case.expected_classes) == {"correctness"}


def test_build_cases_counts_out_of_taxonomy():
    rows = [
        _row(400, "doc-truth", verdict="fixed"),
        _row(400, "doc-truth", verdict="fixed"),
        _row(400, "correctness", verdict="fixed"),
        # An out-of-taxonomy FALSE-POSITIVE row must also be counted, not
        # silently union the empty set and vanish.
        _row(400, "iac-hygiene", verdict="false-positive"),
    ]
    (case,) = build_cases(rows)
    assert case.out_of_taxonomy == {"doc-truth": 2, "iac-hygiene": 1}
    assert set(case.expected_classes) == {"correctness"}


def test_build_cases_counts_unknown_verdicts():
    rows = [
        _row(450, "correctness", verdict="fixed"),
        _row(450, "correctness", verdict="pending"),
        _row(450, "silent-failure", verdict="wontfix"),
    ]
    (case,) = build_cases(rows)
    # Unknown verdicts are excluded from scoring but COUNTED - a
    # mislabeled corpus must say why it yielded nothing.
    assert case.unknown_verdicts == {"pending": 1, "wontfix": 1}
    assert set(case.expected_classes) == {"correctness"}


# --- scoring: catch-rate ----------------------------------------------------


def test_score_catch_rate_per_class():
    rows = [
        _row(1, "silent-failure"),
        _row(2, "silent-failure"),
        _row(2, "correctness"),
    ]
    cases = build_cases(rows)
    replays = {
        "githumps/grug#1": CaseReplay(
            case_id="githumps/grug#1",
            emitted={"silent-failure": 1},
            errored=False,
        ),
        "githumps/grug#2": CaseReplay(
            case_id="githumps/grug#2",
            emitted={"correctness": 2},
            errored=False,
        ),
    }
    report = score(cases, replays)
    # silent-failure expected on 2 cases, caught on 1.
    assert report.per_class_catch["silent-failure"] == 0.5
    # correctness expected on 1 case, caught on it.
    assert report.per_class_catch["correctness"] == 1.0
    # Micro overall: 2 caught cells / 3 expected cells.
    assert abs(report.overall_catch - 2 / 3) < 1e-9


def test_score_catch_via_alias():
    # Ledger says test-gap; Elder can only say test-coverage/test-fidelity.
    rows = [_row(5, "test-gap")]
    cases = build_cases(rows)
    replays = {
        "githumps/grug#5": CaseReplay(
            case_id="githumps/grug#5",
            emitted={"test-coverage": 1},
            errored=False,
        ),
    }
    report = score(cases, replays)
    assert report.per_class_catch["test-gap"] == 1.0


# --- scoring: noise ---------------------------------------------------------


def test_score_noise_counts_fp_only_emissions():
    rows = [
        _row(7, "silent-failure", verdict="false-positive"),
        _row(7, "correctness", verdict="fixed"),
    ]
    cases = build_cases(rows)
    replays = {
        "githumps/grug#7": CaseReplay(
            case_id="githumps/grug#7",
            # 3 noise emissions (known-FP cell, counted PER FINDING not
            # per class) + 3 other findings.
            emitted={"silent-failure": 3, "correctness": 2, "performance": 1},
            errored=False,
        ),
    }
    report = score(cases, replays)
    assert abs(report.noise_rate - 3 / 6) < 1e-9


def test_score_noise_vacuous_zero_when_nothing_emitted():
    rows = [_row(8, "correctness")]
    cases = build_cases(rows)
    replays = {
        "githumps/grug#8": CaseReplay(
            case_id="githumps/grug#8", emitted={}, errored=False
        ),
    }
    report = score(cases, replays)
    assert report.noise_rate == 0.0
    assert report.per_class_catch["correctness"] == 0.0


# --- scoring: errored cases (honest-zero rule) ------------------------------


def test_score_errored_case_excluded_from_denominators():
    rows = [
        _row(10, "correctness"),
        _row(11, "correctness"),
    ]
    cases = build_cases(rows)
    replays = {
        "githumps/grug#10": CaseReplay(
            case_id="githumps/grug#10", emitted={"correctness": 1}, errored=False
        ),
        "githumps/grug#11": CaseReplay(
            case_id="githumps/grug#11", emitted={}, errored=True
        ),
    }
    report = score(cases, replays)
    # The errored case must NOT drag catch to 0.5 - it is not a miss, it is
    # a non-run. It is reported, not scored.
    assert report.per_class_catch["correctness"] == 1.0
    assert report.errored_cases == ("githumps/grug#11",)


def test_score_all_errored_guard():
    rows = [_row(20, "correctness")]
    cases = build_cases(rows)
    replays = {
        "githumps/grug#20": CaseReplay(
            case_id="githumps/grug#20", emitted={}, errored=True
        ),
    }
    report = score(cases, replays)
    assert report.all_errored
    # An all-errored run must never look like a valid baseline.
    assert report.per_class_catch == {}


# --- baseline round-trip + regression gate ----------------------------------


def _report(rows, replays):
    return score(build_cases(rows), replays)


def test_baseline_roundtrip_and_no_regression_on_identical():
    rows = [_row(1, "correctness")]
    replays = {
        "githumps/grug#1": CaseReplay(
            case_id="githumps/grug#1", emitted={"correctness": 1}, errored=False
        ),
    }
    report = _report(rows, replays)
    baseline = to_baseline_dict(report, prompt_sha="abc", backend="cave")
    assert baseline["prompt_sha"] == "abc"
    assert compare_to_baseline(report, baseline["backends"]["cave"]) == []


def test_compare_to_baseline_flags_catch_drop_and_noise_rise():
    rows = [
        _row(1, "correctness"),
        _row(1, "silent-failure", verdict="false-positive"),
    ]
    good = {
        "githumps/grug#1": CaseReplay(
            case_id="githumps/grug#1", emitted={"correctness": 1}, errored=False
        ),
    }
    bad = {
        "githumps/grug#1": CaseReplay(
            case_id="githumps/grug#1",
            emitted={"silent-failure": 5},  # misses correctness, emits known FP
            errored=False,
        ),
    }
    baseline = to_baseline_dict(_report(rows, good), prompt_sha="abc", backend="cave")
    regressions = compare_to_baseline(
        _report(rows, bad), baseline["backends"]["cave"]
    )
    joined = " ".join(regressions)
    assert "overall_catch" in joined
    assert "noise_rate" in joined


def test_compare_to_baseline_flags_coverage_loss():
    rows = [_row(1, "correctness"), _row(2, "correctness")]
    full = {
        "githumps/grug#1": CaseReplay(
            case_id="githumps/grug#1", emitted={"correctness": 1}, errored=False
        ),
        "githumps/grug#2": CaseReplay(
            case_id="githumps/grug#2", emitted={"correctness": 1}, errored=False
        ),
    }
    partial = {
        "githumps/grug#1": CaseReplay(
            case_id="githumps/grug#1", emitted={"correctness": 1}, errored=False
        ),
        "githumps/grug#2": CaseReplay(
            case_id="githumps/grug#2", emitted={}, errored=True
        ),
    }
    baseline = to_baseline_dict(_report(rows, full), prompt_sha="abc", backend="cave")
    # Rates are identical (1.0) over the surviving case - but the errored
    # case + shrunken coverage must fail the check anyway.
    regressions = compare_to_baseline(
        _report(rows, partial), baseline["backends"]["cave"]
    )
    joined = " ".join(regressions)
    assert "errored" in joined
    assert "cases_scored shrank" in joined


def test_compare_to_baseline_tolerates_within_tolerance():
    rows = [_row(1, "correctness"), _row(2, "correctness")]
    full = {
        "githumps/grug#1": CaseReplay(
            case_id="githumps/grug#1", emitted={"correctness": 1}, errored=False
        ),
        "githumps/grug#2": CaseReplay(
            case_id="githumps/grug#2", emitted={"correctness": 1}, errored=False
        ),
    }
    report = _report(rows, full)
    baseline = to_baseline_dict(report, prompt_sha="abc", backend="cave")
    # A drop smaller than the tolerance passes.
    assert (
        compare_to_baseline(report, baseline["backends"]["cave"], catch_tolerance=0.5)
        == []
    )


def test_score_raises_on_orphan_replay():
    """A replay whose case_id matches no case would silently vanish from
    every metric - the join-key-drift tripwire must raise instead."""
    import pytest

    cases = build_cases([_row(1, "correctness")])
    orphan = {
        "githumps/grug#999": CaseReplay(
            case_id="githumps/grug#999", emitted={}, errored=False
        ),
    }
    with pytest.raises(ValueError, match="unknown cases"):
        score(cases, orphan)


def test_score_case_with_no_replay_is_errored():
    """A case the replays dict never mentions did not run - it must land
    in errored_cases, not silently shrink the corpus."""
    cases = build_cases([_row(1, "correctness")])
    report = score(cases, {})
    assert report.errored_cases == ("githumps/grug#1",)
    assert report.all_errored


def test_merge_baseline_same_prompt_keeps_other_backends():
    existing = {
        "prompt_sha": "abc",
        "backends": {"openrouter": {"overall_catch": 0.5}, "sparkles": {"overall_catch": 0.1}},
    }
    fresh = {
        "prompt_sha": "abc",
        "backends": {"sparkles": {"overall_catch": 0.2}},
    }
    merged, dropped = merge_baseline(existing, fresh)
    assert dropped == []
    assert merged["backends"]["openrouter"] == {"overall_catch": 0.5}
    assert merged["backends"]["sparkles"] == {"overall_catch": 0.2}


def test_merge_baseline_changed_prompt_drops_stale_backends():
    """Other backends' scores describe the OLD prompt - carrying them
    under the new prompt_sha would re-bless stale data as fresh."""
    existing = {
        "prompt_sha": "old",
        "backends": {"openrouter": {"overall_catch": 0.5}, "sparkles": {"overall_catch": 0.1}},
    }
    fresh = {
        "prompt_sha": "new",
        "backends": {"sparkles": {"overall_catch": 0.2}},
    }
    merged, dropped = merge_baseline(existing, fresh)
    assert dropped == ["openrouter"]
    assert set(merged["backends"]) == {"sparkles"}
    assert merged["prompt_sha"] == "new"


def test_bounded_hunks_truncates_at_whole_hunk_boundary():
    from elder_eval.runner import bounded_hunks

    hunk = (
        "diff --git a/{f} b/{f}\n--- a/{f}\n+++ b/{f}\n"
        "@@ -1 +1,2 @@\n a = 1\n+b = 2\n"
    )
    diff = "".join(hunk.format(f=f"f{i}.py") for i in range(3))
    all_hunks, truncated = bounded_hunks(diff, budget=10_000)
    assert len(all_hunks) == 3 and not truncated
    # A tight budget keeps only WHOLE leading hunks and reports truncation.
    body_len = len(all_hunks[0].body)
    kept, truncated = bounded_hunks(diff, budget=body_len + 1)
    assert len(kept) == 1 and truncated
    assert kept[0].body.startswith("@@")


def test_bounded_hunks_keeps_single_oversized_hunk():
    from elder_eval.runner import bounded_hunks

    diff = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
        "@@ -1 +1,2 @@\n a = 1\n+b = 2\n"
    )
    # An empty replay would be a worse lie than an oversized prompt.
    kept, truncated = bounded_hunks(diff, budget=1)
    assert len(kept) == 1 and not truncated


def test_score_threads_truncated_cases_into_report():
    rows = [_row(1, "correctness")]
    cases = build_cases(rows)
    replays = {
        "githumps/grug#1": CaseReplay(
            case_id="githumps/grug#1",
            emitted={"correctness": 1},
            errored=False,
            truncated=True,
        ),
    }
    report = score(cases, replays)
    assert report.truncated_cases == ("githumps/grug#1",)
    baseline = to_baseline_dict(report, prompt_sha="abc", backend="cave")
    assert baseline["backends"]["cave"]["truncated_cases"] == ["githumps/grug#1"]


def test_run_case_parse_failure_is_errored(monkeypatch):
    """A broken/unparseable LLM response must be errored=True, never a
    fabricated 'Elder found nothing' - a fake zero recorded into the
    baseline would bless a broken parser as real behavior forever."""
    import httpx

    from elder_eval import runner
    from sast_benchmark.backends import BenchBackend

    (case,) = build_cases([_row(1, "correctness")])
    backend = BenchBackend(name="fake", url="http://invalid", model="m", api_key="")
    monkeypatch.setattr(
        runner, "_post",
        lambda b, m: httpx.Response(
            200, content=b"not json at all",
            request=httpx.Request("POST", "http://invalid"),
        ),
    )
    diff = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
        "@@ -1 +1,2 @@\n a = 1\n+b = 2\n"
    )
    replay = runner.run_case(backend, case, diff)
    assert replay.errored
    assert replay.emitted == {}


# --- the CI gate: prompt changes require a re-recorded baseline --------------


def test_prompt_sha_is_sha256_of_prompt_source():
    import code_review_prompt

    src = code_review_prompt.__file__
    assert src is not None
    expected = hashlib.sha256(open(src, "rb").read()).hexdigest()
    assert compute_prompt_sha() == expected


def test_baseline_exists_and_prompt_sha_matches():
    """THE CI gate (#537): if this fails, code_review_prompt.py changed
    without re-running the eval. Run:

        python -m elder_eval --record   (with a bench backend configured)

    and commit the refreshed elder_eval/baseline.json IN THE SAME PR as
    the prompt change."""
    assert BASELINE_PATH.exists(), (
        "elder_eval/baseline.json missing - record it with "
        "`python -m elder_eval --record`"
    )
    baseline = load_baseline()
    assert baseline["prompt_sha"] == compute_prompt_sha(), (
        "code_review_prompt.py changed but elder_eval/baseline.json was not "
        "re-recorded - run `python -m elder_eval --record` and commit the "
        "refreshed baseline in this PR"
    )


# --- runner pure bits (no network) -------------------------------------------


def test_classes_for_findings_maps_rule_to_bug_class():
    findings = (
        Finding(
            path="a.py", line=1, rule="sync-io-in-async",
            severity="high", message="m",
        ),
        Finding(
            path="a.py", line=2, rule="null-deref",
            severity="high", message="m",
        ),
        Finding(
            path="a.py", line=3, rule="off-by-one-or-bounds",
            severity="low", message="m",
        ),
    )
    classes = classes_for_findings(findings)
    assert classes == {"async-blocker": 1, "correctness": 2}


def test_classes_for_findings_unknown_rule_falls_back_to_rule_name():
    findings = (
        Finding(
            path="a.py", line=1, rule="Some Novel Rule",
            severity="low", message="m",
        ),
    )
    assert classes_for_findings(findings) == {"some-novel-rule": 1}


def test_run_eval_fetch_failure_is_errored_case():
    """run_eval's injectable fetch: a diff-fetch failure (404'd corpus PR,
    rate limit, network) must become an errored CaseReplay, never a crash
    and never a fake 'Elder found nothing'."""
    import httpx

    from elder_eval.runner import run_eval
    from sast_benchmark.backends import BenchBackend

    rows = [_row(1, "correctness"), _row(2, "correctness")]
    cases = build_cases(rows)
    backend = BenchBackend(name="fake", url="http://invalid", model="m", api_key="")

    def failing_fetch(repo: str, pr: int, token: str) -> str:
        if pr == 1:
            raise httpx.HTTPStatusError(
                "gone",
                request=httpx.Request("GET", "http://x"),
                response=httpx.Response(404, request=httpx.Request("GET", "http://x")),
            )
        raise ValueError("boom")

    replays = run_eval(backend, cases, fetch=failing_fetch)
    assert all(r.errored for r in replays.values())
    report = score(cases, replays)
    assert report.all_errored


def test_diff_to_hunks_converts_unified_diff():
    diff = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1,2 +1,3 @@\n"
        " a = 1\n"
        "+b = 2\n"
        " c = 3\n"
    )
    hunks = diff_to_hunks(diff)
    assert len(hunks) == 1
    assert hunks[0].path == "x.py"
    assert hunks[0].body.startswith("@@")
