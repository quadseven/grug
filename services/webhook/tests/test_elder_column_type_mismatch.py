"""Tests for the integer-column/datetime eval fixture (#1493).

No LLM, no network. Checks that the fixture is the reviewed head, that the
counterexample really raises the live TypeError, that Elder's context now
shows every read of the touched column, and that the miss is recorded in
Elder's scoreboard as a missed blocking defect.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
import pytest
from elder_eval import column_type_mismatch as ctm
from sast_benchmark.backends import BenchBackend

_BACKEND = BenchBackend(name="fake", url="http://bench.invalid", model="m", api_key="")


@dataclass(frozen=True)
class _F:
    path: str
    line: int
    message: str


def test_fixture_names_the_reviewed_miss():
    fixture = ctm.FIXTURE
    assert fixture.name == "column_datetime_type_mismatch"
    assert fixture.path == ctm.PATH
    assert fixture.finding_class == "type-mismatch"
    assert ctm.REPO == "example/bridge"
    assert ctm.REVIEWED_HEAD == "0badc0d"


def test_hunks_match_the_head_snapshot():
    """Every context and added line of the fixture hunk sits at its new-side
    line number in the snapshot - the diff and the snapshot are one head."""
    snapshot = ctm.head_content().splitlines()
    for hunk in ctm.hunks():
        header, *lines = hunk.body.splitlines()
        n = int(header.split("+")[1].split(",")[0])
        for line in lines:
            if line.startswith("-"):
                continue
            assert snapshot[n - 1] == line[1:], (n, line)
            n += 1


def test_fixture_data_is_ascii():
    for path in ctm.FIXTURE_DIR.iterdir():
        assert path.read_text().isascii(), path.name


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, _sql, _params):
        return _Result(self.rows)


def _head_module():
    namespace = {}
    exec(compile(ctm.head_content(), "<head>", "exec"), namespace)  # noqa: S102
    return namespace


def test_counterexample_raises_typeerror_on_the_reviewed_head():
    """A Unix-integer created_at compared with a datetime is the live crash."""
    module = _head_module()
    rows = [{"created_at": 1_700_000_000}]
    since = datetime(2026, 10, 28, tzinfo=timezone.utc)
    with pytest.raises(TypeError):
        module["list_recent"](_Conn(rows), "author", since)


def test_existing_readers_treat_the_column_as_an_integer():
    module = _head_module()
    row = {"created_at": "1700000000"}
    assert module["age_seconds"](row) == 1_700_000_000
    assert module["created_at"](row) == datetime.fromtimestamp(1_700_000_000)  # noqa: DTZ006


def test_review_context_shows_every_read_of_the_touched_column():
    """The SQL read, both int() readers and the new datetime comparison are
    in the prompt together, in the reviewer AND judge views."""
    for text in (
        ctm.evidence().reviewer_view().text,
        ctm.evidence().judge_view().text,
    ):
        assert "SQL COLUMN READS" in text
        assert "columns: created_at" in text
        assert (
            '8:         "SELECT id, author, created_at FROM posts WHERE author = ?"'
            in text
        )
        assert '14:     return int(row["created_at"])' in text
        assert '25:     return datetime.fromtimestamp(int(row["created_at"]))' in text
        assert (
            '20:     recent = [row for row in rows if row["created_at"] > cutoff]'
            in text
        )


def test_catch_needs_the_defect_lines_and_a_signal_term():
    hit = _F(
        ctm.PATH,
        20,
        "created_at is an integer column; comparing it with a datetime raises TypeError",
    )
    assert ctm.caught_finding([hit])
    assert not ctm.caught_finding([_F(ctm.PATH, 20, "rename this variable")])
    assert not ctm.caught_finding([_F(ctm.PATH, 8, hit.message)])
    assert not ctm.caught_finding([_F("other.py", 20, hit.message)])


def test_scoreboard_records_the_miss_as_blocking():
    baseline = json.loads(ctm.BASELINE_PATH.read_text())
    measurement = baseline["measurements"][ctm.MEASUREMENT]
    findings = [_F(f["path"], f["line"], f["message"]) for f in measurement["findings"]]
    result = ctm.score_findings(findings)
    assert result == measurement["caught"]
    assert result == {ctm.FIXTURE.name: False}
    assert measurement["recall"] == ctm.recall_for(findings) == 0.0
    assert measurement["blocking"] is True
    assert ctm.FIXTURE.name in measurement["missed_blocking_defects"]


def _response(findings: list[dict]) -> httpx.Response:
    content = json.dumps({"findings": findings})
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_replay_sends_the_column_reads_and_scores_a_catch():
    sent: list[list[dict[str, str]]] = []

    def post(_backend, messages):
        sent.append(messages)
        return _response(
            [
                {
                    "path": ctm.PATH,
                    "line": 20,
                    "rule": "correctness",
                    "severity": "high",
                    "message": (
                        "created_at is a Unix integer; the new comparison uses a "
                        "datetime and raises TypeError"
                    ),
                }
            ]
        )

    run = ctm.replay(_BACKEND, post=post)
    assert not run.errored
    prompt = "\n".join(m["content"] for m in sent[0])
    assert "SQL COLUMN READS" in prompt
    assert '14:     return int(row["created_at"])' in prompt
    assert ctm.caught_finding(run.findings)
    assert ctm.recall_for(run.findings) == 1.0


def test_replay_transport_failure_is_errored_not_a_zero():
    def post(_backend, _messages):
        raise httpx.ConnectError("down")

    assert ctm.replay(_BACKEND, post=post).errored
