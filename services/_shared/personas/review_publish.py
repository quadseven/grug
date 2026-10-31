"""Shared review-publish seam for the check-run + inline-review personas.

Elder (code_reviewer), Guard and Smasher all publish the same two surfaces
(a check run and an inline review) and all fetch the PR diff the same way.
This module is the one owner of that shared machinery, so no persona imports
an underscore-private name from another persona's module:

  * the GitHub reads every review needs (`fetch_pr_diff`, `fetch_file_contents`,
    `prior_finding_keys`);
  * the degradation vocabulary (`is_real_degradation` and friends);
  * `PublishOutcome`, the SINGLE derivation of "did it degrade / did a publish
    surface fail" - the check conclusion, the review event, the persona result
    string and the Activity-feed degraded reason all read off one value, so
    the check run, the log and the feed cannot tell different stories ("no
    lies", ADR-0003).

Rendering of the Elder-voiced review body stays in `code_reviewer.dispatch`.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, replace
from typing import Literal
from urllib.parse import quote

import httpx

from github_app_auth import with_install_token_retry
from github_checks_client import CheckConclusion
from github_reviews_client import ReviewEvent
from personas.code_reviewer.dedup import prior_keys_from_comments
from personas.code_reviewer.persona import CodeReviewEvaluation

__all__ = [
    "BENIGN_DEGRADATIONS",
    "DIFF_FETCH_TIMEOUT",
    "LOW_CONFIDENCE",
    "PARTIAL_COVERAGE",
    "PUBLISHABLE_DEGRADATIONS",
    "PersonaResultStr",
    "PublishOutcome",
    "ReviewMode",
    "fetch_file_contents",
    "fetch_pr_diff",
    "fetch_pr_diff_with_scope",
    "is_blackout",
    "is_low_confidence",
    "is_partial_coverage",
    "is_real_degradation",
    "plan_publish",
    "prior_finding_keys",
]

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.code_reviewer")

# 10s (was 30s) - a GitHub diff fetch is fast; the over-generous 30s let a
# hung fetch alone eat most of the webhook Lambda budget (#252). Well under
# the 60s budget. NOTE: the FULL synchronous path (diff + review LLM + publish
# + dedup + capture + judge, xretries x2 backends) is NOT bounded by 60s - a
# hung backend can blow it; the real fix is async offload (#272).
DIFF_FETCH_TIMEOUT = 10
# The dedup comments-fetch is on the SYNCHRONOUS webhook path (now a 60s
# Lambda budget, #252) and is best-effort: it must not be able to exhaust the
# budget before its own try/except degrades to post-everything. So it
# gets a tight per-request timeout + a low page cap - distinct from the
# 10s diff fetch. 3 pages x 100 = 300 comments covers virtually every
# PR; beyond that, dedup degrades to partial (a few duplicate comments -
# the safe direction) rather than risking a hard handler timeout.
_COMMENT_FETCH_TIMEOUT = 4
_MAX_COMMENT_PAGES = 3

# Literal (not bool) so a future "degraded"/"experimental" mode can't
# silently invert `if not blocking` call sites.
ReviewMode = Literal["advisory", "blocking"]

# Closed set so a new return site can't introduce an undocumented value.
# `review_rejected` (#770): the check-run landed but GitHub returned a
# deterministic 4xx on the inline review. A completed pass minus its
# advisory surface - the durable lane treats it as terminal, never redrives.
PersonaResultStr = Literal[
    "pass", "fail", "skipped", "publish_failed", "review_rejected",
    "unhandled_error",
]

# Degradations that are NOT a failure to review. `no_diff` means there was
# nothing to look at (an empty commit, a docs-only push already covered) - the
# rest of dispatch already treats it as equivalent to clean at the two publish
# gates, and treating it as an alarm here contradicted that.
#
# Observed live on #801: an empty re-trigger commit produced "Grug eyes cloudy
# this pass - read for self", which reads as "the reviewer broke" for a PR
# where simply nothing had changed.
BENIGN_DEGRADATIONS = frozenset({"no_diff"})


# Elder reviewed most of the diff but not all of it. Distinct from a real
# degradation, where it saw nothing: the findings it DID publish are valid,
# diff-anchored evidence, so the surfaces must not tell the author to
# disregard the pass.
PARTIAL_COVERAGE = "partial_review"


# grug#1097: the only answer for some of the diff was a short empty one that
# no other model confirmed, kept because every later model failed. It is a
# clean result, not a trusted one: findings from the rest still publish, the
# check stays advisory, and the surfaces say low confidence rather than
# "could not see".
LOW_CONFIDENCE = "low_confidence_review"

# Degradations whose findings are real, diff-anchored evidence and publish.
PUBLISHABLE_DEGRADATIONS = frozenset({PARTIAL_COVERAGE, LOW_CONFIDENCE})


def is_real_degradation(reason: str | None) -> bool:
    return bool(reason) and reason not in BENIGN_DEGRADATIONS


def is_partial_coverage(reason: str | None) -> bool:
    """Elder saw most of the diff, not none of it."""
    return reason == PARTIAL_COVERAGE


def is_low_confidence(reason: str | None) -> bool:
    """Elder has an answer, but a short empty one nobody confirmed."""
    return reason == LOW_CONFIDENCE


def is_blackout(reason: str | None) -> bool:
    """Elder produced nothing usable this pass - the 'could not see' case.

    Split out from `is_real_degradation` so partial coverage stops borrowing
    the blackout vocabulary. Both are still 'news' for `worth_an_email`; they
    are simply not the same news."""
    return is_real_degradation(reason) and reason not in PUBLISHABLE_DEGRADATIONS

def fetch_pr_diff(
    install_token: str,
    owner: str,
    repo: str,
    pull_number: int,
    *,
    base_sha: str = "",
    head_sha: str = "",
) -> str:
    """GET an immutable base/head diff, falling back when compare is unavailable."""
    diff, _, _ = fetch_pr_diff_with_scope(
        install_token, owner, repo, pull_number,
        base_sha=base_sha, head_sha=head_sha,
    )
    return diff


def _compare_is_clean_ancestor(
    install_token: str, owner: str, repo: str, base_sha: str, head_sha: str,
) -> bool:
    """Whether `base_sha` is a clean ancestor of `head_sha` per GitHub's own
    compare `status` (grug#845) - the only shape where `base_sha..head_sha`
    is a real, reviewable delta rather than a diff between two unrelated
    points in history. After a force-push, rebase or amend, `base_sha` can
    stop being an ancestor of the PR's head; GitHub's compare endpoint still
    answers with a real diff in that case (a "diverged" or "behind"
    comparison), not an error, so nothing about a bare diff fetch reveals
    it. This asks the SAME compare with the JSON media type, which - unlike
    the diff media type - carries a `status` field.

    Fail closed: any 4xx/5xx, a malformed body, or a `status` other than
    "ahead" is NOT a clean ancestor - an unreadable status is not proof of
    a clean history, so the caller falls back to the full PR diff exactly
    as it already does for a compare that 404s/422s outright."""
    try:
        resp = httpx.get(
            f"https://api.github.com/repos/{quote(owner, safe='')}/"
            f"{quote(repo, safe='')}/compare/{quote(base_sha, safe='')}..."
            f"{quote(head_sha, safe='')}",
            headers={
                "Authorization": f"Bearer {install_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=DIFF_FETCH_TIMEOUT,
        )
    except httpx.RequestError:
        return False
    if resp.status_code >= 400:
        return False
    try:
        body = resp.json()
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    # "ahead": base IS an ancestor, head has commits base lacks - the only
    # trustworthy shape. "diverged" (both sides have commits the other
    # lacks - exactly a force-push/rebase/amend) and "behind" (head is
    # BEHIND base) both mean base is not a valid delta-review base point.
    if not (body.get("status") == "ahead" and body.get("behind_by", 0) == 0):
        return False
    # grug#1092: "ahead" is also what a merge-from-main update reports, yet
    # that range carries everything main gained, which is not the PR's work.
    # Any merge commit in the range makes it unreviewable as a delta, so the
    # caller falls back to the PR's own diff. The commit list is capped per
    # page: one shorter than ahead_by cannot prove the absence of a merge.
    commits = body.get("commits")
    ahead_by = body.get("ahead_by", 0)
    if not isinstance(commits, list) or not isinstance(ahead_by, int):
        return False
    if len(commits) < ahead_by:
        return False
    for commit in commits:
        parents = commit.get("parents") if isinstance(commit, dict) else None
        if not isinstance(parents, list) or len(parents) > 1:
            return False
    return True


def fetch_pr_diff_with_scope(
    install_token: str,
    owner: str,
    repo: str,
    pull_number: int,
    *,
    base_sha: str = "",
    head_sha: str = "",
) -> tuple[str, bool, bool]:
    """Return (diff, used_compare, baseline_rejected).

    `used_compare` is whether an immutable compare supplied the diff.
    `baseline_rejected` (#845) is True only when a baseline WAS supplied
    but turned out not to be an ancestor of head (diverged/rewritten
    history) - distinct from merely unavailable (404/422), so the caller
    can report the rewrite instead of silently swallowing it."""
    repo_url = (
        f"https://api.github.com/repos/{quote(owner, safe='')}/"
        f"{quote(repo, safe='')}"
    )
    headers = {
        "Authorization": f"Bearer {install_token}",
        "Accept": "application/vnd.github.diff",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if base_sha and head_sha:
        url = (
            f"{repo_url}/compare/{quote(base_sha, safe='')}..."
            f"{quote(head_sha, safe='')}"
        )
    else:
        url = f"{repo_url}/pulls/{pull_number}"
    resp = httpx.get(
        url,
        headers=headers,
        timeout=DIFF_FETCH_TIMEOUT,
    )
    used_compare = bool(base_sha and head_sha)
    baseline_rejected = False
    if used_compare and resp.status_code in {404, 422}:
        # GitHub can reject compare requests for forked or recently rewritten
        # histories while the PR diff remains readable. Snapshot checks before
        # and after inference still prevent this mutable fallback from being
        # published after the PR moves.
        log.info(
            "immutable_compare_unavailable_falling_back",
            extra={
                "owner": owner,
                "repo": repo,
                "pull_number": pull_number,
                "status_code": resp.status_code,
            },
        )
        resp = httpx.get(
            f"{repo_url}/pulls/{pull_number}",
            headers=headers,
            timeout=DIFF_FETCH_TIMEOUT,
        )
        used_compare = False
    elif used_compare and not _compare_is_clean_ancestor(
        install_token, owner, repo, base_sha, head_sha,
    ):
        # grug#845: the diff-media-type compare above returned 200 with a
        # REAL diff even though base is not an ancestor of head (diverged/
        # rewritten history) - GitHub raises no error for this. Reviewing
        # that diff would silently review the wrong code, reported as a
        # partial review of the right code. Fall back to the full PR diff,
        # same as the 404/422 branch above.
        log.info(
            "immutable_compare_diverged_falling_back",
            extra={"owner": owner, "repo": repo, "pull_number": pull_number},
        )
        resp = httpx.get(
            f"{repo_url}/pulls/{pull_number}",
            headers=headers,
            timeout=DIFF_FETCH_TIMEOUT,
        )
        used_compare = False
        baseline_rejected = True
    resp.raise_for_status()
    return resp.text, used_compare, baseline_rejected


# Full-file context (#336). Cap the number of changed files we fetch full
# content for, so a sweeping PR can't fan out into dozens of API calls or blow
# the LLM context budget. Files beyond the cap (and any that error) degrade to
# diff-only - correctness is unchanged, only the extra context is skipped.
_MAX_CONTEXT_FILES = 20


def fetch_file_contents(
    install_token: str,
    owner: str,
    repo: str,
    paths: tuple[str, ...],
    ref: str,
    *,
    only_404_skips: bool = False,
) -> dict[str, str]:
    """Fetch the full content of each changed file at `ref` (head SHA) so the
    Elder + judge can see mitigations outside the diff hunk (#336 - the #1149
    false-positive class). Best-effort: a per-file fetch failure (deleted file,
    binary, 404, timeout) is skipped, not raised - the review still runs
    diff-only for that file. Returns path -> content for the files that fetched.

    `only_404_skips` is for a caller that reads an absent file as a fact: only
    a file GitHub says is not there is skipped, and any other failure raises.
    """
    contents: dict[str, str] = {}
    for path in paths[:_MAX_CONTEXT_FILES]:
        try:
            # quote the path SEGMENT (safe="/" keeps the dir separators): a
            # filename with a space, `#`, `?`, or unicode would otherwise
            # truncate/reshape the URL -> silent 404 -> diff-only degrade that
            # masks the encoding bug as a "fetch skip".
            resp = httpx.get(
                f"https://api.github.com/repos/{owner}/{repo}/contents/{quote(path, safe='/')}",
                params={"ref": ref},
                headers={
                    "Authorization": f"Bearer {install_token}",
                    # `.raw` returns the file body directly (no base64 JSON).
                    "Accept": "application/vnd.github.raw",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=DIFF_FETCH_TIMEOUT,
            )
            resp.raise_for_status()
            contents[path] = resp.text
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            if only_404_skips and not (
                isinstance(e, httpx.HTTPStatusError)
                and e.response.status_code == httpx.codes.NOT_FOUND
            ):
                raise
            # Deleted-in-PR / binary / rename-old-path / transient - diff-only
            # is the correct, safe degrade. Log so a systemic fetch outage is
            # visible (not silently reverting every review to hunk-blind).
            log.info(
                "code_review_file_fetch_skipped",
                extra={"path": path, "ref": ref, "error": str(e)},
            )
    return contents


def fetch_review_comment_body(
    install_token: str, owner: str, repo: str, comment_id: int,
) -> str:
    """The body of one inline review comment. Raises on any failure, so the
    caller can tell a comment GitHub no longer has from one it could not read."""
    resp = httpx.get(
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        f"/pulls/comments/{int(comment_id)}",
        headers={
            "Authorization": f"Bearer {install_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=DIFF_FETCH_TIMEOUT,
    )
    resp.raise_for_status()
    return str((resp.json() or {}).get("body") or "")


def _fetch_pr_review_comments(
    install_token: str, owner: str, repo: str, pull_number: int,
) -> list[dict]:
    """GET the PR's inline review comments (paginated). Used to dedup
    findings already posted on a prior review pass (#189). Returns the
    raw comment dicts (each carries `path`, `line`, `body`)."""
    out: list[dict] = []
    for page in range(1, _MAX_COMMENT_PAGES + 1):
        resp = httpx.get(
            f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/pulls/{pull_number}/comments",
            params={"per_page": 100, "page": page},
            headers={
                "Authorization": f"Bearer {install_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=_COMMENT_FETCH_TIMEOUT,
        )
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, list):
            # A non-list 200 (proxy interstitial / error envelope) is
            # NOT "no comments" - log it rather than silently treating
            # it as empty (which would let stale findings re-post).
            log.warning(
                "code_review_comments_non_list_body",
                extra={"repo": f"{owner}/{repo}", "pr": pull_number},
            )
            break
        out.extend(body)
        # GitHub returns a short (<per_page) final page; stop there.
        if len(body) < 100:
            break
    else:
        # Hit the page cap without a short page - a PR with >5000 review
        # comments is implausible; log so a runaway pagination is visible
        # rather than silently capping the dedup set.
        log.warning(
            "code_review_comments_page_cap_hit",
            extra={"repo": f"{owner}/{repo}", "pr": pull_number,
                   "max_pages": _MAX_COMMENT_PAGES},
        )
    return out

def prior_finding_keys(
    installation_id: int, owner: str, repo_name: str, pull_number: int,
) -> tuple[frozenset[str], bool]:
    """Fetch prior Grug review comments and build the dedup key set.
    Returns `(keys, degraded)`. Best-effort: a fetch failure returns
    `(frozenset(), True)` - we fall back to posting everything (a
    duplicate comment is a lesser evil than skipping the whole review).
    `degraded` lets the caller distinguish "fetch failed -> empty" from
    the legitimate "no prior comments -> empty" in the dispatch log."""
    try:
        comments = with_install_token_retry(
            installation_id,
            lambda token: _fetch_pr_review_comments(
                token, owner, repo_name, pull_number,
            ),
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.warning(
            "code_review_prior_comments_fetch_failed",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(e).__name__,
            },
        )
        return frozenset(), True
    return frozenset(prior_keys_from_comments(comments)), False


@dataclass(frozen=True)
class PublishOutcome:
    """The publish state x verdict of one review, derived once.

    Built by `plan_publish` BEFORE anything is posted (it fixes the check
    conclusion and review event), then `settled` with what the two publish
    surfaces actually did. Every consumer - the check-run payload, the
    `code_reviewer_dispatched` log's `result`, `record_check_verdict` - reads
    this one value instead of re-deriving the degrade/publish decision.
    """

    conclusion: CheckConclusion
    event: ReviewEvent
    eval_degraded_reason: str | None
    passed: bool
    check_publish_failed: bool = False
    review_publish_failed: bool = False
    review_publish_rejected: bool = False

    def settled(
        self,
        *,
        check_publish_failed: bool = False,
        review_publish_failed: bool = False,
        review_publish_rejected: bool = False,
    ) -> "PublishOutcome":
        """Record what the publish surfaces did; returns a new outcome."""
        return replace(
            self,
            check_publish_failed=check_publish_failed,
            review_publish_failed=review_publish_failed,
            review_publish_rejected=review_publish_rejected,
        )

    @property
    def result(self) -> PersonaResultStr:
        """The per-persona result string.

        Either publish surface failing -> `publish_failed`. The
        `code_reviewer_dispatched` log uses this result; without consulting
        `review_publish_failed`, an inline-comment publish 5xx would let the
        log fire with `result="pass"` while comments never reached GitHub -
        DD dashboards would overstate success rate.

        `review_publish_rejected` (#770) is the permanent-4xx sibling: GitHub
        refused the inline review outright (422 for a comment outside the
        diff), so a retry with the same payload is guaranteed waste. It maps
        to `review_rejected`, which rerun completes rather than redrives. It
        ranks BELOW `publish_failed` (a real outage still owns the redrive)
        and below a degraded eval (whose degradation owns the retry decision
        - `all_failed` must still redrive for the model, whatever GitHub said
        about the deterministic findings that rode along), and above
        pass/fail so the dashboard never counts a lost review as a clean one.
        """
        if self.check_publish_failed or self.review_publish_failed:
            return "publish_failed"
        if self.eval_degraded_reason:
            return "skipped"
        if self.review_publish_rejected:
            return "review_rejected"
        return "pass" if self.passed else "fail"

    @property
    def feed_degraded_reason(self) -> str | None:
        """Degraded reason for the Activity feed row.

        The verdict resolves to `errored` (never a fake pass/block) when the
        LLM degraded OR the check-run never reached GitHub: the feed must not
        claim a verdict for a check that isn't on the PR.
        """
        return self.eval_degraded_reason or (
            "check_publish_failed" if self.check_publish_failed else None
        )


def plan_publish(
    evaluation: CodeReviewEvaluation, *, mode: ReviewMode,
    degraded_fails_closed: bool = False,
) -> PublishOutcome:
    """Single source of truth for the advisory-vs-blocking gate.

    The returned outcome's (conclusion, event) both encode the same mode
    toggle and must stay aligned. Centralising avoids the class of bug where
    the check-run says "failure" but the inline review says "COMMENT" (or
    vice-versa) because the two if/else branches drifted.

    `degraded_fails_closed` (grug#1212, ADR-0023) is Elder's: a required
    check that concludes `neutral` passes on GitHub, so in BLOCK mode a
    degraded Elder review (every model failed, a parse failure, a partial
    or low-confidence review) concludes `failure`. The event stays
    `COMMENT`: a degraded review is not a verdict to request changes on.
    `no_diff` is not a degradation (nothing to review) and keeps neutral.
    Guard and Smasher share this gate and do not opt in.
    """
    conclusion, event = _gate(evaluation, mode, degraded_fails_closed)
    return PublishOutcome(
        conclusion=conclusion,
        event=event,
        eval_degraded_reason=evaluation.degraded_reason,
        passed=evaluation.passed,
    )


def _gate(
    evaluation: CodeReviewEvaluation, mode: ReviewMode, degraded_fails_closed: bool,
) -> tuple[CheckConclusion, ReviewEvent]:
    if (
        degraded_fails_closed
        and mode == "blocking"
        and is_real_degradation(evaluation.degraded_reason)
    ):
        return "failure", "COMMENT"
    # Otherwise a degraded evaluation (LLM outage, parse failure, empty
    # diff) forces advisory regardless of mode.
    if mode == "advisory" or evaluation.degraded_reason:
        return "neutral", "COMMENT"
    if evaluation.conclusion == "failure":
        return "failure", "REQUEST_CHANGES"
    return evaluation.conclusion, "COMMENT"
