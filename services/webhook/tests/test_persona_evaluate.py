"""Tests for personas.tpm.persona — _summary + evaluate_pull_request (pure)
+ publish_tpm_evaluation (impure).

Spec 0002 split evaluate from publish so the rollup is testable without
GitHub or AWS round-trips. Pure tests are no-mock; publish tests mock
with_install_token_retry + post_check_run at the seam boundary
(`personas.publish_check`) — since #550 the tpm module publishes via
`publish_persona_check`, so the transport patch targets live there.
"""

from __future__ import annotations

import logging

from unittest.mock import patch

import pytest

import personas.publish_check as publish_check
import personas.tpm.persona as persona
from personas.tpm.dor_checks import CheckResult


_GOOD_BODY = """## Why
We need this for the launch tomorrow morning, fixes a Seer HIGH.

## Acceptance criteria
- [x] one
- [x] two
- [x] three

## Out of scope
nothing

closes #1

**Size:** S
"""

_BODY_MISSING_ISSUE_LINK = """## Why
We need this for the launch tomorrow morning, fixes a Seer HIGH.

## Acceptance criteria
- [x] one
- [x] two
- [x] three

## Out of scope
nothing

**Size:** S
"""

_BODY_MISSING_SCOPE_FENCE = """## Why
We need this for the launch tomorrow morning, fixes a Seer HIGH.

## Acceptance criteria
- [x] one
- [x] two
- [x] three

closes #1

**Size:** S
"""

_BODY_MISSING_SCOPE_AND_LINK = """## Why
We need this for the launch tomorrow morning, fixes a Seer HIGH.

## Acceptance criteria
- [x] one
- [x] two
- [x] three

**Size:** S
"""


def test_summary_pass_renders_check_count():
    results = [
        CheckResult("why", True, "ok"),
        CheckResult("acceptance", True, "3 bullets"),
    ]
    title, summary = persona._summary(results)
    assert "Hunt Plan ready" in title or "Chief pass" in title
    assert "all 2 checks" in title
    # Caveman DISPLAY name; the key stays "why" (ADR-0002).
    assert "| why hunt | pass |" in summary


def test_summary_fail_counts_blocking():
    results = [
        CheckResult("why", True, "ok"),
        CheckResult("acceptance", False, "0 bullets"),
        CheckResult("estimate", False, "no Size"),
    ]
    title, summary = persona._summary(results)
    assert "Hunt Plan hold" in title or "Chief hold" in title
    assert "2/3 plan checks fail" in title or "2/3 blocking" in title
    # The hold title names the failed checks with their caveman labels when
    # three or fewer fail, so the PR checks list says why, not just "hold".
    assert "(what good look like, how big; see Details)" in title
    assert "| why hunt | pass |" in summary
    assert "| what good look like | fail |" in summary


def test_summary_hold_names_failing_checks_when_four_fail_it_falls_back():
    """More than three failures fall back to the short pointer so the
    title stays a title; the breakdown still lives in Details."""
    results = [
        CheckResult("why", False, "missing"),
        CheckResult("acceptance", False, "0 bullets"),
        CheckResult("estimate", False, "no Size"),
        CheckResult("scope-fence", False, "missing"),
    ]
    title, _ = persona._summary(results)
    assert title == "Hunt Plan hold - 4/4 plan checks fail - see Details for which"


def test_summary_hold_uses_caveman_labels_not_check_keys():
    """The inline names use the same caveman labels the summary table
    shows (ADR-0002): the key is what code matches on, the label is what
    a human reads."""
    results = [
        CheckResult("why", True, "ok"),
        CheckResult("linked-issue-epic", False, "no epic"),
        CheckResult("estimate", True, "S"),
    ]
    title, _ = persona._summary(results)
    assert "(which epic; see Details)" in title
    assert "linked-issue-epic" not in title


def test_summary_table_header_present():
    title, summary = persona._summary([CheckResult("x", True, "y")])
    assert summary.startswith("| Check | Status | Detail |")
    assert "|---|---|---|" in summary.split("\n")[1]


def test_summary_skipped_check_is_not_counted_as_a_pass():
    """#782 criterion 3: a fail-open check that never ran must not hide
    inside "all N checks". It drops from the numerator, is named in the
    title, and its row reads `skipped`, not `pass`."""
    results = [
        CheckResult("why", True, "ok"),
        CheckResult("acceptance", True, "3 bullets"),
        CheckResult("linked-issue-completeness", True, "fetch failed (fail-open)", skipped=True),
    ]
    title, summary = persona._summary(results)
    assert "Hunt Plan ready" in title          # still not a hold: fail-open kept
    assert "all 3 checks" not in title
    assert "2/3 checks" in title
    assert "ticket done skipped" in title
    assert "| ticket done | skipped |" in summary
    assert "| ticket done | pass |" not in summary


