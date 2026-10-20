"""Canonical review session and finding lifecycle (Hunt Board data model).

These pin the properties the Hunt Board relies on:
  - one session per PR, carrying commit sha, trigger, status, timestamps
    and persona/model provenance for every run;
  - a finding keeps ONE identity across pushes while its code stands,
    even when the line number shifts;
  - a rerun never deletes history: a finding that is not raised again
    becomes addressed, outdated or superseded, with the reason recorded;
  - human decisions (dismissed, resolved) are never overridden by a rerun;
  - every write is idempotent, so a retried webhook changes nothing;
  - a failed persona leaves its findings alone (partial failure);
  - concurrent personas never lose each other's work.
"""
from __future__ import annotations

import threading

import pytest

import review_session as rs

REPO = "acme/widgets"
PR = 7


def _finding(
    *,
    path: str = "app/handler.py",
    line: int = 10,
    rule: str = "unchecked-none",
    evidence: str = "user = lookup(user_id)",
    severity: str = "high",
    persona: str = "elder",
    message: str = "lookup can return None",
    comment: int | None = None,
    confidence: float | None = 0.8,
) -> rs.ObservedFinding:
    return rs.ObservedFinding(
        persona=persona,
        path=path,
        line=line,
        rule=rule,
        category="correctness",
        severity=severity,
        confidence=confidence,
        message=message,
        evidence=evidence,
        inline_comment_id=comment,
    )


def _run(
    run_id: str,
    head: str,
    *,
    persona: str = "elder",
    trigger: str = "synchronize",
    at: str = "2026-10-19T10:00:00Z",
) -> rs.ReviewRun:
    return rs.ReviewRun(
        run_id=run_id,
        persona=persona,
        head_sha=head,
        trigger=trigger,
        started_at=at,
        backend="openrouter",
        model="example-model-1",
    )


def _reviewed(session, run, findings, *, anchors=None, check_run_id=None,
              at="2026-10-19T10:05:00Z"):
    session = rs.start_run(session, run)
    return rs.complete_run(
        session, run.run_id, findings=findings, anchors=anchors or {},
        at=at, check_run_id=check_run_id,
    )


def _by_rule(session, rule):
    [rec] = [f for f in session.findings if f.rule == rule]
    return rec


# -- session provenance -------------------------------------------------------


def test_a_review_run_creates_one_session_with_provenance():
    s = rs.new_session(REPO, PR, at="2026-10-19T09:59:00Z")
    s = rs.start_run(s, _run("r1", "aaa111", trigger="opened"))
    assert s.status == "running"
    assert s.head_sha == "aaa111" and s.trigger == "opened"

    s = rs.complete_run(s, "r1", findings=[_finding()], anchors={},
                        at="2026-10-19T10:05:00Z", check_run_id=501)
    assert s.status == "complete"
    assert s.completed_at == "2026-10-19T10:05:00Z"
    [run] = s.runs
    assert (run.persona, run.backend, run.model) == (
        "elder", "openrouter", "example-model-1")
    assert run.status == "complete" and run.check_run_id == 501
    assert run.started_at == "2026-10-19T10:00:00Z"
    assert run.completed_at == "2026-10-19T10:05:00Z"


def test_a_new_commit_updates_the_same_session_not_a_new_one():
    s = rs.new_session(REPO, PR, at="t0")
    s = _reviewed(s, _run("r1", "aaa111", trigger="opened"), [_finding()])
    s = rs.start_run(s, _run("r2", "bbb222", at="2026-10-19T11:00:00Z"))
    assert (s.repo, s.pr_number) == (REPO, PR)
    assert s.head_sha == "bbb222" and s.trigger == "synchronize"
    assert s.status == "running"
    assert [r.run_id for r in s.runs] == ["r1", "r2"]


