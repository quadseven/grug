"""grug#1397: Elder does not publish a finding its own text withdraws.

The four withdrawn texts below are the shape seen on one real 4-file review:
each ended in "No finding." and each still became a reply-required thread.
"""
from __future__ import annotations

import logging
import re

import pytest

import llm_client as lc
from llm_client import Backend, Finding as LlmFinding, LlmReviewResponse
from personas.code_reviewer import dispatch as cr_dispatch
from personas.code_reviewer.diff_parser import parse_diff
from personas.code_reviewer.persona import evaluate_diff


_DIFF = """diff --git a/src/x.py b/src/x.py
--- a/src/x.py
+++ b/src/x.py
@@ -1,2 +1,7 @@
 context
-old
+new1
+new2
+new3
+new4
+new5
+new6
"""

_WITHDRAWN_TEXTS = (
    "The cache grows per key but keys are capped. No unbounded growth. No finding.",
    "Each pass rewrites the same row. The row stays bounded. No finding.",
    "Helper looks unused. It is used by the test. Not dead. No finding.",
    "The guard reads inverted at first, but both branches return early. "
    "Not a live defect. No finding.",
)


def _llm(line: int, message: str, *, severity: str = "high",
         rule: str = "logic-error", withdrawn: bool = False) -> LlmFinding:
    # `withdrawn` is passed only when set, so the text-only cases exercise
    # the same constructor call the code had before grug#1397.
    extra = {"withdrawn": True} if withdrawn else {}
    return LlmFinding(
        path="src/x.py", line=line, rule=rule, severity=severity,  # type: ignore[arg-type]
        message=message, **extra,
    )


def _evaluate(*findings: LlmFinding):
    return evaluate_diff(parse_diff(_DIFF), LlmReviewResponse(
        kind="reviewed", findings=findings, backend_used=Backend.POOLSIDE,
    ))


def test_withdrawn_texts_post_no_thread_while_real_finding_posts() -> None:
    real = _llm(6, "Null head deref when the list is empty; this crashes.")
    evaluation = _evaluate(
        *(_llm(i + 2, text) for i, text in enumerate(_WITHDRAWN_TEXTS)), real,
    )

    assert [f.message for f in evaluation.findings] == [real.message]
    assert evaluation.dropped_withdrawn == 4
    # The withdrawn "high" candidates never reach the verdict: only the real
    # finding blocks.
    assert evaluation.conclusion == "failure"

    result, posted = cr_dispatch.build_review_result(
        evaluation, head_sha="a" * 40, event="COMMENT",
    )
    assert result is not None
    assert len(result.comments) == 1
    assert [f.message for f in posted] == [real.message]
    assert not any("No finding" in c.body for c in result.comments)

    _, summary = cr_dispatch._summary_markdown(evaluation)
    assert "dropped 4 candidate(s) the reviewer itself withdrew" in summary


def test_withdrawn_high_candidates_alone_do_not_fail_the_verdict() -> None:
    evaluation = _evaluate(*(_llm(2, t) for t in _WITHDRAWN_TEXTS[:1]))
    assert evaluation.findings == ()
    assert evaluation.conclusion == "success"
    assert evaluation.dropped_withdrawn == 1
    result, posted = cr_dispatch.build_review_result(
        evaluation, head_sha="a" * 40, event="COMMENT",
    )
    assert (result, posted) == (None, ())


def test_structured_is_finding_false_is_dropped_whatever_the_text() -> None:
    evaluation = _evaluate(
        _llm(2, "Possible race on the shared counter.", withdrawn=True),
    )
    assert evaluation.findings == ()
    assert evaluation.dropped_withdrawn == 1


def test_coerce_reads_only_a_real_json_false_as_withdrawn() -> None:
    base = {"path": "a.py", "line": 1, "rule": "r", "severity": "low", "message": "m"}
    assert lc._coerce_finding({**base, "is_finding": False})[0].withdrawn is True
    assert lc._coerce_finding({**base, "is_finding": True})[0].withdrawn is False
    assert lc._coerce_finding(base)[0].withdrawn is False
    # A malformed value degrades to "still a finding", never the reverse.
    assert lc._coerce_finding({**base, "is_finding": "false"})[0].withdrawn is False


def test_cave_schema_allows_is_finding() -> None:
    items = lc._CAVE_FINDINGS_RESPONSE_FORMAT["json_schema"]["schema"][
        "properties"]["findings"]["items"]
    assert items["properties"]["is_finding"] == {"type": "boolean"}


def test_phrase_backstop_reads_only_the_final_sentence() -> None:
    # Mentioning the words earlier is not a withdrawal.
    live = _llm(2, "No finding of a caller guards this path, so it leaks the handle.")
    assert not lc.is_withdrawn_finding(live)
    assert not lc.is_withdrawn_finding(_llm(2, "This is not a defect in the parser alone."))
    for text in _WITHDRAWN_TEXTS:
        assert lc.is_withdrawn_finding(_llm(2, text))
    assert lc.is_withdrawn_finding(_llm(2, "Checked both arms. Not a defect."))


def test_merge_keeps_the_live_finding_when_one_model_withdrew_it() -> None:
    withdrawn = _llm(2, _WITHDRAWN_TEXTS[0], severity="critical")
    live = _llm(2, "Unbounded growth: keys come from user input.", severity="medium")
    for order in ((withdrawn, live), (live, withdrawn)):
        merged = lc._merge_review_findings([
            lc._SuccessfulReview(
                backend=Backend.POOLSIDE, model=f"m{i}", findings=(f,),
                review_span_context=None,
            )
            for i, f in enumerate(order)
        ])
        assert [(f.message, f.severity) for f in merged] == [
            (live.message, "medium"),
        ]
        assert not lc.is_withdrawn_finding(merged[0])