def test_summary_hold_still_names_skipped_check():
    """A hold with a skipped check alongside: the fail count is unchanged
    (a skipped check never blocks) but the skip stays visible."""
    results = [
        CheckResult("why", False, "missing"),
        CheckResult("linked-issue-completeness", True, "no fetcher (fail-open)", skipped=True),
    ]
    title, _ = persona._summary(results)
    assert "Hunt Plan hold - 1/2 plan checks fail" in title
    assert "(why hunt; see Details)" in title
    assert "ticket done skipped" in title


def test_summary_no_skips_keeps_all_n_wording():
    """Regression guard for the unchanged happy path: with every check
    actually evaluated the title still says "all N checks"."""
    title, _ = persona._summary([CheckResult("why", True, "ok"), CheckResult("estimate", True, "S")])
    assert title == "Hunt Plan ready - all 2 checks"


# --- evaluate_pull_request (pure) ---

def test_evaluate_pull_request_passes_on_good_body():
    evaluation = persona.evaluate_pull_request(_GOOD_BODY)

    assert evaluation.passed is True
    assert evaluation.conclusion == "success"
    assert len(evaluation.results) == 9  # 9 dor checks
    assert all(r.passed for r in evaluation.results)


def test_evaluate_pull_request_loom_footer_failure_is_advisory():
    """grug#1201: a Loom-ticket ref with no audit footer fails `loom-footer`
    but never blocks merge - it sits in _ADVISORY_CHECKS like `issue-link`."""
    body = _GOOD_BODY + "\nRefs quadseven/loom#510\n"  # leak-guard-allow: public repo; the input the check parses
    evaluation = persona.evaluate_pull_request(body)

    footer = next(r for r in evaluation.results if r.name == "loom-footer")
    assert footer.passed is False
    assert evaluation.passed is True
    assert evaluation.conclusion == "success"


def test_evaluate_pull_request_without_fetcher_marks_linked_issue_skipped():
    """#782: a bare `evaluate_pull_request(body)` on a body that closes an
    issue still fails open (pass) - but the linked-issue result carries
    `skipped=True`, so the published title cannot claim all six ran."""
    evaluation = persona.evaluate_pull_request(_GOOD_BODY)  # closes #1, no fetcher

    assert evaluation.passed is True
    lic = next(r for r in evaluation.results if r.name == "linked-issue-completeness")
    assert lic.skipped is True
    title, _ = persona._summary(list(evaluation.results))
    assert "all 9 checks" not in title
    # All three IO checks name the ticket and have no fetcher here (grug#1034,
    # plus the new in-scope check).
    assert "6/9 checks (ticket done, which epic, in scope skipped)" in title


def test_evaluate_pull_request_fails_on_empty_body():
    evaluation = persona.evaluate_pull_request("")

    assert evaluation.passed is False
    assert evaluation.conclusion == "failure"
    assert any(not r.passed for r in evaluation.results)


def test_evaluate_issue_link_only_fail_is_advisory():
    """issue-link is advisory — missing it should NOT block the PR."""
    evaluation = persona.evaluate_pull_request(_BODY_MISSING_ISSUE_LINK)

    assert evaluation.passed is True
    assert evaluation.conclusion == "success"
    issue_link_result = next(r for r in evaluation.results if r.name == "issue-link")
    assert issue_link_result.passed is False  # check itself failed...
    # ...but overall evaluation still passes


def test_evaluate_scope_fence_fail_is_blocking():
    """scope-fence is blocking — missing it MUST block the PR."""
    evaluation = persona.evaluate_pull_request(_BODY_MISSING_SCOPE_FENCE)

    assert evaluation.passed is False
    assert evaluation.conclusion == "failure"
    scope_result = next(r for r in evaluation.results if r.name == "scope-fence")
    assert scope_result.passed is False


def test_evaluate_mixed_advisory_and_blocking_failure():
    """When both advisory (issue-link) and blocking (scope-fence) fail,
    the blocking check determines the verdict; the advisory failure
    should NOT inflate the blocking count in the summary."""
    evaluation = persona.evaluate_pull_request(_BODY_MISSING_SCOPE_AND_LINK)

    assert evaluation.passed is False
    assert evaluation.conclusion == "failure"
    scope = next(r for r in evaluation.results if r.name == "scope-fence")
    link = next(r for r in evaluation.results if r.name == "issue-link")
    assert scope.passed is False
    assert link.passed is False
    title, summary = persona._summary(list(evaluation.results))
    assert title == "Hunt Plan hold - 1/9 plan checks fail (where stop; see Details)"


def test_evaluate_in_scope_fail_is_advisory_by_default():
    """Out-of-scope changed files fail `in-scope` but never block merge
    (advisory by default)."""
    body = """## Why
we need this for the launch tomorrow morning

## Acceptance criteria
- [x] one
- [x] two
- [x] three

## Out of scope
nothing

closes #42

**Size:** M
"""
    def fetcher(_num: int) -> str:
        return "the foo handler\n"
    evaluation = persona.evaluate_pull_request(
        body,
        fetch_issue=fetcher,
        fetch_changed_files=lambda: ["src/foo.py", "src/billing/billing.py"],
    )
    inscope = next(r for r in evaluation.results if r.name == "in-scope")
    assert not inscope.passed
    assert "src/billing/billing.py" in inscope.detail
    # Advisory: the verdict stays green.
    assert evaluation.passed is True
    assert evaluation.conclusion == "success"


