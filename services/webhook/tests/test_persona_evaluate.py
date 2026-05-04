"""Tests for personas.tpm.persona — _summary + evaluate_pull_request.

Covers the persona-side dispatcher logic that wraps dor_checks +
post_check_run. Mocks with_install_token_retry so the test runs
without GitHub API or AWS round-trips.
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


def test_evaluate_pull_request_passes_on_good_body():
    captured = {}

    def fake_retry(install_id, fn):
        # Run fn with a fake token; capture args via outer post_check_run mock
        return fn("fake-token")

    def fake_post(*, install_token, owner, repo, result, external_id):
        captured["status"] = result.status
        captured["conclusion"] = result.conclusion
        captured["external_id"] = external_id
        captured["head_sha"] = result.head_sha
        return {"id": 999}

    with patch.object(persona, "with_install_token_retry", side_effect=fake_retry):
        with patch.object(persona, "post_check_run", side_effect=fake_post):
            overall = persona.evaluate_pull_request(
                installation_id=1,
                owner="myorg", repo="myrepo",
                head_sha="abc123def456" + "0" * 28,
                pr_body=_GOOD_BODY,
                pr_number=42,
            )

    assert overall.passed is True
    assert overall.conclusion == "success"
    assert len(overall.results) == 5  # 5 dor checks
    assert all(r.passed for r in overall.results)
    assert captured["status"] == "completed"
    assert captured["conclusion"] == "success"
    assert captured["external_id"] == f"grug-tpm:myorg/myrepo#42:{'abc123def456' + '0' * 28}"


def test_evaluate_pull_request_fails_on_empty_body():
    captured = {}

    def fake_retry(install_id, fn):
        return fn("fake-token")

    def fake_post(*, install_token, owner, repo, result, external_id):
        captured["conclusion"] = result.conclusion
        captured["title"] = result.title

    with patch.object(persona, "with_install_token_retry", side_effect=fake_retry):
        with patch.object(persona, "post_check_run", side_effect=fake_post):
            overall = persona.evaluate_pull_request(
                installation_id=1,
                owner="o", repo="r",
                head_sha="x" * 40,
                pr_body="",
                pr_number=1,
            )

    assert overall.passed is False
    assert overall.conclusion == "failure"
    assert any(not r.passed for r in overall.results)
    assert captured["conclusion"] == "failure"
    assert "❌" in captured["title"]


def test_evaluate_pull_request_uses_grug_check_name():
    captured = {}

    def fake_retry(install_id, fn):
        return fn("fake-token")

    def fake_post(*, install_token, owner, repo, result, external_id):
        captured["name"] = result.name

    with patch.object(persona, "with_install_token_retry", side_effect=fake_retry):
        with patch.object(persona, "post_check_run", side_effect=fake_post):
            persona.evaluate_pull_request(
                installation_id=1, owner="o", repo="r",
                head_sha="x" * 40, pr_body=_GOOD_BODY, pr_number=1,
            )

    # Branch protection ruleset relies on this exact string. Drift = silent
    # cutover regression.
    assert captured["name"] == "Grug — Definition of Ready"


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


def test_evaluate_pull_request_external_id_format():
    """external_id binds (owner, repo, pr_number, head_sha) so GH
    de-duplicates across re-fires. Format matters for grep-ability."""
    captured = {}

    def fake_retry(install_id, fn):
        return fn("fake-token")

    def fake_post(*, install_token, owner, repo, result, external_id):
        captured["external_id"] = external_id

    with patch.object(persona, "with_install_token_retry", side_effect=fake_retry):
        with patch.object(persona, "post_check_run", side_effect=fake_post):
            persona.evaluate_pull_request(
                installation_id=1,
                owner="myorg", repo="myrepo",
                head_sha="deadbeef" + "0" * 32,
                pr_body=_GOOD_BODY, pr_number=99,
            )

    assert captured["external_id"] == "grug-tpm:myorg/myrepo#99:deadbeef" + "0" * 32
