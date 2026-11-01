"""Different-lineage second review for high-risk PRs (grug#1573)."""
from __future__ import annotations

from unittest.mock import patch

import httpx

import llm_client
from ledger import parse_row, second_lineage_yield
from llm_client import (
    Backend,
    Finding as LlmFinding,
    FindingOrigin,
    Hunk,
    LlmReviewResponse,
)
from personas import review_publish
from personas.code_reviewer import dispatch as cr_dispatch
from personas.code_reviewer.judge import RefuteOutcome
from review_evidence import assemble_evidence

_AUTH_DIFF = """diff --git a/src/auth/session.py b/src/auth/session.py
--- a/src/auth/session.py
+++ b/src/auth/session.py
@@ -1,2 +1,3 @@
 context
+check = token == expected
 tail
"""
_PLAIN_DIFF = _AUTH_DIFF.replace("src/auth/session.py", "src/plain.py")


def _second_model_finding(path="src/auth/session.py", severity="high"):
    origin = FindingOrigin(backend=Backend.SUBSCRIPTION, model="gpt-6-luna")
    return LlmFinding(
        path=path, line=2, rule="timing-unsafe-compare", origins=(origin,),
        severity=severity, message="token compared with ==",  # type: ignore[arg-type]
    )


class _Run:
    def __init__(self, monkeypatch, *, second=None):
        self.second_calls: list = []
        self.second = second or LlmReviewResponse(
            kind="reviewed", findings=(_second_model_finding(),),
            backend_used=Backend.SUBSCRIPTION, model_name="gpt-6-luna",
            models_used=("gpt-6-luna",),
        )
        self.posted: list = []
        self.checks: list = []
        self.rows: list = []
        monkeypatch.setenv("GRUG_SECOND_LINEAGE_REVIEW", "1")
        monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: LlmReviewResponse(
            kind="reviewed", backend_used=Backend.POOLSIDE,
            models_used=("deepseek-v4.1-flash",),
        ))
        monkeypatch.setattr(cr_dispatch, "review_second_lineage", self._second)
        monkeypatch.setattr(cr_dispatch, "grade_findings", lambda *a, **kw: ())
        monkeypatch.setattr(
            cr_dispatch, "refute_with_outcome",
            lambda *a, **kw: RefuteOutcome((), frozenset()),
        )
        monkeypatch.setattr(
            cr_dispatch, "post_check_run",
            lambda t, o, r, result, external_id=None: self.checks.append(result) or {},
        )
        monkeypatch.setattr(
            cr_dispatch, "post_review",
            lambda *a, **kw: self.posted.append(kw["result"]) or {},
        )
        monkeypatch.setattr(
            cr_dispatch, "fetch_file_contents",
            lambda t, o, r, paths, sha: {
                p: "context\ncheck = token == expected\ntail\n" for p in paths
            },
        )
        monkeypatch.setattr(
            cr_dispatch, "put_second_lineage_rows", lambda rows: self.rows.extend(rows),
        )
        for module in (cr_dispatch, review_publish):
            monkeypatch.setattr(
                module, "with_install_token_retry", lambda inst, fn: fn("t"),
            )

    def _second(self, evidence, installation_id, primary_models, cancel_event=None):
        self.second_calls.append((evidence, primary_models))
        return self.second

    def review(self, diff=_AUTH_DIFF):
        payload = {
            "action": "opened", "installation": {"id": 1},
            "repository": {"id": 2, "name": "r", "owner": {"login": "o"}},
            "pull_request": {
                "number": 3, "head": {"sha": "abcd1234"}, "base": {"sha": "base1234"},
                "title": "t", "body": "b", "user": {"login": "a"},
            },
        }
        reply = httpx.Response(
            200, text=diff, request=httpx.Request("GET", "https://example.test"),
        )
        with patch("httpx.get", return_value=reply):
            cr_dispatch.dispatch_code_review(payload, blocking=True)


