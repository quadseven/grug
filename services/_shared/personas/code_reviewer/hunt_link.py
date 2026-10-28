"""Elder's side of the Hunt Board: turn one review pass into session input.

`hunt_recorder` is persona-agnostic; this adapts what Elder knows after a
pass (its findings, the head blobs it read, the files in the diff, the
inline comments and check-run it published) into `ObservedFinding`s and the
anchor verdicts `review_session.complete_run` reconciles with.

ANCHORS. For each finding still open from an earlier pass, did its anchored
code survive this pass? Only code this pass READ can be judged:

  - a full review whose diff no longer contains the file: `gone` (a
    force-push dropped it, or the file left the PR) - outdated, never
    addressed, since that is no evidence of a fix;
  - the file was read and the anchored line is no longer in it as a whole line: `changed`
    (addressed, if the pass does not raise it again);
  - anything else, including every file a delta review did not read:
    `unchanged`, so the finding stays open. A moved line, or a file nobody
    looked at, never closes one.

A degraded pass is different: one that saw only part of the diff
(`PUBLISHABLE_DEGRADATIONS`) still reports the findings it made, but says
nothing about the ones it did not raise, so no anchor is judged. Any other
degradation means the pass did not look, and is recorded as a failed run.
"""
from __future__ import annotations

from typing import Callable, Iterable, Mapping, Optional

import review_session as rs
from personas import hunt_recorder
from personas.code_reviewer.persona import Finding
from personas.review_publish import PUBLISHABLE_DEGRADATIONS

PERSONA = "elder"
# Elder's findings are model output unless a deterministic scanner made them.
_SOURCE = "llm"

# (file, rule, line) of a posted finding -> the inline comment id it got.
CommentIds = Mapping[tuple[str, str, int], int]


def _squash(text: str) -> str:
    return " ".join(text.split())


def evidence_line(file_contents: Mapping[str, str], path: str, line: int) -> str:
    """The exact anchored line at the reviewed head, or "" when unread.

    `line` is 1-based, the new-file line number a diff hunk and a GitHub
    inline comment use. `Finding.__post_init__` rejects zero, and the diff
    parser's new-side cursor starts at the hunk's 1-based `new_start`."""
    lines = file_contents.get(path, "").splitlines()
    return lines[line - 1] if 0 < line <= len(lines) else ""


def observe(
    findings: Iterable[Finding],
    file_contents: Mapping[str, str],
    comment_ids: CommentIds,
    category_of: Callable[[str], str],
) -> tuple[rs.ObservedFinding, ...]:
    return tuple(
        rs.ObservedFinding(
            persona=PERSONA,
            path=f.file,
            line=f.line,
            rule=f.rule_name,
            category=category_of(f.rule_name),
            severity=f.severity,
            confidence=None,
            message=f.message,
            evidence=evidence_line(file_contents, f.file, f.line),
            source=_SOURCE,
            inline_comment_id=comment_ids.get((f.file, f.rule_name, f.line)),
            suggestion=f.suggestion,
        )
        for f in findings
    )


def _anchor(
    rec: rs.FindingRecord,
    file_contents: Mapping[str, str],
    diff_paths: frozenset[str],
    delta: bool,
) -> rs.AnchorStatus:
    if not delta and rec.path not in diff_paths:
        return "gone"
    text = file_contents.get(rec.path)
    needle = _squash(rec.evidence)
    if text is None or not needle:
        return "unchanged"
    present = needle in {_squash(line) for line in text.splitlines()}
    return "unchanged" if present else "changed"


def anchors_for(
    prior: tuple[rs.FindingRecord, ...],
    file_contents: Mapping[str, str],
    diff_paths: frozenset[str],
    delta: bool,
) -> dict[str, rs.AnchorStatus]:
    return {
        rec.finding_id: _anchor(rec, file_contents, diff_paths, delta)
        for rec in prior
        if rec.persona == PERSONA and rec.state == "open"
    }


def record_elder_run(
    ref: hunt_recorder.PrRef,
    *,
    head_sha: str,
    trigger: str,
    started_at: str,
    backend: str,
    model: str,
    findings: Iterable[Finding],
    file_contents: Mapping[str, str],
    diff_paths: frozenset[str],
    delta: bool,
    comment_ids: CommentIds,
    check_run_id: Optional[int],
    category_of: Callable[[str], str],
    degraded_reason: str = "",
) -> Optional[rs.ReviewSession]:
    """Record one Elder pass in the PR's canonical session. Never raises."""
    meta = hunt_recorder.RunMeta(PERSONA, head_sha, trigger, started_at, backend, model)
    did_not_look = bool(degraded_reason) and degraded_reason not in PUBLISHABLE_DEGRADATIONS
    saw_part = degraded_reason in PUBLISHABLE_DEGRADATIONS

    def anchors(prior: tuple[rs.FindingRecord, ...]) -> dict[str, rs.AnchorStatus]:
        if saw_part:
            return {}
        return anchors_for(prior, file_contents, diff_paths, delta)

    return hunt_recorder.record_run(
        ref, meta,
        findings=() if did_not_look else observe(
            findings, file_contents, comment_ids, category_of),
        anchors=anchors,
        check_run_id=check_run_id,
        error=degraded_reason if did_not_look else "",
    )
