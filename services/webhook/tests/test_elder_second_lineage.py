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

    def review(self, diff=_AUTH_DIFF, footer="", cancelled=False):
        import threading

        cancel = threading.Event()
        if cancelled:
            cancel.set()
        payload = {
            "action": "opened", "installation": {"id": 1},
            "repository": {"id": 2, "name": "r", "owner": {"login": "o"}},
            "pull_request": {
                "number": 3, "head": {"sha": "abcd1234"}, "base": {"sha": "base1234"},
                "title": "t", "body": f"b{footer}", "user": {"login": "a"},
            },
        }
        reply = httpx.Response(
            200, text=diff, request=httpx.Request("GET", "https://example.test"),
        )
        with patch("httpx.get", return_value=reply):
            cr_dispatch.dispatch_code_review(payload, blocking=True, cancel_event=cancel)


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


def _reply(*models, findings=()):
    return LlmReviewResponse(
        kind="reviewed", findings=findings, backend_used=Backend.SUBSCRIPTION,
        models_used=models,
    )


def test_a_same_family_reply_posts_no_second_lineage_finding(monkeypatch):
    run = _Run(monkeypatch, second=_reply(
        "deepseek-v4.1-pro", findings=(_second_model_finding(),),
    ))

    run.review()

    assert run.posted == []
    assert [r["verdict"] for r in run.rows] == ["single-lineage"]


def test_a_failed_merge_never_reports_two_lineages(monkeypatch):
    run = _Run(monkeypatch)
    monkeypatch.setattr(
        cr_dispatch, "merge_by_fingerprint",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    run.review()

    assert "Single-lineage review" in run.checks[-1].summary
    assert run.rows[-1]["verdict"] == "single-lineage"


def _is_second(reply, primary, author=None):
    return cr_dispatch._is_second_lineage(reply, primary, author)


def test_an_empty_or_unknown_primary_fails_closed():
    assert not _is_second(_reply("gpt-6-luna"), ())
    assert not _is_second(_reply("gpt-6-luna"), ("spark:warm-any",))
    assert not _is_second(_reply("gpt-6-luna"), ("deepseek-v4", "spark:warm-any"))


def test_an_unknown_answering_model_fails_closed():
    assert not _is_second(_reply("subscription-lane"), ("deepseek-v4",))


def test_a_reply_on_the_author_family_is_not_a_second_lineage():
    assert not _is_second(_reply("gpt-6-luna"), ("deepseek-v4",), "gpt")
    assert _is_second(_reply("gpt-6-luna"), ("deepseek-v4",), "claude")


def _lane_headers(monkeypatch, *, primary, author=None):
    from llm_client import _subscription_fallback_config, _merged_headers

    monkeypatch.setenv("GRUG_SUBSCRIPTION_URL", "http://lane.test/v1/chat/completions")
    seen: list = []

    def _tier(messages, variant, pr_tags, hunks, pr_context, installation_id):
        seen.append(_merged_headers(_subscription_fallback_config(), "k"))
        return LlmReviewResponse(kind="reviewed")

    monkeypatch.setattr(llm_client, "_run_subscription_fallback", _tier)
    context = {"author_family": author} if author else {}
    evidence = assemble_evidence([Hunk("a/auth.py", "+a")], pr_context=context)
    llm_client.review_second_lineage(evidence, 1, primary)
    return seen[0]


def test_the_wire_header_prefers_the_author_family(monkeypatch):
    headers = _lane_headers(monkeypatch, primary=("deepseek-v4",), author="gpt")

    assert headers["X-Review-Author-Family"] == "gpt"


def test_with_no_author_the_wire_header_names_the_primary_family(monkeypatch):
    headers = _lane_headers(monkeypatch, primary=("deepseek-v4",))

    assert headers["X-Review-Author-Family"] == "deepseek"


def test_a_cancelled_review_writes_no_rows_and_spends_no_call(monkeypatch):
    run = _Run(monkeypatch)

    run.review(cancelled=True)

    assert run.second_calls == []
    assert run.rows == []


def test_a_diff_too_big_for_one_call_never_reaches_the_lane(monkeypatch):
    monkeypatch.setattr(llm_client, "review_cohort_char_budget", lambda: 10)
    monkeypatch.setattr(
        llm_client, "_run_subscription_fallback",
        lambda *a: (_ for _ in ()).throw(AssertionError("no call")),
    )
    evidence = assemble_evidence([Hunk("a/auth.py", "+" + "a" * 50)])

    response = llm_client.review_second_lineage(evidence, 1, ("gpt-6-luna",))

    assert response.kind == "all_failed"


def test_half_the_daily_cap_stays_for_the_last_resort(monkeypatch):
    monkeypatch.setattr(llm_client, "_subscription_daily_cap", lambda: 10)
    monkeypatch.setattr(llm_client, "_subscription_calls_today", lambda: 5)
    monkeypatch.setattr(
        llm_client, "_run_subscription_fallback",
        lambda *a: (_ for _ in ()).throw(AssertionError("no call")),
    )
    evidence = assemble_evidence([Hunk("a/auth.py", "+a")])

    assert llm_client.review_second_lineage(evidence, 1, ()).kind == "all_failed"


def test_the_second_pass_never_raises_a_primary_severity():
    from personas.code_reviewer.persona import Finding
    from personas.code_reviewer.quarantine import merge_by_fingerprint

    def _f(severity):
        return Finding(
            file="a.py", line=1, severity=severity, rule_name="r", message="m",
            suggestion=None, category="security",
        )

    merged = merge_by_fingerprint((_f("medium"),), (_f("high"),), second_opinion=True)

    assert [f.severity for f in merged.findings] == ["medium"]


def test_second_only_rows_are_redacted_and_stable_across_pushes():
    finding = cr_dispatch.Finding(
        file="a.py", line=1, severity="high", rule_name="r",
        message="[Second lineage] leaked ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        suggestion=None,
    )

    def _rows(head):
        return cr_dispatch._second_lineage_rows(
            "second-lineage-run", (finding,), repo="o/r", pull_number=1,
            head_sha=head, model_calls=1,
        )

    first, second = _rows("aaa"), _rows("bbb")

    assert "ghp_" not in first[0]["finding"]
    assert not first[0]["finding"].startswith("[Second lineage]")
    assert first[0]["evidence"] == second[0]["evidence"]


def test_migration_paths_are_high_risk_for_this_pass_only():
    from personas.code_reviewer.quarantine import high_risk_paths, sensitive_paths
    from personas.code_reviewer.repo_config import RepoConfig

    path = ("db/migrations/0042_add.sql",)

    assert high_risk_paths(path, RepoConfig()) == path
    assert sensitive_paths(path, RepoConfig()) == ()


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
