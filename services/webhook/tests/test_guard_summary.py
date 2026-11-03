"""Guard's check-run summary table is built from finding text that carries
judge prose and the raw PR line: a pipe, newline or fence in it must not
break the table or swallow the rest of the summary (Elder's own table goes
through the same escaping)."""
from __future__ import annotations

import re

from personas.code_reviewer.persona import CodeReviewEvaluation, Finding
from personas.guard import dispatch as guard_dispatch

_CELL_PIPE = re.compile(r"(?<!\\)\|")


def test_summary_table_escapes_pipes_newlines_fences_and_mentions():
    finding = Finding(
        file="deploy/docker-compose.yml", line=3, severity="high",
        rule_name="iac.compose-shell", suggestion=None,
        message='command: sh -c "a | b"\n```\n@someone look',
    )
    evaluation = CodeReviewEvaluation(findings=(finding,), conclusion="failure")

    _title, body = guard_dispatch._summary_markdown(evaluation)

    rows = body.splitlines()
    assert len(rows) == 3, body
    assert len(_CELL_PIPE.findall(rows[2])) == 6, rows[2]
    assert "```" not in body
    assert "@someone" not in body
    assert "a \\| b" in rows[2]