def test_evaluate_in_scope_fail_blocks_when_config_promotes():
    """org/repo config can promote `in-scope` to blocking: pass a smaller
    advisory set and the same out-of-scope rider turns the verdict red."""
    body = """## Why
we need this for the launch tomorrow morning

## Acceptance criteria
- [x] one
- [x] two
- [x] three

## Out of scope
nothing

closes #42

**Size:** M
"""
    def fetcher(_num: int) -> str:
        return "the foo handler\n"
    advisory = frozenset({"issue-link", "loom-footer"})  # in-scope not advisory
    evaluation = persona.evaluate_pull_request(
        body,
        fetch_issue=fetcher,
        fetch_changed_files=lambda: ["src/foo.py", "src/billing/billing.py"],
        advisory_checks=advisory,
    )
    inscope = next(r for r in evaluation.results if r.name == "in-scope")
    assert not inscope.passed
    assert evaluation.passed is False
    assert evaluation.conclusion == "failure"


def test_summary_advisory_check_renders_warning_icon():
    """Advisory checks that fail should render warn not fail in the summary."""
    results = [
        CheckResult("why", passed=True, detail="ok"),
        CheckResult("issue-link", passed=False, detail="no link"),
    ]
    title, summary = persona._summary(results)
    assert "Hunt Plan ready" in title or "Chief pass" in title
    assert "warn" in summary
    assert "fail" not in summary


def test_evaluate_pull_request_is_pure_no_external_calls():
    """Spec 0002 attests `evaluate_pull_request_is_pure_function`.
    Verify the function never touches with_install_token_retry or
    post_check_run (both live behind the publish seam since #550) —
    any patched call would assert-fail."""
    with patch.object(publish_check, "with_install_token_retry") as retry_mock, \
         patch.object(publish_check, "post_check_run") as post_mock:
        persona.evaluate_pull_request(_GOOD_BODY)
    retry_mock.assert_not_called()
    post_mock.assert_not_called()


# --- publish_tpm_evaluation (impure, via the shared seam since #550) ---
#
# The seam (`personas.publish_check.publish_persona_check`) owns the
# transport, so the patch targets are publish_check's imports. Its
# post_check_run call is positional: (token, owner, repo, result,
# external_id=...) — fakes must match, or the TypeError is swallowed by
# the seam's total publish boundary and every publish surfaces as
# publish_failed.

def _seam_retry(install_id, fn):
    return fn("fake-token")


def test_publish_tpm_evaluation_posts_on_success():
    evaluation = persona.evaluate_pull_request(_GOOD_BODY)
    captured = {}

    def fake_post(install_token, owner, repo, result, *, external_id):
        captured["status"] = result.status
        captured["conclusion"] = result.conclusion
        captured["external_id"] = external_id
        captured["head_sha"] = result.head_sha
        return {"id": 999}

    with patch.object(publish_check, "with_install_token_retry", side_effect=_seam_retry):
        with patch.object(publish_check, "post_check_run", side_effect=fake_post):
            out = persona.publish_tpm_evaluation(
                evaluation,
                installation_id=1,
                owner="myorg", repo="myrepo",
                head_sha="abc123def456" + "0" * 28,
                pr_number=42,
            )

    assert captured["status"] == "completed"
    assert captured["conclusion"] == "success"
    assert captured["external_id"] == f"grug-tpm:myorg/myrepo#42:{'abc123def456' + '0' * 28}"  # leak-guard-allow: placeholder repo, not a real one
    assert out == {"persona": "tpm", "result": "pass"}


def test_publish_tpm_evaluation_posts_on_failure():
    evaluation = persona.evaluate_pull_request("")
    captured = {}

    def fake_post(install_token, owner, repo, result, *, external_id):
        captured["conclusion"] = result.conclusion
        captured["title"] = result.title

    with patch.object(publish_check, "with_install_token_retry", side_effect=_seam_retry):
        with patch.object(publish_check, "post_check_run", side_effect=fake_post):
            out = persona.publish_tpm_evaluation(
                evaluation,
                installation_id=1,
                owner="o", repo="r",
                head_sha="x" * 40,
                pr_number=1,
            )

    assert captured["conclusion"] == "failure"
    assert "Hunt Plan hold" in captured["title"] or "Chief hold" in captured["title"]
    assert out == {"persona": "tpm", "result": "fail"}