def test_a_stale_run_finishing_late_does_not_rewind_the_head():
    """Webhooks arrive out of order: the review of an older head can finish
    after the newer head's review started. The session head stays on the
    newest commit, and the stale run's findings do not reopen anything."""
    s = rs.new_session(REPO, PR, at="t0")
    s = rs.start_run(s, _run("old", "aaa111", at="2026-10-19T10:00:00Z"))
    s = rs.start_run(s, _run("new", "bbb222", at="2026-10-19T10:01:00Z"))
    s = rs.complete_run(s, "old", findings=[_finding()], anchors={}, at="x")
    assert s.head_sha == "bbb222"
    assert s.findings == ()
    assert s.status == "running"


# -- finding identity ---------------------------------------------------------


def test_finding_has_stable_identity_and_full_evidence():
    s = _reviewed(rs.new_session(REPO, PR, at="t0"), _run("r1", "aaa111"),
                  [_finding(comment=9001)], check_run_id=501)
    [rec] = s.findings
    assert rec.finding_id == _finding().finding_id
    assert len(rec.finding_id) == 16
    assert (rec.path, rec.line, rec.evidence) == (
        "app/handler.py", 10, "user = lookup(user_id)")
    assert (rec.severity, rec.category, rec.confidence) == (
        "high", "correctness", 0.8)
    assert rec.state == "open"
    assert rec.first_seen_sha == rec.last_seen_sha == "aaa111"
    assert rec.inline_comment_ids == (9001,)
    assert rec.check_run_ids == (501,)


def test_identity_survives_a_line_shift_and_whitespace():
    """A line moving down because code was added above it is the same
    finding; it must not be closed and reopened under a new identity."""
    a = _finding(line=10, evidence="user = lookup(user_id)")
    b = _finding(line=42, evidence="    user  =  lookup(user_id)   ")
    assert a.finding_id == b.finding_id


def test_identity_differs_by_rule_path_and_code():
    base = _finding()
    assert base.finding_id != _finding(rule="other-rule").finding_id
    assert base.finding_id != _finding(path="app/other.py").finding_id
    assert base.finding_id != _finding(evidence="user = fetch(user_id)").finding_id


def test_unchanged_finding_keeps_identity_across_reruns():
    s = _reviewed(rs.new_session(REPO, PR, at="t0"), _run("r1", "aaa111"),
                  [_finding(line=10)])
    first = s.findings[0]
    s = _reviewed(s, _run("r2", "bbb222"), [_finding(line=14)],
                  anchors={first.finding_id: "unchanged"})
    [rec] = s.findings
    assert rec.finding_id == first.finding_id
    assert rec.state == "open"
    assert rec.line == 14
    assert (rec.first_seen_sha, rec.last_seen_sha) == ("aaa111", "bbb222")


# -- reruns never delete history ---------------------------------------------


def test_new_commit_that_changes_the_code_marks_the_finding_addressed():
    s = _reviewed(rs.new_session(REPO, PR, at="t0"), _run("r1", "aaa111"),
                  [_finding()])
    fid = s.findings[0].finding_id
    s = _reviewed(s, _run("r2", "bbb222"), [], anchors={fid: "changed"})
    [rec] = s.findings
    assert rec.state == "addressed"
    assert rec.state_sha == "bbb222"
    assert [h.state for h in rec.history] == ["open", "addressed"]


def test_force_push_that_drops_the_anchor_marks_the_finding_outdated():
    """A force-push can remove the anchored code from the PR entirely. That
    is not evidence of a fix, so it reads outdated, never addressed."""
    s = _reviewed(rs.new_session(REPO, PR, at="t0"), _run("r1", "aaa111"),
                  [_finding()])
    fid = s.findings[0].finding_id
    s = _reviewed(s, _run("r2", "ccc333", trigger="force-push"), [],
                  anchors={fid: "gone"})
    [rec] = s.findings
    assert rec.state == "outdated"
    assert rec.history[-1].reason == "anchored code no longer in the diff"


