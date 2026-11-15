"""The eval behind the free-model confirmation rule: its corpus, its scoring and
the checked-in report that the runtime allowlist reads."""
from __future__ import annotations

from datetime import date

import httpx
import llm_client as lc
import opencode_go_free as og
import pytest
from elder_eval import free_confirm as fc
from elder_eval import free_confirm_corpus as corpus
from elder_eval.runner import diff_to_hunks
from unittest.mock import patch

CASES = corpus.build_cases()


# --- the corpus -------------------------------------------------------------------


def test_the_corpus_has_forty_or_more_cases_half_clean_half_defective() -> None:
    assert len(CASES) >= 40
    assert sum(c.defective for c in CASES) == sum(not c.defective for c in CASES)
    assert len({c.case_id for c in CASES}) == len(CASES)


def test_the_corpus_spans_the_languages_the_fleet_reviews() -> None:
    assert {c.language for c in CASES} >= {
        "python", "typescript", "shell", "sql", "cpp", "yaml", "go",
    }


def test_defect_kinds_are_varied() -> None:
    assert len({c.defect_kind for c in CASES}) >= 20


def test_every_defect_case_has_a_clean_twin_that_differs_from_it() -> None:
    by_id = {c.case_id: c for c in CASES}
    for case in CASES:
        if not case.defective:
            continue
        twin = by_id[case.case_id.replace("-defect", "-clean")]
        assert twin.path == case.path
        assert twin.source != case.source


def test_every_case_is_sized_where_the_relaxation_applies() -> None:
    """At least the size where a short empty answer is untrusted, and under
    the 10,000-token limit of the relaxation."""
    for case in CASES:
        chars = sum(len(h.body) for h in diff_to_hunks(corpus.diff_for(case)))
        assert chars >= lc._DEGENERATE_MIN_DIFF_CHARS, case.case_id
        assert chars <= 10_000 * 4, case.case_id


def test_the_source_file_is_a_hunk_of_its_own_and_the_padding_is_prose() -> None:
    for case in CASES:
        hunks = diff_to_hunks(corpus.diff_for(case))
        assert [h.path for h in hunks if h.path == case.path] == [case.path]
        assert all(h.path.endswith(".md") for h in hunks if h.path != case.path)


def test_the_corpus_is_ascii_and_free_of_private_detail() -> None:
    for case in CASES:
        text = corpus.diff_for(case)
        assert text.isascii(), case.case_id
        for banned in ("\u2014", "ts.net", "192.168.", "/Users/", "C:\\Users"):
            assert banned not in text, (case.case_id, banned)


def test_the_corpus_digest_is_stable() -> None:
    assert corpus.corpus_sha() == corpus.corpus_sha()
    assert len(corpus.corpus_sha()) == 16


# --- scoring ----------------------------------------------------------------------

DEFECT = next(c for c in CASES if c.defective)
CLEAN = next(c for c in CASES if not c.defective)


def test_an_answer_with_no_findings_is_empty_or_clean() -> None:
    assert fc.label_answer(DEFECT, []) == fc.EMPTY
    assert fc.label_answer(CLEAN, []) == fc.CLEAN


def test_a_finding_on_the_defective_file_is_a_catch() -> None:
    assert fc.label_answer(DEFECT, [DEFECT.path]) == fc.CAUGHT
    assert fc.label_answer(DEFECT, ["docs/other.md"]) == fc.OFF_TARGET
    assert fc.label_answer(CLEAN, ["anything"]) == fc.FINDING


def test_an_error_is_never_an_empty_answer() -> None:
    assert fc.label_answer(DEFECT, None) == fc.ERROR


def _results(defect_labels: list[str], clean_labels: list[str]) -> list[fc.CaseResult]:
    rows = [fc.CaseResult(f"d{i}", True, lab) for i, lab in enumerate(defect_labels)]
    return rows + [fc.CaseResult(f"c{i}", False, lab) for i, lab in enumerate(clean_labels)]


def test_rates_leave_errors_out_of_both_sides() -> None:
    stats = fc.summarize(_results(
        [fc.EMPTY, fc.CAUGHT, fc.CAUGHT, fc.ERROR, fc.OFF_TARGET],
        [fc.FINDING, fc.CLEAN, fc.ERROR, fc.CLEAN],
    ))
    assert stats["defect_scored"] == 4 and stats["defect_total"] == 5
    assert stats["false_empty"] == 1 and stats["false_empty_rate"] == 0.25
    assert stats["caught"] == 2 and stats["off_target"] == 1
    assert stats["clean_scored"] == 3 and stats["false_finding"] == 1
    assert stats["errors"] == 2