def test_publish_tpm_evaluation_uses_grug_check_name():
    evaluation = persona.evaluate_pull_request(_GOOD_BODY)
    captured = {}

    def fake_post(install_token, owner, repo, result, *, external_id):
        captured["name"] = result.name

    with patch.object(publish_check, "with_install_token_retry", side_effect=_seam_retry):
        with patch.object(publish_check, "post_check_run", side_effect=fake_post):
            persona.publish_tpm_evaluation(
                evaluation,
                installation_id=1, owner="o", repo="r",
                head_sha="x" * 40, pr_number=1,
            )

    # Branch protection ruleset relies on this exact string. Drift = silent
    # cutover regression.
    assert captured["name"] == "Grug - Chief"


def test_publish_tpm_evaluation_external_id_format():
    """external_id binds (owner, repo, pr_number, head_sha) so GH
    de-duplicates across re-fires. Format matters for grep-ability."""
    evaluation = persona.evaluate_pull_request(_GOOD_BODY)
    captured = {}

    def fake_post(install_token, owner, repo, result, *, external_id):
        captured["external_id"] = external_id

    with patch.object(publish_check, "with_install_token_retry", side_effect=_seam_retry):
        with patch.object(publish_check, "post_check_run", side_effect=fake_post):
            persona.publish_tpm_evaluation(
                evaluation,
                installation_id=1,
                owner="myorg", repo="myrepo",
                head_sha="deadbeef" + "0" * 32,
                pr_number=99,
            )

    assert captured["external_id"] == "grug-tpm:myorg/myrepo#99:deadbeef" + "0" * 32  # leak-guard-allow: placeholder repo, not a real one


# --- #550 regression: publish failure must leave an honest Activity row ---

def test_publish_failure_records_check_publish_failed_row():
    """THE #550 regression test. Before the seam migration, a
    post_check_run raise propagated out of publish_tpm_evaluation and
    record_check_verdict was never called — Chief left NO Activity row
    (no errored row, no re-run button), unlike Warder/Guard/Smasher.
    After: the seam classifies the failure and records an honest row
    with degraded_reason='check_publish_failed'."""
    evaluation = persona.evaluate_pull_request(_GOOD_BODY)
    recorded: dict = {}

    with patch.object(publish_check, "with_install_token_retry", side_effect=_seam_retry), \
         patch.object(publish_check, "post_check_run", side_effect=RuntimeError("gh 502")), \
         patch.object(publish_check, "record_check_verdict", side_effect=lambda **kw: recorded.update(kw)):
        out = persona.publish_tpm_evaluation(
            evaluation,
            installation_id=7, owner="o", repo="r",
            head_sha="a" * 40, pr_number=3,
        )

    assert out == {"persona": "tpm", "result": "publish_failed"}
    assert recorded["persona_key"] == "tpm"
    assert recorded["degraded_reason"] == "check_publish_failed"
    assert recorded["blocking"] is True
    # The row still carries the evaluation's real conclusion — the check
    # verdict is what Chief DECIDED, degraded_reason is what went wrong.
    assert recorded["conclusion"] == "success"


def test_publish_success_row_parity_degraded_reason_none():
    """#550 AC: on success, Chief's row is unchanged vs the pre-seam
    behavior — TPM never degrades at the eval layer, so degraded_reason
    must stay None (the seam's honest merge must not invent one)."""
    evaluation = persona.evaluate_pull_request(_GOOD_BODY)
    recorded: dict = {}

    with patch.object(publish_check, "with_install_token_retry", side_effect=_seam_retry), \
         patch.object(publish_check, "post_check_run", side_effect=lambda *a, **kw: {"id": 1}), \
         patch.object(publish_check, "record_check_verdict", side_effect=lambda **kw: recorded.update(kw)):
        persona.publish_tpm_evaluation(
            evaluation,
            installation_id=1, owner="o", repo="r",
            head_sha="b" * 40, pr_number=4,
        )

    assert recorded["degraded_reason"] is None
    assert recorded["conclusion"] == "success"
    assert recorded["findings_count"] == 0
    assert recorded["blocking"] is True


def test_publish_logs_tpm_publishing_and_published_on_success(caplog):
    """#550 AC: the `tpm_publishing` / `tpm_published` log lines are a DD
    monitor contract — both must survive the seam migration verbatim as
    STATIC event names (no dynamic f-string names)."""
    evaluation = persona.evaluate_pull_request(_GOOD_BODY)

    with caplog.at_level(logging.INFO):
        with patch.object(publish_check, "with_install_token_retry", side_effect=_seam_retry), \
             patch.object(publish_check, "post_check_run", side_effect=lambda *a, **kw: {"id": 1}), \
             patch.object(publish_check, "record_check_verdict"):
            persona.publish_tpm_evaluation(
                evaluation,
                installation_id=1, owner="o", repo="r",
                head_sha="c" * 40, pr_number=5,
            )

    messages = [r.getMessage() for r in caplog.records]
    assert "tpm_publishing" in messages
    assert "tpm_published" in messages
    # Order matters: publishing is the intent log, published the outcome.
    assert messages.index("tpm_publishing") < messages.index("tpm_published")


