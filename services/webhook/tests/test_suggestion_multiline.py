"""Multi-line committable suggestions (#1759, part of #1755).

A suggestion that rewrites several lines anchors the review comment with
`start_line` and `line` on the right side. Every line of the span must be shown
by ONE diff hunk with no deleted line inside it; anything else stays prose.
"""
from __future__ import annotations

import ast
import json

import pytest

from personas.code_reviewer import dedup
from personas.code_reviewer.markings import build_review_result
from personas.code_reviewer.persona import CodeReviewEvaluation, Finding
from personas.code_reviewer.suggestion import (
    checked_span_suggestion,
    diff_right_side,
)
from github_reviews_client import InlineComment, ReviewResult, SpanComment

PATH = "app/pick.py"

# 12 lines. Lines 3-5 are a guard plus its early return, 8-12 a split call.
FILE = (
    "import os\n"                              # 1
    "\n"                                       # 2
    "def pick(options, wanted):\n"             # 3
    "    if not options:\n"                    # 4
    "        return None\n"                    # 5
    "    return best(options)\n"               # 6
    "\n"                                       # 7
    "def build(a, b, c):\n"                    # 8
    "    result = combine(\n"                  # 9
    "        a,\n"                             # 10
    "        b,\n"                             # 11
    "        c,\n"                             # 12
    "    )\n"                                  # 13
    "    return result\n"                      # 14
)


def _diff(*hunks: str, path: str = PATH) -> str:
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        + "\n".join(hunks) + "\n"
    )


# One hunk covering lines 3-14, every line added.
ALL_ADDED = _diff(
    "@@ -0,0 +3,12 @@\n"
    "+def pick(options, wanted):\n"
    "+    if not options:\n"
    "+        return None\n"
    "+    return best(options)\n"
    "+\n"
    "+def build(a, b, c):\n"
    "+    result = combine(\n"
    "+        a,\n"
    "+        b,\n"
    "+        c,\n"
    "+    )\n"
    "+    return result"
)


def _finding(line: int, suggestion: str | None, message: str = "m") -> Finding:
    return Finding(
        file=PATH, line=line, severity="high", rule_name="null-deref",
        message=message, suggestion=suggestion,
    )


def _span(finding: Finding, diff: str = ALL_ADDED, text: str = FILE):
    return checked_span_suggestion(
        finding, text, diff_right_side(diff).get(PATH, ()),
    )


def _comment(finding: Finding, diff: str = ALL_ADDED, text: str = FILE):
    review, _ = build_review_result(
        CodeReviewEvaluation(
            findings=(finding,), conclusion="failure", degraded_reason=None,
        ),
        head_sha="a" * 40, event="COMMENT",
        file_contents={PATH: text}, diff_text=diff,
    )
    assert review is not None
    return review.comments[0]


def test_two_line_rewrite_in_one_hunk_anchors_the_span():
    f = _finding(4, "    if options is None:\n        return []")
    span = _span(f)
    assert (span.start_line, span.end_line) == (4, 5)

    c = _comment(f)
    assert isinstance(c, SpanComment)
    assert (c.start_line, c.line) == (4, 5)
    assert (c.start_side, c.side) == ("RIGHT", "RIGHT")
    assert "```suggestion\n    if options is None:\n        return []\n```" in c.body
    assert "lines 4-5" in c.body


def test_five_line_rewrite_in_one_hunk_anchors_the_span():
    new = (
        "    result = combine(\n"
        "        a,\n"
        "        b,\n"
        "        c,\n"
        "        strict=True,\n"
        "    )"
    )
    f = _finding(10, new)  # a line inside the split call
    span = _span(f)
    assert (span.start_line, span.end_line) == (9, 13)

    c = _comment(f)
    assert (c.start_line, c.line) == (9, 13)
    assert f"```suggestion\n{new}\n```" in c.body
    # the five replaced lines applied to the file still parse
    lines = FILE.splitlines()
    ast.parse("\n".join(lines[:8] + new.split("\n") + lines[13:]))


