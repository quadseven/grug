"""Hunt Board wiring: persona runs flow into the one canonical session and
out to the one GitHub projection.

Integration tests over an in-memory compare-and-swap store and a fake board.
They pin the issue's scenarios end to end through the recorder, the Elder
adapter and the projection:

  - a new commit updates the same session and keeps unchanged identities;
  - a force-push that drops a file leaves the finding outdated, not addressed;
  - a dismissal (a thumbs-down on the inline comment) survives a rerun;
  - a failed or degraded run leaves findings alone and reads as partial;
  - two personas finishing at once both land;
  - a retried webhook neither adds a run nor republishes the board;
  - inline comments and the check-run link back to the same record.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest

import review_session as rs
from personas import hunt_board, hunt_recorder
from personas.code_reviewer import hunt_link
from personas.code_reviewer.persona import Finding
from personas.review_publish import PARTIAL_COVERAGE

INSTALL = 4242
OWNER, NAME = "acme", "widgets"
REPO = f"{OWNER}/{NAME}"
PR = 7
PATH = "app/handler.py"
LINE_A = "user = lookup(user_id)"
LINE_B = "total = sum(rows)"


class _Store:
    """In-memory stand-in for the HUNT# row: versioned compare-and-swap."""

    def __init__(self) -> None:
        self.row: dict[str, Any] | None = None
        self.writes = 0
        self._lock = threading.Lock()

    def get(self, **_kw):
        with self._lock:
            return None if self.row is None else dict(self.row)

    def put(self, *, data, expected_version, **_kw):
        with self._lock:
            current = 0 if self.row is None else int(self.row["version"])
            if current != expected_version:
                return False
            self.row = dict(data)
            self.writes += 1
            return True


class _Board:
    """Records what would be PATCHed onto the PR's board comment."""

    def __init__(self) -> None:
        self.published: list[str] = []

    def publish(self, _token, _owner, _repo, _pr, session, **_kw):
        section = hunt_board.render(session)
        if self.published and self.published[-1] == section:
            return {"board": "unchanged"}
        self.published.append(section)
        return {"board": "updated"}


@pytest.fixture
def env(monkeypatch):
    store, board = _Store(), _Board()
    monkeypatch.setattr("adapters.install_store.get_review_session", store.get)
    monkeypatch.setattr("adapters.install_store.put_review_session", store.put)
    monkeypatch.setattr(hunt_board, "publish", board.publish)
    monkeypatch.setattr(
        hunt_recorder, "with_install_token_retry",
        lambda _install, fn: fn("token"),
    )
    return SimpleNamespace(store=store, board=board)


def _finding(rule="unchecked-none", line=10, file=PATH, sev="high"):
    return Finding(
        file=file, line=line, severity=sev, rule_name=rule,
        message=f"{rule} problem", suggestion=None,
    )


def _contents(*lines: str, path: str = PATH) -> dict[str, str]:
    return {path: "\n".join(("import os",) * 9 + lines) + "\n"}


def _elder(env, head, findings, *, contents, at, trigger="review",
           comment_ids=None, check_run_id=501, delta=False, degraded_reason="",
           diff_paths=(PATH,)):
    hunt_link.record_elder_run(
        hunt_recorder.PrRef(INSTALL, OWNER, NAME, PR),
        head_sha=head, trigger=trigger, started_at=at,
        backend="openrouter", model="example-model-1",
        findings=findings, file_contents=contents,
        diff_paths=frozenset(diff_paths), delta=delta,
        comment_ids=comment_ids or {}, check_run_id=check_run_id,
        category_of=lambda rule: "correctness", degraded_reason=degraded_reason,
    )
    return rs.from_dict(env.store.row)


def test_a_review_creates_the_session_and_publishes_the_board(env):
    s = _elder(env, "aaaaaaa1", [_finding()], contents=_contents(LINE_A),
               at="2026-10-19T10:00:00Z", comment_ids={(PATH, "unchecked-none", 10): 91})
    assert (s.repo, s.pr_number, s.head_sha, s.trigger) == (REPO, PR, "aaaaaaa1", "review")
    run = s.runs[0]
    assert (run.backend, run.model, run.status) == ("openrouter", "example-model-1", "complete")
    assert run.started_at == "2026-10-19T10:00:00Z" and run.completed_at
    [f] = s.findings
    assert f.evidence == LINE_A and f.category == "correctness" and f.state == "open"
    assert len(env.board.published) == 1
    assert "Open findings (1)" in env.board.published[0]


