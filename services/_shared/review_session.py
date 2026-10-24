"""Canonical review session and finding lifecycle (the Hunt Board model).

THE PROBLEM. A PR's review state is spread over a walkthrough comment, the
board, inline threads with living-review chips, check-runs, reaction
records and ledger rows. Each surface knows a piece; none of them can say
"what is open, what was addressed, what went stale, and is the review
done" for the PR as a whole. Reruns make it worse: a re-review re-derives
everything from scratch, so a finding's history lives only in whichever
comment happened to be edited last.

THE MODEL. One `ReviewSession` per PR is the single authoritative record:

  - `runs`: every persona run (`ReviewRun`) with its commit sha, trigger,
    status, timestamps and model provenance (backend + model), plus the
    check-run it published. A rerun appends; it never rewrites history.
  - `findings`: one `FindingRecord` per distinct finding, keyed by a
    stable `finding_id`. Its lifecycle state is explicit (`FindingState`)
    and every transition is appended to `history` with the sha, time and
    reason, so nothing is ever silently deleted.

Every GitHub surface is a PROJECTION of this record: the Hunt Board section
(`personas/hunt_board.py`), and the inline comments and check-runs, which
link back to records through `inline_comment_ids` / `check_run_ids`.

STABLE IDENTITY. `finding_id` hashes (persona, path, rule, the anchored
code with whitespace collapsed). The line number is NOT part of it: code
added above a finding shifts its line, and that must not close one finding
and open another. Two findings of one rule on byte-identical lines in one
file share an identity; that is accepted (they are one fix). When a finding
carries no evidence text the line is used instead, so it still has a key.

RECONCILING A RERUN (`complete_run`). For the completing persona only:

  - raised again (same id): same record, `last_seen_sha` moves on, and an
    addressed / outdated / superseded record reopens. A human decision
    (dismissed, resolved) is never overridden by a model;
  - not raised again, and a NEW finding of the same rule on the same path
    appeared: `superseded`, with `superseded_by` pointing at it;
  - not raised again, and the caller reports the anchored lines changed:
    `addressed` in that sha (the living-review meaning);
  - not raised again, and the anchored code left the diff (a force-push
    dropped it, the file left the PR): `outdated`. That is not evidence of
    a fix, so it never reads as addressed;
  - not raised again on untouched code: still `open`. A finding is never
    closed because a line merely moved, nor because a delta review did not
    look at it (the carry-forward rule).

The caller supplies each prior finding's `AnchorStatus` because only it has
the diff / review-thread listing that knows (GitHub's `isOutdated`).

IDEMPOTENCY AND CONCURRENCY. Every mutation is a pure function returning a
new session, and a replayed mutation returns the input unchanged: a run is
started once per `run_id`, completed once, an event that would not change
the state is dropped. `update_session` wraps load -> mutate -> save in a
compare-and-swap loop on `version`, so two personas finishing at the same
moment both land (the loser re-reads and re-applies its mutation), and a
retried webhook whose mutation is a no-op does not even write.

A run of an older head that finishes after a newer head's run started is
recorded but not reconciled: its findings describe code the PR has moved
past, and reconciling them would reopen what the newer review closed.

Pure: no I/O, no clock. The store adapter persists `to_dict` output; the
dispatch layer supplies timestamps.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, fields, replace
from typing import Any, Callable, Iterable, Literal, Mapping, Optional, get_args

FindingState = Literal[
    "open", "addressed", "outdated", "superseded", "dismissed", "resolved",
]
FINDING_STATES: frozenset[str] = frozenset(get_args(FindingState))

# States a person put a finding in. A model rerun never overrides these.
HUMAN_STATES: frozenset[str] = frozenset({"dismissed", "resolved"})
# States a rerun put a finding in. Raising the finding again reopens them.
MACHINE_CLOSED_STATES: frozenset[str] = frozenset(
    {"addressed", "outdated", "superseded"}
)

RunStatus = Literal["running", "complete", "failed"]
SessionStatus = Literal["pending", "running", "complete", "partial", "failed"]
AnchorStatus = Literal["unchanged", "changed", "gone"]
Event = Literal["dismissed", "resolved", "reopened"]
EVENTS: frozenset[str] = frozenset(get_args(Event))

_ID_LEN = 16


class SessionConflict(RuntimeError):
    """`update_session` lost the compare-and-swap race too many times."""


def finding_identity(persona: str, path: str, rule: str, evidence: str, line: int) -> str:
    """The stable key of one finding. See the module docstring."""
    anchor = " ".join((evidence or "").split()) or f"line:{int(line)}"
    raw = "\x1f".join((persona, path, rule, anchor))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_ID_LEN]


@dataclass(frozen=True)
class ObservedFinding:
    """One finding as a persona run reported it, before reconciliation."""

    persona: str
    path: str
    line: int
    rule: str
    category: str
    severity: str
    confidence: Optional[float]
    message: str
    # The exact anchored code (the line(s) the finding is about), as read
    # from the diff at the reviewed head. This is the identity anchor.
    evidence: str
    # `confidence` is a probability in [0, 1], or None when the producer
    # gave none (a deterministic scanner).
    source: str = "llm"
    inline_comment_id: Optional[int] = None
    # The model's suggested replacement for the anchored line, or None when
    # the producer did not supply a fix hint. /grug fix reads this to apply
    # unresolved findings as a single commit. Kept as None (not "") to
    # preserve the empty-vs-absent distinction.
    suggestion: str | None = None

    @property
    def finding_id(self) -> str:
        return finding_identity(
            self.persona, self.path, self.rule, self.evidence, self.line,
        )


@dataclass(frozen=True)
class StateChange:
    state: str
    sha: str
    at: str
    reason: str = ""


@dataclass(frozen=True)
class FindingRecord:
    finding_id: str
    persona: str
    path: str
    line: int
    rule: str
    category: str
    severity: str
    confidence: Optional[float]
    message: str
    evidence: str
    source: str
    state: str
    state_sha: str
    first_seen_sha: str
    last_seen_sha: str
    history: tuple[StateChange, ...]
    superseded_by: str = ""
    inline_comment_ids: tuple[int, ...] = ()
    check_run_ids: tuple[int, ...] = ()
    suggestion: str | None = None


@dataclass(frozen=True)
class ReviewRun:
    run_id: str
    persona: str
    head_sha: str
    trigger: str
    started_at: str
    status: str = "running"
    completed_at: str = ""
    backend: str = ""
    model: str = ""
    error: str = ""
    check_run_id: Optional[int] = None


@dataclass(frozen=True)
class ReviewSession:
    repo: str
    pr_number: int
    created_at: str
    updated_at: str
    head_sha: str = ""
    trigger: str = ""
    runs: tuple[ReviewRun, ...] = ()
    findings: tuple[FindingRecord, ...] = ()
    walkthrough: str = ""
    related: tuple[str, ...] = ()
    version: int = 0
    # Started-at of the run that set `head_sha`: an older head's run that
    # starts or finishes late must not rewind the session.
    head_started_at: str = field(default="", repr=False)

    def head_runs(self) -> tuple[ReviewRun, ...]:
        """The latest run of each persona at the current head."""
        latest: dict[str, ReviewRun] = {}
        for run in self.runs:
            if run.head_sha == self.head_sha:
                latest[run.persona] = run
        return tuple(latest.values())

    @property
    def status(self) -> SessionStatus:
        runs = self.head_runs()
        if not runs:
            return "pending"
        states = {r.status for r in runs}
        if "running" in states:
            return "running"
        if states == {"complete"}:
            return "complete"
        if states == {"failed"}:
            return "failed"
        return "partial"

    @property
    def completed_at(self) -> str:
        """When the head's review finished, or "" while it is still running."""
        if self.status in ("pending", "running"):
            return ""
        return max(r.completed_at for r in self.head_runs())


