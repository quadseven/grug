"""#540: per-backend/model Elder telemetry. Every posted finding and every
human verdict is counted with `backend` + `model` tags, and the per-model
precision derived from the ledger is published as a gauge, so a monitor can
see which model's findings get accepted or refuted."""
from __future__ import annotations

import pytest

import observability
from personas.code_reviewer import dispatch as cr_dispatch
from personas.code_reviewer import reactions as cr_reactions
from personas.code_reviewer.persona import Finding
from llm_client import Backend, FindingOrigin

from tests.test_code_reviewer_reactions import _ensemble_record, _learning_record


@pytest.fixture
def emitted(monkeypatch):
    counts: list[tuple] = []
    gauges: list[tuple] = []
    monkeypatch.setattr(
        observability, "emit_count",
        lambda metric, value=1, tags=None: counts.append((metric, value, tags)),
    )
    monkeypatch.setattr(
        observability, "emit_gauge",
        lambda metric, value, tags=None: gauges.append((metric, value, tags)),
    )
    return counts, gauges


@pytest.fixture
def store(monkeypatch):
    from adapters import install_store
    rows: list[dict] = []
    monkeypatch.setattr(install_store, "put_ledger_row", lambda r: rows.append(r))
    monkeypatch.setattr(install_store, "list_ledger_rows", lambda repo: list(rows))
    monkeypatch.setattr(install_store, "put_repo_practices", lambda *a: None)
    monkeypatch.setattr(install_store, "put_repo_exemplars", lambda *a: None)
    return rows


def _ensemble_learning(comment_id=9):
    rec = _ensemble_record(comment_id=comment_id)
    rec.update({"finding_text": "unchecked optional", "head_sha": "abc",
                "trust_reactors": True})
    rec["finding_tags"]["severity"] = "high"
    return rec


def test_verdict_counts_per_backend_and_model(emitted, store):
    counts, _ = emitted
    cr_reactions._record_reaction_learning(_ensemble_learning(), "confirmed")
    verdicts = [c for c in counts if c[0] == "grug.elder.finding_verdict"]
    assert {(t["backend"], t["model"], t["verdict"]) for _, _, t in verdicts} == {
        ("poolside", "poolside/laguna-m.1", "accepted"),
        ("openrouter", "anthropic/claude-opus-4.7", "accepted"),
    }
    assert all(v == 1 for _, v, _ in verdicts)


def test_false_positive_counts_as_rejected(emitted, store):
    counts, _ = emitted
    cr_reactions._record_reaction_learning(_ensemble_learning(), "false_positive")
    assert {t["verdict"] for m, _, t in counts if m == "grug.elder.finding_verdict"} == {"rejected"}


def test_legacy_record_without_origin_tagged_unknown(emitted, store):
    counts, _ = emitted
    cr_reactions._record_reaction_learning(_learning_record(), "confirmed")
    tags = [t for m, _, t in counts if m == "grug.elder.finding_verdict"]
    assert tags == [{"backend": "unknown", "model": "unknown", "verdict": "accepted"}]


def test_precision_gauge_per_model_from_ledger_corpus(emitted, store):
    _, gauges = emitted
    cr_reactions._record_reaction_learning(_ensemble_learning(9), "confirmed")
    # a second finding refuted for both models -> 1 accepted / 2 labeled
    second = _ensemble_learning(10)
    second["finding_text"] = "another"
    cr_reactions._record_reaction_learning(second, "false_positive")
    last = {}
    for m, v, t in gauges:
        last[(m, t["backend"], t["model"])] = v
    key = ("poolside", "poolside/laguna-m.1")
    assert last[("grug.elder.reviewer_precision", *key)] == 0.5
    assert last[("grug.elder.reviewer_labeled", *key)] == 2.0


def test_untrusted_record_emits_nothing(emitted, store):
    counts, gauges = emitted
    rec = _ensemble_learning()
    rec["trust_reactors"] = False
    cr_reactions._record_reaction_learning(rec, "confirmed")
    assert counts == [] and gauges == []


def test_posted_finding_counted_per_origin(emitted, monkeypatch):
    counts, _ = emitted
    monkeypatch.setattr(cr_dispatch, "put_comment_record", lambda **kw: None)
    f = Finding(
        file="src/x.py", line=2, severity="medium", rule_name="silent-failure",
        message="m", suggestion=None,
        origins=(
            FindingOrigin(backend=Backend.POOLSIDE, model="poolside/laguna-m.1"),
            FindingOrigin(backend=Backend.CAVE, model="code-specialist"),
        ),
    )
    n = cr_dispatch._capture_comment_records(
        [{"id": 5, "path": "src/x.py", "body": "m\n<!-- grug-rule:silent-failure -->"}],
        (f,), install_id=1, repo="o/r", pr_number=3,
        review_span_context=None, head_sha="abc", author_login="a",
    )
    assert n == 1
    posted = [t for m, _, t in counts if m == "grug.elder.finding_posted"]
    assert posted == [
        {"backend": "poolside", "model": "poolside/laguna-m.1"},
        {"backend": "cave", "model": "code-specialist"},
    ]


def test_metric_failure_never_breaks_capture(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("udp down")
    monkeypatch.setattr(observability, "emit_count", boom)
    monkeypatch.setattr(cr_dispatch, "put_comment_record", lambda **kw: None)
    f = Finding(file="x.py", line=1, severity="low", rule_name="r", message="m",
                suggestion=None,
                origins=(FindingOrigin(backend=Backend.CAVE, model="m"),))
    assert cr_dispatch._capture_comment_records(
        [{"id": 1, "path": "x.py", "body": "<!-- grug-rule:r -->"}], (f,),
        install_id=1, repo="o/r", pr_number=1, review_span_context=None,
        head_sha="a", author_login="a",
    ) == 1


def test_same_model_name_on_two_backends_stays_two_producers():
    from personas.code_reviewer.model_metrics import origin_dims
    dims = origin_dims([
        {"backend": "cave", "model": "m1"},
        {"backend": "openrouter", "model": "m1"},
        {"backend": "cave", "model": "m1"},
    ])
    assert [(b, m) for _l, b, m in dims] == [("cave", "m1"), ("openrouter", "m1")]


def test_reaction_reviewer_labels_stay_deduped_across_backends():
    """The same model served by two backends is two producers for telemetry
    but ONE ledger label; writing that label twice would count one human
    verdict twice in the precision corpus."""
    from personas.code_reviewer.reactions import _reaction_reviewers

    record = {"finding_origins": [
        {"backend": "opencode-go", "model": "m"},
        {"backend": "openrouter", "model": "m"},
        {"backend": "cave", "model": "other"},
    ]}
    assert _reaction_reviewers(record) == ["grug-elder/m", "grug-elder/other"]