def test_a_model_with_no_scored_cases_has_no_rate_and_does_not_qualify() -> None:
    stats = fc.summarize(_results([fc.ERROR] * 24, [fc.ERROR] * 24))
    assert stats["false_empty_rate"] is None
    assert not og.model_qualifies(stats)


def test_the_report_names_the_date_corpus_and_models() -> None:
    per_model = {"m-free": _results([fc.CAUGHT] * 24, [fc.CLEAN] * 24)}
    report = fc.build_report(per_model, when=date(2026, 11, 14))
    assert report["date"] == "2026-11-14"
    assert report["corpus"]["sha"] == corpus.corpus_sha()
    assert report["corpus"]["cases"] == len(CASES)
    assert report["allowlist"] == ["m-free"]
    assert og.qualified_models(report, today=date(2026, 11, 14)) == {"m-free"}


# --- one live-shaped call, with the network faked ----------------------------------


@pytest.fixture
def _go(monkeypatch):
    monkeypatch.setattr(lc, "_load_opencode_go_key", lambda: "test-key")
    monkeypatch.setattr(lc, "_spark_data_class_header", lambda: "public")
    monkeypatch.setattr(lc, "_opencode_go_free_catalog", None, raising=False)
    monkeypatch.setattr(lc, "_opencode_go_free_gate", None, raising=False)
    monkeypatch.setattr(lc, "_bench_state", {}, raising=False)
    monkeypatch.setenv("GRUG_OPENCODE_GO_FREE_DISCOVERY", "1")
    monkeypatch.setenv("GRUG_OPENCODE_GO_API_KEY_SSM", "/test/opencode-go")
    listing = {"object": "list", "data": [{
        "id": "m-free", "type": "chat",
        "cost": {"text": {"input": {"price": 0}, "output": {"price": 0}}},
        "api": {"chat-completions": {"base_url": "https://example.invalid/v1"}},
    }]}
    monkeypatch.setattr(
        lc, "_opencode_go_free_models_fetch", lambda: httpx.Response(200, json=listing),
        raising=False,
    )


def _reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 9_000, "completion_tokens": 40},
    })


def test_a_live_call_scores_a_catch_an_empty_answer_and_an_error(_go) -> None:
    finding = (
        '{"findings": [{"path": "%s", "line": 1, "rule": "r", "severity": "medium", '
        '"message": "m"}]}' % DEFECT.path
    )
    no_sleep = lambda _s: None  # noqa: E731
    with patch.object(httpx, "post", return_value=_reply(finding)):
        assert fc.ask_model("m-free", DEFECT, sleep=no_sleep).label == fc.CAUGHT
    with patch.object(httpx, "post", return_value=_reply('{"findings": []}')):
        assert fc.ask_model("m-free", DEFECT, sleep=no_sleep).label == fc.EMPTY
        assert fc.ask_model("m-free", CLEAN, sleep=no_sleep).label == fc.CLEAN
    with patch.object(httpx, "post", side_effect=httpx.ConnectError("down")) as post:
        assert fc.ask_model("m-free", DEFECT, sleep=no_sleep).label == fc.ERROR
    assert post.call_count == fc.ATTEMPTS


def test_a_permanent_refusal_is_not_retried(_go) -> None:
    refused = httpx.Response(400, json={"error": {"message": "response_format is not enabled"}})
    with patch.object(httpx, "post", return_value=refused) as post:
        result = fc.ask_model("m-free", DEFECT, sleep=lambda _s: None)
    assert result.label == fc.ERROR
    assert post.call_count == 1


# --- the checked-in report ---------------------------------------------------------


def test_the_checked_in_report_belongs_to_this_corpus_and_its_allowlist_is_derived() -> None:
    report = og.load_eval_report()
    assert report is not None, "services/_shared/opencode_go_free_eval.json is missing"
    assert report["corpus"]["sha"] == corpus.corpus_sha(), "the corpus changed: re-run the eval"
    assert report["corpus"]["defect"] >= og.MIN_DEFECT_CASES
    date.fromisoformat(report["date"])
    derived = sorted(m for m, s in report["models"].items() if og.model_qualifies(s))
    assert report["allowlist"] == derived
    for model, stats in report["models"].items():
        assert stats["defect_scored"] + sum(
            1 for c, lab in stats["cases"].items() if c.endswith("-defect") and lab == fc.ERROR
        ) == report["corpus"]["defect"], model