def test_span_leaving_the_hunk_falls_back_to_prose():
    # The hunk shows lines 3-5 only; the guard fits, the split call does not.
    diff = _diff(
        "@@ -3,3 +3,3 @@\n"
        " def pick(options, wanted):\n"
        "+    if not options:\n"
        "+        return None"
    )
    assert _span(_finding(4, "    if x:\n        return 1"), diff) is not None
    f = _finding(10, "    result = combine(a, b, c)\n    log(result)")
    assert _span(f, diff) is None
    c = _comment(f, diff)
    assert not isinstance(c, SpanComment)
    assert "```suggestion" not in c.body
    assert "log(result)" in c.body  # still shown, as fenced prose


def test_span_across_two_hunks_falls_back_to_prose():
    # Lines 4-5 sit in different hunks: line 5 is shown by the second only.
    diff = _diff(
        "@@ -3,2 +3,2 @@\n"
        " def pick(options, wanted):\n"
        "+    if not options:",
        "@@ -20,2 +5,2 @@\n"
        "+        return None\n"
        " "
    )
    assert _span(_finding(4, "    if x:\n        return 1"), diff) is None


def test_span_crossing_a_deleted_line_falls_back_to_prose():
    diff = _diff(
        "@@ -3,4 +3,3 @@\n"
        " def pick(options, wanted):\n"
        "     if not options:\n"
        "-        old()\n"
        "         return None\n"
        "     return best(options)"
    )
    # 4 and 5 are adjacent on the new side but a removed line sits between.
    assert _span(_finding(4, "    if x:\n        return 1"), diff) is None
    # A span clear of the deletion still anchors.
    ok = _finding(6, "    value = best(options)\n    return value")
    assert _span(ok, diff) is not None


def test_span_on_unchanged_context_lines_still_anchors():
    # Only line 8 is added; the rest of the call is plain context GitHub shows.
    diff = _diff(
        "@@ -9,6 +9,6 @@\n"
        " def build(a, b, c):\n"
        "+    result = combine(\n"
        "         a,\n"
        "         b,\n"
        "         c,\n"
        "     )\n"
        "     return result"
    )
    f = _finding(11, "    result = combine(a, b, c)\n    log(result)")
    span = _span(f, diff)
    assert (span.start_line, span.end_line) == (9, 13)


def test_span_touching_lines_the_diff_does_not_show_falls_back():
    assert _span(_finding(4, "    if x:\n        return 1"), "") is None
    other = _diff("@@ -1,1 +1,1 @@\n+import os", path="app/other.py")
    assert _span(_finding(4, "    if x:\n        return 1"), other) is None


def test_suggestion_reproduces_the_indentation_of_the_replaced_lines():
    # The model dropped the indentation; every line gets the original's.
    f = _finding(4, "if options is None:\n    return []")
    span = _span(f)
    assert span.text == "    if options is None:\n        return []"
    # An already-indented suggestion is kept verbatim, not indented twice.
    g = _finding(4, "    if options is None:\n        return []")
    assert _span(g).text == span.text
    assert f"```suggestion\n{span.text}\n```" in _comment(f).body


def test_rewrite_that_breaks_parsing_falls_back():
    f = _finding(4, "    if options is None:\nreturn []")
    assert _span(f) is None
    assert "```suggestion" not in _comment(f).body


def test_replacement_longer_than_the_limit_falls_back():
    seven = "\n".join(f"    x{i} = {i}" for i in range(7))
    assert _span(_finding(4, seven)) is None
    assert _span(_finding(4, "\n".join(seven.split("\n")[:6]))) is not None


def test_span_longer_than_the_limit_falls_back():
    long_file = "def f():\n" + "    x = [\n" + "        1,\n" * 6 + "    ]\n"
    diff = _diff(
        "@@ -0,0 +1,9 @@\n" + "\n".join(
            "+" + line for line in long_file.splitlines()
        ),
    )
    f = _finding(3, "    x = [\n        1,\n    ]")
    assert _span(f, diff, long_file) is None


def test_unchanged_replacement_and_non_python_fall_back():
    same = _finding(4, "    if not options:\n        return None")
    assert _span(same) is None
    txt = Finding(
        file="notes.md", line=4, severity="low", rule_name="r",
        message="m", suggestion="a\nb",
    )
    assert checked_span_suggestion(txt, FILE, ()) is None