def new_session(repo: str, pr_number: int, *, at: str) -> ReviewSession:
    return ReviewSession(
        repo=repo, pr_number=int(pr_number), created_at=at, updated_at=at,
    )


def _run_index(session: ReviewSession, run_id: str) -> int:
    for i, run in enumerate(session.runs):
        if run.run_id == run_id:
            return i
    raise KeyError(f"unknown run {run_id!r}")


def start_run(session: ReviewSession, run: ReviewRun) -> ReviewSession:
    """Record that a persona run began. Replaying the same `run_id` is a
    no-op, whatever state that run has reached since."""
    if any(r.run_id == run.run_id for r in session.runs):
        return session
    run = replace(run, status="running", completed_at="", error="")
    out = replace(
        session,
        runs=session.runs + (run,),
        updated_at=max(session.updated_at, run.started_at),
    )
    if run.started_at >= session.head_started_at:
        out = replace(
            out, head_sha=run.head_sha, trigger=run.trigger,
            head_started_at=run.started_at,
        )
    return out


def fail_run(session: ReviewSession, run_id: str, *, at: str, error: str) -> ReviewSession:
    """A run that could not finish. Its persona's findings are left exactly
    as they were: a failed run is no evidence about any of them."""
    i = _run_index(session, run_id)
    run = session.runs[i]
    if run.status != "running":
        return session
    done = replace(run, status="failed", completed_at=at, error=error)
    return replace(
        session,
        runs=session.runs[:i] + (done,) + session.runs[i + 1:],
        updated_at=at,
    )