def test_a_moved_line_alone_never_resolves_a_finding():
    """Out of scope by the ticket: a finding is never closed solely because
    its line moved. Not raised again on untouched code -> still open."""
    s = _reviewed(rs.new_session(REPO, PR, at="t0"), _run("r1", "aaa111"),
                  [_finding()])
    fid = s.findings[0].finding_id
    s = _reviewed(s, _run("r2", "bbb222"), [], anchors={fid: "unchanged"})
    assert s.findings[0].state == "open"
    s = _reviewed(s, _run("r3", "ccc333"), [], anchors={})
    assert s.findings[0].state == "open"


def test_reworded_finding_on_rewritten_code_supersedes_the_old_one():
    s = _reviewed(rs.new_session(REPO, PR, at="t0"), _run("r1", "aaa111"),
                  [_finding(evidence="user = lookup(user_id)")])
    old = s.findings[0].finding_id
    new = _finding(evidence="user = lookup(user_id) or None", line=11)
    s = _reviewed(s, _run("r2", "bbb222"), [new], anchors={old: "changed"})
    assert len(s.findings) == 2, "history is kept, never deleted"
    by_id = {f.finding_id: f for f in s.findings}
    assert by_id[old].state == "superseded"
    assert by_id[old].superseded_by == new.finding_id
    assert by_id[new.finding_id].state == "open"


def test_raised_again_after_addressed_reopens_with_the_same_identity():
    s = _reviewed(rs.new_session(REPO, PR, at="t0"), _run("r1", "aaa111"),
                  [_finding()])
    fid = s.findings[0].finding_id
    s = _reviewed(s, _run("r2", "bbb222"), [], anchors={fid: "changed"})
    s = _reviewed(s, _run("r3", "ccc333"), [_finding()])
    [rec] = s.findings
    assert rec.finding_id == fid
    assert rec.state == "open"
    assert [h.state for h in rec.history] == ["open", "addressed", "open"]


# -- human decisions ----------------------------------------------------------


def test_dismissal_is_recorded_and_survives_a_rerun():
    s = _reviewed(rs.new_session(REPO, PR, at="t0"), _run("r1", "aaa111"),
                  [_finding()])
    fid = s.findings[0].finding_id
    s = rs.apply_event(s, fid, "dismissed", sha="aaa111", at="t2",
                       reason="maintainer thumbs-down")
    s = _reviewed(s, _run("r2", "bbb222"), [_finding()])
    [rec] = s.findings
    assert rec.state == "dismissed"
    assert rec.last_seen_sha == "bbb222"


def test_resolution_then_reopen_round_trips():
    s = _reviewed(rs.new_session(REPO, PR, at="t0"), _run("r1", "aaa111"),
                  [_finding()])
    fid = s.findings[0].finding_id
    s = rs.apply_event(s, fid, "resolved", sha="aaa111", at="t2")
    assert s.findings[0].state == "resolved"
    s = rs.apply_event(s, fid, "reopened", sha="aaa111", at="t3")
    assert s.findings[0].state == "open"
    assert [h.state for h in s.findings[0].history] == [
        "open", "resolved", "open"]


def test_a_repeated_event_is_a_no_op():
    s = _reviewed(rs.new_session(REPO, PR, at="t0"), _run("r1", "aaa111"),
                  [_finding()])
    fid = s.findings[0].finding_id
    once = rs.apply_event(s, fid, "resolved", sha="aaa111", at="t2")
    twice = rs.apply_event(once, fid, "resolved", sha="aaa111", at="t9")
    assert twice == once


def test_unknown_finding_or_event_is_rejected():
    s = rs.new_session(REPO, PR, at="t0")
    with pytest.raises(KeyError):
        rs.apply_event(s, "0" * 16, "resolved", sha="x", at="t")
    s = _reviewed(s, _run("r1", "aaa111"), [_finding()])
    with pytest.raises(ValueError):
        rs.apply_event(s, s.findings[0].finding_id, "deleted", sha="x", at="t")