def test_inline_comment_and_check_run_link_back_to_the_record(env):
    s = _elder(env, "aaaaaaa1", [_finding()], contents=_contents(LINE_A),
               at="2026-10-19T10:00:00Z", comment_ids={(PATH, "unchecked-none", 10): 91})
    [f] = s.findings
    assert rs.finding_for_comment(s, 91) == f
    assert rs.findings_for_check_run(s, 501) == (f,)
    assert "#discussion_r91" in env.board.published[-1]
    assert "/runs/501" in env.board.published[-1]


def test_a_new_commit_keeps_identity_when_the_line_moves(env):
    first = _elder(env, "aaaaaaa1", [_finding(line=10)], contents=_contents(LINE_A),
                   at="2026-10-19T10:00:00Z")
    moved = _elder(env, "bbbbbbb2", [_finding(line=11)],
                   contents=_contents("# header", LINE_A), at="2026-10-19T11:00:00Z",
                   check_run_id=502)
    assert [f.finding_id for f in moved.findings] == [f.finding_id for f in first.findings]
    [f] = moved.findings
    assert f.state == "open" and f.first_seen_sha == "aaaaaaa1" and f.last_seen_sha == "bbbbbbb2"
    assert f.line == 11 and f.check_run_ids == (501, 502)
    assert len(moved.runs) == 2 and moved.head_sha == "bbbbbbb2"


def test_changed_code_not_raised_again_is_addressed_and_kept(env):
    _elder(env, "aaaaaaa1", [_finding()], contents=_contents(LINE_A),
           at="2026-10-19T10:00:00Z")
    s = _elder(env, "bbbbbbb2", [], contents=_contents("user = lookup(user_id) or Anon()"),
               at="2026-10-19T11:00:00Z")
    [f] = s.findings
    assert f.state == "addressed" and f.state_sha == "bbbbbbb2"
    assert [h.state for h in f.history] == ["open", "addressed"]
    assert "Closed findings (1)" in env.board.published[-1]


def test_force_push_that_drops_the_file_leaves_the_finding_outdated(env):
    _elder(env, "aaaaaaa1", [_finding()], contents=_contents(LINE_A),
           at="2026-10-19T10:00:00Z")
    s = _elder(env, "ccccccc3", [], contents={}, diff_paths=("other.py",),
               at="2026-10-19T12:00:00Z")
    [f] = s.findings
    assert f.state == "outdated", "code that left the diff is no evidence of a fix"


def test_a_delta_review_never_closes_what_it_did_not_read(env):
    _elder(env, "aaaaaaa1", [_finding()], contents=_contents(LINE_A),
           at="2026-10-19T10:00:00Z")
    s = _elder(env, "bbbbbbb2", [], contents={}, diff_paths=("docs/readme.md",),
               delta=True, at="2026-10-19T11:00:00Z")
    assert [f.state for f in s.findings] == ["open"]


def test_dismissal_survives_a_rerun_and_reaches_the_board(env):
    s = _elder(env, "aaaaaaa1", [_finding()], contents=_contents(LINE_A),
               at="2026-10-19T10:00:00Z", comment_ids={(PATH, "unchecked-none", 10): 91})
    ref = hunt_recorder.PrRef(INSTALL, OWNER, NAME, PR)
    hunt_recorder.record_comment_event(ref, 91, "dismissed", sha="aaaaaaa1")
    s = _elder(env, "bbbbbbb2", [_finding()], contents=_contents(LINE_A),
               at="2026-10-19T11:00:00Z", trigger="rerun")
    [f] = s.findings
    assert f.state == "dismissed"
    assert "dismissed" in env.board.published[-1]


def test_resolution_then_reopen_round_trips(env):
    _elder(env, "aaaaaaa1", [_finding()], contents=_contents(LINE_A),
           at="2026-10-19T10:00:00Z", comment_ids={(PATH, "unchecked-none", 10): 91})
    ref = hunt_recorder.PrRef(INSTALL, OWNER, NAME, PR)
    hunt_recorder.record_comment_event(ref, 91, "resolved", sha="aaaaaaa1")
    assert rs.from_dict(env.store.row).findings[0].state == "resolved"
    hunt_recorder.record_comment_event(ref, 91, "reopened", sha="aaaaaaa1")
    assert rs.from_dict(env.store.row).findings[0].state == "open"