def complete_run(
    session: ReviewSession,
    run_id: str,
    *,
    findings: Iterable[ObservedFinding],
    anchors: Mapping[str, AnchorStatus],
    at: str,
    check_run_id: Optional[int] = None,
) -> ReviewSession:
    """Record a finished run and reconcile its persona's findings.

    `anchors` maps a prior finding's id to what happened to its anchored
    code at this head; an id absent from it is treated as `unchanged`.
    Completing a run twice (a retried webhook) returns the session as is.
    """
    i = _run_index(session, run_id)
    run = session.runs[i]
    if run.status != "running":
        return session
    done = replace(run, status="complete", completed_at=at, check_run_id=check_run_id)
    out = replace(
        session,
        runs=session.runs[:i] + (done,) + session.runs[i + 1:],
        updated_at=at,
    )
    if run.head_sha != session.head_sha:
        return out
    return replace(
        out,
        findings=_reconcile(
            session.findings, run, list(findings), anchors, at, check_run_id,
        ),
    )


def _add(ids: tuple[int, ...], value: Optional[int]) -> tuple[int, ...]:
    if value is None or value in ids:
        return ids
    return ids + (int(value),)


def _transition(rec: FindingRecord, state: str, sha: str, at: str, reason: str) -> FindingRecord:
    return replace(
        rec,
        state=state,
        state_sha=sha,
        history=rec.history + (StateChange(state, sha, at, reason),),
    )


def _reconcile(
    prior: tuple[FindingRecord, ...],
    run: ReviewRun,
    observed: list[ObservedFinding],
    anchors: Mapping[str, AnchorStatus],
    at: str,
    check_run_id: Optional[int],
) -> tuple[FindingRecord, ...]:
    sha = run.head_sha
    incoming: dict[str, ObservedFinding] = {}
    # A run only speaks for its own persona; another persona's findings in
    # the list are ignored rather than allowed to close that persona's.
    for f in observed:
        if f.persona == run.persona:
            incoming.setdefault(f.finding_id, f)
    known = {r.finding_id for r in prior}
    fresh = [f for fid, f in incoming.items() if fid not in known]

    out: list[FindingRecord] = []
    for rec in prior:
        if rec.persona != run.persona:
            out.append(rec)
            continue
        seen = incoming.get(rec.finding_id)
        if seen is not None:
            rec = replace(
                rec,
                line=seen.line,
                message=seen.message,
                severity=seen.severity,
                category=seen.category,
                confidence=seen.confidence,
                evidence=seen.evidence,
                suggestion=seen.suggestion,
                last_seen_sha=sha,
                inline_comment_ids=_add(rec.inline_comment_ids, seen.inline_comment_id),
                check_run_ids=_add(rec.check_run_ids, check_run_id),
            )
            if rec.state in MACHINE_CLOSED_STATES:
                rec = replace(
                    _transition(rec, "open", sha, at, "raised again"),
                    superseded_by="",
                )
            out.append(rec)
            continue
        if rec.state != "open":
            out.append(rec)
            continue
        successor = _successor(rec, fresh)
        anchor = anchors.get(rec.finding_id, "unchanged")
        if successor is not None:
            rec = replace(
                _transition(rec, "superseded", sha, at, "replaced by a newer finding"),
                superseded_by=successor.finding_id,
            )
        elif anchor == "changed":
            rec = _transition(
                rec, "addressed", sha, at, "anchored code changed and not raised again",
            )
        elif anchor == "gone":
            rec = _transition(
                rec, "outdated", sha, at, "anchored code no longer in the diff",
            )
        out.append(rec)

    for f in fresh:
        out.append(FindingRecord(
            finding_id=f.finding_id,
            persona=f.persona,
            path=f.path,
            line=f.line,
            rule=f.rule,
            category=f.category,
            severity=f.severity,
            confidence=f.confidence,
            message=f.message,
            evidence=f.evidence,
            source=f.source,
            state="open",
            state_sha=sha,
            first_seen_sha=sha,
            last_seen_sha=sha,
            history=(StateChange("open", sha, at, "first raised"),),
            inline_comment_ids=_add((), f.inline_comment_id),
            check_run_ids=_add((), check_run_id),
            suggestion=f.suggestion,
        ))
    return tuple(out)