# -- links back to records ----------------------------------------------------


def test_inline_comments_and_check_runs_link_back_to_the_record():
    s = _reviewed(rs.new_session(REPO, PR, at="t0"), _run("r1", "aaa111"),
                  [_finding(comment=11)], check_run_id=501)
    fid = s.findings[0].finding_id
    s = _reviewed(s, _run("r2", "bbb222"), [_finding(comment=11)],
                  check_run_id=502)
    s = rs.link_inline_comment(s, fid, 12)
    s = rs.link_inline_comment(s, fid, 12)
    rec = s.findings[0]
    assert rec.inline_comment_ids == (11, 12)
    assert rec.check_run_ids == (501, 502)
    assert rs.finding_for_comment(s, 12) == rec
    assert rs.finding_for_comment(s, 999) is None
    assert rs.findings_for_check_run(s, 502) == (rec,)


# -- idempotency and failure --------------------------------------------------


def test_a_retried_completion_changes_nothing():
    """A webhook retry delivers the same completion twice. The second apply
    must be a byte-for-byte no-op, or the projection would churn."""
    s = rs.start_run(rs.new_session(REPO, PR, at="t0"), _run("r1", "aaa111"))
    once = rs.complete_run(s, "r1", findings=[_finding()], anchors={},
                           at="t1", check_run_id=501)
    twice = rs.complete_run(once, "r1", findings=[_finding(), _finding(rule="x")],
                            anchors={}, at="t9", check_run_id=777)
    assert twice == once
    assert rs.start_run(once, _run("r1", "aaa111")) == once


def test_partial_failure_keeps_the_failed_personas_findings():
    s = rs.new_session(REPO, PR, at="t0")
    s = _reviewed(s, _run("e1", "aaa111"), [_finding()])
    s = _reviewed(s, _run("g1", "aaa111", persona="guard"),
                  [_finding(persona="guard", rule="secret")])
    s = rs.start_run(s, _run("e2", "bbb222"))
    s = rs.start_run(s, _run("g2", "bbb222", persona="guard"))
    s = rs.complete_run(s, "e2", findings=[], anchors={}, at="t5")
    s = rs.fail_run(s, "g2", at="t6", error="llm timeout")
    assert s.status == "partial"
    guard = _by_rule(s, "secret")
    assert guard.state == "open", "a failed run proves nothing about a finding"
    assert [r.status for r in s.runs if r.persona == "guard"] == [
        "complete", "failed"]
    assert s.runs[-1].error == "llm timeout"


def test_all_runs_failed_is_failed_and_completion_after_failure_is_ignored():
    s = rs.start_run(rs.new_session(REPO, PR, at="t0"), _run("r1", "aaa111"))
    s = rs.fail_run(s, "r1", at="t1", error="boom")
    assert s.status == "failed"
    assert rs.complete_run(s, "r1", findings=[_finding()], anchors={}, at="t2") == s


def test_completing_an_unknown_run_raises():
    with pytest.raises(KeyError):
        rs.complete_run(rs.new_session(REPO, PR, at="t0"), "nope",
                        findings=[], anchors={}, at="t")


def test_reconcile_only_touches_the_completing_personas_findings():
    s = rs.new_session(REPO, PR, at="t0")
    s = _reviewed(s, _run("g1", "aaa111", persona="guard"),
                  [_finding(persona="guard", rule="secret")])
    gid = s.findings[0].finding_id
    s = _reviewed(s, _run("e1", "bbb222"), [], anchors={gid: "changed"})
    assert _by_rule(s, "secret").state == "open"


# -- persistence round trip ---------------------------------------------------