def test_high_risk_path_gets_the_second_pass_and_one_review(monkeypatch):
    run = _Run(monkeypatch)

    run.review()

    assert len(run.second_calls) == 1
    assert run.second_calls[0][1] == ("deepseek-v4.1-flash",)
    assert len(run.posted) == 1
    assert "Second lineage" in run.posted[0].comments[0].body


def test_ordinary_paths_never_get_the_second_pass(monkeypatch):
    run = _Run(monkeypatch)

    run.review(diff=_PLAIN_DIFF)

    assert run.second_calls == []
    assert run.rows == []


def test_switch_off_makes_no_call(monkeypatch):
    run = _Run(monkeypatch)
    monkeypatch.delenv("GRUG_SECOND_LINEAGE_REVIEW")

    run.review()

    assert run.second_calls == []


def test_an_outage_marks_the_pr_single_lineage(monkeypatch):
    run = _Run(monkeypatch, second=LlmReviewResponse(kind="all_failed", error="down"))

    run.review()

    assert "Single-lineage review" in run.checks[-1].summary
    assert run.rows[-1]["verdict"] == "single-lineage"


def test_a_reply_from_the_same_family_marks_single_lineage(monkeypatch):
    run = _Run(monkeypatch, second=LlmReviewResponse(
        kind="reviewed", backend_used=Backend.SUBSCRIPTION,
        models_used=("deepseek-v4.1-pro",),
    ))

    run.review()

    assert "Single-lineage review" in run.checks[-1].summary


def test_a_clean_second_pass_is_not_marked(monkeypatch):
    run = _Run(monkeypatch)

    run.review()

    assert "Single-lineage review" not in run.checks[-1].summary
    assert run.rows[-1]["verdict"] == "second-lineage-run"
    assert run.rows[-1]["model_calls"] >= 1


def test_each_second_only_blocking_finding_gets_a_ledger_row(monkeypatch):
    run = _Run(monkeypatch)

    run.review()

    assert [r["verdict"] for r in run.rows] == ["second-only", "second-lineage-run"]


def test_ledger_reports_yield_and_cost_per_incremental_finding():
    def _row(verdict, calls=0):
        return parse_row({
            "repo": "o/r", "pr": 1, "reviewer": "elder-second-lineage",
            "severity": "high", "class": "c", "finding": "f", "verdict": verdict,
            "evidence": f"e{verdict}", "ts": "", "model_calls": calls,
        })

    report = second_lineage_yield([
        _row("second-lineage-run", 3), _row("second-lineage-run", 3),
        _row("second-only"), _row("single-lineage", 1),
    ])

    assert report == {
        "runs": 2, "second_only_blocking": 1, "single_lineage": 1,
        "model_calls": 7, "calls_per_finding": 7.0,
    }


def test_second_pass_rows_never_become_eval_cases():
    from elder_eval.corpus import build_cases

    row = parse_row({
        "repo": "o/r", "pr": 1, "reviewer": "elder-second-lineage",
        "severity": "high", "class": "c", "finding": "f", "verdict": "second-only",
        "evidence": "e", "ts": "",
    })

    assert build_cases([row]) == ()


def test_the_lane_is_told_which_family_to_avoid(monkeypatch):
    seen: list = []

    def _tier(messages, variant, pr_tags, hunks, pr_context, installation_id):
        seen.append(llm_client._review_author_family())
        return LlmReviewResponse(kind="reviewed")

    monkeypatch.setattr(llm_client, "_run_subscription_fallback", _tier)
    evidence = assemble_evidence([Hunk("a/auth.py", "+a")])

    llm_client.review_second_lineage(evidence, 1, ("gpt-6-luna",))

    assert seen == ["gpt"]
    assert llm_client._review_author_family() is None


def test_no_subscription_lane_is_a_failed_second_pass_not_paid_spend(monkeypatch):
    monkeypatch.setattr(llm_client, "_subscription_fallback_config", lambda: None)
    monkeypatch.setattr(
        llm_client, "_call_backend",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no other backend")),
    )
    evidence = assemble_evidence([Hunk("a/auth.py", "+a")])

    response = llm_client.review_second_lineage(evidence, 1, ("gpt-6-luna",))

    assert response.kind == "all_failed"