def test_publish_failure_logs_publish_failed_not_published(caplog):
    """On a publish failure the outcome log is the seam's
    `tpm_publish_failed` (the same event name the dispatcher used
    pre-migration, passed via publish_failed_log_name) — and
    `tpm_published` must NOT fire, or DD reads a failed publish as
    success."""
    evaluation = persona.evaluate_pull_request(_GOOD_BODY)

    with caplog.at_level(logging.INFO):
        with patch.object(publish_check, "with_install_token_retry", side_effect=_seam_retry), \
             patch.object(publish_check, "post_check_run", side_effect=RuntimeError("gh 502")), \
             patch.object(publish_check, "record_check_verdict"):
            persona.publish_tpm_evaluation(
                evaluation,
                installation_id=1, owner="o", repo="r",
                head_sha="d" * 40, pr_number=6,
            )

    messages = [r.getMessage() for r in caplog.records]
    assert "tpm_publishing" in messages
    assert "tpm_publish_failed" in messages
    assert "tpm_published" not in messages


# --- TpmEvaluation dataclass invariants ---

def test_tpm_evaluation_is_frozen():
    """TpmEvaluation is frozen so callers can't mutate the rollup."""
    from dataclasses import FrozenInstanceError
    e = persona.TpmEvaluation(
        passed=True,
        results=(CheckResult("why", True, "ok"),),
        conclusion="success",
    )
    with pytest.raises(FrozenInstanceError):
        e.passed = False  # type: ignore[misc]


def test_tpm_evaluation_results_is_tuple():
    """results is a tuple (immutable) — caller can iterate but not append."""
    e = persona.TpmEvaluation(
        passed=True,
        results=(CheckResult("why", True, "ok"),),
        conclusion="success",
    )
    assert isinstance(e.results, tuple)
    with pytest.raises(AttributeError):
        e.results.append(CheckResult("x", False, "y"))  # type: ignore[attr-defined]


def test_publish_tpm_records_chief_verdict_excluding_advisory():
    """Activity feed (PRD #301): Chief's `findings_count` is failed BLOCKING
    checks only — the advisory `issue-link` failure must NOT count, else a PR
    that only fails the advisory check would read warn/block instead of pass.
    record_check_verdict is mocked so the assertion is real (otherwise the
    no-DDB write would be swallowed by the best-effort guard)."""
    evaluation = persona.TpmEvaluation(
        passed=False,
        results=(
            CheckResult("why", False, "too short"),        # blocking fail
            CheckResult("issue-link", False, "no link"),   # advisory fail (excluded)
            CheckResult("acceptance", True, "ok"),
        ),
        conclusion="failure",
    )
    recorded: dict = {}
    with patch.object(publish_check, "with_install_token_retry", side_effect=lambda i, fn: fn("t")):
        with patch.object(publish_check, "post_check_run", side_effect=lambda *a, **kw: {"id": 1}):
            with patch.object(publish_check, "record_check_verdict", side_effect=lambda **kw: recorded.update(kw)):
                persona.publish_tpm_evaluation(
                    evaluation, installation_id=1, owner="o", repo="r",
                    head_sha="x" * 40, pr_number=1,
                )
    assert recorded["persona_key"] == "tpm"
    assert recorded["blocking"] is True
    assert recorded["conclusion"] == "failure"
    assert recorded["findings_count"] == 1   # only the blocking 'why'; issue-link advisory excluded


def test_publish_tpm_records_chief_zero_findings_on_pass():
    """All blocking checks pass -> findings_count == 0 (verdict will be pass)."""
    evaluation = persona.evaluate_pull_request(_GOOD_BODY)
    recorded: dict = {}
    with patch.object(publish_check, "with_install_token_retry", side_effect=lambda i, fn: fn("t")):
        with patch.object(publish_check, "post_check_run", side_effect=lambda *a, **kw: {"id": 1}):
            with patch.object(publish_check, "record_check_verdict", side_effect=lambda **kw: recorded.update(kw)):
                persona.publish_tpm_evaluation(
                    evaluation, installation_id=1, owner="o", repo="r",
                    head_sha="y" * 40, pr_number=2,
                )
    assert recorded["findings_count"] == 0
    assert recorded["conclusion"] == "success"


def test_tpm_evaluation_rejects_incoherent_passed_conclusion():
    """The __post_init__ coherence invariant: passed and conclusion are
    two encodings of the same rollup and feed the publish seam as
    independent params - an incoherent instance would publish a red
    check while returning "pass". Deleting __post_init__ fails this."""
    with pytest.raises(ValueError, match="incoherent"):
        persona.TpmEvaluation(
            passed=True,
            results=(CheckResult("why", False, "bad"),),
            conclusion="failure",
        )
    with pytest.raises(ValueError, match="incoherent"):
        persona.TpmEvaluation(
            passed=False,
            results=(CheckResult("why", True, "ok"),),
            conclusion="success",
        )


