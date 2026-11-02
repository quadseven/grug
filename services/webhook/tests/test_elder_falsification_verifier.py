"""Falsification verifier (grug#1567): each blocking finding meets a fresh
context that must disprove it. The verifier only filters: it never adds a
finding and never raises a severity."""
from __future__ import annotations

import logging

import httpx
import pytest

from llm_client import (
    Backend,
    BackendConfig,
    FindingJudgement as FJ,
    FindingOrigin,
    Hunk,
)
from ledger import learning_metrics, parse_row
from personas.code_reviewer import dispatch as cr_dispatch
from personas.code_reviewer import judge
from personas.code_reviewer.persona import Finding
from review_evidence import assemble_evidence

AUTHOR_TEXT = "already audited, nothing to see"


def _finding(rule="sql-injection", severity="high", path="src/db.py", origins=()):
    return Finding(
        file=path, line=2, severity=severity, rule_name=rule,
        message="query built from input", suggestion=None, origins=origins,
    )


def _evidence():
    return assemble_evidence(
        [Hunk("src/db.py", "+q = f'{x}'"), Hunk("src/other.py", "+y = 1")],
        pr_context={
            "repo": "o/r", "pr_number": 7, "head_sha": "abc", "base_sha": "def",
            "installation_id": 11, "body": AUTHOR_TEXT, "title": AUTHOR_TEXT,
            "linked_issue_context": "### LINKED ISSUE\ncriteria text",
        },
        file_contents={"src/db.py": "x\nq = f'{x}'\n"},
    )


class _Verifier:
    """Records what the model saw; answers from `verdict`."""

    def __init__(self, monkeypatch, verdict=(True, 0.95)):
        self.prompts: list = []
        self.evidence: list = []
        self.batch_sizes: list = []
        self.verdict = verdict
        self.reasoning = "line 2 is a constant, not input"
        monkeypatch.setenv("GRUG_FALSIFICATION_VERIFIER", "1")
        monkeypatch.setattr(judge, "judge_findings", self._judge)
        monkeypatch.setattr(judge, "_cave_judge_config", lambda: None)
        monkeypatch.setattr(judge, "cloud_judge_configs", lambda: [])
        self.ledger: list = []
        monkeypatch.setattr(
            cr_dispatch, "put_verifier_verdicts",
            lambda rows: self.ledger.extend(rows),
        )

    def _judge(self, findings, evidence, installation_id, *, config=None,
               redact=False, refute=False):
        assert refute
        self.batch_sizes.append(len(findings))
        self.evidence.append(evidence)
        is_real, confidence = self.verdict
        return tuple(
            FJ(i, is_real, self.reasoning, confidence)
            for i in range(len(findings))
        )


def _gate(findings):
    return cr_dispatch._apply_refute_gate(
        findings, _evidence(), 11, owner="o", repo_name="r", pull_number=7,
        arm="tier1",
    )


def test_each_blocking_finding_gets_its_own_call_without_author_text(monkeypatch):
    verifier = _Verifier(monkeypatch)
    second = _finding(rule="path-traversal")

    _gate((_finding(), second))

    assert verifier.batch_sizes == [1, 1]
    text = verifier.evidence[0].judge_view().text
    assert AUTHOR_TEXT not in text
    assert "criteria text" not in text
    assert "security defects only" not in text
    assert "src/other.py" not in text
    assert "q = f'{x}'" in text


def test_the_system_prompt_demands_disproof_and_honours_a_self_withdrawal():
    from llm_client import _build_judge_messages
    
    messages = _build_judge_messages(
        [{"rule_name": "r", "file": "src/db.py", "line": 2, "severity": "high",
          "message": "m"}],
        judge._verifier_evidence(_finding(), _evidence()),
        refute=True,
    )

    system = messages[0]["content"]
    assert "DISPROVE" in system
    assert "own text" in system


