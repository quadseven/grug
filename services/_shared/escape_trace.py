"""Trace a merged fix or revert pull request back to the reviewed pull
request it corrects (#1568 slice 1).

This is the PURE tracing step of the escaped-defect loop. Given a merged
correcting PR it works out which earlier PR introduced the defect, how the
link was established, and which review findings (if any) had already touched
the files both PRs changed. Classifying the escape, writing it to the ledger
and feeding the replay set are later slices; nothing here stores anything.

No I/O. Every lookup goes through the injected `TraceSources`, so callers
choose the fetchers and tests need no network.

Link evidence, strongest first, so the first one that resolves to a merged
earlier PR in the same repo wins:

- `revert-body`: "Reverts #N" (also a repo-qualified reference and a pull URL), the line
  GitHub's Revert button writes.
- `regression-body`: "Fixes regression from #N", "regression introduced in
  #N", "regression caused by #N".
- `revert-trailer`: a commit message trailer "This reverts commit <sha>",
  resolved to a PR through `pull_for_commit`.
- `regression-trailer`: a commit message trailer "Regression-of: #N".

A revert of a revert is followed: the escaped PR is the one directly
reverted, `chain` lists every PR walked, and `reverts_root` says whether the
net effect removes the root change or re-applies it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

# Revert-of-revert walks stop here; a real chain is one or two deep.
MAX_CHAIN_DEPTH = 5

REVERT_BODY = "revert-body"
REGRESSION_BODY = "regression-body"
REVERT_TRAILER = "revert-trailer"
REGRESSION_TRAILER = "regression-trailer"
LINK_KINDS = (REVERT_BODY, REGRESSION_BODY, REVERT_TRAILER, REGRESSION_TRAILER)
_REVERT_KINDS = frozenset({REVERT_BODY, REVERT_TRAILER})

# A pull reference: "#12", a repo-qualified number or a pull request URL.
_REF = (
    r"(?:https?://github\.com/(?P<urepo>[\w.-]+/[\w.-]+)/pull/"
    r"|(?P<rrepo>[\w.-]+/[\w.-]+)#|#)(?P<num>\d+)"
)
_REVERT_BODY_RE = re.compile(r"\breverts\s+" + _REF, re.IGNORECASE)
_REGRESSION_BODY_RE = re.compile(
    r"\bregression\s+(?:from|in|introduced\s+(?:in|by)|caused\s+by)\s+" + _REF,
    re.IGNORECASE,
)
_REVERT_TRAILER_RE = re.compile(
    r"^\s*This reverts commit\s+(?P<sha>[0-9a-f]{7,40})\b",
    re.IGNORECASE | re.MULTILINE,
)
_REGRESSION_TRAILER_RE = re.compile(
    r"^\s*Regression-of:\s*" + _REF, re.IGNORECASE | re.MULTILINE
)


@dataclass(frozen=True)
class PullRef:
    """What the trace needs to know about one pull request."""

    number: int
    title: str = ""
    body: str = ""
    merged: bool = False
    files: tuple[str, ...] = ()
    # Full messages of the PR's commits, for the trailer links.
    commit_messages: tuple[str, ...] = ()


@dataclass(frozen=True)
class FindingRef:
    """One review finding recorded against a PR, as the trace needs it."""

    pr: int
    path: str
    finding_class: str = ""
    verdict: str = ""
    reviewer: str = ""


@dataclass(frozen=True)
class TraceSources:
    """The injected lookups. Each may raise; the trace does not swallow."""

    get_pull: Callable[[int], PullRef | None]
    pull_for_commit: Callable[[str], int | None]
    findings_for_pull: Callable[[int], Sequence[FindingRef]]


@dataclass(frozen=True)
class EscapeTrace:
    """The result of tracing one correcting PR."""

    correcting_pr: int
    # None when nothing linked the correcting PR to an earlier merged PR.
    escaped_pr: int | None
    link: str | None
    # Every PR walked, escaped first; longer than one only for a revert chain.
    chain: tuple[int, ...] = ()
    # For a revert link: True if the net effect removes the root change, False
    # if a revert of a revert re-applies it. None for a non-revert link.
    reverts_root: bool | None = None
    # Findings on the escaped PR whose file the correcting PR also changed.
    touching_findings: tuple[FindingRef, ...] = ()
    # Why nothing was traced, when escaped_pr is None.
    reason: str = ""
    # Paths changed by both the escaped and correcting PRs. Empty means the
    # trace has no shared-file evidence for classifying review visibility.
    shared_files: tuple[str, ...] = ()

    @property
    def traced(self) -> bool:
        return self.escaped_pr is not None

    @property
    def root_pr(self) -> int | None:
        return self.chain[-1] if self.chain else None


def _ref_number(m: re.Match[str], repo: str) -> int | None:
    """The PR number of a reference, or None if it names another repo."""
    named = m.group("urepo") or m.group("rrepo")
    if named and named.lower() != repo.lower():
        return None
    return int(m.group("num"))


def _candidates(pr: PullRef, repo: str) -> list[tuple[str, int | str]]:
    """Link evidence in `pr`, strongest first: (kind, pr number or sha)."""
    out: list[tuple[str, int | str]] = []
    for kind, rx in ((REVERT_BODY, _REVERT_BODY_RE), (REGRESSION_BODY, _REGRESSION_BODY_RE)):
        for m in rx.finditer(pr.body or ""):
            n = _ref_number(m, repo)
            if n is not None:
                out.append((kind, n))
    messages = "\n".join(pr.commit_messages)
    for m in _REVERT_TRAILER_RE.finditer(messages):
        out.append((REVERT_TRAILER, m.group("sha").lower()))
    for m in _REGRESSION_TRAILER_RE.finditer(messages):
        n = _ref_number(m, repo)
        if n is not None:
            out.append((REGRESSION_TRAILER, n))
    return out


def _resolve(
    pr: PullRef, repo: str, sources: TraceSources, *, revert_only: bool = False,
) -> tuple[str, PullRef] | None:
    """The first link from `pr` to an earlier, merged, other PR, or None."""
    for kind, ref in _candidates(pr, repo):
        if revert_only and kind not in _REVERT_KINDS:
            continue
        number = ref if isinstance(ref, int) else sources.pull_for_commit(ref)
        if not number or number == pr.number:
            continue
        target = sources.get_pull(number)
        if target is not None and target.merged:
            return kind, target
    return None


def trace_escape(
    correcting: PullRef, repo: str, sources: TraceSources,
    *, max_depth: int = MAX_CHAIN_DEPTH,
) -> EscapeTrace:
    """Trace one correcting PR to the earlier PR it corrects.

    `repo` is "owner/name"; references to any other repo are ignored.
    """
    if not correcting.merged:
        return EscapeTrace(correcting.number, None, None, reason="correcting PR is not merged")
    first = _resolve(correcting, repo, sources)
    if first is None:
        return EscapeTrace(
            correcting.number, None, None, reason="no link to an earlier merged PR",
        )
    kind, escaped = first
    chain = [escaped.number]
    if kind in _REVERT_KINDS:
        cur = escaped
        while len(chain) < max_depth:
            nxt = _resolve(cur, repo, sources, revert_only=True)
            if nxt is None or nxt[1].number in chain or nxt[1].number == correcting.number:
                break
            chain.append(nxt[1].number)
            cur = nxt[1]
    shared = set(correcting.files) & set(escaped.files)
    touching = tuple(
        f for f in sources.findings_for_pull(escaped.number) if f.path in shared
    )
    return EscapeTrace(
        correcting_pr=correcting.number,
        escaped_pr=escaped.number,
        link=kind,
        chain=tuple(chain),
        reverts_root=(len(chain) % 2 == 1) if kind in _REVERT_KINDS else None,
        touching_findings=touching,
        shared_files=tuple(sorted(shared)),
    )
