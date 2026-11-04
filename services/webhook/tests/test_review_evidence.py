"""One assembled evidence object for the Elder reviewer and judge (#1189).

Reviewer/judge evidence parity used to be a convention kept by two prompt
renderers and two assembly paths. These tests pin it as a construction:
both prompts derive from one `ElderEvidence`, the size budgets have one
derivation, and redaction keeps ADR-0009's split (a SaaS-bound judge call
is redacted, the in-cluster Cave call gets the raw evidence).
"""

from __future__ import annotations

import inspect
import json

import httpx

import llm_client as lc
import review_evidence as ev
from llm_client import Hunk
from personas.code_reviewer import judge

_SECRET = "AKIAIOSFODNN7EXAMPLE"
_FINDINGS = [{"rule_name": "exposed-secret", "file": "src/a.py", "line": 1,
              "severity": "low", "message": "key committed"}]


def _evidence(**overrides) -> ev.ElderEvidence:
    kwargs = {
        "pr_context": {
            "title": "Change API contract",
            "body": "All callers must migrate",
            "base_sha": "base",
            "head_sha": "head",
            "linked_issue_context": "### LINKED ISSUE\n- [ ] migrate callers",
        },
        "file_contents": {"src/a.py": "new_api()\n", "src/b.py": "other()\n"},
        "cross_file_contents": {"src/c.py": "18: old_api()"},
        "runtime_context": "src/a.py: 12 errors in 24h",
        "ci_context": "tests: failure",
        "repo_docs_context": "ADR-0001 says so",
    }
    kwargs.update(overrides)
    return ev.assemble_evidence(
        [
            Hunk(path="src/a.py", body="@@ -1 +1 @@\n+new_api()"),
            Hunk(path="src/b.py", body="@@ -1 +1 @@\n+other()"),
        ],
        **kwargs,
    )


def test_assemble_normalizes_absent_signals():
    evidence = ev.assemble_evidence([Hunk(path="x.py", body="@@ -1 +1 @@\n+x")])
    assert evidence.hunks == (Hunk(path="x.py", body="@@ -1 +1 @@\n+x"),)
    assert evidence.file_contents == {}
    assert evidence.cross_file_contents == {}
    assert evidence.pr_context is None


def test_assemble_bounds_cross_file_snippets_once():
    huge = "\n".join("x" for _ in range(ev.MAX_FILE_CONTEXT_LINES + 1))
    evidence = _evidence(
        cross_file_contents={"big.py": huge, "empty.py": "", "ok.py": "1: ok()"},
    )
    assert evidence.cross_file_contents == {"ok.py": "1: ok()"}


def test_reviewer_and_judge_views_share_the_evidence_blocks():
    evidence = _evidence()
    reviewer = evidence.reviewer_view()
    judge_view = evidence.judge_view()

    assert reviewer.has_intent and judge_view.has_intent
    shared = [
        block for block in reviewer.blocks
        if "cross-file context" not in block
        and not block.startswith(("### CI STATUS", "### REPO DOCS"))
    ]
    # Same blocks, byte for byte; only the order differs (the judge reads
    # the linked-issue criteria before the diff).
    assert sorted(shared) == sorted(
        block for block in judge_view.blocks if "cross-file context" not in block
    )
    assert any("src/c.py (UNCHANGED" in block for block in reviewer.blocks)
    assert any("src/c.py (UNCHANGED" in block for block in judge_view.blocks)


def test_reviewer_only_signals_stay_out_of_the_judge_view():
    evidence = _evidence()
    reviewer = evidence.reviewer_view(review_map="### REVIEW MAP\nx")
    judge_text = evidence.judge_view().text
    assert "### CI STATUS\ntests: failure" in reviewer.text
    assert "### REPO DOCS\nADR-0001 says so" in reviewer.text
    assert "### REVIEW MAP" in reviewer.text
    assert "CI STATUS" not in judge_text
    assert "REPO DOCS" not in judge_text


def test_file_budget_applies_to_both_views():
    big = "\n".join(f"x{i}" for i in range(ev.MAX_FILE_CONTEXT_LINES + 1))
    evidence = _evidence(file_contents={"src/a.py": big})
    assert "FULL FILE" not in evidence.reviewer_view().text
    assert "FULL FILE" not in evidence.judge_view().text