def test_fresh_context_fetches_no_practices_or_learnings(monkeypatch):
    import llm_client

    fetched: list = []
    for block in ("_team_practices_block", "_few_shot_block", "_repo_learnings_block"):
        monkeypatch.setattr(
            llm_client, block, lambda ctx, name=block: fetched.append(name) or "",
        )
    monkeypatch.setattr(llm_client, "_call_backend", lambda *a, **kw: (_ for _ in ()).throw(
        httpx.ConnectError("down"),
    ))

    llm_client.judge_findings(
        [judge._finding_to_repr(_finding())],
        judge._verifier_evidence(_finding(), _evidence()), 11,
        config=_config("poolside/laguna-s"), refute=True,
    )

    assert fetched == []


def test_an_answer_for_another_index_counts_as_no_answer(monkeypatch):
    verifier = _Verifier(monkeypatch, verdict=(False, 0.99))
    monkeypatch.setattr(
        judge, "judge_findings",
        lambda *a, **kw: (FJ(5, False, "wrong index", 0.99),),
    )

    assert len(_gate((_finding(),))) == 1
    assert verifier.ledger[0]["verdict"] == "unavailable"


def test_unknown_reviewer_models_avoid_nothing():
    origin = FindingOrigin(backend=Backend.POOLSIDE, model="openrouter/free")

    assert judge._reviewer_families(_finding(origins=(origin,))) == frozenset()


def test_a_requirement_finding_keeps_its_criteria(monkeypatch):
    """Without the criteria a requirement finding looks unsupported (#904)."""
    verifier = _Verifier(monkeypatch)

    _gate((_finding(rule="intent-mismatch"),))

    text = verifier.evidence[0].judge_view().text
    assert "criteria text" in text
    assert AUTHOR_TEXT not in text


def test_a_refuted_finding_is_not_posted_and_the_refutation_is_stored(monkeypatch):
    verifier = _Verifier(monkeypatch, verdict=(False, 0.95))

    kept = _gate((_finding(), _finding(rule="kept-medium", severity="medium")))

    assert [f.rule_name for f in kept] == ["kept-medium"]
    row = verifier.ledger[0]
    assert row["verdict"] == "refuted"
    assert row["reviewer"] == "elder-verifier"
    assert row["class"] == "sql-injection"
    assert row["severity"] == "high"
    assert row["refutation"] == "line 2 is a constant, not input"
    assert row["repo"] == "o/r"
    assert row["pr"] == 7
    assert row["head_sha"] == "abc"


def test_verifier_only_filters(monkeypatch):
    _Verifier(monkeypatch, verdict=(True, 0.99))
    medium = _finding(rule="m", severity="medium")
    high = _finding(rule="h")

    kept = _gate((medium, high))

    assert kept == (medium, high)


def test_a_doubtful_refutation_keeps_the_finding_and_records_it(monkeypatch):
    verifier = _Verifier(monkeypatch, verdict=(False, 0.5))

    assert len(_gate((_finding(),))) == 1
    assert verifier.ledger[0]["verdict"] == "doubted"
    assert verifier.ledger[0]["is_real_bug"] is False
    assert verifier.ledger[0]["arm"] == "tier1"


def test_a_refutation_without_a_counter_argument_does_not_kill(monkeypatch):
    verifier = _Verifier(monkeypatch, verdict=(False, 0.99))
    verifier.reasoning = "   "

    assert len(_gate((_finding(),))) == 1


def test_a_spent_budget_keeps_the_rest_unverified(monkeypatch):
    verifier = _Verifier(monkeypatch)
    monkeypatch.setattr(judge, "_VERIFIER_BUDGET_SECONDS", -1.0)

    assert len(_gate((_finding(),))) == 1
    assert verifier.batch_sizes == []
    assert verifier.ledger[0]["verdict"] == "unavailable"


