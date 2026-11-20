"""The model's answer to the rendered suggestion block (#1759).

`checked_span_suggestion` takes the span from the Python statement that holds
the finding's `line`, so the wire format needs no start-line field: a model
that sends a multi-line `suggestion` is the only thing the gate waits for.
These tests feed the JSON a model would send through the real parser and the
real review builder, so a step between the model and the gate that dropped or
flattened a multi-line suggestion would fail here.
"""
from __future__ import annotations

import json

import llm_client as lc
from github_reviews_client import InlineComment, SpanComment
from personas.code_reviewer.markings import build_review_result
from personas.code_reviewer.persona import CodeReviewEvaluation, Finding

PATH = "app/pick.py"
FILE = (
    "def pick(options):\n"             # 1
    "    if not options:\n"           # 2
    "        return None\n"           # 3
    "    return best(options)\n"      # 4
)
DIFF = (
    f"diff --git a/{PATH} b/{PATH}\n--- a/{PATH}\n+++ b/{PATH}\n"
    "@@ -0,0 +1,4 @@\n"
    "+def pick(options):\n"
    "+    if not options:\n"
    "+        return None\n"
    "+    return best(options)\n"
)
TWO_LINES = "if not options:\n    raise ValueError('options')"


def _comment(line: int, **extra: object):
    """Model JSON -> parser -> persona Finding -> built review comment."""
    answer = json.dumps({"findings": [{
        "path": PATH, "line": line, "rule": "null-deref",
        "severity": "high", "message": "m", **extra,
    }]})
    (raw,) = lc._parse_findings_content(answer, "m").findings
    finding = Finding(
        file=raw.path, line=raw.line, severity=raw.severity,
        rule_name=raw.rule, message=raw.message, suggestion=raw.suggestion,
    )
    review, _ = build_review_result(
        CodeReviewEvaluation(
            findings=(finding,), conclusion="failure", degraded_reason=None,
        ),
        head_sha="a" * 40, event="COMMENT",
        file_contents={PATH: FILE}, diff_text=DIFF,
    )
    assert review is not None
    return review.comments[0]


def test_model_multi_line_suggestion_reaches_the_gate_as_a_span():
    comment = _comment(2, suggestion=TWO_LINES)
    assert isinstance(comment, SpanComment)
    assert (comment.start_line, comment.line) == (2, 3)
    assert "```suggestion\n    if not options:\n        raise ValueError('options')\n```" in comment.body


def test_model_finding_without_a_suggestion_stays_a_plain_comment():
    comment = _comment(2)
    assert type(comment) is InlineComment
    assert comment.line == 2
    assert "```suggestion" not in comment.body


def test_model_single_line_suggestion_is_unchanged():
    comment = _comment(4, suggestion="return best(options) or None")
    assert type(comment) is InlineComment
    assert "```suggestion\n    return best(options) or None\n```" in comment.body


def test_span_the_gate_refuses_falls_back_to_a_plain_finding():
    broken = "if not options:\n    return (None"
    comment = _comment(2, suggestion=broken)
    assert type(comment) is InlineComment
    assert comment.line == 2
    assert "```suggestion" not in comment.body
