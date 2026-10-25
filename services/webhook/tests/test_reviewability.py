"""Behavior tests for the ReviewabilityAssessment.

Pure, deterministic, no model calls. Every score is 0.0 (poor) to 1.0
(good) and carries evidence, so the assessment reports dimensions rather
than a single opaque score.
"""

from __future__ import annotations

from dataclasses import dataclass

from reviewability import (
    classify_hunk,
    dimension_score,
    assess_reviewability,
)


@dataclass(frozen=True)
class _Hunk:
    path: str
    body: str


# ── noise separation ───────────────────────────────────────────────────────


def test_classify_hunk_separates_noise_from_semantic() -> None:
    cases = [
        ("poetry.lock", "", True, "lockfile"),
        ("package-lock.json", "", True, "lockfile"),
        ("vendor/acme/lib.py", "", True, "vendored"),
        ("third_party/foo.js", "", True, "vendored"),
        ("gen/weather_pb2.py", "", True, "generated"),
        ("dist/bundle.min.js", "x" * 600, True, "generated"),
        ("src/payments.py", "+def charge():\n+    return 1\n", False, ""),
        ("tests/test_payments.py", "+def test_charge():\n+    pass\n", False, ""),
    ]
    for path, body, expect_noise, expect_reason in cases:
        is_noise, reason = classify_hunk(path, body)
        assert is_noise is expect_noise, (path, body, is_noise, reason)
        assert reason == expect_reason, (path, reason)


def test_assessment_separates_noise_paths() -> None:
    hunks = [
        _Hunk("poetry.lock", ""),
        _Hunk("vendor/acme/lib.py", ""),
        _Hunk("src/payments.py", "+def charge():\n+    return 1\n"),
        _Hunk("tests/test_payments.py", "+def test_charge():\n+    pass\n"),
    ]
    assessment = assess_reviewability(hunks)
    assert assessment.noise_paths == ("poetry.lock", "vendor/acme/lib.py")
    assert assessment.semantic_paths == ("src/payments.py", "tests/test_payments.py")
    noise = next(d for d in assessment.dimensions if d.name == "generated_vendor_noise")
    assert noise.score == 0.5
    assert "2 of 4 hunks" in noise.evidence


# ── dimensions are evidence-backed ─────────────────────────────────────────


def test_every_dimension_has_a_score_and_evidence() -> None:
    hunks = [
        _Hunk("src/payments.py", "+def charge():\n+    return 1\n"),
        _Hunk("tests/test_payments.py", "+def test_charge():\n+    pass\n"),
    ]
    assessment = assess_reviewability(hunks)
    for dim in assessment.dimensions:
        assert 0.0 <= dim.score <= 1.0, dim
        assert dim.evidence, dim.name
        assert isinstance(dim.paths, tuple)
    names = {dim.name for dim in assessment.dimensions}
    assert names == {
        "size",
        "cohesion",
        "cross_boundary_coupling",
        "generated_vendor_noise",
        "test_to_change_alignment",
        "partitionability",
    }


# ── coherent large change vs unrelated bundled change ──────────────────────


def test_coherent_large_change_is_reviewable() -> None:
    """One semantic unit (payments) across contract/implementation/test/docs."""
    hunks = [
        _Hunk("schemas/payment.py", "+TABLE payment\n"),
        _Hunk("src/payments.py", "+def charge():\n+    return 1\n"),
        _Hunk("tests/test_payments.py", "+def test_charge():\n+    pass\n"),
        _Hunk("docs/payments.md", "+# Payments\n"),
    ]
    assessment = assess_reviewability(hunks)

    assert assessment.reviewable is True
    assert assessment.partitionable is True
    cohesion = dimension_score(assessment, "cohesion")
    assert cohesion >= 0.7, cohesion
    # One suggested cohort, not a fragmented decomposition.
    assert len(assessment.suggested_cohorts) == 1
    assert assessment.decision == "pass"


def test_unrelated_bundled_change_is_not_reviewable() -> None:
    """Unrelated areas bundled into one PR: low cohesion, many cohorts."""
    hunks = [
        _Hunk("src/a.py", "+def a():\n+    return 1\n"),
        _Hunk("docs/readme.md", "+# notes\n"),
        _Hunk("infra/main.tf", '+resource "x" {}\n'),
        _Hunk("lib/util.py", "+def util():\n+    return 2\n"),
        _Hunk("tests/test_zzz.py", "+def test_zzz():\n+    pass\n"),
    ]
    assessment = assess_reviewability(hunks)

    assert assessment.reviewable is False
    cohesion = dimension_score(assessment, "cohesion")
    assert cohesion < 0.4, cohesion
    # The bundle is flagged as poorly bounded and offers a decomposition.
    assert assessment.decision in ("warn", "block")
    assert len(assessment.suggested_cohorts) >= 1
    for cohort in assessment.suggested_cohorts:
        assert cohort.risk_reduced


# ── suggested cohorts: high-risk first with files/symbols/dependencies ─────