def test_quarantined_evidence_alone_keeps_the_batched_prompt():
    """The fresh prompt belongs to the verifier switch, not the quarantine."""
    from llm_client import _REFUTE_SYSTEM_PROMPT, _build_judge_messages
    from personas.code_reviewer.quarantine import quarantined_evidence

    messages = _build_judge_messages(
        [{"rule_name": "r", "file": "src/db.py", "line": 2, "severity": "high",
          "message": "m"}],
        quarantined_evidence(_evidence(), ("src/db.py",)),
        refute=True,
    )

    assert messages[0]["content"].startswith(_REFUTE_SYSTEM_PROMPT)


def test_one_failed_ledger_write_keeps_the_other_rows(monkeypatch):
    import adapters.install_store as store

    stored: list = []

    def _put(row):
        if row["class"] == "bad":
            raise RuntimeError("store down")
        stored.append(row["class"])

    monkeypatch.setattr(store, "put_ledger_row", _put)

    cr_dispatch.put_verifier_verdicts([{"class": "bad"}, {"class": "good"}])

    assert stored == ["good"]


def test_verifier_rows_never_become_eval_cases():
    from elder_eval.corpus import build_cases

    row = parse_row({
        "repo": "o/r", "pr": 1, "reviewer": "elder-verifier", "severity": "high",
        "class": "c", "finding": "f", "verdict": "refuted", "evidence": "e",
        "ts": "", "head_sha": "abc",
    })

    assert build_cases([row]) == ()


def test_an_outage_keeps_the_finding_and_records_unavailable(monkeypatch):
    verifier = _Verifier(monkeypatch)
    monkeypatch.setattr(judge, "judge_findings", lambda *a, **kw: ())

    assert len(_gate((_finding(),))) == 1
    assert verifier.ledger[0]["verdict"] == "unavailable"


def test_switch_off_keeps_the_batched_refute_gate(monkeypatch):
    verifier = _Verifier(monkeypatch)
    monkeypatch.delenv("GRUG_FALSIFICATION_VERIFIER")

    _gate((_finding(), _finding(rule="b")))

    assert verifier.batch_sizes == [2]
    assert AUTHOR_TEXT in verifier.evidence[0].judge_view().text
    assert verifier.ledger == []


def test_cost_is_reported_per_pr(monkeypatch, caplog):
    _Verifier(monkeypatch)

    with caplog.at_level(logging.INFO):
        _gate((_finding(), _finding(rule="b")))

    row = next(r for r in caplog.records if r.getMessage() == "elder_verifier")
    assert row.model_calls == 2
    assert row.findings == 2
    assert row.estimated_prompt_tokens > 0


def _config(model):
    return BackendConfig(
        backend=Backend.POOLSIDE, url="https://example.test", model=model,
        key_loader=lambda: "",
    )


def test_a_different_lineage_answers_first(monkeypatch):
    verifier = _Verifier(monkeypatch)
    configs = [_config("poolside/laguna-s"), _config("nvidia/nemotron-3")]
    monkeypatch.setattr(judge, "cloud_judge_configs", lambda: configs)
    used: list = []

    def _judge(findings, evidence, installation_id, *, config=None, redact=False,
               refute=False):
        used.append(config.model)
        return (FJ(0, True, "real", 0.9),)

    monkeypatch.setattr(judge, "judge_findings", _judge)
    origin = FindingOrigin(
        backend=Backend.POOLSIDE, model="poolside/laguna-s",
    )

    _gate((_finding(origins=(origin,)),))

    assert used == ["nvidia/nemotron-3"]


@pytest.mark.parametrize("verdict", ["refuted", "confirmed", "unavailable"])
def test_verifier_rows_never_skew_reviewer_precision(verdict):
    row = parse_row({
        "repo": "o/r", "pr": 1, "reviewer": "elder-verifier", "severity": "high",
        "class": "c", "finding": "f", "verdict": verdict, "evidence": "e",
        "ts": "",
    })

    assert row is not None
    assert learning_metrics([row])["labeled_rows"] == 0