def test_to_dict_from_dict_round_trips():
    s = _reviewed(rs.new_session(REPO, PR, at="t0"), _run("r1", "aaa111"),
                  [_finding(comment=3, confidence=None)], check_run_id=8)
    s = rs.apply_event(s, s.findings[0].finding_id, "dismissed", sha="aaa111",
                       at="t2", reason="noise")
    s = rs.set_walkthrough(s, "Adds a handler.", related=("Refs #12",))
    assert rs.from_dict(rs.to_dict(s)) == s


def test_from_dict_ignores_fields_a_newer_writer_added():
    """A row written by a newer grug (rolling deploy) must still load."""
    s = _reviewed(rs.new_session(REPO, PR, at="t0"), _run("r1", "aaa111"),
                  [_finding()])
    data = rs.to_dict(s)
    data["runs"][0]["future_field"] = 1
    data["findings"][0]["history"][0]["future_field"] = 1
    assert rs.from_dict(data) == s


def test_an_older_run_starting_late_does_not_rewind_updated_at():
    s = rs.start_run(rs.new_session(REPO, PR, at="2026-10-19T09:00:00Z"),
                     _run("new", "bbb222", at="2026-10-19T10:05:00Z"))
    s = rs.start_run(s, _run("old", "aaa111", at="2026-10-19T10:00:00Z"))
    assert s.updated_at == "2026-10-19T10:05:00Z"
    assert s.head_sha == "bbb222"


# -- concurrency (compare-and-swap update) ------------------------------------


class _MemStore:
    """An in-memory CAS store with the same contract as the Postgres one:
    `save` succeeds only when the stored version equals `expected`."""

    def __init__(self):
        self.data: dict | None = None
        self.lock = threading.Lock()
        self.conflicts = 0

    def load(self):
        with self.lock:
            return None if self.data is None else dict(self.data)

    def save(self, data: dict, expected: int) -> bool:
        with self.lock:
            current = 0 if self.data is None else int(self.data["version"])
            if current != expected:
                self.conflicts += 1
                return False
            self.data = data
            return True


def test_concurrent_personas_completing_never_lose_each_other():
    store = _MemStore()
    barrier = threading.Barrier(4)
    personas = ("elder", "guard", "teller", "smasher")

    def worker(persona: str) -> None:
        run = _run(f"{persona}-1", "aaa111", persona=persona)
        finding = _finding(persona=persona, rule=f"{persona}-rule")
        barrier.wait()

        def mutate(s):
            s = rs.start_run(s, run)
            return rs.complete_run(s, run.run_id, findings=[finding],
                                   anchors={}, at="t1")

        rs.update_session(store.load, store.save, REPO, PR, mutate, at="t0")

    threads = [threading.Thread(target=worker, args=(p,)) for p in personas]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = rs.from_dict(store.data)
    assert sorted(r.persona for r in final.runs) == sorted(personas)
    assert sorted(f.rule for f in final.findings) == sorted(
        f"{p}-rule" for p in personas)
    assert final.status == "complete"
    assert final.version == 4


def test_update_session_retries_on_conflict_then_gives_up():
    calls = {"n": 0}

    def save(_data, _expected):
        calls["n"] += 1
        return False

    with pytest.raises(rs.SessionConflict):
        rs.update_session(lambda: None, save, REPO, PR,
                          lambda s: rs.set_walkthrough(s, "changed"),
                          at="t0", max_attempts=3)
    assert calls["n"] == 3


def test_update_session_skips_the_write_when_nothing_changed():
    """A retried webhook whose mutation is a no-op must not bump the version
    (that would make every retry look like a change to the projection)."""
    store = _MemStore()
    run = _run("r1", "aaa111")

    def mutate(s):
        s = rs.start_run(s, run)
        return rs.complete_run(s, "r1", findings=[_finding()], anchors={}, at="t1")

    first = rs.update_session(store.load, store.save, REPO, PR, mutate, at="t0")
    again = rs.update_session(store.load, store.save, REPO, PR, mutate, at="t0")
    assert first.version == again.version == 1
    assert store.data["version"] == 1
