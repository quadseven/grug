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
from llm_client import Backend, Finding, FindingJudgement, LlmReviewResponse
from review_pipeline import ReviewCoverage


def _row(
    pr: int,
    finding_class: str,
    verdict: str = "fixed",
    reviewer: str = "codex",
    severity: str = "HIGH",
    repo: str = "quadseven/grug",
    commit: str | None = None,
    head_sha: str | None = None,
) -> LedgerRow:
    return LedgerRow(
        repo=repo,
        pr=pr,
        reviewer=reviewer,
        severity=severity,
        finding_class=finding_class,
        finding=f"synthetic {finding_class} finding",
        verdict=verdict,
        commit=commit,
        head_sha=head_sha,
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


def test_parse_row_normalizes_annotated_verdicts():
    """Historical ledger rows embed the reason in the verdict -
    'declined(bounded: ...)' - the leading token is the label. Without
    normalization those rows silently matched NO verdict class."""
    from ledger import parse_row

    row = parse_row({
        "repo": "quadseven/grug", "pr": 1, "reviewer": "codex",
        "class": "correctness", "finding": "f",
        "verdict": "declined(bounded: advisory only)",
    })
    assert row is not None
    assert row.verdict == "declined"
    assert row.accepted


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


# --- #545: snapshot-anchored replay corpus derivation -----------------------


_SHA_A = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
_SHA_B = "b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3"


def test_parse_row_reads_head_sha():
    from ledger import parse_row

    row = parse_row({
        "repo": "quadseven/grug", "pr": 1, "reviewer": "codex",
        "class": "correctness", "finding": "f", "verdict": "fixed",
        "head_sha": _SHA_A,
    })
    assert row is not None
    assert row.head_sha == _SHA_A


def test_build_cases_anchors_on_explicit_head_sha():
    """A row carrying `head_sha` (#545, the reviewed PR-head) anchors the
    case directly - no fix-commit guessing needed."""
    (case,) = build_cases([_row(600, "correctness", head_sha=_SHA_A)])
    assert case.anchored
    assert case.anchor_head_sha == _SHA_A
    assert case.anchor_fix_commit is None


def test_build_cases_falls_back_to_commit_as_fix_sha():
    """Historical rows have no `head_sha` - a sha-shaped `commit` is treated
    as the FIX commit (its PARENT is the pre-fix state, resolved later by
    the live runner)."""
    (case,) = build_cases([_row(601, "correctness", commit="65103f0")])
    assert case.anchored
    assert case.anchor_fix_commit == "65103f0"
    assert case.anchor_head_sha is None


def test_build_cases_rejects_non_sha_commit_junk():
    """The real corpus's `commit` field carries free text on many rows
    ('-', 'golang:1.26', 'sed-sim-test', ...) - none of that may be treated
    as a fix-commit SHA. Unanchored means final-diff replay, tagged False."""
    for junk in ("-", "golang:1.26", "sed-sim-test", "GRUG_RA_ROLE_ARN"):
        (case,) = build_cases([_row(602, "correctness", commit=junk)])
        assert not case.anchored, f"{junk!r} must not look like a sha"
        assert case.anchor_fix_commit is None


def test_build_cases_no_anchor_information_is_unanchored():
    (case,) = build_cases([_row(603, "correctness")])
    assert not case.anchored
    assert case.anchor_head_sha is None
    assert case.anchor_fix_commit is None


def test_build_cases_head_sha_outranks_fix_commit():
    """A PR with one row carrying a fix-commit and another carrying the
    real reviewed head must prefer the explicit head - it is strictly more
    trustworthy than a guess."""
    rows = [
        _row(604, "correctness", commit="65103f0"),
        _row(604, "silent-failure", head_sha=_SHA_A),
    ]
    (case,) = build_cases(rows)
    assert case.anchor_head_sha == _SHA_A
    assert case.anchor_fix_commit is None


def test_build_cases_anchor_conflict_keeps_first_seen_and_does_not_crash():
    """Two rows on the same PR disagreeing on `head_sha` must not raise -
    the eval keeps running on a messy corpus (logged, not fatal)."""
    rows = [
        _row(605, "correctness", head_sha=_SHA_A),
        _row(605, "silent-failure", head_sha=_SHA_B),
    ]
    (case,) = build_cases(rows)
    assert case.anchor_head_sha == _SHA_A


# --- scoring: catch-rate ----------------------------------------------------


def test_score_catch_rate_per_class():
    rows = [
        _row(1, "silent-failure"),
        _row(2, "silent-failure"),
        _row(2, "correctness"),
    ]
    cases = build_cases(rows)
    replays = {
        "quadseven/grug#1": CaseReplay(
            case_id="quadseven/grug#1",
            emitted={"silent-failure": 1},
            errored=False,
        ),
        "quadseven/grug#2": CaseReplay(
            case_id="quadseven/grug#2",
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
        "quadseven/grug#5": CaseReplay(
            case_id="quadseven/grug#5",
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
        "quadseven/grug#7": CaseReplay(
            case_id="quadseven/grug#7",
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
        "quadseven/grug#8": CaseReplay(
            case_id="quadseven/grug#8", emitted={}, errored=False
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
        "quadseven/grug#10": CaseReplay(
            case_id="quadseven/grug#10", emitted={"correctness": 1}, errored=False
        ),
        "quadseven/grug#11": CaseReplay(
            case_id="quadseven/grug#11", emitted={}, errored=True
        ),
    }
    report = score(cases, replays)
    # The errored case must NOT drag catch to 0.5 - it is not a miss, it is
    # a non-run. It is reported, not scored.
    assert report.per_class_catch["correctness"] == 1.0
    assert report.errored_cases == ("quadseven/grug#11",)


def test_score_all_errored_guard():
    rows = [_row(20, "correctness")]
    cases = build_cases(rows)
    replays = {
        "quadseven/grug#20": CaseReplay(
            case_id="quadseven/grug#20", emitted={}, errored=True
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
        "quadseven/grug#1": CaseReplay(
            case_id="quadseven/grug#1", emitted={"correctness": 1}, errored=False
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
        "quadseven/grug#1": CaseReplay(
            case_id="quadseven/grug#1", emitted={"correctness": 1}, errored=False
        ),
    }
    bad = {
        "quadseven/grug#1": CaseReplay(
            case_id="quadseven/grug#1",
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
        "quadseven/grug#1": CaseReplay(
            case_id="quadseven/grug#1", emitted={"correctness": 1}, errored=False
        ),
        "quadseven/grug#2": CaseReplay(
            case_id="quadseven/grug#2", emitted={"correctness": 1}, errored=False
        ),
    }
    partial = {
        "quadseven/grug#1": CaseReplay(
            case_id="quadseven/grug#1", emitted={"correctness": 1}, errored=False
        ),
        "quadseven/grug#2": CaseReplay(
            case_id="quadseven/grug#2", emitted={}, errored=True
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
        "quadseven/grug#1": CaseReplay(
            case_id="quadseven/grug#1", emitted={"correctness": 1}, errored=False
        ),
        "quadseven/grug#2": CaseReplay(
            case_id="quadseven/grug#2", emitted={"correctness": 1}, errored=False
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
        "quadseven/grug#999": CaseReplay(
            case_id="quadseven/grug#999", emitted={}, errored=False
        ),
    }
    with pytest.raises(ValueError, match="unknown cases"):
        score(cases, orphan)


def test_score_unscorable_case_counts_rows_without_erroring():
    """A fully out-of-taxonomy case is never replayed - its excluded-row
    tallies must still reach the report, and it must NOT read as errored
    (which would trip the --record refusal and all_errored guard)."""
    rows = [
        _row(1, "correctness"),
        _row(2, "doc-truth"),  # entire case out of taxonomy -> unscorable
    ]
    cases = build_cases(rows)
    replays = {
        "quadseven/grug#1": CaseReplay(
            case_id="quadseven/grug#1", emitted={"correctness": 1}, errored=False
        ),
    }
    report = score(cases, replays)
    assert report.out_of_taxonomy == {"doc-truth": 1}
    assert report.errored_cases == ()
    assert report.cases_scored == 1


def test_score_case_with_no_replay_is_errored():
    """A case the replays dict never mentions did not run - it must land
    in errored_cases, not silently shrink the corpus."""
    cases = build_cases([_row(1, "correctness")])
    report = score(cases, {})
    assert report.errored_cases == ("quadseven/grug#1",)
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
        "quadseven/grug#1": CaseReplay(
            case_id="quadseven/grug#1",
            emitted={"correctness": 1},
            errored=False,
            truncated=True,
        ),
    }
    report = score(cases, replays)
    assert report.truncated_cases == ("quadseven/grug#1",)
    baseline = to_baseline_dict(report, prompt_sha="abc", backend="cave")
    assert baseline["backends"]["cave"]["truncated_cases"] == ["quadseven/grug#1"]


def test_score_threads_anchored_cases_into_report_and_baseline():
    """#545: which scored cases replayed the pre-fix snapshot must reach
    the report AND the baseline - a mixed corpus must not average the bias
    away silently."""
    rows = [_row(1, "correctness"), _row(2, "correctness")]
    cases = build_cases(rows)
    replays = {
        "quadseven/grug#1": CaseReplay(
            case_id="quadseven/grug#1", emitted={"correctness": 1},
            errored=False, anchored=True,
        ),
        "quadseven/grug#2": CaseReplay(
            case_id="quadseven/grug#2", emitted={"correctness": 1},
            errored=False, anchored=False,
        ),
    }
    report = score(cases, replays)
    assert report.anchored_cases == ("quadseven/grug#1",)
    assert report.cases_scored == 2
    baseline = to_baseline_dict(report, prompt_sha="abc", backend="cave")
    assert baseline["backends"]["cave"]["anchored_cases"] == ["quadseven/grug#1"]


def test_score_threads_staged_cases_into_report_and_baseline():
    """#859 follow-up: a case reviewed via multiple staged cohort calls
    (instead of one monolithic call) must say so in the report AND the
    baseline - that case's number describes a different methodology than
    an unstaged replay, and a reader must be able to see which cases it
    is, not infer it."""
    rows = [_row(1, "correctness")]
    cases = build_cases(rows)
    replays = {
        "quadseven/grug#1": CaseReplay(
            case_id="quadseven/grug#1", emitted={"correctness": 1},
            errored=False, staged=True,
        ),
    }
    report = score(cases, replays)
    assert report.staged_cases == ("quadseven/grug#1",)
    baseline = to_baseline_dict(report, prompt_sha="abc", backend="cave")
    assert baseline["backends"]["cave"]["staged_cases"] == ["quadseven/grug#1"]


# --- #859 follow-up: bench mode stages an oversized diff like production --


def _diff_for(*paths: str) -> str:
    """One trivial one-line-added hunk per path, unified-diff shaped."""
    return "".join(
        f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n"
        "@@ -1 +1,2 @@\n a = 1\n+b = 2\n"
        for p in paths
    )


def _bench_response(findings: list[dict]):
    import httpx
    import json as _json

    body = _json.dumps({"findings": findings})
    return httpx.Response(
        200,
        json={"model": "m", "choices": [{"message": {"content": body}}]},
        request=httpx.Request("POST", "http://x"),
    )


def _finding(path: str) -> dict:
    return {
        "path": path, "line": 1, "rule": "correctness",
        "severity": "high", "message": "m",
    }


def test_run_case_sends_one_call_for_a_diff_that_fits_one_cohort(monkeypatch):
    """Pre-existing (small-diff) behavior must not change: exactly one
    backend call, not staged. This is the regression guard for every
    OTHER corpus case's comparability with the existing baseline."""
    from elder_eval import runner
    from sast_benchmark.backends import BenchBackend

    calls = []

    def fake_post(backend, messages):
        calls.append(messages)
        return _bench_response([])

    monkeypatch.setattr(runner, "_post", fake_post)
    (case,) = build_cases([_row(1, "correctness")])
    backend = BenchBackend(name="fake", url="http://invalid", model="m", api_key="")
    replay = runner.run_case(backend, case, _diff_for("x.py"))

    assert len(calls) == 1
    assert replay.staged is False
    assert replay.errored is False


def test_run_case_stages_an_oversized_diff_into_multiple_cohort_calls(monkeypatch):
    """THE fix (#859 follow-up): bench mode used to send the WHOLE diff as
    ONE monolithic call no matter its size - which is exactly what made
    grug#494 (56 files, 5236 lines) time out and refuse to record even at
    a 900s ceiling. A diff too big for one bounded cohort must now be
    staged into multiple smaller calls via the real `plan_review` packer,
    and every cohort's findings must reach the replay - not just the
    first one."""
    from elder_eval import runner
    from sast_benchmark.backends import BenchBackend

    diff = _diff_for("a.py", "b.py")
    hunks = runner.diff_to_hunks(diff)
    assert len(hunks) == 2
    one_hunk_chars = max(len(h.body) for h in hunks)
    # Fits ONE hunk alone, not both together - forces the packer to split
    # this into 2 cohorts instead of 1.
    monkeypatch.setattr(runner, "review_cohort_char_budget", lambda: one_hunk_chars + 1)
    monkeypatch.setattr(runner, "_review_cohort_paths", lambda: 10)

    calls = []

    def fake_post(backend, messages):
        calls.append(messages)
        # Each call's response is scoped to whichever path is actually in
        # that call's prompt, so the assertion below can only pass if
        # BOTH cohorts really ran (not just the first, would-be-only one).
        content = messages[-1]["content"]
        path = "a.py" if "a.py" in content else "b.py"
        return _bench_response([_finding(path)])

    monkeypatch.setattr(runner, "_post", fake_post)
    (case,) = build_cases([_row(2, "correctness")])
    backend = BenchBackend(name="fake", url="http://invalid", model="m", api_key="")
    replay = runner.run_case(backend, case, diff)

    assert len(calls) == 2, (
        "a diff too big for one cohort must cost multiple bounded calls, "
        "not one monolithic call"
    )
    assert replay.staged is True
    assert replay.errored is False
    assert replay.emitted == {"correctness": 2}, "findings from BOTH cohorts must merge"


def test_run_case_refuses_a_hunk_too_large_for_any_cohort(monkeypatch):
    """A hunk bigger than a WHOLE cohort can never be reviewed - the
    planner will not truncate it (truncation corrupts line anchors), same
    as production's `split_oversized_hunks`. It must never fabricate a
    partial answer: no model call happens at all, and the case errors."""
    from elder_eval import runner
    from sast_benchmark.backends import BenchBackend

    diff = _diff_for("big.py")
    hunks = runner.diff_to_hunks(diff)
    monkeypatch.setattr(
        runner, "review_cohort_char_budget", lambda: len(hunks[0].body) - 1
    )
    monkeypatch.setattr(runner, "_review_cohort_paths", lambda: 10)

    calls = []
    monkeypatch.setattr(runner, "_post", lambda b, m: calls.append(m))
    (case,) = build_cases([_row(3, "correctness")])
    backend = BenchBackend(name="fake", url="http://invalid", model="m", api_key="")
    replay = runner.run_case(backend, case, diff)

    assert calls == [], "an unreviewable oversized hunk must never reach a model call"
    assert replay.errored is True


def test_run_case_staged_cohort_failure_errors_the_whole_case(monkeypatch):
    """A staged replay is scored ALL-OR-NOTHING: one cohort's parse
    failure must not silently shrink the case into a fake, apparently-
    complete replay of only the cohorts that happened to succeed -
    mirrors `run_production_case`'s refusal to score incomplete
    `review_diff` coverage."""
    from elder_eval import runner
    from sast_benchmark.backends import BenchBackend

    diff = _diff_for("a.py", "b.py")
    hunks = runner.diff_to_hunks(diff)
    one_hunk_chars = max(len(h.body) for h in hunks)
    monkeypatch.setattr(runner, "review_cohort_char_budget", lambda: one_hunk_chars + 1)
    monkeypatch.setattr(runner, "_review_cohort_paths", lambda: 10)

    calls = []

    def fake_post(backend, messages):
        import httpx

        calls.append(messages)
        if len(calls) == 1:
            return _bench_response([_finding("a.py")])
        return httpx.Response(
            200, content=b"not json at all",
            request=httpx.Request("POST", "http://x"),
        )

    monkeypatch.setattr(runner, "_post", fake_post)
    (case,) = build_cases([_row(4, "correctness")])
    backend = BenchBackend(name="fake", url="http://invalid", model="m", api_key="")
    replay = runner.run_case(backend, case, diff)

    assert len(calls) == 2
    assert replay.errored is True
    assert replay.emitted == {}, "a partial staged replay must never publish partial findings"


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


def test_run_case_thinking_model_null_content_is_diagnosed_not_a_bare_typeerror(
    monkeypatch, caplog,
):
    """grug#881: a response shaped like the real failing run (`content:
    null`, `finish_reason: "length"`) must take the dedicated
    `eval_case_parse_failed` branch - diagnosable in one log line - never
    fall through to the generic `eval_case_errored` catch-all as a bare
    `kind=TypeError` (which is what cost 90 minutes of CI on run
    31901049362, scoring zero cases)."""
    import httpx

    from elder_eval import runner
    from sast_benchmark.backends import BenchBackend

    def fake_post(backend, messages):
        body = {
            "model": "poolside/laguna-s-2.1:free",
            "choices": [
                {
                    "message": {"role": "assistant", "content": None},
                    "finish_reason": "length",
                }
            ],
        }
        return httpx.Response(
            200, json=body, request=httpx.Request("POST", "http://invalid"),
        )

    monkeypatch.setattr(runner, "_post", fake_post)
    (case,) = build_cases([_row(5, "correctness")])
    backend = BenchBackend(name="fake", url="http://invalid", model="m", api_key="")
    diff = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
        "@@ -1 +1,2 @@\n a = 1\n+b = 2\n"
    )
    with caplog.at_level("WARNING"):
        replay = runner.run_case(backend, case, diff)

    assert replay.errored is True
    messages = [r.message for r in caplog.records]
    assert any("eval_case_parse_failed" in m for m in messages), (
        "a null-content response must log via the dedicated parse-failed "
        "branch, which carries the diagnosable err= detail"
    )
    assert not any("eval_case_errored" in m for m in messages), (
        "it must NOT fall through to the generic catch-all - that is "
        "exactly the grug#881 bug (a raised TypeError logged as one word)"
    )


def test_run_case_errored_logs_exception_message_not_just_kind(monkeypatch, caplog):
    """grug#881 defect 2: `kind=TypeError` alone told an operator nothing
    for 90 minutes of CI. Any exception that DOES reach the catch-all must
    have its message logged too, not just its class name."""
    from elder_eval import runner
    from sast_benchmark.backends import BenchBackend

    def fake_post(backend, messages):
        raise RuntimeError("boom-specific-detail-worth-logging")

    monkeypatch.setattr(runner, "_post", fake_post)
    (case,) = build_cases([_row(6, "correctness")])
    backend = BenchBackend(name="fake", url="http://invalid", model="m", api_key="")
    diff = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
        "@@ -1 +1,2 @@\n a = 1\n+b = 2\n"
    )
    with caplog.at_level("WARNING"):
        replay = runner.run_case(backend, case, diff)

    assert replay.errored is True
    assert any(
        "boom-specific-detail-worth-logging" in r.message for r in caplog.records
    ), "the exception MESSAGE must reach the log, not just kind=RuntimeError"


def test_run_case_empty_content_after_thinking_logs_finish_reason(monkeypatch, caplog):
    """grug#881: the shape the issue observed live on the in-cluster gateway
    - `content: ""`, the budget spent in `reasoning`, `finish_reason:
    "length"` - must surface finish_reason on the one `eval_case_parse_failed`
    line, so a 90-minute run's log says WHY the case produced nothing."""
    import httpx

    from elder_eval import runner
    from sast_benchmark.backends import BenchBackend

    def fake_post(backend, messages):
        body = {
            "model": "some-thinking-model",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning": "Here's a thinking process:\n\n1. **An",
                    },
                    "finish_reason": "length",
                }
            ],
        }
        return httpx.Response(
            200, json=body, request=httpx.Request("POST", "http://invalid"),
        )

    monkeypatch.setattr(runner, "_post", fake_post)
    (case,) = build_cases([_row(7, "correctness")])
    backend = BenchBackend(name="fake", url="http://invalid", model="m", api_key="")
    diff = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
        "@@ -1 +1,2 @@\n a = 1\n+b = 2\n"
    )
    with caplog.at_level("WARNING"):
        replay = runner.run_case(backend, case, diff)

    assert replay.errored is True
    parse_failed = [
        r.getMessage() for r in caplog.records if "eval_case_parse_failed" in r.getMessage()
    ]
    assert parse_failed, "empty content must take the dedicated parse-failed branch"
    assert "finish_reason=length" in parse_failed[0]
    assert "reasoning field present" in parse_failed[0]
    assert not any("eval_case_errored" in r.getMessage() for r in caplog.records)


def test_production_case_uses_full_diff_and_scores_complete_staged_result():
    from elder_eval.runner import run_production_case

    (case,) = build_cases([_row(30, "correctness")])
    diff = (
        "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
        "diff --git a/tests/test_a.py b/tests/test_a.py\n--- a/tests/test_a.py\n+++ b/tests/test_a.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    seen = {}

    def review(hunks, **kwargs):
        seen["paths"] = [h.path for h in hunks]
        seen["context"] = kwargs["pr_context"]
        return LlmReviewResponse(
            kind="reviewed",
            findings=(Finding(
                path="src/a.py",
                line=1,
                rule="correctness",
                severity="high",
                message="wrong result",
            ),),
            backend_used=Backend.CAVE,
            model_name="specialist",
            coverage=ReviewCoverage(
                total_cohorts=2,
                completed_cohorts=2,
                failed_cohorts=(),
                cohort_labels=("src", "tests"),
            ),
        )

    replay = run_production_case(case, diff, review=review)

    assert seen["paths"] == ["src/a.py", "tests/test_a.py"]
    assert seen["context"]["review_phase"] == "eval-production"
    assert replay.emitted == {"correctness": 1}
    assert replay.errored is False


def test_production_case_refuses_to_score_partial_coverage():
    from elder_eval.runner import run_production_case

    (case,) = build_cases([_row(31, "correctness")])
    diff = (
        "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )

    def review(_hunks, **_kwargs):
        return LlmReviewResponse(
            kind="reviewed",
            coverage=ReviewCoverage(
                total_cohorts=2,
                completed_cohorts=1,
                failed_cohorts=(2,),
                cohort_labels=("src", "tests"),
            ),
            error="partial review: cohorts [2] failed",
        )

    replay = run_production_case(case, diff, review=review)

    assert replay.errored is True
    assert replay.emitted == {}


def test_production_case_can_score_post_judge_published_findings():
    from elder_eval.runner import run_production_case

    (case,) = build_cases([_row(32, "correctness")])
    diff = (
        "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )

    def review(_hunks, **_kwargs):
        return LlmReviewResponse(
            kind="reviewed",
            findings=(
                Finding(
                    path="src/a.py",
                    line=1,
                    rule="correctness",
                    severity="medium",
                    message="looks wrong",
                ),
            ),
        )

    def grade(evaluation, _hunks, _installation_id, **_kwargs):
        assert evaluation.findings[0].rule_name == "correctness"
        return (
            FindingJudgement(
                finding_index=0,
                is_real_bug=False,
                reasoning="contradicted by evidence",
                confidence=0.95,
            ),
        )

    replay = run_production_case(
        case,
        diff,
        review=review,
        published=True,
        grade=grade,
    )

    assert replay.emitted == {}
    assert replay.errored is False


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


# --- #545: snapshot-anchored replay (live runner, no network) ---------------


def test_resolve_anchor_sha_prefers_explicit_head_sha():
    """An explicit reviewed head never needs a GitHub round-trip."""
    from elder_eval.runner import resolve_anchor_sha

    (case,) = build_cases([_row(610, "correctness", head_sha=_SHA_A)])

    def must_not_be_called(*_a, **_kw):
        raise AssertionError("fetch_parent must not run when head_sha is set")

    assert resolve_anchor_sha(case, "tok", fetch_parent=must_not_be_called) == _SHA_A


def test_resolve_anchor_sha_resolves_fix_commit_parent():
    """No head_sha - the (sha-shaped) `commit` is the FIX commit; its
    PARENT is the pre-fix anchor, resolved through the injected callable."""
    from elder_eval.runner import resolve_anchor_sha

    (case,) = build_cases([_row(611, "correctness", commit="65103f0")])
    seen = {}

    def fetch_parent(repo: str, commit_sha: str, token: str) -> str | None:
        seen["args"] = (repo, commit_sha, token)
        return "parent-sha"

    assert resolve_anchor_sha(case, "tok", fetch_parent=fetch_parent) == "parent-sha"
    assert seen["args"] == ("quadseven/grug", "65103f0", "tok")


def test_resolve_anchor_sha_none_when_case_unanchored():
    from elder_eval.runner import resolve_anchor_sha

    (case,) = build_cases([_row(612, "correctness")])
    assert resolve_anchor_sha(case, "tok") is None


def test_diff_for_case_uses_anchored_diff_when_resolvable():
    """The core #545 behavior: an anchored case replays base...anchor_sha,
    NOT the PR's final merged diff, and reports anchored=True."""
    from elder_eval.runner import diff_for_case

    (case,) = build_cases([_row(613, "correctness", head_sha=_SHA_A)])
    calls = []

    def fake_resolve(case_, token, **_kw):
        calls.append(("resolve", case_.case_id, token))
        return _SHA_A

    def fake_fetch_anchored(repo, pr, anchor_sha, token):
        calls.append(("fetch_anchored", repo, pr, anchor_sha, token))
        return "PRE-FIX DIFF"

    def fake_fetch_final(repo, pr, token):
        calls.append(("fetch_final",))
        return "FINAL DIFF"

    diff, anchored = diff_for_case(
        case, "tok",
        fetch_final=fake_fetch_final,
        resolve_anchor=fake_resolve,
        fetch_anchored=fake_fetch_anchored,
    )
    assert (diff, anchored) == ("PRE-FIX DIFF", True)
    assert ("fetch_final",) not in calls
    assert ("fetch_anchored", "quadseven/grug", 613, _SHA_A, "tok") in calls


def test_diff_for_case_falls_back_when_case_unanchored():
    from elder_eval.runner import diff_for_case

    (case,) = build_cases([_row(614, "correctness")])
    diff, anchored = diff_for_case(
        case, "", fetch_final=lambda repo, pr, token: "FINAL DIFF"
    )
    assert (diff, anchored) == ("FINAL DIFF", False)


def test_diff_for_case_falls_back_when_anchor_resolution_raises():
    """A GitHub error resolving the fix-commit's parent (or the compare
    call) must fall back to the final diff, never error the whole case -
    only #545's own resolution machinery may be flaky, not the case."""
    from elder_eval.runner import diff_for_case

    (case,) = build_cases([_row(615, "correctness", head_sha=_SHA_A)])

    def boom(*_a, **_kw):
        raise RuntimeError("github unreachable")

    diff, anchored = diff_for_case(
        case, "",
        fetch_final=lambda repo, pr, token: "FINAL DIFF",
        resolve_anchor=boom,
    )
    assert (diff, anchored) == ("FINAL DIFF", False)


def test_diff_for_case_falls_back_when_anchor_unresolvable():
    """A fix commit with no reachable parent (resolve_anchor returns None,
    e.g. a root commit) must fall back, not raise or silently error."""
    from elder_eval.runner import diff_for_case

    (case,) = build_cases([_row(616, "correctness", commit="65103f0")])
    diff, anchored = diff_for_case(
        case, "",
        fetch_final=lambda repo, pr, token: "FINAL DIFF",
        resolve_anchor=lambda case_, token: None,
    )
    assert (diff, anchored) == ("FINAL DIFF", False)


def test_run_eval_threads_anchored_flag_from_diff_for_case(monkeypatch):
    """run_eval must stamp whatever `diff_for_case` decided onto the
    CaseReplay it produces - the report/baseline split (#545) is built on
    this."""
    import json

    import httpx

    from elder_eval import runner
    from elder_eval.runner import run_eval
    from sast_benchmark.backends import BenchBackend

    (case,) = build_cases([_row(617, "correctness", head_sha=_SHA_A)])
    backend = BenchBackend(name="fake", url="http://invalid", model="m", api_key="")

    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    monkeypatch.setattr(
        runner, "diff_for_case",
        lambda case_, token, fetch_final: (diff, True),
    )
    monkeypatch.setattr(
        runner, "_post",
        lambda b, m: httpx.Response(
            200,
            json={
                "model": "m",
                "choices": [{"message": {"content": json.dumps({"findings": [
                    {"path": "a.py", "line": 1, "rule": "correctness",
                     "severity": "high", "message": "m"},
                ]})}}],
            },
            request=httpx.Request("POST", "http://invalid"),
        ),
    )

    replays = run_eval(backend, [case])
    assert replays[case.case_id].anchored is True
    assert replays[case.case_id].emitted == {"correctness": 1}


def test_run_production_eval_threads_anchored_flag_from_diff_for_case(monkeypatch):
    from elder_eval import runner
    from elder_eval.runner import run_production_eval

    (case,) = build_cases([_row(618, "correctness", head_sha=_SHA_A)])
    monkeypatch.setattr(
        runner, "diff_for_case",
        lambda case_, token, fetch_final: (
            "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
            "@@ -1 +1 @@\n-old\n+new\n",
            True,
        ),
    )

    def review(_hunks, **_kwargs):
        return LlmReviewResponse(
            kind="reviewed",
            findings=(Finding(
                path="a.py", line=1, rule="correctness",
                severity="high", message="wrong result",
            ),),
        )

    replays = run_production_eval([case], review=review)
    assert replays[case.case_id].anchored is True
    assert replays[case.case_id].emitted == {"correctness": 1}


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


# --- #764 fallout: PR-less ledger rows are unscorable, not errors ------------

def test_build_cases_skips_rows_with_no_pr():
    """`LedgerRow.pr` became optional in #764 so consensus findings written
    outside a PR context stop being deleted from the corpus. They carry a
    real verdict and must count toward class precision - but this eval
    REPLAYS a case by fetching its PR diff, and a row with no PR has no
    diff. Unscorable by construction, not an error.

    Left in, they reached the runner, which fetched `/pulls/None`, took a
    404, and marked the case errored - and `--record` refuses to write a
    baseline when ANY case errors. Six recovered rows blocked every future
    re-record."""
    from elder_eval.corpus import build_cases
    from ledger import LedgerRow

    def _row(pr, cls="silent-failure"):
        return LedgerRow(
            repo="quadseven/grug", pr=pr, reviewer="codex", severity="HIGH",
            finding_class=cls, finding="x", verdict="fixed", evidence="e",
            ts="2026-07-21T00:00:00Z",
        )

    cases = build_cases([_row(None), _row(10), _row(None)])
    assert [c.pr for c in cases] == [10]


def test_build_cases_does_not_crash_on_mixed_none_and_int_pr():
    """Latent crash guard. The grouping key is `(repo, pr)`, so ONE repo
    holding both a None and an int pr made `sorted(grouped)` raise
    `TypeError: '<' not supported between 'int' and 'NoneType'` and kill
    the whole eval. Masked today only because the PR-less rows say
    `quadseven/grug` and the older rows say `githumps/grug` - a
    coincidence, not a guarantee."""
    from elder_eval.corpus import build_cases
    from ledger import LedgerRow

    rows = [
        LedgerRow(repo="same/repo", pr=None, reviewer="c", severity="HIGH",
                  finding_class="silent-failure", finding="a", verdict="fixed",
                  evidence="e", ts="2026-07-21T00:00:00Z"),
        LedgerRow(repo="same/repo", pr=7, reviewer="c", severity="HIGH",
                  finding_class="silent-failure", finding="b", verdict="fixed",
                  evidence="e", ts="2026-07-20T00:00:00Z"),
    ]
    cases = build_cases(rows)   # must not raise
    assert [c.pr for c in cases] == [7]


def test_committed_corpus_yields_no_prless_cases():
    """End-to-end on the REAL corpus: the six #764-recovered rows must not
    produce a case, or `--record` stays blocked."""
    from pathlib import Path
    from elder_eval.corpus import build_cases
    from ledger import parse_jsonl

    p = Path(__file__).resolve().parents[3] / "logs" / "review-ledger.jsonl"
    cases = build_cases(parse_jsonl(p.read_text()))
    assert all(c.pr is not None for c in cases)
    assert not any("#None" in c.case_id for c in cases)


# --- grug#916: backend attribution + the anonymous-GitHub guard ------------


def _reviewed(backends, findings=()):
    """An LlmReviewResponse that SUCCEEDS, attributed to `backends`."""
    return LlmReviewResponse(
        kind="reviewed",
        findings=tuple(findings),
        backends_used=tuple(backends),
        models_used=tuple(b.value for b in backends),
    )


def test_production_case_records_the_backend_that_actually_answered():
    """grug#916: `review_diff`'s cloud chain falls back to Cave WITHOUT
    raising, so a case configured for the cloud tier can be served by the
    fallback and still score. The replay must name who answered."""
    from elder_eval.runner import run_production_case

    (case,) = build_cases([_row(40, "correctness")])
    diff = (
        "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    finding = Finding(
        path="src/a.py", line=1, rule="correctness",
        severity="high", message="wrong result",
    )

    replay = run_production_case(
        case, diff, review=lambda *_a, **_k: _reviewed([Backend.CAVE], [finding])
    )

    assert replay.errored is False
    assert replay.emitted == {"correctness": 1}
    # The whole point: the fallback is NAMED, not silently absorbed.
    assert replay.backends == ("cave",)


def test_production_case_attribution_falls_back_to_the_singular_field():
    """An older response shape carries only `backend_used`. It must still
    attribute - reporting nothing would read as "no fallback happened"."""
    from elder_eval.runner import run_production_case

    (case,) = build_cases([_row(41, "correctness")])
    diff = (
        "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )

    def review(*_a, **_k):
        return LlmReviewResponse(kind="reviewed", backend_used=Backend.OPENCODE_GO)

    assert run_production_case(case, diff, review=review).backends == ("opencode-go",)


def test_score_aggregates_backend_attribution_over_scored_cases():
    """A run served by two different backends must say so per backend, and
    an ERRORED case must not be attributed to anyone (it never ran)."""
    cases = build_cases([_row(50, "correctness"), _row(51, "correctness"),
                         _row(52, "correctness")])
    replays = {
        "quadseven/grug#50": CaseReplay(
            case_id="quadseven/grug#50", emitted={"correctness": 1},
            errored=False, backends=("opencode-go",),
        ),
        "quadseven/grug#51": CaseReplay(
            case_id="quadseven/grug#51", emitted={"correctness": 1},
            errored=False, backends=("cave",),
        ),
        "quadseven/grug#52": CaseReplay(
            case_id="quadseven/grug#52", emitted={}, errored=True,
            backends=("cave",),
        ),
    }

    report = score(cases, replays)

    assert report.backend_attribution == {"opencode-go": 1, "cave": 1}
    assert to_baseline_dict(report, prompt_sha="x", backend="production")[
        "backends"
    ]["production"]["backend_attribution"] == {"cave": 1, "opencode-go": 1}


def test_score_attribution_is_empty_for_bench_replays():
    """Bench mode posts to ONE configured backend, so its replays carry no
    attribution and the report must not invent one."""
    cases = build_cases([_row(53, "correctness")])
    replays = {
        "quadseven/grug#53": CaseReplay(
            case_id="quadseven/grug#53", emitted={"correctness": 1}, errored=False,
        )
    }

    assert score(cases, replays).backend_attribution == {}


def test_main_refuses_to_run_without_a_github_token(monkeypatch, capsys):
    """grug#916 root cause: the 2026-08-28 run scored 8 of 18 cases and
    errored 10 on the anonymous 60/hr cap, and the 0.19 catch rate it
    printed was read as a backend signal. Refuse instead."""
    from elder_eval.__main__ import main

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    assert main([]) == 2
    err = capsys.readouterr().err
    assert "GITHUB_TOKEN is not set" in err
    assert "--allow-anonymous" in err


def test_main_allow_anonymous_opts_past_the_token_guard(monkeypatch, capsys):
    """The escape hatch must actually get past the guard - a genuinely
    tiny public corpus can still be replayed tokenless on purpose."""
    from elder_eval import __main__ as cli

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(cli, "_run_requested_mode",
                        lambda *_a, **_k: (None, "", None))

    # Reaches _run_requested_mode (whose None replays exit 2) rather than
    # being stopped by the token guard, which prints the anonymous error.
    assert cli.main(["--allow-anonymous"]) == 2
    assert "GITHUB_TOKEN is not set" not in capsys.readouterr().err
