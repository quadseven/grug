"""Carry Elder's passing verdict to a new head whose own diff is unchanged.

GitHub's update-branch and a stack's cascade rebase make new heads that
leave the PR's own diff (merge base to head) as it was. The merge gate
wants `Grug - Elder` success on the exact head, so each such head used to
wait for a full model review of code Elder had already passed (#1648).
The Living Hunt delta does not help: a merge-from-base range holds a
merge commit and a rebase rewrites history, so both fall back to a full
review of the PR's diff (review_publish, #845 and #1092).

On a re-review, Elder reads the record of the last head it finished with
(`ELDER#LAST`), which now also holds a patch-id of that head's PR diff and
an id of the review context (mode, title, body, labels, author family and
the base repo's `.grug.yaml`). It carries only when
every one of these holds:

  - that head's newest `Grug - Elder` run from this app completed
    `success` (read live from GitHub, so a later deep append's failure
    or a degraded run is never carried);
  - the new head's PR diff, read against the PR's own base ref (another
    PR's branch for a stacked PR), has the same patch-id;
  - the review context is unchanged;
  - the review threads can be listed, so carry-forward still applies.

Then Elder posts the check with "carried from <sha>: diff unchanged", no
model call and no inline review. Anything else, including any read that
fails, returns None and the caller runs the normal review.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from activity_log import record_check_verdict
from github_app_auth import get_app_id, with_install_token_retry
from github_checks_client import CheckRunResult, list_check_runs_for_ref, post_check_run
from personas.code_reviewer import carry_forward
from personas.tribe import CHECK_ELDER

log = logging.getLogger(
    f"{os.getenv('DD_SERVICE', 'grug')}.persona.code_reviewer.carry_verdict"
)

# Production reviews arrive from the durable queue as "review"; a raw
# webhook push is "synchronize". `/grug improve` ("rerun") always reviews.
CARRY_ACTIONS = frozenset({"synchronize", "review"})

_HUNK_HEADER_RE = re.compile(r"^@@ [^@]* @@")
_BINARY_MARKERS = ("Binary files ", "GIT binary patch")


def patch_id(diff_text: str) -> str:
    """A patch-id of a unified diff, stable across line-number shifts.

    Every line is hashed byte for byte, whitespace included, since an
    indent can move a statement in or out of a block. Only `index` lines
    (blob ids change with the base) and the hunk line numbers are dropped;
    the hunk's section heading and its context lines stay, so lines moved
    under another function do not match. "" for a diff with no change, and
    for one with a binary file, whose content the diff text does not show."""
    lines: list[str] = []
    changed = False
    for raw in diff_text.splitlines():
        if raw.startswith(_BINARY_MARKERS):
            return ""
        if raw.startswith("index "):
            continue
        header = _HUNK_HEADER_RE.match(raw)
        if header:
            lines.append("@@" + raw[header.end():])
            continue
        if raw.startswith(("+", "-")) and not raw.startswith(("+++ ", "--- ")):
            changed = True
        elif raw.startswith(("rename from ", "deleted file", "new file", "old mode")):
            changed = True
        lines.append(raw)
    if not changed:
        return ""
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def context_id(*parts: str) -> str:
    """An id of the other review inputs: any change re-reviews."""
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


@dataclass(frozen=True)
class CarryRequest:
    """One review request, as dispatch_code_review sees it."""

    action: str
    installation_id: int
    owner: str
    repo_name: str
    pull_number: int
    head_sha: str
    blocking: bool
    title: str
    body: str
    labels: frozenset[str] = frozenset()
    author_family: str = ""
    # The base repo's loaded `.grug.yaml`, as its frozen dataclass repr.
    config: str = ""

    @property
    def pr(self) -> str:
        return f"{self.owner}/{self.repo_name}#{self.pull_number}"

    @property
    def context(self) -> str:
        return context_id(
            "blocking" if self.blocking else "advisory", self.title, self.body,
            ",".join(sorted(self.labels)), self.author_family, self.config,
        )


def _read_record(req: CarryRequest) -> dict[str, str] | None:
    try:
        from adapters.install_store import get_elder_carry_record

        return get_elder_carry_record(
            install_id=req.installation_id,
            repo=f"{req.owner}/{req.repo_name}",
            pr_number=req.pull_number,
        )
    except Exception as e:  # noqa: BLE001 - no record means a normal review
        log.warning("elder_carry_record_read_failed", extra={"pr": req.pr, "kind": type(e).__name__})
        return None


def _passed_on(req: CarryRequest, sha: str) -> bool:
    """Whether `sha`'s newest Elder run from this app completed `success`."""
    try:
        own_app_id = str(get_app_id() or "")
        runs = with_install_token_retry(
            req.installation_id,
            lambda token: list_check_runs_for_ref(token, req.owner, req.repo_name, sha),
        )
    except Exception as e:  # noqa: BLE001 - unproven verdict means a normal review
        log.warning("elder_carry_prior_check_failed", extra={"pr": req.pr, "kind": type(e).__name__})
        return False
    own = [
        run for run in runs or ()
        if isinstance(run, dict)
        and run.get("name") == CHECK_ELDER
        and own_app_id
        and str((run.get("app") or {}).get("id")) == own_app_id
    ]
    return bool(own) and all(
        run.get("status") == "completed" and run.get("conclusion") == "success"
        for run in own
    )