def test_check_display_names_are_caveman_but_keys_are_not():
    """DISPLAY renames, key stays (ADR-0002).

    `CheckResult.name` is load-bearing: `_ADVISORY_CHECKS` keys off it to
    decide blocking-vs-warn, and Chief's issue-time remedy text keys off it
    too. Renaming the VALUES would silently make `issue-link` blocking.
    """
    assert persona.check_display_name("scope-fence") == "where stop"
    assert persona.check_display_name("issue-link") == "which ticket"
    # The advisory split still keys off the real name, not the label.
    assert "issue-link" in persona._ADVISORY_CHECKS
    assert persona._blocking_failures(
        [CheckResult("issue-link", False, "x")]) == []
    # Unknown check degrades to its key rather than taking the summary down.
    assert persona.check_display_name("brand-new") == "brand-new"


def test_every_check_has_a_display_name():
    """A check added without a label renders as a bare key next to caveman
    siblings - findable now rather than in an email."""
    from personas.tpm.dor_checks import run_all
    body = "## Why\nreal reason goes here\n## Acceptance criteria\n- [ ] a\n- [ ] b\n- [ ] c\nSize: M\n## Out of scope\nnot the db\n"
    for r in run_all(body):
        assert persona.check_display_name(r.name) != r.name, r.name


# --- Agent-authored PRs skip the Hunt Plan (neutral, never red) ---

_AGENT_BODY_NO_PLAN = """Fixes the thing.

https://claude.ai/code/session_01Ht7JaVzv2aacJ9r6Srso9r
"""

_HUMAN_BODY_NO_PLAN = "Fixes the thing.\n"


def test_agent_authored_pr_without_plan_is_neutral_not_failure():
    """A body with no Why/acceptance/Size/scope fence fails Chief for a
    person, but an agent-authored body (session link) concludes NEUTRAL:
    passes the required `Grug - Chief` context without format edits."""
    evaluation = persona.evaluate_pull_request(_AGENT_BODY_NO_PLAN)
    assert evaluation.conclusion == "neutral"
    assert evaluation.passed is True
    assert all(r.skipped for r in evaluation.results)


def test_explicit_agent_marker_is_neutral():
    body = "No plan here.\n\n<!-- grug:agent-authored -->\n"
    assert persona.evaluate_pull_request(body).conclusion == "neutral"


def test_agent_skip_never_fetches_linked_issues():
    """The skip is decided on the body alone: no issue fetch is spent."""
    def boom(*_a, **_kw):
        raise AssertionError("fetcher must not be called on an agent PR")
    body = _GOOD_BODY + "\nhttps://claude.ai/code/session_abc123\n"
    evaluation = persona.evaluate_pull_request(
        body, fetch_issue=boom, fetch_issue_facts=boom,
    )
    assert evaluation.conclusion == "neutral"


def test_human_pr_without_plan_still_fails():
    """No marker -> Chief still enforces the Hunt Plan for people."""
    evaluation = persona.evaluate_pull_request(_HUMAN_BODY_NO_PLAN)
    assert evaluation.conclusion == "failure"
    assert evaluation.passed is False
    names = {r.name for r in evaluation.results if not r.passed}
    assert {"why", "acceptance", "estimate", "scope-fence"} <= names


def test_non_session_claude_link_is_not_a_marker():
    """Only a session link counts; a plain claude.ai mention in a human
    body must not switch Chief off."""
    body = _HUMAN_BODY_NO_PLAN + "\nSee https://claude.ai/code for context.\n"
    assert persona.is_agent_authored(body) is False
    assert persona.evaluate_pull_request(body).conclusion == "failure"


def test_claude_code_attribution_footer_is_neutral():
    """Claude Code's default PR attribution ends the body with
    `Generated with [Claude Code](https://claude.com/claude-code)`. A body
    carrying that footer without a session link is still agent-authored
    (mod-overseer #821: 4/7 Hunt Plan failures on such a body)."""
    body = _HUMAN_BODY_NO_PLAN + "\nGenerated with [Claude Code](https://claude.com/claude-code)\n"
    assert persona.is_agent_authored(body) is True
    assert persona.evaluate_pull_request(body).conclusion == "neutral"


def test_prose_mention_of_claude_code_is_not_a_marker():
    """A human writing 'I used claude code to draft this' in prose must not
    switch Chief off — only the template's exact markdown footer counts."""
    body = _HUMAN_BODY_NO_PLAN + "\nI used claude code to help draft this plan.\n"
    assert persona.is_agent_authored(body) is False
    assert persona.evaluate_pull_request(body).conclusion == "failure"