def _successor(rec: FindingRecord, fresh: list[ObservedFinding]) -> Optional[ObservedFinding]:
    """The new finding that replaces `rec`: same rule, same file, nearest line."""
    same = [f for f in fresh if f.path == rec.path and f.rule == rec.rule]
    if not same:
        return None
    return min(same, key=lambda f: (abs(f.line - rec.line), f.line))


def _finding_index(session: ReviewSession, finding_id: str) -> int:
    for i, rec in enumerate(session.findings):
        if rec.finding_id == finding_id:
            return i
    raise KeyError(f"unknown finding {finding_id!r}")


def _replace_finding(session: ReviewSession, i: int, rec: FindingRecord, at: str) -> ReviewSession:
    return replace(
        session,
        findings=session.findings[:i] + (rec,) + session.findings[i + 1:],
        updated_at=at,
    )


def apply_event(
    session: ReviewSession,
    finding_id: str,
    event: str,
    *,
    sha: str,
    at: str,
    reason: str = "",
) -> ReviewSession:
    """A person acted on a finding: dismissed it (a maintainer's
    thumbs-down), resolved its thread, or undid either (`reopened`).
    An event that would not change the state is a no-op."""
    if event not in EVENTS:
        raise ValueError(f"unknown finding event {event!r}; expected one of {sorted(EVENTS)}")
    i = _finding_index(session, finding_id)
    rec = session.findings[i]
    if event == "reopened":
        if rec.state not in HUMAN_STATES:
            return session
        target = "open"
    else:
        target = event
    if rec.state == target:
        return session
    return _replace_finding(session, i, _transition(rec, target, sha, at, reason), at)


def link_inline_comment(session: ReviewSession, finding_id: str, comment_id: int) -> ReviewSession:
    i = _finding_index(session, finding_id)
    rec = session.findings[i]
    ids = _add(rec.inline_comment_ids, comment_id)
    if ids == rec.inline_comment_ids:
        return session
    return _replace_finding(session, i, replace(rec, inline_comment_ids=ids), session.updated_at)


def finding_for_comment(session: ReviewSession, comment_id: int) -> Optional[FindingRecord]:
    return next(
        (r for r in session.findings if int(comment_id) in r.inline_comment_ids), None,
    )


def findings_for_check_run(session: ReviewSession, check_run_id: int) -> tuple[FindingRecord, ...]:
    return tuple(r for r in session.findings if int(check_run_id) in r.check_run_ids)


def set_walkthrough(
    session: ReviewSession, text: str, *, related: tuple[str, ...] = (),
) -> ReviewSession:
    """Teller's summary and the related-context lines the board shows."""
    if session.walkthrough == text and session.related == tuple(related):
        return session
    return replace(session, walkthrough=text, related=tuple(related))


# -- persistence ---------------------------------------------------------------


