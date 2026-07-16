"""Tests for deterministic docs/code claim consistency (Qodo/CR class)."""

from __future__ import annotations

from personas.code_reviewer.claim_check import scan_claim_checks
from personas.code_reviewer.diff_parser import DiffHunk


def _hunk(path: str, start: int, added: list[str]) -> DiffHunk:
    body = f"@@ -1 +{start} @@\n" + "\n".join("+" + ln for ln in added)
    return DiffHunk(
        file_path=path,
        new_start=start,
        new_lines=frozenset(range(start, start + len(added))),
        body=body,
    )


_SETTLE_IMPL = (
    "def adaptive_elder_settle_seconds(pr, *, base_seconds: int) -> int:\n"
    "    base = max(0, int(base_seconds))\n"
    "    if changed <= 5 and churn <= 120:\n"
    "        return 0\n"
    "    if changed <= 12 and churn <= 400:\n"
    "        return min(base, 3)\n"
    "    return base\n"
)

_DEEP_EXCLUSIVE = (
    "def decide_deep_escalation(hunks, pr_context):\n"
    "    threshold = 500\n"
    "    added = 10\n"
    "    # Exclusive bound: N means more than N added lines.\n"
    "    if threshold > 0 and added > threshold:\n"
    "        reasons.append('diff')\n"
    "    return reasons\n"
)

_DEEP_INCLUSIVE = (
    "def decide_deep_escalation(hunks, pr_context):\n"
    "    threshold = 500\n"
    "    added = 10\n"
    "    if threshold > 0 and added >= threshold:\n"
    "        reasons.append('diff')\n"
    "    return reasons\n"
)


class TestSettleCapDrift:
    def test_flags_comment_claiming_5_when_code_is_3(self):
        """The exact #664 Qodo miss: k8s comment says 5s, code is min(base, 3)."""
        hunks = (
            _hunk(
                "k8s/webhook-deployment.yaml",
                98,
                [
                    "            # Base settle; Swift Hunt zeros tiny PRs "
                    "and caps medium (Steady) at 5s.",
                    '            - {name: GRUG_ELDER_SETTLE_SECONDS, value: "5"}',
                ],
            ),
        )
        out = scan_claim_checks(
            hunks,
            {
                "services/_shared/personas/code_reviewer/snapshot.py": _SETTLE_IMPL,
            },
        )
        assert len(out) == 1
        f = out[0]
        assert f.rule_name == "doc-code-claim-drift"
        assert f.severity == "medium"
        assert f.effort == "quick-win"
        assert f.line == 98
        assert "5" in f.message
        assert "3" in f.message
        assert "min(base" in f.message

    def test_clean_when_comment_matches_code(self):
        hunks = (
            _hunk(
                "k8s/webhook-deployment.yaml",
                98,
                [
                    "            # Base settle; Swift Hunt zeros tiny PRs "
                    "and caps medium (Steady) at 3s.",
                ],
            ),
        )
        out = scan_claim_checks(
            hunks,
            {
                "services/_shared/personas/code_reviewer/snapshot.py": _SETTLE_IMPL,
            },
        )
        assert out == ()

    def test_no_false_positive_without_implementation(self):
        """Foreign repos / missing sources: fail open, no finding."""
        hunks = (
            _hunk(
                "docs/RUNBOOK.md",
                10,
                ["Base settle caps medium at 5s for Steady Hunt."],
            ),
        )
        assert scan_claim_checks(hunks, {}) == ()