def test_publish_agent_skip_posts_neutral_with_skip_title():
    evaluation = persona.evaluate_pull_request(_AGENT_BODY_NO_PLAN)
    posted: dict = {}
    recorded: dict = {}

    def fake_post(*a, **kw):
        posted.update(kw)
        posted["args"] = a
        return {"id": 1}

    with patch.object(publish_check, "with_install_token_retry", side_effect=lambda i, fn: fn("t")):
        with patch.object(publish_check, "post_check_run", side_effect=fake_post):
            with patch.object(publish_check, "record_check_verdict", side_effect=lambda **kw: recorded.update(kw)):
                result = persona.publish_tpm_evaluation(
                    evaluation, installation_id=1, owner="o", repo="r",
                    head_sha="z" * 40, pr_number=3,
                )
    assert result["result"] == "pass"
    assert recorded["conclusion"] == "neutral"
    assert recorded["findings_count"] == 0
    check = posted["args"][3]
    assert check.conclusion == "neutral"
    assert check.title == "Hunt Plan skipped - agent-authored PR, author not an operator"


def test_tpm_evaluation_rejects_failed_neutral():
    with pytest.raises(ValueError, match="incoherent"):
        persona.TpmEvaluation(
            passed=False,
            results=(CheckResult("why", True, "ok", skipped=True),),
            conclusion="neutral",
        )


def test_pr_template_is_not_agent_authored():
    """The PR template is what a PERSON starts from in the GitHub UI. If it
    carried an agent-authored marker, every PR opened from it would skip
    Chief. It once ended with the Claude Code attribution footer, which the
    detector now treats as an agent marker."""
    from pathlib import Path

    template = Path(__file__).resolve().parents[3] / ".github" / "pull_request_template.md"
    assert template.is_file(), template
    assert persona.is_agent_authored(template.read_text()) is False


# --- grug#1251: operator PRs with an agent footer still get the plan ---

_OPERATOR_PR = {"user": {"login": "op", "type": "User"}, "author_association": "OWNER"}
_LOOM_FOOTER = "\nLoom ticket #7, lane fix, model opus\n"


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_trusted_operator_is_a_write_level_human(association):
    pr = {"user": {"login": "op", "type": "User"}, "author_association": association}
    assert persona.is_trusted_operator(pr) is True


@pytest.mark.parametrize("pr", [
    {"user": {"login": "dependabot[bot]", "type": "Bot"}, "author_association": "OWNER"},
    {"user": {"login": "stranger", "type": "User"}, "author_association": "CONTRIBUTOR"},
    {"user": {"login": "stranger", "type": "User"}, "author_association": "NONE"},
    {"user": {"login": "op", "type": "User"}},
    {},
    None,
])
def test_untrusted_or_unknown_author_is_not_an_operator(pr):
    assert persona.is_trusted_operator(pr) is False


def test_operator_pr_with_footer_and_no_why_fails():
    """The gap #1251 closes: an operator session's PR carries the agent
    footer, and on main that switched Chief off (neutral). A missing
    `## Why` must now fail."""
    body = _GOOD_BODY.replace("## Why\n", "## Context\n") + (
        "\nGenerated with [Claude Code](https://claude.com/claude-code)\n"
        "https://claude.ai/code/session_01Ht7JaVzv2aacJ9r6Srso9r\n"
    )
    evaluation = persona.evaluate_pull_request(body, author_trusted=True)
    assert evaluation.conclusion == "failure"
    assert evaluation.passed is False
    assert [r.name for r in evaluation.results if not r.passed] == ["why"]


def test_operator_pr_with_footer_and_full_plan_succeeds():
    body = _GOOD_BODY + "\nhttps://claude.ai/code/session_abc123\n"
    evaluation = persona.evaluate_pull_request(body, author_trusted=True)
    assert evaluation.conclusion == "success"
    assert evaluation.rule == persona.RULE_OPERATOR_CHECKED


def test_operator_check_uses_the_issue_fetchers():
    seen: list[str] = []

    def fetch(*_a, **_kw):
        seen.append("called")
        return "- [x] done\n"

    body = _GOOD_BODY + "\nhttps://claude.ai/code/session_abc123\n"
    persona.evaluate_pull_request(body, author_trusted=True, fetch_issue=fetch)
    assert seen


def test_loom_lane_pr_still_skips_for_an_operator_author():
    """Fleet lane PRs open through the operator's account too; the Loom
    audit footer is what marks them, and they keep the neutral skip."""
    body = _AGENT_BODY_NO_PLAN + _LOOM_FOOTER
    evaluation = persona.evaluate_pull_request(body, author_trusted=True)
    assert evaluation.conclusion == "neutral"
    assert evaluation.rule == persona.RULE_SKIP_LOOM_LANE


_SESSION_LINK = "\nhttps://claude.ai/code/session_01Ht7JaVzv2aacJ9r6Srso9r\n"
_OPERATOR_SESSION_FOOTER = (
    "\nLoom ticket #5690, lane operator-session, model claude-opus-5-5.\n"
)


