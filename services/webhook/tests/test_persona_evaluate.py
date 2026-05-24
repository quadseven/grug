"""Tests for personas.tpm.persona — _summary + evaluate_pull_request (pure)
+ publish_tpm_evaluation (impure).

Spec 0002 split evaluate from publish so the rollup is testable without
GitHub or AWS round-trips. Pure tests are no-mock; publish tests mock
with_install_token_retry + post_check_run.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import personas.tpm.persona as persona
from personas.tpm.dor_checks import CheckResult


_GOOD_BODY = """## Why
We need this for the launch tomorrow morning, fixes a Sentry HIGH.

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
We need this for the launch tomorrow morning, fixes a Sentry HIGH.

## Acceptance criteria
- [x] one
- [x] two
- [x] three

## Out of scope
nothing

**Size:** S
"""

_BODY_MISSING_SCOPE_FENCE = """## Why
We need this for the launch tomorrow morning, fixes a Sentry HIGH.

## Acceptance criteria
- [x] one
- [x] two
- [x] three

closes #1

**Size:** S
"""

_BODY_MISSING_SCOPE_AND_LINK = """## Why
We need this for the launch tomorrow morning, fixes a Sentry HIGH.

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
    assert "✅" in title
    assert "all 2 checks" in title
    assert "| why | ✅ |" in summary


def test_summary_fail_counts_blocking():
    results = [
        CheckResult("why", True, "ok"),
        CheckResult("acceptance", False, "0 bullets"),
        CheckResult("estimate", False, "no Size"),
    ]
    title, summary = persona._summary(results)
    assert "❌" in title
    assert "2/3 blocking" in title
    assert "| why | ✅ |" in summary
    assert "| acceptance | ❌ |" in summary


def test_summary_table_header_present():
    title, summary = persona._summary([CheckResult("x", True, "y")])
    assert summary.startswith("| Check | Status | Detail |")
    assert "|---|---|---|" in summary.split("\n")[1]


# --- evaluate_pull_request (pure) ---

def test_evaluate_pull_request_passes_on_good_body():
    evaluation = persona.evaluate_pull_request(_GOOD_BODY)

    assert evaluation.passed is True
    assert evaluation.conclusion == "success"
    assert len(evaluation.results) == 5  # 5 dor checks
    assert all(r.passed for r in evaluation.results)


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
    assert "1/5 blocking" in title  # only scope-fence counts, not issue-link


def test_summary_advisory_check_renders_warning_icon():
    """Advisory checks that fail should render ⚠️ not ❌ in the summary."""
    results = [
        CheckResult("why", True, "ok"),
        CheckResult("issue-link", False, "no link"),
    ]
    title, summary = persona._summary(results)
    assert "✅" in title  # overall pass (issue-link is advisory)
    assert "⚠️" in summary
    assert "❌" not in summary


def test_evaluate_pull_request_is_pure_no_external_calls():
    """Spec 0002 attests `evaluate_pull_request_is_pure_function`.
    Verify the function never touches with_install_token_retry or
    post_check_run — any patched call would assert-fail."""
    with patch.object(persona, "with_install_token_retry") as retry_mock, \
         patch.object(persona, "post_check_run") as post_mock:
        persona.evaluate_pull_request(_GOOD_BODY)
    retry_mock.assert_not_called()
    post_mock.assert_not_called()


# --- publish_tpm_evaluation (impure) ---

def test_publish_tpm_evaluation_posts_on_success():
    evaluation = persona.evaluate_pull_request(_GOOD_BODY)
    captured = {}

    def fake_retry(install_id, fn):
        return fn("fake-token")

    def fake_post(*, install_token, owner, repo, result, external_id):
        captured["status"] = result.status
        captured["conclusion"] = result.conclusion
        captured["external_id"] = external_id
        captured["head_sha"] = result.head_sha
        return {"id": 999}

    with patch.object(persona, "with_install_token_retry", side_effect=fake_retry):
        with patch.object(persona, "post_check_run", side_effect=fake_post):
            persona.publish_tpm_evaluation(
                evaluation,
                installation_id=1,
                owner="myorg", repo="myrepo",
                head_sha="abc123def456" + "0" * 28,
                pr_number=42,
            )

    assert captured["status"] == "completed"
    assert captured["conclusion"] == "success"
    assert captured["external_id"] == f"grug-tpm:myorg/myrepo#42:{'abc123def456' + '0' * 28}"


def test_publish_tpm_evaluation_posts_on_failure():
    evaluation = persona.evaluate_pull_request("")
    captured = {}

    def fake_retry(install_id, fn):
        return fn("fake-token")

    def fake_post(*, install_token, owner, repo, result, external_id):
        captured["conclusion"] = result.conclusion
        captured["title"] = result.title

    with patch.object(persona, "with_install_token_retry", side_effect=fake_retry):
        with patch.object(persona, "post_check_run", side_effect=fake_post):
            persona.publish_tpm_evaluation(
                evaluation,
                installation_id=1,
                owner="o", repo="r",
                head_sha="x" * 40,
                pr_number=1,
            )

    assert captured["conclusion"] == "failure"
    assert "❌" in captured["title"]


def test_publish_tpm_evaluation_uses_grug_check_name():
    evaluation = persona.evaluate_pull_request(_GOOD_BODY)
    captured = {}

    def fake_retry(install_id, fn):
        return fn("fake-token")

    def fake_post(*, install_token, owner, repo, result, external_id):
        captured["name"] = result.name

    with patch.object(persona, "with_install_token_retry", side_effect=fake_retry):
        with patch.object(persona, "post_check_run", side_effect=fake_post):
            persona.publish_tpm_evaluation(
                evaluation,
                installation_id=1, owner="o", repo="r",
                head_sha="x" * 40, pr_number=1,
            )

    # Branch protection ruleset relies on this exact string. Drift = silent
    # cutover regression.
    assert captured["name"] == "Grug — Definition of Ready"


def test_publish_tpm_evaluation_external_id_format():
    """external_id binds (owner, repo, pr_number, head_sha) so GH
    de-duplicates across re-fires. Format matters for grep-ability."""
    evaluation = persona.evaluate_pull_request(_GOOD_BODY)
    captured = {}

    def fake_retry(install_id, fn):
        return fn("fake-token")

    def fake_post(*, install_token, owner, repo, result, external_id):
        captured["external_id"] = external_id

    with patch.object(persona, "with_install_token_retry", side_effect=fake_retry):
        with patch.object(persona, "post_check_run", side_effect=fake_post):
            persona.publish_tpm_evaluation(
                evaluation,
                installation_id=1,
                owner="myorg", repo="myrepo",
                head_sha="deadbeef" + "0" * 32,
                pr_number=99,
            )

    assert captured["external_id"] == "grug-tpm:myorg/myrepo#99:deadbeef" + "0" * 32


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
