"""Living review: Elder's inline findings show their current state (#1187).

A finding Elder posted used to look the same forever, so a fixed finding
read exactly like an open one. This module edits Elder's own inline comment
in place to show what became of it:

  - Addressed in <sha>: a later push changed or removed the anchored lines
    (GitHub's `isOutdated`) and the review of that push did not raise the
    finding again nearby. Only the review lane can say this, because only
    it knows what the new review found.
  - Resolved: a person (or the #1102 resolver) resolved the thread. Grug
    never resolves a thread for this; it only annotates. Unresolving the
    thread puts the finding back to open.
  - Dismissed: a write-authorized maintainer reacted thumbs-down (the
    reaction poller's `last_verdict`). Withdrawing that verdict to a
    thumbs-up puts the finding back to open.

The edited body is a state chip, then the original body verbatim with its
header line struck through. A hidden `grug-state` marker at the top makes
the edit idempotent per (finding, state, sha): a comment is only edited when
the state it should show differs from the state it carries. The original
text is recoverable exactly, and its trailing `grug-rule` marker stays last,
so dedup and the thread resolver still recognise the finding.

Three lanes call `sync_pr`, all on paths that already exist: the review lane
after a published re-review (`dispatch._tidy_fixed_threads`), the
reaction-poll lane (`sync_due_prs` from `poller_handler`), and the learn
lane after a maintainer replies to a finding (`rerun._run_learn`). A
maintainer's reply is acknowledged with the learn lane's existing reaction,
never a comment (operator decision, pinned by
`test_learn_outcomes_never_post_a_comment`); the Resolved chip is the
in-place acknowledgement.

Each transition records the state on the CommentRecord and gives the
ledger's stable evidence row its resolution (`reactions.ledger_label`)
before the comment is edited, so a failed edit is retried on the next pass
and the ledger is never behind the comment. Nothing here raises.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from urllib.parse import quote

import httpx
from adapters.install_store import (  # type: ignore
    CommentRecord,
    list_comment_records_for_pr,
    update_comment_record_resolution,
)
from github_app_auth import with_install_token_retry
from personas.code_reviewer.dedup import parse_rule
from personas.code_reviewer.persona import Finding
from personas.code_reviewer.reactions import ledger_label, record_ledger_learning
from personas.code_reviewer.thread_resolver import (
    _NEARBY_LINES,
    _fetch_threads,
    is_grug,
)

log = logging.getLogger(
    f"{os.getenv('DD_SERVICE', 'grug')}.persona.code_reviewer.living_review"
)

_GH_API = "https://api.github.com"
_TIMEOUT = 10
# GitHub rejects a review-comment body above this many characters.
_MAX_BODY = 65536
# Distinct PRs one install syncs per poll tick: one GraphQL listing each.
MAX_PRS_PER_POLL = 10

ORIGINAL_MARK = "<!-- grug-original -->"
_STATE_RE = re.compile(
    r"\A<!-- grug-state:(addressed|resolved|dismissed)(?::([0-9A-Za-z]{1,40}))? -->\n"
)


@dataclass(frozen=True)
class FindingState:
    state: str  # "addressed" | "resolved" | "dismissed"
    sha: str = ""


RESOLVED = FindingState("resolved")
DISMISSED = FindingState("dismissed")


@dataclass(frozen=True)
class ReviewEvidence:
    """What a complete re-review of `head_sha` found, so a changed line the
    review did not raise again can read as addressed in that head.

    `trusted_push` is True when the head branch lives in the base repository,
    so whoever pushed it has write access. A fork push needs none, so on its
    own it may show a finding addressed but never teach the ledger `fixed`."""
    head_sha: str
    findings: tuple[Finding, ...]
    trusted_push: bool = False


@dataclass(frozen=True)
class Transition:
    comment_id: int
    target: Optional[FindingState]  # None = back to open
    body: str
    # False when the new state came from an untrusted (fork) push alone.
    teaches: bool = True


def parse_state(body: str) -> Optional[FindingState]:
    m = _STATE_RE.match(body or "")
    return FindingState(m.group(1), m.group(2) or "") if m else None


def original_body(body: str) -> str:
    """The finding as Elder first posted it, with any state wrapper removed."""
    if parse_state(body) is None:
        return body or ""
    _, sep, tail = body.partition(f"\n{ORIGINAL_MARK}\n")
    if not sep:
        return body
    header, nl, rest = tail.partition("\n")
    if len(header) > 4 and header.startswith("~~") and header.endswith("~~"):
        header = header[2:-2]
    return header + nl + rest


def _chip(state: FindingState) -> str:
    if state.state == "addressed":
        return (
            f"**Addressed in {state.sha}**: a later push changed the flagged "
            f"lines and the review of {state.sha} did not raise this again."
        )
    if state.state == "resolved":
        return "**Resolved**: this thread was resolved."
    return "**Dismissed**: a maintainer marked this finding a false positive."


def render(original: str, state: Optional[FindingState]) -> str:
    """`original` as it should read in `state` (None = open, unchanged)."""
    if state is None:
        return original
    marker = f"<!-- grug-state:{state.state}{':' + state.sha if state.sha else ''} -->"
    header, nl, rest = original.partition("\n")
    struck = f"~~{header}~~" if header.strip() else header
    return (
        f"{marker}\n{_chip(state)} Original finding below.\n\n"
        f"{ORIGINAL_MARK}\n{struck}{nl}{rest}"
    )


def _elder_root(thread: Mapping[str, Any]) -> Optional[dict]:
    """The thread's first comment when it is one of Elder's findings."""
    nodes = (thread.get("comments") or {}).get("nodes") or []
    root = nodes[0] if nodes else {}
    if not is_grug((root.get("author") or {}).get("login")):
        return None
    if not root.get("databaseId") or parse_rule(root.get("body") or "") is None:
        return None
    return root


def _addressed(
    thread: Mapping[str, Any], root: dict, review: Optional[ReviewEvidence],
) -> bool:
    if review is None or not thread.get("isOutdated"):
        return False
    rule = parse_rule(root.get("body") or "")
    line = int(root.get("originalLine") or root.get("line") or 0)
    path = root.get("path")
    return not any(
        f.rule_name == rule and f.file == path
        and abs(f.line - line) <= _NEARBY_LINES
        for f in review.findings
    )


def target_state(
    thread: Mapping[str, Any], root: dict, verdict: Optional[str],
    review: Optional[ReviewEvidence],
) -> Optional[FindingState]:
    """The state this finding should show. Dismissal outranks everything;
    an earlier addressed state is sticky (its sha names the push that
    changed the code); a resolved state follows the thread."""
    if verdict == "false_positive":
        return DISMISSED
    current = parse_state(root.get("body") or "")
    if current is not None and current.state == "addressed":
        return current
    if review is not None and review.head_sha and _addressed(thread, root, review):
        return FindingState("addressed", review.head_sha[:7])
    if thread.get("isResolved"):
        return RESOLVED
    return None


def plan(
    threads: list[dict], verdicts: Mapping[int, Optional[str]],
    review: Optional[ReviewEvidence] = None,
) -> list[Transition]:
    """The edits that bring each Elder finding to its current state.

    `verdicts` maps comment id to the stored reaction verdict. A finding
    with no verdict keeps a dismissal its body already shows, and an empty
    map never dismisses anything new."""
    out: list[Transition] = []
    for t in threads:
        root = _elder_root(t)
        if root is None:
            continue
        cid = int(root["databaseId"])
        body = root.get("body") or ""
        verdict = verdicts.get(cid)
        if verdict is None and parse_state(body) == DISMISSED:
            verdict = "false_positive"
        target = target_state(t, root, verdict, review)
        if target == parse_state(body):
            continue
        new_body = render(original_body(body), target)
        if len(new_body) > _MAX_BODY:
            log.info("elder_living_review_body_too_long", extra={"comment_id": cid})
            continue
        from_push = target is not None and target.state == "addressed"
        teaches = not from_push or (review is not None and review.trusted_push)
        out.append(Transition(cid, target, new_body, teaches))
    return out


def count_addressed(threads: list[dict], review: ReviewEvidence) -> int:
    """Findings this review moves to addressed, from the listing alone.

    The check title is written before the edits; a finding a maintainer
    thumbs-downed since the last poll shows as dismissed instead."""
    return sum(
        1 for t in plan(threads, {}, review)
        if t.target is not None and t.target.state == "addressed"
    )


def note_on_title(title: str, addressed: int, head_sha: str) -> str:
    """Name the addressed findings on the check title, which is also the
    summary the Activity feed records for this review."""
    if not addressed:
        return title
    return f"{title}; {addressed} earlier finding(s) addressed in {head_sha[:7]}"


def _patch_comment(token: str, owner: str, repo: str, comment_id: int, body: str) -> None:
    resp = httpx.patch(
        f"{_GH_API}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        f"/pulls/comments/{comment_id}",
        json={"body": body},
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()


def _trusted_verdict(record: CommentRecord) -> Optional[str]:
    """The record's reaction verdict when its reactors were permission
    filtered. A legacy record kept every reactor's thumbs, so its verdict
    must not dismiss a finding (and drop it from the blocking carry-forward)
    or label the ledger."""
    return record.get("last_verdict") if record.get("trust_reactors") else None


def _record_transition(
    installation_id: int, record: Optional[CommentRecord], t: Transition,
) -> None:
    """Store the state and give the ledger row its resolution evidence.

    An untrusted push alone labels nothing; with a trusted thumbs-up beside
    it the finding was real and its code changed, so it is `fixed`."""
    if record is None:
        return
    state = t.target.state if t.target else None
    sha = t.target.sha if t.target else ""
    update_comment_record_resolution(
        install_id=installation_id, comment_id=t.comment_id, state=state, sha=sha,
    )
    resolution = {"state": state, "sha": sha} if state else None
    verdict = _trusted_verdict(record)
    # The label comes from this transition. `record` was listed before the
    # update above, so its stored `resolution` is the previous state; the
    # record handed on carries this one, so nothing downstream reads stale.
    label = ledger_label(verdict, resolution if t.teaches or verdict else None)
    if label is not None:
        record_ledger_learning({**record, "resolution": resolution}, label, resolution)


def _apply(
    installation_id: int, owner: str, repo: str,
    transitions: list[Transition], records: Mapping[int, CommentRecord],
) -> int:
    edited = 0
    for t in transitions:
        try:
            _record_transition(installation_id, records.get(t.comment_id), t)
            with_install_token_retry(
                installation_id,
                lambda tok, tr=t: _patch_comment(tok, owner, repo, tr.comment_id, tr.body),
            )
            edited += 1
            log.info("elder_finding_state_changed", extra={
                "repo": f"{owner}/{repo}", "comment_id": t.comment_id,
                "state": t.target.state if t.target else "open",
                "sha": t.target.sha if t.target else "",
            })
        except Exception as e:  # noqa: BLE001 - one bad edit must not stop the rest
            log.warning("elder_living_review_edit_failed", extra={
                "repo": f"{owner}/{repo}", "comment_id": t.comment_id,
                "kind": type(e).__name__,
            })
    return edited


def sync_pr(
    installation_id: int, owner: str, repo: str, pull_number: int,
    *, threads: Optional[list[dict]] = None,
    review: Optional[ReviewEvidence] = None,
) -> int:
    """Bring every Elder finding on the PR to its current state.

    `threads` reuses a listing the caller already has; None lists here.
    `review` is passed only by the review lane. Returns the number of
    comments edited. Never raises."""
    pr_ref = f"{owner}/{repo}#{pull_number}"
    try:
        listing: list[dict] = threads if threads is not None else (
            with_install_token_retry(
                installation_id,
                lambda tok: _fetch_threads(tok, owner, repo, pull_number),
            ) or []
        )
        records: dict[int, CommentRecord] = {
            int(r["comment_id"]): r
            for r in list_comment_records_for_pr(
                installation_id, f"{owner}/{repo}", pull_number,
            )
        }
        verdicts = {cid: _trusted_verdict(r) for cid, r in records.items()}
        transitions = plan(listing, verdicts, review)
    except Exception as e:  # noqa: BLE001 - annotating never fails a lane
        log.warning("elder_living_review_list_failed",
                    extra={"pr": pr_ref, "kind": type(e).__name__})
        return 0
    edited = _apply(installation_id, owner, repo, transitions, records)
    if transitions:
        log.info("elder_living_review_synced", extra={
            "pr": pr_ref, "planned": len(transitions), "edited": edited,
        })
    return edited


def sync_due_prs(records: list[dict], *, install_id: int) -> int:
    """Poll lane: sync each distinct PR among the due reaction records, at
    most MAX_PRS_PER_POLL per tick. Returns the number of comments edited."""
    prs: list[tuple[str, str, int]] = []
    for r in records:
        owner, sep, name = str(r.get("repo", "")).partition("/")
        key = (owner, name, int(r.get("pr_number") or 0))
        if sep and key[2] and key not in prs:
            prs.append(key)
    if len(prs) > MAX_PRS_PER_POLL:
        log.info("elder_living_review_poll_capped",
                 extra={"install_id": install_id, "prs": len(prs)})
    return sum(
        sync_pr(install_id, owner, name, pr)
        for owner, name, pr in prs[:MAX_PRS_PER_POLL]
    )