def _carried_from(req: CarryRequest, fetch_pr_diff: Callable[[], str]) -> tuple[str, str] | None:
    """(prior head, patch-id) when the verdict on the prior head carries."""
    if req.action not in CARRY_ACTIONS:
        return None
    record = _read_record(req)
    if not record:
        return None
    prior, stored = record.get("head_sha", ""), record.get("patch_id", "")
    if not prior or prior == req.head_sha or not stored:
        return None
    if record.get("context_id", "") != req.context:
        log.info("elder_carry_context_changed", extra={"pr": req.pr})
        return None
    if not _passed_on(req, prior):
        return None
    try:
        current = patch_id(fetch_pr_diff())
    except Exception as e:  # noqa: BLE001 - unreadable diff means a normal review
        log.warning("elder_carry_diff_failed", extra={"pr": req.pr, "kind": type(e).__name__})
        return None
    if current != stored:
        log.info("elder_carry_diff_changed", extra={"pr": req.pr, "prior": prior[:8]})
        return None
    return prior, current


def passed_diff_id(
    conclusion: str, *, full_diff: str | None, fetch_pr_diff: Callable[[], str],
) -> str:
    """The patch-id to store beside a finished head: "" unless it passed.

    `full_diff` is the review's own diff when it read the whole PR; a delta
    review passes None and the PR diff is read once. A failed read stores
    no id, so the next head reviews normally."""
    if conclusion != "success":
        return ""
    try:
        return patch_id(full_diff if full_diff is not None else fetch_pr_diff())
    except Exception as e:  # noqa: BLE001 - no id only costs a later carry
        log.warning("elder_carry_patch_id_failed", extra={"kind": type(e).__name__})
        return ""


def store_record(req: CarryRequest, *, diff_id: str) -> None:
    """Move the Living Hunt baseline to this head, with its diff and context ids."""
    from adapters.install_store import put_elder_last_reviewed

    put_elder_last_reviewed(
        install_id=req.installation_id,
        repo=f"{req.owner}/{req.repo_name}",
        pr_number=req.pull_number,
        head_sha=req.head_sha,
        patch_id=diff_id,
        context_id=req.context if diff_id else "",
    )


def carry_if_unchanged(
    req: CarryRequest, *, fetch_pr_diff: Callable[[], str],
) -> dict[str, Any] | None:
    """Publish the carried check and return the dispatch result, or None.

    None means the caller runs the normal review. A check publish failure
    returns `publish_failed`, so the durable consumer redrives."""
    carried = _carried_from(req, fetch_pr_diff)
    if carried is None:
        return None
    prior, diff_id = carried
    threads = carry_forward.list_threads(
        req.installation_id, req.owner, req.repo_name, req.pull_number,
    )
    if threads is None:
        return None
    line = f"carried from {prior[:12]}: diff unchanged"
    conclusion, title, summary = carry_forward.apply_to_check(
        carry_forward.select_carried(threads, ()),
        conclusion="success",
        title=f"Elder clear - {line}",
        summary=(
            f"{line}.\n\nThis head's own diff (merge base to head) has the "
            f"same patch-id as `{prior[:12]}`, which Elder passed, so that "
            "verdict stands without a new model review."
        ),
        blocking_mode=req.blocking,
    )
    try:
        with_install_token_retry(
            req.installation_id,
            lambda token: post_check_run(
                token, req.owner, req.repo_name,
                CheckRunResult(
                    name=CHECK_ELDER, head_sha=req.head_sha, status="completed",
                    conclusion=conclusion, title=title, summary=summary,
                ),
                external_id=f"grug-cr:{req.pr}:{req.head_sha}",
            ),
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.error("elder_carry_publish_failed", extra={"pr": req.pr, "kind": type(e).__name__})
        return {"persona": "code_reviewer", "result": "publish_failed"}
    try:
        record_check_verdict(
            install_id=req.installation_id, persona_key="code_reviewer",
            repo=f"{req.owner}/{req.repo_name}", pr_number=req.pull_number,
            head_sha=req.head_sha, conclusion=conclusion, summary=title,
            findings_count=0, blocking=req.blocking,
        )
        if conclusion == "success":
            store_record(req, diff_id=diff_id)
    except Exception as e:  # noqa: BLE001 - memory never changes the published check
        log.warning("elder_carry_record_failed", extra={"pr": req.pr, "kind": type(e).__name__})
    log.info("elder_verdict_carried", extra={"pr": req.pr, "from": prior[:8], "conclusion": conclusion})
    result = "pass" if conclusion == "success" else "fail"
    return {"persona": "code_reviewer", "result": result, "carried_from": prior}