def test_suggested_cohorts_are_high_risk_first() -> None:
    """Implementation + verification split is high risk and leads."""
    hunks = [
        _Hunk(
            "src/account.py", "+class Account:\n+    def charge(self):\n+        pass\n"
        ),
        _Hunk("tests/test_account.py", "+def test_charge():\n+    pass\n"),
        _Hunk("docs/account.md", "+# account\n"),
    ]
    assessment = assess_reviewability(hunks)

    assert len(assessment.suggested_cohorts) >= 1
    first = assessment.suggested_cohorts[0]
    # The implementation+verification cohort is high risk and comes first.
    assert {"src/account.py", "tests/test_account.py"}.issubset(set(first.paths))
    assert "charge" in first.symbols
    assert "behavior and its evidence" in first.risk_reduced


def test_suggested_cohort_carries_files_symbols_and_dependencies() -> None:
    hunks = [
        _Hunk(
            "src/client.py",
            "+import requests\n+from src.auth import Auth\n+class Client:\n+    def get(self):\n+        pass\n",
        ),
        _Hunk("tests/test_client.py", "+import pytest\n+def test_get():\n+    pass\n"),
    ]
    assessment = assess_reviewability(hunks)
    cohort = assessment.suggested_cohorts[0]
    assert "src/client.py" in cohort.paths
    assert "tests/test_client.py" in cohort.paths
    assert "Client" in cohort.symbols or "get" in cohort.symbols
    assert "requests" in cohort.dependencies or "auth" in cohort.dependencies
    assert cohort.risk_reduced


# ── policy: warn / block / ignore without model prompts ────────────────────


def test_policy_warn_keeps_advisory_when_not_reviewable() -> None:
    hunks = [
        _Hunk("src/a.py", "+def a():\n+    return 1\n"),
        _Hunk("docs/readme.md", "+# notes\n"),
        _Hunk("infra/main.tf", '+resource "x" {}\n'),
    ]
    assessment = assess_reviewability(hunks, policy="warn")
    assert assessment.reviewable is False
    assert assessment.decision == "warn"


def test_policy_block_escalates_when_not_reviewable() -> None:
    hunks = [
        _Hunk("src/a.py", "+def a():\n+    return 1\n"),
        _Hunk("docs/readme.md", "+# notes\n"),
        _Hunk("infra/main.tf", '+resource "x" {}\n'),
    ]
    assessment = assess_reviewability(hunks, policy="block")
    assert assessment.reviewable is False
    assert assessment.decision == "block"


def test_policy_ignore_silences_the_finding() -> None:
    hunks = [
        _Hunk("src/a.py", "+def a():\n+    return 1\n"),
        _Hunk("docs/readme.md", "+# notes\n"),
        _Hunk("infra/main.tf", '+resource "x" {}\n'),
    ]
    assessment = assess_reviewability(hunks, policy="ignore")
    assert assessment.reviewable is False
    assert assessment.decision == "pass"


def test_policy_default_is_warn() -> None:
    hunks = [
        _Hunk("src/a.py", "+def a():\n+    return 1\n"),
        _Hunk("docs/readme.md", "+# notes\n"),
    ]
    assessment = assess_reviewability(hunks)
    assert assessment.policy == "warn"
    assert assessment.decision in ("warn", "pass")


def test_reviewable_change_passes_under_block_policy() -> None:
    hunks = [
        _Hunk("src/payments.py", "+def charge():\n+    return 1\n"),
        _Hunk("tests/test_payments.py", "+def test_charge():\n+    pass\n"),
    ]
    assessment = assess_reviewability(hunks, policy="block")
    assert assessment.reviewable is True
    assert assessment.decision == "pass"


# ── partitionability and size ──────────────────────────────────────────────


def test_truncated_plan_is_not_partitionable() -> None:
    hunks = [_Hunk(f"area{i}/impl.py", "+x" * 60) for i in range(8)]
    assessment = assess_reviewability(
        hunks,
        max_cohort_chars=100,
        max_cohorts=3,
    )
    assert assessment.partitionable is False
    assert assessment.reviewable is False
    part = dimension_score(assessment, "partitionability")
    assert part < 0.5, part
    part_dim = next(d for d in assessment.dimensions if d.name == "partitionability")
    assert "reachable" in part_dim.evidence


def test_size_dimension_reports_budget_breach() -> None:
    hunks = [
        _Hunk("src/big.py", "+x" * 200),
        _Hunk("tests/test_big.py", "+y" * 200),
    ]
    assessment = assess_reviewability(hunks, max_cohort_chars=100)
    size = dimension_score(assessment, "size")
    assert size < 1.0, size
    size_dim = next(d for d in assessment.dimensions if d.name == "size")
    assert "Exceeds one bounded review unit" in size_dim.evidence


# ── empty diff ─────────────────────────────────────────────────────────────


def test_empty_diff_is_reviewable_and_trivially_partitionable() -> None:
    assessment = assess_reviewability([])
    assert assessment.reviewable is True
    assert assessment.partitionable is True
    assert assessment.decision == "pass"
    assert len(assessment.suggested_cohorts) == 0