_COLUMN_FILE = '''\
def list_posts(conn):
    return conn.execute("SELECT id, created_at FROM posts").fetchall()


def age_seconds(row):
    return int(row["created_at"])


def recent(conn, cutoff):
    rows = list_posts(conn)
    return [row for row in rows if row["created_at"] > cutoff]
'''
_COLUMN_HUNK = (
    "@@ -10,2 +10,2 @@\n"
    "     rows = list_posts(conn)\n"
    '-    return rows\n'
    '+    return [row for row in rows if row["created_at"] > cutoff]'
)


def _column_evidence(content: str = _COLUMN_FILE) -> ev.ElderEvidence:
    return ev.assemble_evidence(
        [Hunk(path="bridge/posts.py", body=_COLUMN_HUNK)],
        file_contents={"bridge/posts.py": content},
    )


def test_column_reads_show_every_read_of_a_touched_column():
    """The integer column is read as a SQL column and converted with int()
    elsewhere; both readers must sit in the prompt beside the new datetime
    comparison, in the reviewer AND judge views."""
    evidence = _column_evidence()
    for text in (evidence.reviewer_view().text, evidence.judge_view().text):
        assert "SQL COLUMN READS" in text
        assert "columns: created_at" in text
        assert '2:     return conn.execute("SELECT id, created_at FROM posts")' in text
        assert '6:     return int(row["created_at"])' in text
        assert '11:     return [row for row in rows if row["created_at"] > cutoff]' in text


def test_column_reads_are_size_independent():
    """Above the full-file budget the old reader would leave the prompt;
    the column block still carries it."""
    filler = [f"# filler {i}" for i in range(ev.MAX_FILE_CONTEXT_LINES + 2)]
    filler[1] = 'SQL = "SELECT id, created_at FROM posts"'
    filler[2] = 'def age_seconds(row): return int(row["created_at"])'
    changed = len(filler)
    filler.append('def recent(row, cutoff): return row["created_at"] > cutoff')
    content = "\n".join(filler) + "\n"
    evidence = ev.assemble_evidence(
        [Hunk(path="bridge/posts.py",
              body=f"@@ -{changed},0 +{changed + 1} @@\n+{filler[-1]}")],
        file_contents={"bridge/posts.py": content},
    )
    text = evidence.reviewer_view().text
    assert "FULL FILE" not in text
    assert "SQL COLUMN READS" in text
    assert '2: SQL = "SELECT id, created_at FROM posts"' in text
    assert '3: def age_seconds(row): return int(row["created_at"])' in text


def test_column_reads_stay_out_when_the_touched_column_has_one_reader():
    content = (
        'def recent(conn):\n'
        '    return [row for row in list_posts(conn) if row["created_at"] > 0]\n'
    )
    hunk = (
        "@@ -1,2 +1,2 @@\n"
        " def recent(conn):\n"
        '-    return list_posts(conn)\n'
        '+    return [row for row in list_posts(conn) if row["created_at"] > 0]'
    )
    evidence = ev.assemble_evidence(
        [Hunk(path="bridge/posts.py", body=hunk)],
        file_contents={"bridge/posts.py": content},
    )
    assert "SQL COLUMN READS" not in evidence.reviewer_view().text


def test_scoped_to_keeps_only_the_named_paths():
    scoped = _evidence().scoped_to({"src/a.py"})
    assert [h.path for h in scoped.hunks] == ["src/a.py"]
    assert scoped.file_contents == {"src/a.py": "new_api()\n"}
    assert scoped.cross_file_contents == {"src/c.py": "18: old_api()"}
    assert scoped.runtime_context == "src/a.py: 12 errors in 24h"
    assert scoped.pr_context == _evidence().pr_context


def test_seal_redacts_only_when_asked():
    text = f"key = {_SECRET}"
    assert _SECRET not in ev.seal(text, redact=True)
    assert ev.seal(text, redact=False) == text


def _capture_judge_prompt(monkeypatch) -> list[list[dict]]:
    sent: list[list[dict]] = []

    def fake_call(config, messages, **_kw):
        sent.append(messages)
        verdicts = {"verdicts": [{"index": 0, "is_real_bug": True,
                                  "confidence": 0.9, "reasoning": "ok"}]}
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(verdicts)}}]},
        )

    monkeypatch.setattr(lc, "_call_backend", fake_call)
    return sent