def test_operator_session_footer_without_why_fails_for_an_operator():
    """Operator sessions are told to write the Loom footer with lane
    `operator-session`. That footer used to switch Chief off (neutral
    skip-loom-lane) before the trusted-author check ever ran, so a
    missing `## Why` went through unchecked."""
    body = (
        _GOOD_BODY.replace("## Why\n", "## Context\n")
        + _SESSION_LINK + _OPERATOR_SESSION_FOOTER
    )
    evaluation = persona.evaluate_pull_request(body, author_trusted=True)
    assert evaluation.conclusion == "failure"
    assert evaluation.rule == persona.RULE_OPERATOR_CHECKED
    assert [r.name for r in evaluation.results if not r.passed] == ["why"]


def test_operator_session_footer_with_full_plan_succeeds():
    body = _GOOD_BODY + _SESSION_LINK + _OPERATOR_SESSION_FOOTER
    evaluation = persona.evaluate_pull_request(body, author_trusted=True)
    assert evaluation.conclusion == "success"
    assert evaluation.rule == persona.RULE_OPERATOR_CHECKED


def test_operator_session_footer_from_an_untrusted_author_still_skips():
    """The lane name alone cannot turn a skip into a check: an untrusted
    author who writes `lane operator-session` keeps the Loom-lane skip."""
    body = _AGENT_BODY_NO_PLAN + _OPERATOR_SESSION_FOOTER
    evaluation = persona.evaluate_pull_request(body, author_trusted=False)
    assert evaluation.conclusion == "neutral"
    assert evaluation.rule == persona.RULE_SKIP_LOOM_LANE


def test_fleet_lane_footer_still_skips_for_an_operator_author():
    body = (
        _AGENT_BODY_NO_PLAN
        + "\nLoom ticket #5690, lane spark-3, model qwen3-coder.\n"
    )
    evaluation = persona.evaluate_pull_request(body, author_trusted=True)
    assert evaluation.conclusion == "neutral"
    assert evaluation.rule == persona.RULE_SKIP_LOOM_LANE


def test_explicit_opt_out_still_skips_for_an_operator_author():
    body = "No plan here.\n\n<!-- grug:agent-authored -->\n"
    evaluation = persona.evaluate_pull_request(body, author_trusted=True)
    assert evaluation.conclusion == "neutral"
    assert evaluation.rule == persona.RULE_SKIP_OPT_OUT


def test_agent_footer_from_an_untrusted_author_still_skips():
    evaluation = persona.evaluate_pull_request(_AGENT_BODY_NO_PLAN)
    assert evaluation.conclusion == "neutral"
    assert evaluation.rule == persona.RULE_SKIP_NOT_OPERATOR


def test_loom_footer_without_agent_marker_is_still_checked():
    """A Loom footer alone was never a skip; only footer + agent marker is."""
    evaluation = persona.evaluate_pull_request(
        _HUMAN_BODY_NO_PLAN + _LOOM_FOOTER, author_trusted=True,
    )
    assert evaluation.conclusion == "failure"


def _publish_title(evaluation) -> str:
    posted: dict = {}

    def fake_post(*a, **kw):
        posted["args"] = a
        return {"id": 1}

    with patch.object(publish_check, "with_install_token_retry", side_effect=lambda i, fn: fn("t")):
        with patch.object(publish_check, "post_check_run", side_effect=fake_post):
            with patch.object(publish_check, "record_check_verdict", side_effect=lambda **kw: None):
                persona.publish_tpm_evaluation(
                    evaluation, installation_id=1, owner="o", repo="r",
                    head_sha="z" * 40, pr_number=3,
                )
    return posted["args"][3].title


@pytest.mark.parametrize(("body", "trusted", "expected"), [
    (_AGENT_BODY_NO_PLAN + _LOOM_FOOTER, True, "Hunt Plan skipped - Loom lane PR"),
    ("x\n<!-- grug:agent-authored -->\n", True, "Hunt Plan skipped - agent opt-out marker"),
    (_AGENT_BODY_NO_PLAN, False,
     "Hunt Plan skipped - agent-authored PR, author not an operator"),
])
def test_skip_title_names_the_rule(body, trusted, expected):
    evaluation = persona.evaluate_pull_request(body, author_trusted=trusted)
    assert _publish_title(evaluation) == expected


def test_checked_operator_title_says_it_was_checked():
    body = _GOOD_BODY + "\nhttps://claude.ai/code/session_abc123\n"
    title = _publish_title(persona.evaluate_pull_request(body, author_trusted=True))
    assert title.startswith("Hunt Plan ready - ")
    assert title.endswith(" - operator PR, agent footer checked")


def test_failed_operator_title_says_it_was_checked():
    body = _HUMAN_BODY_NO_PLAN + "\nhttps://claude.ai/code/session_abc123\n"
    title = _publish_title(persona.evaluate_pull_request(body, author_trusted=True))
    assert title.startswith("Hunt Plan hold - ")
    assert title.endswith(" - operator PR, agent footer checked")
