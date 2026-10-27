"""Dispatch wiring for the covered_by_passing_check kill (grug #763).

The kill itself is fixture-tested in test_code_review_verify.py. These pin
the dispatch side: the head's check runs become evidence, the deep arm
lists them only when a caller-not-updated finding could use them, and any
failure is no evidence (every finding kept), never an aborted review.
"""

from __future__ import annotations

import httpx
from personas.code_reviewer import dispatch as cr_dispatch
from personas.code_reviewer.persona import Finding
from personas.code_reviewer.verify import CheckEvidence

_HEAD = "9f7c499" + "0" * 33


def _finding(rule_name: str) -> Finding:
    return Finding(
        file="infra/scripts/attest_persona_purity.py", line=5,
        severity="medium", rule_name=rule_name, message="msg",
        suggestion=None,
    )


def _run(**kw) -> dict:
    base = {
        "name": "attest persona purity", "head_sha": _HEAD,
        "status": "completed", "conclusion": "success",
        "output": {"title": None, "summary": None, "text": None},
    }
    base.update(kw)
    return base


def _patch(monkeypatch, listing):
    calls: list[str] = []

    def _list(token, owner, repo, ref):
        calls.append(ref)
        return listing(ref)

    monkeypatch.setattr(
        cr_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("fake-token"),
    )
    monkeypatch.setattr(cr_dispatch, "list_check_runs_for_ref", _list)
    return calls


def _fetch(findings, head_sha=_HEAD):
    return cr_dispatch._fetch_check_evidence(
        findings, installation_id=1, owner="o", repo_name="r",
        pull_number=7, head_sha=head_sha,
    )


def test_check_evidence_skips_malformed_rows():
    evidence = cr_dispatch._check_evidence([_run(), "junk", None])
    assert evidence == (CheckEvidence(
        name="attest persona purity", head_sha=_HEAD,
        status="completed", conclusion="success", text="",
    ),)
    assert cr_dispatch._check_evidence(None) == ()


def test_deep_arm_lists_the_head_checks_for_a_caller_not_updated_finding(
    monkeypatch,
):
    calls = _patch(monkeypatch, lambda ref: [_run()])
    evidence = _fetch((_finding("caller-not-updated"),))
    assert calls == [_HEAD]
    assert [e.name for e in evidence] == ["attest persona purity"]


def test_deep_arm_skips_the_listing_without_a_caller_not_updated_finding(
    monkeypatch,
):
    calls = _patch(monkeypatch, lambda ref: [_run()])
    assert _fetch((_finding("logic-error"),)) == ()
    assert _fetch((_finding("caller-not-updated"),), head_sha="") == ()
    assert calls == []


def test_a_failed_listing_is_no_evidence(monkeypatch):
    def _boom(ref):
        raise httpx.ConnectError("down")

    _patch(monkeypatch, _boom)
    assert _fetch((_finding("caller-not-updated"),)) == ()