class TestDeepBoundDrift:
    def test_flags_inclusive_comment_when_code_is_exclusive(self):
        hunks = (
            _hunk(
                "k8s/consumer-deployment.yaml",
                95,
                [
                    "            # auto-deep when added lines >= 500 "
                    "(inclusive threshold).",
                    '            - {name: GRUG_DEEP_DIFF_LINES, value: "500"}',
                ],
            ),
        )
        out = scan_claim_checks(
            hunks,
            {"services/_shared/llm_client.py": _DEEP_EXCLUSIVE},
        )
        assert len(out) == 1
        f = out[0]
        assert f.rule_name == "doc-code-claim-drift"
        assert "exclusive" in f.message.lower() or ">" in f.message
        assert "inclusive" in f.message.lower() or ">=" in f.message

    def test_clean_when_comment_says_exclusive_and_code_is(self):
        hunks = (
            _hunk(
                "k8s/consumer-deployment.yaml",
                95,
                [
                    "            # auto-deep only when added lines > 500 "
                    "(exclusive; see decide_deep_escalation).",
                ],
            ),
        )
        out = scan_claim_checks(
            hunks,
            {"services/_shared/llm_client.py": _DEEP_EXCLUSIVE},
        )
        assert out == ()

    def test_flags_exclusive_comment_when_code_is_inclusive(self):
        hunks = (
            _hunk(
                "docs/adr/0019-tiered-elder-review.md",
                52,
                [
                    "- Diff size: escalate only above threshold "
                    "(exclusive) for GRUG_DEEP_DIFF_LINES",
                ],
            ),
        )
        out = scan_claim_checks(
            hunks,
            {"services/_shared/llm_client.py": _DEEP_INCLUSIVE},
        )
        assert len(out) == 1
        assert out[0].rule_name == "doc-code-claim-drift"


class TestIntraPrClaimConflict:
    def test_conflicting_settle_claims_without_code(self):
        hunks = (
            _hunk(
                "k8s/webhook-deployment.yaml",
                10,
                ["# caps medium at 5s"],
            ),
            _hunk(
                "k8s/consumer-deployment.yaml",
                10,
                ["# caps medium at 3s"],
            ),
        )
        out = scan_claim_checks(hunks, {})
        # Outliers vs mode: 5 appears once, 3 once - both are modes tied.
        # Counter.most_common(1) picks one; the other is flagged.
        assert len(out) >= 1
        assert all(f.rule_name == "doc-code-claim-drift" for f in out)


class TestNonClaimNoise:
    def test_ignores_unrelated_min_base_in_comment(self):
        # Without settle/medium language in a form we match, pure code
        # changes should not invent findings.
        hunks = (
            _hunk(
                "services/foo.py",
                1,
                ["return min(base, 99)"],
            ),
        )
        out = scan_claim_checks(
            hunks,
            {"services/foo.py": "def f(base):\n    return min(base, 3)\n"},
        )
        # The ADDED line is code, not a comment claim; and it's the
        # implementation itself - no finding expected from claim scanner.
        assert out == ()

    def test_fixture_added_gt_threshold_does_not_poison_deep_fact(self):
        """CodeRabbit: test/fixture files must not overwrite policy facts."""
        hunks = (
            _hunk(
                "k8s/consumer-deployment.yaml",
                95,
                [
                    "            # auto-deep only when added lines > 500 "
                    "(exclusive; see decide_deep_escalation).",
                ],
            ),
        )
        # Real policy is exclusive; a fixture file claims inclusive.
        out = scan_claim_checks(
            hunks,
            {
                "services/_shared/llm_client.py": _DEEP_EXCLUSIVE,
                "services/webhook/tests/test_llm_client.py": (
                    "def test_fixture():\n"
                    "    if added >= threshold:\n"
                    "        pass\n"
                ),
            },
        )
        # Still clean: exclusive claim matches exclusive policy; fixture ignored.
        assert out == ()

    def test_conflicting_policy_values_are_unknown_not_last_wins(self):
        """Two different min(base, N) in snapshot.py -> no fact, no finding."""
        hunks = (
            _hunk(
                "k8s/webhook-deployment.yaml",
                98,
                ["# caps medium (Steady) at 5s."],
            ),
        )
        src = (
            "def adaptive_elder_settle_seconds(pr, *, base_seconds: int) -> int:\n"
            "    if tiny:\n"
            "        return min(base, 0)\n"
            "    return min(base, 3)\n"
        )
        out = scan_claim_checks(
            hunks,
            {"services/_shared/personas/code_reviewer/snapshot.py": src},
        )
        assert out == ()