def to_dict(session: ReviewSession) -> dict[str, Any]:
    """JSON-safe form for the store. Inverse of `from_dict`."""
    return {
        "repo": session.repo,
        "pr_number": session.pr_number,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "head_sha": session.head_sha,
        "head_started_at": session.head_started_at,
        "trigger": session.trigger,
        "walkthrough": session.walkthrough,
        "related": list(session.related),
        "version": session.version,
        "runs": [vars(r).copy() for r in session.runs],
        "findings": [
            {
                **{k: v for k, v in vars(f).items() if k not in (
                    "history", "inline_comment_ids", "check_run_ids")},
                "history": [vars(h).copy() for h in f.history],
                "inline_comment_ids": list(f.inline_comment_ids),
                "check_run_ids": list(f.check_run_ids),
            }
            for f in session.findings
        ],
    }


def _known(cls: type, d: Mapping[str, Any]) -> dict[str, Any]:
    """Only the keys `cls` declares: a row written by a newer grug with an
    extra field must still load, not raise TypeError."""
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in d.items() if k in names}


def from_dict(data: Mapping[str, Any]) -> ReviewSession:
    def _finding(d: Mapping[str, Any]) -> FindingRecord:
        conf = d.get("confidence")
        return FindingRecord(
            finding_id=str(d["finding_id"]),
            persona=str(d["persona"]),
            path=str(d["path"]),
            line=int(d["line"]),
            rule=str(d["rule"]),
            category=str(d.get("category", "")),
            severity=str(d.get("severity", "")),
            confidence=None if conf is None else float(conf),
            message=str(d.get("message", "")),
            evidence=str(d.get("evidence", "")),
            source=str(d.get("source", "")),
            state=str(d["state"]),
            state_sha=str(d.get("state_sha", "")),
            first_seen_sha=str(d.get("first_seen_sha", "")),
            last_seen_sha=str(d.get("last_seen_sha", "")),
            history=tuple(
                StateChange(**_known(StateChange, h)) for h in d.get("history", [])
            ),
            superseded_by=str(d.get("superseded_by", "")),
            inline_comment_ids=tuple(int(x) for x in d.get("inline_comment_ids", [])),
            check_run_ids=tuple(int(x) for x in d.get("check_run_ids", [])),
            suggestion=d.get("suggestion"),
        )

    def _run(d: Mapping[str, Any]) -> ReviewRun:
        cr = d.get("check_run_id")
        return ReviewRun(**{
            **_known(ReviewRun, d), "check_run_id": None if cr is None else int(cr),
        })

    return ReviewSession(
        repo=str(data["repo"]),
        pr_number=int(data["pr_number"]),
        created_at=str(data.get("created_at", "")),
        updated_at=str(data.get("updated_at", "")),
        head_sha=str(data.get("head_sha", "")),
        head_started_at=str(data.get("head_started_at", "")),
        trigger=str(data.get("trigger", "")),
        walkthrough=str(data.get("walkthrough", "")),
        related=tuple(str(x) for x in data.get("related", [])),
        version=int(data.get("version", 0)),
        runs=tuple(_run(r) for r in data.get("runs", [])),
        findings=tuple(_finding(f) for f in data.get("findings", [])),
    )


def update_session(
    load: Callable[[], Optional[Mapping[str, Any]]],
    save: Callable[[dict[str, Any], int], bool],
    repo: str,
    pr_number: int,
    mutate: Callable[[ReviewSession], ReviewSession],
    *,
    at: str,
    max_attempts: int = 8,
) -> ReviewSession:
    """Load, mutate, compare-and-swap save; re-read and retry on a lost race.

    `save(data, expected)` must store `data` only when the stored version is
    still `expected` (0 = no row yet) and report whether it did. `mutate`
    must be a pure function of the session it is given, because it is
    re-applied to the fresh copy after a conflict. A mutation that changes
    nothing (a replayed webhook) returns without writing.
    """
    for _ in range(max_attempts):
        raw = load()
        current = from_dict(raw) if raw else new_session(repo, pr_number, at=at)
        changed = mutate(current)
        if changed == current:
            return current
        nxt = replace(changed, version=current.version + 1)
        if save(to_dict(nxt), current.version):
            return nxt
    raise SessionConflict(
        f"review session {repo}#{pr_number}: lost the update race {max_attempts} times"
    )