def test_saas_judge_path_redacts_the_evidence(monkeypatch):
    """ADR-0009: a SaaS judge never sees the raw secret."""
    monkeypatch.setenv("GRUG_REVIEW_BACKEND_PRIORITY", "cloud")
    monkeypatch.setenv("GRUG_CAVE_GATEWAY_URL", "http://cave.test")
    sent = _capture_judge_prompt(monkeypatch)
    evidence = _evidence(file_contents={"src/a.py": f"KEY = '{_SECRET}'\n"})

    verdicts = judge._judge_evidence_packet(_FINDINGS, evidence, 1)

    assert len(verdicts) == 1
    assert len(sent) == 1
    assert _SECRET not in sent[0][1]["content"]
    assert "[REDACTED:aws-access-key]" in sent[0][1]["content"]


def test_cave_judge_path_keeps_the_raw_evidence(monkeypatch):
    """ADR-0009: the in-cluster Cave judge needs the raw value to tell a
    live key from a docs example, and it never leaves the cluster."""
    monkeypatch.delenv("GRUG_REVIEW_BACKEND_PRIORITY", raising=False)
    monkeypatch.setenv("GRUG_CAVE_GATEWAY_URL", "http://cave.test")
    sent = _capture_judge_prompt(monkeypatch)
    evidence = _evidence(file_contents={"src/a.py": f"KEY = '{_SECRET}'\n"})

    verdicts = judge._judge_evidence_packet(_FINDINGS, evidence, 1)

    assert len(verdicts) == 1
    assert len(sent) == 1
    assert _SECRET in sent[0][1]["content"]


def test_judge_does_not_reassemble_evidence():
    """Grep-clean: the judge persona takes the assembled evidence and never
    rebuilds hunks, whole files, or cross-file snippets on its own."""
    source = inspect.getsource(judge)
    for needle in ("Hunk(", "file_path", "file_contents", "cross_file_contents",
                   "runtime_context"):
        assert needle not in source, needle


def test_llm_client_has_no_second_evidence_renderer():
    """Both prompts render evidence through `review_evidence` only."""
    source = inspect.getsource(lc)
    # Needles are the rendered block headers, not prose: a comment that
    # mentions "cross-file context" in passing is not a renderer.
    for needle in ("FULL FILE (", "cross-file context)", "MAX_FILE_CONTEXT",
                   "PULL REQUEST INTENT\\n", "ENCLOSING SCOPE ("):
        assert needle not in source, needle


_SECRET_FINDING = [{"rule_name": "exposed-secret", "file": "src/a.py", "line": 1,
                    "severity": "low", "message": f"key {_SECRET} committed"}]


def test_saas_judge_path_redacts_a_secret_quoted_in_a_finding(monkeypatch):
    """The finding lines are sealed with the evidence: a finding can quote
    the secret it flags, and a SaaS judge must not receive it."""
    monkeypatch.setenv("GRUG_REVIEW_BACKEND_PRIORITY", "cloud")
    monkeypatch.setenv("GRUG_CAVE_GATEWAY_URL", "http://cave.test")
    sent = _capture_judge_prompt(monkeypatch)

    judge._judge_evidence_packet(_SECRET_FINDING, _evidence(), 1)

    assert len(sent) == 1
    assert _SECRET not in sent[0][1]["content"]


def test_cave_refute_path_keeps_the_raw_evidence(monkeypatch):
    """Refute mode follows the same ADR-0009 split as grading: the Cave
    call gets the raw value."""
    monkeypatch.delenv("GRUG_REVIEW_BACKEND_PRIORITY", raising=False)
    monkeypatch.setenv("GRUG_CAVE_GATEWAY_URL", "http://cave.test")
    sent = _capture_judge_prompt(monkeypatch)
    evidence = _evidence(file_contents={"src/a.py": f"KEY = '{_SECRET}'\n"})

    judge._judge_evidence_packet(_FINDINGS, evidence, 1, refute=True)

    assert len(sent) == 1
    assert _SECRET in sent[0][1]["content"]