# grug#1529: the two critical markings that blocked a pull request.
_OMIT_TEXTS = (
    "Grug see nothing. No GITHUB_ENV writes here. Omit.",
    "The reason is a code the operator chose. Omit - not a secret.",
    "The value is a path. Omit \u2014 not a secret.",
)
_LIVE_OMIT_TEXTS = (
    "The token reaches the log. It must be omitted.",
    "The header carries the key. Omit this.",
    "The key is committed here. Omit - not a hardcoded secret.",
    "The token reaches the log line. Omit the token from the log.",
    "Omit. That is what the old comment says, but the key still reaches the log.",
)


def test_omit_text_is_only_recognized_in_its_exact_forms() -> None:
    for text in _OMIT_TEXTS:
        assert lc.concludes_omit(_llm(2, text)), text
    for text in _LIVE_OMIT_TEXTS:
        assert not lc.concludes_omit(_llm(2, text)), text


@pytest.mark.parametrize("severity", ["critical", "high", "medium", "low"])
def test_omit_text_annotates_and_moves_nothing_else(severity: str) -> None:
    evaluation = _evaluate(_llm(2, _OMIT_TEXTS[0], severity=severity, rule="some-rule"))
    assert [f.severity for f in evaluation.findings] == [severity]
    assert evaluation.findings[0].message.startswith(_OMIT_TEXTS[0])
    assert "says to omit it" in evaluation.findings[0].message
    assert evaluation.dropped_withdrawn == 0
    assert evaluation.omit_flagged_rules == ("some-rule",)


def test_a_real_finding_that_mentions_omit_is_not_annotated() -> None:
    evaluation = _evaluate(*(
        _llm(i + 2, text, severity="critical")
        for i, text in enumerate(_LIVE_OMIT_TEXTS)
    ))
    assert [f.message for f in evaluation.findings] == list(_LIVE_OMIT_TEXTS)
    assert evaluation.omit_flagged_rules == ()


def test_the_structured_verdict_removes_an_omit_marking() -> None:
    evaluation = _evaluate(_llm(2, _OMIT_TEXTS[0], severity="critical", withdrawn=True))
    assert evaluation.findings == ()
    assert evaluation.conclusion == "success"
    assert evaluation.omit_flagged_rules == ()


def test_omit_text_changes_no_merge_result_in_either_order() -> None:
    omit = _llm(2, _OMIT_TEXTS[0], severity="critical")
    live = _llm(2, "The key reaches the log.", severity="high")
    for order in ((omit, live), (live, omit)):
        merged = lc._merge_review_findings([
            lc._SuccessfulReview(
                backend=Backend.POOLSIDE, model=f"m{i}", findings=(f,),
                review_span_context=None,
            )
            for i, f in enumerate(order)
        ])
        assert [(f.severity, f.message) for f in merged] == [("critical", omit.message)]


def test_merge_of_two_withdrawn_candidates_stays_withdrawn() -> None:
    by_text = _llm(2, _WITHDRAWN_TEXTS[0], severity="medium")
    by_flag = _llm(2, "Fine as written, after a closer read.", severity="high", withdrawn=True)
    for order in ((by_text, by_flag), (by_flag, by_text)):
        merged = lc._merge_review_findings([
            lc._SuccessfulReview(
                backend=Backend.POOLSIDE, model=f"m{i}", findings=(f,),
                review_span_context=None,
            )
            for i, f in enumerate(order)
        ])
        assert lc.is_withdrawn_finding(merged[0])


def test_an_omit_marking_is_logged_with_its_rule_and_head(caplog) -> None:
    evaluation = _evaluate(_llm(2, _OMIT_TEXTS[0], rule="some-rule"))
    with caplog.at_level(logging.INFO):
        cr_dispatch._log_omit_markings(evaluation, pr="pr-7", head_sha="abcdef0123456789")
    lines = [r for r in caplog.records if r.getMessage() == "code_review_omit_marking_flagged"]
    assert [(r.rule, r.head_sha, r.pr) for r in lines] == [("some-rule", "abcdef01", "pr-7")]


def _rule_of(rule: str) -> str:
    finding, reason = lc._coerce_finding(
        {"path": "a.py", "line": 1, "rule": rule, "severity": "high", "message": "m"},
    )
    assert finding is not None and reason == ""
    return finding.rule


@pytest.mark.parametrize("rule", ["bad rule\nforged=1", "a/b", "", "xxx\x00"])
def test_a_rule_that_is_not_a_rule_name_becomes_a_safe_placeholder(rule: str) -> None:
    placeholder = _rule_of(rule)
    assert re.fullmatch(r"unknown-rule-[0-9a-f]{12}", placeholder)
    assert _rule_of(rule) == placeholder


def test_distinct_invalid_rules_stay_distinct_in_the_merge() -> None:
    assert _rule_of("a/b") != _rule_of("a/c")
    assert _rule_of("sql-injection_2") == "sql-injection_2"
    findings = tuple(
        _llm(2, "The key reaches the log.", rule=_rule_of(r)) for r in ("a/b", "a/c")
    )
    merged = lc._merge_review_findings([
        lc._SuccessfulReview(
            backend=Backend.POOLSIDE, model="m", findings=findings,
            review_span_context=None,
        ),
    ])
    assert len(merged) == 2