def test_fence_and_mention_stay_inert_inside_the_block():
    bad = _finding(4, "    if x:\n        return 1\n```\nboom")
    assert _span(bad) is None
    f = _finding(4, "    if x:\n        return 1  # @someone")
    body = _comment(f).body
    assert "```suggestion\n    if x:\n        return 1  # @someone\n```" in body
    # Every mention sits inside a fence, which renders no notification.
    at = body.find("@someone")
    while at != -1:
        assert body[:at].count("```") % 2 == 1
        at = body.find("@someone", at + 1)


def test_single_line_suggestion_is_unchanged_by_the_span_path():
    f = _finding(6, "    return best(options, default)")
    c = _comment(f)
    assert not isinstance(c, SpanComment) and c.line == 6
    assert "```suggestion\n    return best(options, default)\n```" in c.body


def test_no_diff_keeps_every_multi_line_fix_as_prose():
    c = _comment(_finding(4, "    if x:\n        return 1"), diff="")
    assert not isinstance(c, SpanComment)
    assert "```suggestion" not in c.body


def test_wire_payload_carries_start_keys_only_for_a_span(monkeypatch):
    import github_reviews_client as client

    sent: dict = {}

    class _Resp:
        def raise_for_status(self) -> None: ...
        def json(self) -> dict: return {}

    def fake_post(url, json=None, **_kw):
        sent.update(json=json)
        return _Resp()

    monkeypatch.setattr(client.httpx, "post", fake_post)
    result = ReviewResult(
        commit_id="a" * 40, event="COMMENT", body="b",
        comments=(
            SpanComment(path=PATH, line=5, body="x", start_line=4),
            InlineComment(path=PATH, line=6, body="y"),
        ),
    )
    client.post_review("t", "o", "r", pull_number=1, result=result)
    span, plain = sent["json"]["comments"]
    assert span == {
        "path": PATH, "line": 5, "body": "x", "side": "RIGHT",
        "start_line": 4, "start_side": "RIGHT",
    }
    # byte-identical to the one-line shape before multi-line support
    assert plain == {"path": PATH, "line": 6, "body": "y", "side": "RIGHT"}
    json.dumps(sent["json"])


def test_pending_publication_round_trips_both_comment_shapes():
    from dataclasses import asdict
    rows = [asdict(c) for c in (
        SpanComment(path=PATH, line=5, body="x", start_line=4),
        InlineComment(path=PATH, line=6, body="y"),
    )]
    from personas.code_reviewer import publish_retry as pr
    rebuilt = tuple(
        (SpanComment if "start_line" in c else InlineComment)(**c) for c in rows
    )
    assert isinstance(rebuilt[0], SpanComment) and type(rebuilt[1]) is InlineComment
    assert pr.SpanComment is SpanComment


@pytest.mark.parametrize(("start", "line", "side"), [
    (5, 5, "RIGHT"),
    (0, 5, "RIGHT"),
    (3, 5, "LEFT"),
])
def test_span_comment_rejects_a_malformed_span(start, line, side):
    with pytest.raises(ValueError):
        SpanComment(path=PATH, line=line, body="x", side=side, start_line=start)


def test_dedup_treats_every_line_of_a_span_as_commented():
    body = "x\n<!-- grug-rule:null-deref -->"
    keys = dedup.prior_keys_from_comments([
        {"path": PATH, "line": 5, "start_line": 4, "side": "RIGHT", "body": body},
    ])
    assert dedup.finding_key(PATH, 4, "null-deref") in keys
    assert dedup.finding_key(PATH, 5, "null-deref") in keys


def test_three_line_replacement_of_a_three_line_span_parses_applied():
    text = "def f(x):\n    y = g(\n        x,\n    )\n    return y\n"
    diff = _diff(
        "@@ -0,0 +1,5 @@\n+def f(x):\n+    y = g(\n+        x,\n+    )\n+    return y"
    )
    new = "    y = g(\n        x,\n        strict=True)"
    f = _finding(3, new)
    span = _span(f, diff, text)
    assert (span.start_line, span.end_line) == (2, 4)
    lines = text.splitlines()
    ast.parse("\n".join(lines[:1] + new.split("\n") + lines[4:]))