def test_an_event_for_an_unknown_comment_changes_nothing(env):
    _elder(env, "aaaaaaa1", [_finding()], contents=_contents(LINE_A),
           at="2026-10-19T10:00:00Z")
    writes = env.store.writes
    hunt_recorder.record_comment_event(
        hunt_recorder.PrRef(INSTALL, OWNER, NAME, PR), 12345, "dismissed", sha="a")
    assert env.store.writes == writes


def test_a_failed_run_is_partial_and_keeps_the_findings(env):
    hunt_recorder.record_walkthrough(
        hunt_recorder.PrRef(INSTALL, OWNER, NAME, PR),
        head_sha="aaaaaaa1", trigger="review", started_at="2026-10-19T10:00:00Z",
        backend="", model="", text="Adds a handler.", related=("Refs #12",),
    )
    _elder(env, "aaaaaaa1", [_finding()], contents=_contents(LINE_A),
           at="2026-10-19T10:00:01Z")
    s = _elder(env, "aaaaaaa1", [], contents={}, degraded_reason="all_backends_failed",
               at="2026-10-19T10:05:00Z", trigger="rerun")
    assert s.status == "partial"
    assert [f.state for f in s.findings] == ["open"]
    assert "partial" in env.board.published[-1]
    assert "Adds a handler." in env.board.published[-1]


def test_a_partial_coverage_review_adds_findings_but_closes_none(env):
    _elder(env, "aaaaaaa1", [_finding()], contents=_contents(LINE_A),
           at="2026-10-19T10:00:00Z")
    s = _elder(env, "bbbbbbb2", [_finding("other-rule", line=11)],
               contents=_contents(LINE_B, "x = 1"), at="2026-10-19T11:00:00Z",
               degraded_reason=PARTIAL_COVERAGE)
    states = {f.rule: f.state for f in s.findings}
    assert states == {"unchecked-none": "open", "other-rule": "open"}
    assert s.status == "complete"


def test_a_retried_webhook_adds_no_run_and_publishes_nothing(env):
    first = _elder(env, "aaaaaaa1", [_finding()], contents=_contents(LINE_A),
                   at="2026-10-19T10:00:00Z")
    writes, posts = env.store.writes, len(env.board.published)
    again = _elder(env, "aaaaaaa1", [_finding()], at="2026-10-19T10:00:09Z",
                   contents=_contents(LINE_A))
    assert again == first
    assert env.store.writes == writes
    assert len(env.board.published) == posts


def test_concurrent_personas_both_land_and_the_board_converges(env):
    ref = hunt_recorder.PrRef(INSTALL, OWNER, NAME, PR)
    barrier = threading.Barrier(2)

    def teller():
        barrier.wait()
        hunt_recorder.record_walkthrough(
            ref, head_sha="aaaaaaa1", trigger="review",
            started_at="2026-10-19T10:00:00Z", backend="openrouter",
            model="example-model-2", text="Adds a handler.", related=(),
        )

    def elder():
        barrier.wait()
        _elder(env, "aaaaaaa1", [_finding()], contents=_contents(LINE_A),
               at="2026-10-19T10:00:00Z")

    threads = [threading.Thread(target=teller), threading.Thread(target=elder)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    s = rs.from_dict(env.store.row)
    assert {r.persona for r in s.runs} == {"teller", "elder"}
    assert s.walkthrough == "Adds a handler." and len(s.findings) == 1
    assert s.status == "complete"


def test_a_store_outage_never_raises_into_the_review(env, monkeypatch, caplog):
    def boom(**_kw):
        raise RuntimeError("store down")

    monkeypatch.setattr("adapters.install_store.get_review_session", boom)
    with caplog.at_level("WARNING"):
        out = hunt_link.record_elder_run(
            hunt_recorder.PrRef(INSTALL, OWNER, NAME, PR),
            head_sha="aaaaaaa1", trigger="review", started_at="2026-10-19T10:00:00Z",
            backend="", model="", findings=[_finding()],
            file_contents=_contents(LINE_A), diff_paths=frozenset({PATH}),
            delta=False, comment_ids={}, check_run_id=None,
            category_of=lambda rule: "correctness",
        )
    assert out is None
    assert any(r.message == "hunt_session_update_failed" for r in caplog.records)
