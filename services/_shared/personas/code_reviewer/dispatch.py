"""Elder (code-reviewer) persona dispatch orchestration.

End-to-end for one `pull_request` event:
  1. Fetch the PR's unified diff via the GitHub API.
  2. parse_diff → DiffHunks.
  3. review_diff(hunks, installation_id) → LlmReviewResponse.
  4. evaluate_diff(hunks, llm_response) → CodeReviewEvaluation.
  5. Build the inline-comment ReviewResult + the summary CheckRunResult.
  6. Publish via post_check_run + (optionally) post_review.

Advisory-first contract: when `blocking=False` (default per
RepoConfig.code_reviewer_blocking) the check-run conclusion is forced to
`neutral` and the review event to `COMMENT`, even when the evaluation
itself returned `failure`. This lets us turn on the persona for every
install without false-positive LLM findings blocking merges. Operator
flips to blocking via dashboard once trust is established.

Independent from TPM dispatch — the caller (dispatcher.py) calls this
in sequence with TPM but catches exceptions per-persona so one
failing does not skip the other.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from collections import deque
from typing import Any, Literal
from urllib.parse import quote

import httpx

from activity_log import record_check_verdict
from code_review_prompt import RULES
from github_app_auth import get_app_id, with_install_token_retry
from github_checks_client import CheckConclusion, CheckRunResult, post_check_run
from github_reviews_client import (
    InlineComment, ReviewEvent, ReviewResult, get_review_comments, post_review,
)
from llm_client import (
    DeepEscalationDecision,
    Hunk as LlmHunk,
    LlmReviewResponse,
    PrContext,
    decide_deep_escalation,
    review_cohort_char_budget,
    review_diff,
    review_is_staged,
    review_reasoner_diff,
)
from voice_pack import VoiceSelection, entitled_voice
from review_types import EFFORTS, SEVERITIES
from personas.code_reviewer.dedup import (
    dedup_findings, finding_key, parse_rule, prior_keys_from_comments,
    rule_marker,
)
from personas.code_reviewer.diff_parser import (
    DiffHunk, DiffParseError, parse_diff, split_duplicate_hunks,
    split_oversized_hunks, split_reviewable_hunks,
)
from personas.code_reviewer.precedent import (
    class_precision, match_precedent, render_precedent_note,
)
from personas.code_reviewer.claim_check import (
    filter_novel_claim_findings,
    scan_claim_checks,
)
from personas import board
from personas.code_reviewer.complexity import ComplexityScan, scan_complexity_full
from personas.code_reviewer.lint import scan_ruff
from personas.code_reviewer.cross_file import (
    extract_symbols, fetch_cross_file_context,
)
from personas.code_reviewer.omen import build_runtime_context
from personas.code_reviewer.judge import (
    eval_tags, grade_findings, partition_findings, partition_refuted,
    refute_findings, submit_evals,
)
from personas.code_reviewer.persona import (
    CodeReviewEvaluation, Finding, evaluate_diff, with_degradation,
    with_extra_findings, with_findings,
)
from personas.code_reviewer.snapshot import review_freshness_id_from_pr
from personas.code_reviewer.verify import verify_findings
from personas.tribe import CHECK_ELDER
from adapters.install_store import (  # type: ignore
    CommentFindingOrigin,
    put_comment_record,
)

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.code_reviewer")

# Grug's face on the review comment. This dispatch IS the Elder (code-reviewer)
# persona, so it leads with the Elder portrait — hosted at grug.lol/assets and
# rendered via an <img> (GitHub markdown allows width/align) so it's a little
# face, not a giant banner.
_PERSONA = "Elder"
_PERSONA_PORTRAIT = "https://grug.lol/assets/grug_elder.png"

_CHECK_NAME = CHECK_ELDER
# 10s (was 30s) — a GitHub diff fetch is fast; the over-generous 30s let a
# hung fetch alone eat most of the webhook Lambda budget (#252). Well under
# the 60s budget. NOTE: the FULL synchronous path (diff + review LLM + publish
# + dedup + capture + judge, ×retries ×2 backends) is NOT bounded by 60s — a
# hung backend can blow it; the real fix is async offload (#272).
_DIFF_FETCH_TIMEOUT = 10
# The dedup comments-fetch is on the SYNCHRONOUS webhook path (now a 60s
# Lambda budget, #252) and is best-effort: it must not be able to exhaust the
# budget before its own try/except degrades to post-everything. So it
# gets a tight per-request timeout + a low page cap — distinct from the
# 10s diff fetch. 3 pages × 100 = 300 comments covers virtually every
# PR; beyond that, dedup degrades to partial (a few duplicate comments —
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


def _fetch_pr_diff(
    install_token: str,
    owner: str,
    repo: str,
    pull_number: int,
    *,
    base_sha: str = "",
    head_sha: str = "",
) -> str:
    """GET an immutable base/head diff, falling back when compare is unavailable."""
    diff, _ = _fetch_pr_diff_with_scope(
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
            timeout=_DIFF_FETCH_TIMEOUT,
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
    return body.get("status") == "ahead" and body.get("behind_by", 0) == 0


def _fetch_pr_diff_with_scope(
    install_token: str,
    owner: str,
    repo: str,
    pull_number: int,
    *,
    base_sha: str = "",
    head_sha: str = "",
) -> tuple[str, bool]:
    """Return the diff and whether an immutable compare supplied it."""
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
        timeout=_DIFF_FETCH_TIMEOUT,
    )
    used_compare = bool(base_sha and head_sha)
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
            timeout=_DIFF_FETCH_TIMEOUT,
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
            timeout=_DIFF_FETCH_TIMEOUT,
        )
        used_compare = False
    resp.raise_for_status()
    return resp.text, used_compare


def _fetch_current_review_snapshot(
    install_token: str, owner: str, repo: str, pull_number: int,
) -> tuple[str, str, str, bool]:
    """Read current snapshot identity, head SHA, state, and draft status."""
    resp = httpx.get(
        f"https://api.github.com/repos/{quote(owner, safe='')}/"
        f"{quote(repo, safe='')}/pulls/{pull_number}",
        headers={
            "Authorization": f"Bearer {install_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=_DIFF_FETCH_TIMEOUT,
    )
    resp.raise_for_status()
    body = resp.json()
    if not isinstance(body, dict):
        raise ValueError("GitHub PR response is not an object")
    head_sha = str((body.get("head") or {}).get("sha") or "")
    if not head_sha:
        raise ValueError("GitHub PR response has no head SHA")
    return (
        # Freshness, not full snapshot: a base-branch move must not
        # invalidate a review of unchanged code. See review_freshness_id.
        review_freshness_id_from_pr(body),
        head_sha,
        str(body.get("state") or ""),
        bool(body.get("draft", False)),
    )


def _review_snapshot_freshness_failure(
    *,
    installation_id: int,
    owner: str,
    repo_name: str,
    pull_number: int,
    expected_snapshot_id: str,
    expected_head_sha: str,
) -> dict[str, str] | None:
    """Return a non-publishing result when a durable review input is stale."""
    try:
        (
            current_snapshot_id,
            current_head_sha,
            current_state,
            current_draft,
        ) = with_install_token_retry(
            installation_id,
            lambda token: _fetch_current_review_snapshot(
                token, owner, repo_name, pull_number,
            ),
        )
    except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as e:
        log.warning(
            "code_review_freshness_check_failed",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(e).__name__,
            },
        )
        return {
            "persona": "code_reviewer",
            "result": "skipped",
            "degraded_reason": "freshness_check_failed",
        }
    if current_state != "open" or current_draft:
        log.info(
            "code_review_ineligible_before_publish",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "head_sha": current_head_sha[:8],
                "state": current_state,
                "draft": current_draft,
            },
        )
        return {
            "persona": "code_reviewer",
            "result": "skipped",
            "degraded_reason": "pr_ineligible",
        }
    if current_snapshot_id == expected_snapshot_id:
        return None
    # #773: HEAD SHA DECIDES, not the freshness id. The freshness id also
    # hashes title and body, so editing the PR body moved it while the code
    # under review was byte-identical - and this guard then threw the finished
    # review away.
    #
    # That discard is not a lone wasted pass. It re-enqueues against the SAME
    # sha, `elder_in_progress_check_skipped: already_in_progress` declines to
    # post the check-run because one is still open, and the check sits
    # `pending` forever. Live on quadseven/infra#2133 (2026-08-01): Elder
    # passed at 03:00:27, the author edited the body to satisfy Chief's own
    # DoR gate at 03:08, and a docs-only PR went back to pending for 25
    # minutes. Satisfying one grug check should not stall another.
    #
    # Publishing here is safe: `expected_head_sha == current_head_sha` means
    # the diff is identical, so every finding still anchors to real lines in
    # the current code. Only the intent blurb the model ALSO saw has changed.
    # A rewritten intent can legitimately change findings, so the re-enqueued
    # pass still runs - it just no longer has to destroy a good verdict to do
    # it, and the check stays green in the meantime.
    if expected_head_sha and expected_head_sha == current_head_sha:
        log.info(
            "code_review_intent_drift_publishing_anyway",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "head_sha": current_head_sha[:8],
                "reviewed_snapshot_id": expected_snapshot_id[:11],
                "current_snapshot_id": current_snapshot_id[:11],
            },
        )
        return None
    log.info(
        "code_review_stale_before_publish",
        extra={
            "installation_id": installation_id,
            "pr": f"{owner}/{repo_name}#{pull_number}",
            "reviewed_head_sha": expected_head_sha[:8],
            "current_head_sha": current_head_sha[:8],
            "reviewed_snapshot_id": expected_snapshot_id[:11],
            "current_snapshot_id": current_snapshot_id[:11],
        },
    )
    return {
        "persona": "code_reviewer",
        "result": "skipped",
        "degraded_reason": "stale_snapshot",
    }


# Full-file context (#336). Cap the number of changed files we fetch full
# content for, so a sweeping PR can't fan out into dozens of API calls or blow
# the LLM context budget. Files beyond the cap (and any that error) degrade to
# diff-only — correctness is unchanged, only the extra context is skipped.
_MAX_CONTEXT_FILES = 20


def _fetch_base_contents(
    installation_id: int, owner: str, repo_name: str, pull_number: int,
    changed_paths: tuple[str, ...], base_ref: str,
) -> dict[str, str]:
    """Same files at the BASE revision, so complexity can report what THIS PR
    did rather than a function's pre-existing debt (#767).

    Returns {} on any failure, which `scan_complexity` reads as "no base" and
    falls back to the old absolute behaviour. That direction is deliberate:
    going SILENT on a fetch error would hide real regressions, whereas falling
    back only restores the previous noise level.
    """
    if not (base_ref and changed_paths):
        return {}
    try:
        return with_install_token_retry(
            installation_id,
            lambda token: _fetch_file_contents(
                token, owner, repo_name, changed_paths, base_ref
            ),
        ) or {}
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.info(
            "code_review_base_contents_unavailable",
            extra={"pr": f"{owner}/{repo_name}#{pull_number}", "error": str(e)},
        )
        return {}


def _fetch_file_contents(
    install_token: str,
    owner: str,
    repo: str,
    paths: tuple[str, ...],
    ref: str,
) -> dict[str, str]:
    """Fetch the full content of each changed file at `ref` (head SHA) so the
    Elder + judge can see mitigations outside the diff hunk (#336 — the #1149
    false-positive class). Best-effort: a per-file fetch failure (deleted file,
    binary, 404, timeout) is skipped, not raised — the review still runs
    diff-only for that file. Returns path → content for the files that fetched.
    """
    contents: dict[str, str] = {}
    for path in paths[:_MAX_CONTEXT_FILES]:
        try:
            # quote the path SEGMENT (safe="/" keeps the dir separators): a
            # filename with a space, `#`, `?`, or unicode would otherwise
            # truncate/reshape the URL → silent 404 → diff-only degrade that
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
                timeout=_DIFF_FETCH_TIMEOUT,
            )
            resp.raise_for_status()
            contents[path] = resp.text
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            # Deleted-in-PR / binary / rename-old-path / transient — diff-only
            # is the correct, safe degrade. Log so a systemic fetch outage is
            # visible (not silently reverting every review to hunk-blind).
            log.info(
                "code_review_file_fetch_skipped",
                extra={"path": path, "ref": ref, "error": str(e)},
            )
    return contents


# When a PR only retouches k8s/docs comments about settle/deep policy, the
# implementation source of truth may not be in `changed_paths`. Pull these
# known helpers (best-effort) so the claim-check detector can still compare
# numbers. Harmless no-ops on foreign repos (404 -> skip).
_CLAIM_CHECK_POLICY_PATHS: tuple[str, ...] = (
    "services/_shared/personas/code_reviewer/snapshot.py",
    "services/_shared/llm_client.py",
)
_CLAIM_HINT_RE = re.compile(
    r"(?i)(?:settle|steady\s+hunt|swift\s+hunt|deep[_\s-]?diff|"
    r"GRUG_DEEP_DIFF|GRUG_ELDER_SETTLE|min\(\s*base)",
)


def _merge_lint_evidence(evaluation, hunks, file_contents):
    """Fold deterministic ruff findings into the evaluation (#681, epic #707).

    RECALL work: ruff catches the mechanical class the model skims past, and
    cannot hallucinate because the findings are measured rather than
    generated. Merged with the other deterministic sources - i.e. AFTER the
    verification pass and refute gate, both of which exist to police MODEL
    judgment and should not second-guess a linter.

    Extracted rather than inlined so it costs `dispatch_code_review` ONE
    branch instead of four: that function already measures cyclomatic 61 /
    cognitive 62 against caps of 15/25, and its decomposition is #725's job,
    not this slice's. Inert unless GRUG_LINT_EVIDENCE=ruff.
    """
    try:
        lint_findings = scan_ruff(hunks, file_contents or {})
    except Exception:  # noqa: BLE001 - lint evidence must never break a review
        log.warning("code_review_lint_scan_failed", exc_info=True)
        return evaluation
    if not lint_findings:
        return evaluation
    log.info("code_review_lint_findings", extra={"count": len(lint_findings)})
    return with_extra_findings(evaluation, lint_findings)


def _enrich_claim_check_sources(
    installation_id: int,
    owner: str,
    repo_name: str,
    head_sha: str,
    file_contents: dict[str, str],
    hunks: tuple[DiffHunk, ...],
) -> dict[str, str]:
    """Return file_contents plus policy sources needed for claim checks.

    Only fetches when the diff shows claim-ish language and a known policy
    path is missing. Fail-open: any fetch error returns the original map.
    """
    if not any(
        _CLAIM_HINT_RE.search(raw)
        for h in hunks
        for raw in h.body.splitlines()
        if raw.startswith("+")
    ):
        return file_contents
    missing = tuple(p for p in _CLAIM_CHECK_POLICY_PATHS if p not in file_contents)
    if not missing:
        return file_contents
    try:
        extra = with_install_token_retry(
            installation_id,
            lambda token: _fetch_file_contents(
                token, owner, repo_name, missing, head_sha,
            ),
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.info(
            "code_review_claim_check_sources_unavailable",
            extra={"pr": f"{owner}/{repo_name}", "error": str(e)},
        )
        return file_contents
    if not extra:
        return file_contents
    merged = dict(file_contents)
    merged.update(extra)
    return merged


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
            # NOT "no comments" — log it rather than silently treating
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
        # Hit the page cap without a short page — a PR with >5000 review
        # comments is implausible; log so a runaway pagination is visible
        # rather than silently capping the dedup set.
        log.warning(
            "code_review_comments_page_cap_hit",
            extra={"repo": f"{owner}/{repo}", "pr": pull_number,
                   "max_pages": _MAX_COMMENT_PAGES},
        )
    return out


def _prior_finding_keys(
    installation_id: int, owner: str, repo_name: str, pull_number: int,
) -> tuple[frozenset[str], bool]:
    """Fetch prior Grug review comments and build the dedup key set.
    Returns `(keys, degraded)`. Best-effort: a fetch failure returns
    `(frozenset(), True)` — we fall back to posting everything (a
    duplicate comment is a lesser evil than skipping the whole review).
    `degraded` lets the caller distinguish "fetch failed → empty" from
    the legitimate "no prior comments → empty" in the dispatch log."""
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


def _to_llm_hunks(hunks: tuple[DiffHunk, ...]) -> list[LlmHunk]:
    """DiffHunks (parser shape) → llm_client.Hunk (review-input shape).
    The LLM only needs the per-file body; line-number bookkeeping is
    already in `new_lines` for the post-hoc hallucination filter."""
    return [LlmHunk(path=h.file_path, body=h.body) for h in hunks]


def _path_tail(paths: tuple, limit: int = 10) -> str:
    """The `(+N more)` tail for a truncated path list, or empty.

    Extracted because THREE transparency blocks - excluded, oversized and
    duplicate - each rebuilt this same ternary inline, and each one is a branch
    the complexity cap counts. #813's third block pushed _review_transparency to
    cyclomatic 17 against this repo's own cap of 15; folding the repeated shape
    into one place is the fix Elder's deterministic rule asked for.
    """
    return f" (+{len(paths) - limit} more)" if len(paths) > limit else ""


def _coverage_lines(coverage) -> str:
    """The coverage sentence plus any reviewability concerns.

    Split out of _review_transparency for the same cap: the concern loop and
    its heading marker are self-contained and were the densest branch cluster
    in a function that had five other independent blocks.

    "failed" and "never attempted" are reported separately (grug#939). They
    used to share one list, so a cohort the scheduler ran out of budget before
    reaching was announced to the author as a cohort that had been reviewed and
    broken. The two ask for opposite things - a re-run may fix the first and
    can never fix the second - and Elder must not imply it looked at ground it
    never opened.
    """
    unattempted = coverage.unattempted_cohorts
    ran_and_failed = tuple(
        index for index in coverage.failed_cohorts if index not in unattempted
    )
    parts = ""
    if ran_and_failed:
        parts += f"; failed: {', '.join(str(i) for i in ran_and_failed)}"
    if unattempted:
        parts += f"; never attempted: {', '.join(str(i) for i in unattempted)}"
    lines = (
        f"\n\nCoverage: {coverage.completed_cohorts}/{coverage.total_cohorts} "
        f"cohorts completed{parts}."
    )
    for concern in coverage.concerns:
        paths = ", ".join(f"`{_md_code_span(path)}`" for path in concern.paths[:6])
        lines += (
            f"\n- `{_md_code_span(concern.kind)}`: "
            f"{_defused(concern.message)} Paths: {paths}"
        )
    if coverage.concerns:
        marker = lines.index("\n- ", lines.index("Coverage:"))
        lines = f"{lines[:marker]}\n\n**Reviewability**{lines[marker:]}"
    return lines


def _review_transparency(
    evaluation: CodeReviewEvaluation,
    suppressed_count: int,
    excluded_paths: tuple[str, ...],
    oversized_paths: tuple[str, ...] = (),
    duplicate_paths: tuple[tuple[str, str], ...] = (),
) -> str:
    lines = (
        f"\n\nGrug held back {suppressed_count} weak finding(s) his judge doubted."
        if suppressed_count
        else ""
    )
    coverage = evaluation.coverage
    if coverage is not None:
        lines += _coverage_lines(coverage)
    if excluded_paths:
        shown = ", ".join(
            f"`{path.replace(chr(96), '')}`" for path in excluded_paths[:10]
        )
        more = _path_tail(excluded_paths)
        lines += (
            f"\n\nGrug not read {len(excluded_paths)} data/generated "
            f"file(s) - no meat for review there: {shown}{more}."
        )
    if oversized_paths:
        # Distinct sentence from the data/generated line above: these ARE
        # reviewable in principle, they just arrived as a single hunk too
        # big for one bounded cohort. Saying "no meat for review there"
        # would be a lie about a real code change.
        shown = ", ".join(
            f"`{path.replace(chr(96), '')}`" for path in oversized_paths[:10]
        )
        more = _path_tail(oversized_paths)
        lines += (
            f"\n\nGrug not read {len(oversized_paths)} file(s) whose change "
            f"landed as one hunk too big to hold in a single look: "
            f"{shown}{more}. Split the change to get eyes on it."
        )
    if duplicate_paths:
        # Named as PAIRS (#813 acceptance): the exclusion must say WHICH
        # path it duplicates, not just that it was skipped, or "byte-
        # identical" is an unverifiable claim.
        shown = ", ".join(
            f"`{path.replace(chr(96), '')}` (= `{original.replace(chr(96), '')}`)"
            for path, original in duplicate_paths[:10]
        )
        more = _path_tail(duplicate_paths)
        lines += (
            f"\n\nGrug not spend budget re-reading {len(duplicate_paths)} "
            f"file(s) byte-identical to a path already in this diff: "
            f"{shown}{more}."
        )
    return lines


def _clean_review_scope(
    living_range: str,
    excluded_paths: tuple[str, ...],
    oversized_paths: tuple[str, ...] = (),
    duplicate_paths: tuple[tuple[str, str], ...] = (),
) -> str:
    if living_range:
        return (
            "Elder walked the delta diff (full file + cross-file + Omen "
            "runtime signal when mapped). No markings survived the judge. "
            "Code walk steady."
        )
    if excluded_paths or oversized_paths or duplicate_paths:
        return (
            "Elder walked the reviewable diff (full file + cross-file + "
            "Omen when mapped), skipping data/generated paths listed "
            "below. No markings survived the judge on the reviewed paths. "
            "Code walk steady."
        )
    return (
        "Elder walked the whole diff (full file + cross-file + Omen "
        "runtime signal when mapped). No markings survived the judge. "
        "Code walk steady."
    )


def _review_phase_line(review_phase: Literal["tier1", "deep", "dual"]) -> str:
    if review_phase == "tier1":
        return (
            "Tier-1 coder-arm review on the Cave (reasoner may append later "
            "if escalated), graded by the judge, grounded in Lore when prior "
            "tribe history exists."
        )
    if review_phase == "deep":
        return (
            "Deep reasoner arm appended after Tier-1 completed, graded by "
            "the judge, grounded in Lore when prior tribe history exists."
        )
    return (
        "Dual-arm deep review (coder + reasoner on the Cave), graded by "
        "the judge, grounded in Lore when prior tribe history exists."
    )


def _findings_table(evaluation: CodeReviewEvaluation) -> str:
    rows = [
        "| Severity | Effort | File | Line | Rule | Marking |",
        "|---|---|---|---|---|---|",
    ]
    rows.extend(
        f"| {_severity_chip(f.severity)} | {_effort_chip(f.effort)} | "
        f"`{_md_code_span(f.file)}` | {f.line} | "
        f"`{_md_code_span(f.rule_name)}` | {_md_table_cell(f.message)} |"
        for f in evaluation.findings
    )
    return "\n".join(rows)


def _summary_markdown(
    evaluation: CodeReviewEvaluation,
    *,
    suppressed_count: int = 0,
    excluded_paths: tuple[str, ...] = (),
    oversized_paths: tuple[str, ...] = (),
    duplicate_paths: tuple[tuple[str, str], ...] = (),
    living_range: str = "",
    review_phase: Literal["tier1", "deep", "dual"] = "dual",
) -> tuple[str, str]:
    """Render a (title, summary) pair for the check-run output.

    Title is a one-liner status; summary is a Markdown table of findings
    by severity. Operators read this when triaging in GH's Checks tab.
    `suppressed_count` (#467) is how many weak findings the judge held back
    from publication - surfaced as a transparency line so a suppressed
    finding is never a silent gap.
    `living_range` (#557) when set is the prior..head delta Elder reviewed
    (Living Hunt) instead of the full PR base..head.
    `review_phase` (#646): tier1 = coder-only legend; deep = append legend;
    dual = both arms before publish (deep depth / rollback).
    """
    held = _review_transparency(
        evaluation, suppressed_count, excluded_paths, oversized_paths,
        duplicate_paths,
    )
    hunt = (
        (
            "\n\nLiving Hunt: reviewing `"
            + living_range
            + "` (files changed since the last completed Elder pass)."
        )
        if living_range
        else ""
    )

    def hunt_title(title: str) -> str:
        return f"Living Hunt {living_range} - {title}" if living_range else title

    if is_partial_coverage(evaluation.degraded_reason):
        # Reached the PR again only after grug#806: this branch existed but
        # rerun.py's fail-open re-posted "Elder skipped - partial_review" over
        # it, so nobody ever saw it. Same event, same words as the board now.
        title = "WARN Elder review coverage partial"
        return hunt_title(title), (
            "Grug walked part of the diff - some ground did not fit one look, "
            "or a bounded cohort returned nothing usable. Validated markings "
            "from the ground Grug walked are published below; this check stays "
            "advisory. Coverage detail is in the table above."
        ) + held + hunt
    if review_phase == "deep" and is_blackout(evaluation.degraded_reason):
        # grug#848: a blackout in the deep phase is the REASONER arm's -
        # `_async_deep_decision` never starts the deep append over a
        # blacked-out Tier-1 - so the generic "could not see the diff" below
        # would be false here: Tier-1 did see it and its verdict stands. The
        # news is that the second look Tier-1 promised ("reasoner may append
        # later if escalated") never happened, and this neutral is not the
        # reasoner agreeing.
        title = f"WARN Elder deep arm did not run ({evaluation.degraded_reason})"
        return hunt_title(title), (
            "Tier-1 coder-arm review completed and its markings stand. The "
            "deep reasoner arm promised on escalation did not run this pass "
            f"- the mist: `{evaluation.degraded_reason}`. Grug not say the "
            "deeper look agreed; it never looked. This only counsel, merge "
            "not blocked."
        ) + held + hunt
    if evaluation.degraded_reason:
        title = f"WARN Grug eyes clouded ({evaluation.degraded_reason})"
        return hunt_title(title), (
            "Grug Elder could not see the diff this pass. The mist: "
            f"`{evaluation.degraded_reason}`. Grug stay his club — this "
            "only counsel, merge not blocked."
        ) + held + hunt
    if not evaluation.findings:
        title = (
            "Elder clear - no markings"
            if not suppressed_count
            else "Elder clear - weak markings held back"
        )
        scope = _clean_review_scope(
            living_range, excluded_paths, oversized_paths, duplicate_paths,
        )
        return hunt_title(title), ("## Markings Board\n\n" + scope) + held + hunt

    blocking = sum(1 for f in evaluation.findings if f.severity in ("high", "critical"))
    title = f"Elder markings - {blocking} blocking, {len(evaluation.findings)} total"
    title = hunt_title(title)
    table = _findings_table(evaluation)
    phase_line = _review_phase_line(review_phase)
    legend = (
        "## Markings Board\n\n"
        f"{phase_line} "
        "Inline comments carry Fix + agent prompt on each marking.\n\n"
    )
    summary = f"{legend}{table}{held}{hunt}"
    agent = _consolidated_agent_prompt(evaluation)
    if agent:
        summary = f"{summary}\n\n{agent}"
    return title, summary


# GitHub caps check-run summaries at 65536 chars; the findings table is
# unbounded (message-length x count), so the consolidated prompt gets a
# fixed budget well under the cap and truncates by WHOLE findings.
_CONSOLIDATED_PROMPT_BUDGET = 8000


def _consolidated_agent_prompt(evaluation: CodeReviewEvaluation) -> str:
    """One copy-paste prompt covering the findings (#553), deterministic
    and bounded. Truncates by whole findings and SAYS how many were cut -
    a silently-partial prompt would read as the complete work list.

    Returns empty string when there are no findings — never emit a hollow
    "Address each finding below" shell with nothing under it.
    """
    if not evaluation.findings:
        return ""

    header = [
        _AGENT_META_PREAMBLE,
        "",
        "Address each finding below.",
    ]

    body: list[str] = []
    used = sum(len(x) + 1 for x in header)
    included = 0
    for f in evaluation.findings:
        cat = _category_for_rule(f.rule_name)
        entry = (
            f"- {_md_code_span(f.file)}:{f.line} "
            f"[{_severity_chip(f.severity)} | {cat} | "
            f"{_md_code_span(f.rule_name)}] {f.message}"
        )
        if f.suggestion:
            entry += f"\n  Suggested fix: {f.suggestion}"
        if used + len(entry) + 1 > _CONSOLIDATED_PROMPT_BUDGET:
            break
        body.append(entry)
        used += len(entry) + 1
        included += 1
    cut = len(evaluation.findings) - included
    if cut:
        body.append(
            f"(+{cut} more finding(s) - see the findings table above)"
        )
    block = _details_block(
        "Prompt for AI agents (all findings)", "\n".join(header + body)
    )
    # Hard deterministic ceiling: fence growth (backtick-run + 1, twice)
    # and the wrapper are not in the per-entry budget, so cap the WHOLE
    # block - an oversized prompt must degrade loudly, never 422 the
    # check-run publish.
    if len(block) > 2 * _CONSOLIDATED_PROMPT_BUDGET:
        return "(Prompt for AI agents omitted - findings too large; see the table above)"
    return block


# Closed caveman-inspired chrome chips (FLINT-density scanability).
# Identifiers and check *names* stay plain ASCII; Markings surface uses these.
#
# These two are hand-written, so they can drift from the shared vocabulary.
# `_chip_vocabulary_drift()` is what stops that, and it is asserted in tests
# against SEVERITIES/EFFORTS - a level added to review_types but not given a
# chip here would otherwise fall through `.get()` to the bare identifier, and
# nothing would say so.
_SEVERITY_CHIP: dict[str, str] = {
    "critical": "💀 critical",
    "high": "🔥 high",
    "medium": "🟠 medium",
    "low": "👁 low",
}
_EFFORT_CHIP: dict[str, str] = {
    "quick-win": "⚡ quick win",
    "heavy-lift": "🪨 heavy lift",
}


def _chip_vocabulary_drift() -> dict[str, frozenset[str]]:
    """Vocabulary levels with no chip. Empty dict means no drift.

    Returned rather than asserted at import so a drifted chip table degrades
    to the bare identifier in production (ugly) instead of refusing to import
    dispatch (fatal). The test is where it must fail loudly.
    """
    return {
        k: v for k, v in {
            "severity": SEVERITIES - set(_SEVERITY_CHIP),
            "effort": EFFORTS - set(_EFFORT_CHIP),
        }.items() if v
    }


def _severity_chip(severity: str) -> str:
    return _SEVERITY_CHIP.get(severity, severity)


def _effort_chip(effort: str | None) -> str:
    if not effort:
        return "-"
    return _EFFORT_CHIP.get(effort, effort.replace("-", " "))


# Markings v2: rule_name -> ReviewRule for category (bug_class) chips.
_RULES_BY_NAME = {r.name: r for r in RULES}

# Impact one-liners by bug_class (closed taxonomy in code_review_prompt).
_WHY_IT_MATTERS: dict[str, str] = {
    "silent failure": (
        "Errors swallowed hide real failures and make outages hard to debug."
    ),
    "correctness": (
        "Logic bugs ship wrong behavior to users and are expensive to reverse."
    ),
    "async blocker": (
        "Blocking work on async paths freezes event loops and stalls requests."
    ),
    "concurrency": (
        "Race conditions are intermittent, hard to reproduce, and production-only."
    ),
    "test fidelity": (
        "Tests that do not match production behavior give false confidence."
    ),
    "robustness": (
        "Missing guards turn edge cases into crashes under real load."
    ),
    "security": (
        "Security findings can be exploited; treat high/critical before merge."
    ),
    "type design": (
        "Weak types let invalid states compile and fail later at runtime."
    ),
    "maintainability": (
        "Hard-to-follow code slows every future change and hides more bugs."
    ),
    "test coverage": (
        "Unguarded paths regress silently; coverage gaps become merge risk."
    ),
    "performance": (
        "Hot-path waste compounds under concurrency and burns latency budget."
    ),
}

# CR-style agent contract: deterministic, no extra LLM call.
_AGENT_META_PREAMBLE = (
    "Verify each finding against the current code. Fix only if still valid; "
    "skip with a brief reason if already fixed or not applicable. Keep every "
    "change minimal and scoped to the named file/line; do not refactor beyond "
    "the finding. Validate after applying (tests or a focused check)."
)

# Upsert-by-marker issue comment for the Elder review stack (PR timeline).
# The comment Elder finds/upserts is now the shared BOARD (#791), not an
# Elder-private one. Locating by the BOARD marker is what lets Chief and the
# others edit the SAME comment instead of each posting their own - which is
# what turned one review into three emails.
#
# Legacy marker kept for LOCATION only: PRs reviewed before this change carry
# an elder-stack comment, and a fresh board next to it would be the duplicate
# this exists to prevent. Found -> rewritten in place as a board.
_STACK_MARKER = board.BOARD_MARKER
_LEGACY_STACK_MARKER = "<!-- grug-elder-stack -->"
_STACK_COMMENT_TIMEOUT = 10.0


def _category_for_rule(rule_name: str) -> str:
    """Display category from the RULES table; unknown rules stay general."""
    rule = _RULES_BY_NAME.get(rule_name)
    return rule.bug_class if rule is not None else "general"


def _why_it_matters(rule_name: str) -> str:
    cat = _category_for_rule(rule_name)
    return _WHY_IT_MATTERS.get(
        cat,
        "Left unfixed, this can become user-visible breakage or review debt.",
    )


def _details_block(summary: str, content: str) -> str:
    """The one <details> scaffold for agent prompts - the blank lines
    around the fence are load-bearing for GitHub rendering, so both
    surfaces share this instead of hand-building drift-prone copies."""
    return "\n".join(
        ["<details>", f"<summary>{summary}</summary>", "", _fenced(content), "", "</details>"]
    )


def _defused(prose: str) -> str:
    """Neutralize fence-capable runs in PROSE surfaces (comment head,
    table cells): an unterminated ``` or ~~~ in a model message would
    open a fence that swallows the rest of the body - including the
    dedup marker and the suggestion block. Inline code spans (1-2
    backticks) render untouched."""
    out = re.sub(r"`{3,}", "``", prose)
    return re.sub(r"~{3,}", "~~", out)


def _md_code_span(text: str) -> str:
    """Sanitize text for a single backtick-wrapped inline code span.

    Paths and rule names are model-controlled: strip backticks and collapse
    newlines so they cannot terminate the span or inject a second line.
    """
    cleaned = (text or "").replace("`", "")
    cleaned = cleaned.replace('\r', " ").replace('\n', " ")
    cleaned = re.sub(r" +", " ", cleaned).strip()
    return cleaned or "?"


def _md_table_cell(text: str) -> str:
    """Escape review-controlled prose for a GitHub Markdown table cell.

    Pipes break column structure; newlines break the row. Also run the
    prose defuser so an unterminated fence in a finding message cannot
    swallow the rest of the Markings Board.
    """
    cleaned = _defused(text or "")
    cleaned = cleaned.replace("|", '\\|')
    cleaned = cleaned.replace('\r', " ").replace('\n', " ")
    cleaned = re.sub(r" +", " ", cleaned).strip()
    return cleaned


def _fenced(text: str) -> str:
    """Wrap text in a code fence GUARANTEED to contain it: the fence is one
    backtick longer than the longest backtick run inside (CommonMark).
    Model-supplied text with ``` must never break out of the block and
    render live markdown (links, @-mentions that ping) inside an agent
    prompt or the check-run summary."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"


def _agent_prompt_block(f: Finding) -> str:
    """The copy-paste remediation prompt (#553), assembled DETERMINISTICALLY
    from finding fields - no extra LLM call, so it can never hallucinate
    beyond what the finding already claims. Markings v2 adds a CR-style
    verify/skip/minimal meta contract shared with the consolidated prompt."""
    cat = _category_for_rule(f.rule_name)
    content = [
        _AGENT_META_PREAMBLE,
        "",
        f"In `{f.file}:{f.line}` address this {f.severity} / {cat} finding "
        f"(`{f.rule_name}`):",
        f.message,
        f"Why it matters: {_why_it_matters(f.rule_name)}",
        "Fix focus: change only what the finding names; keep the fix "
        "minimal and line-exact; do not refactor beyond it.",
    ]
    if f.suggestion:
        content += ["Suggested fix:", f.suggestion]
    return _details_block("Prompt for AI agents", "\n".join(content))


def _provenance_block(f: Finding) -> str:
    """Render bounded, immutable discovery scope for a finding."""
    if not f.origins:
        return ""
    lines: list[str] = []
    seen: set[tuple] = set()
    for origin in f.origins:
        key = (
            origin.backend,
            origin.model,
            origin.cohort_index,
            origin.cohort_count,
            origin.evidence_paths,
            origin.head_sha,
        )
        if key in seen:
            continue
        seen.add(key)
        scope = ""
        if origin.cohort_index is not None and origin.cohort_count is not None:
            scope = f"; cohort {origin.cohort_index}/{origin.cohort_count}"
        sha = f"; head `{_md_code_span(origin.head_sha[:12])}`" if origin.head_sha else ""
        paths = ", ".join(
            f"`{_md_code_span(path)}`" for path in origin.evidence_paths[:8]
        )
        path_note = f"; evidence paths: {paths}" if paths else ""
        lines.append(
            f"- `{_md_code_span(origin.model)}` ({origin.backend.value}){scope}{sha}{path_note}"
        )
    return _details_block("Evidence and provenance", "\n".join(lines))


def _inline_comment_body(f: Finding, precedent_note: str = "") -> str:
    """Format one finding as a structured Marking (#553 / #617 / Markings v2).

    Shape (Markings Board):
      - severity · category · rule · effort chip
      - What Elder sees (the finding)
      - Why it matters (taxonomy impact one-liner)
      - Where (file:line, always)
      - Fix (committable suggestion when safe, else fenced prose)
      - Lore (precedent + measured confidence when the ledger has history)
      - Prompt for AI agents (CR-style verify + copy-paste repair brief)

    Appends a hidden `grug-rule` marker (rendered invisibly by GitHub)
    so a later `synchronize` push can recognise this comment as a Grug
    finding for dedup (#189) — see dedup.parse_rule. The marker stays
    LAST (dedup.parse_rule reads the last marker in the body)."""
    cat = _category_for_rule(f.rule_name)
    # CR-dense header: severity chip | category | rule | effort chip
    chip = (
        f"{_severity_chip(f.severity)} | _{_md_code_span(cat)}_ | "
        f"`{_md_code_span(f.rule_name)}`"
    )
    if f.effort:
        chip += f" | {_effort_chip(f.effort)}"
    head = (
        f"{chip}\n\n"
        f"**What Elder sees**\n\n{_defused(f.message)}\n\n"
        f"**Why it matters**\n\n{_defused(_why_it_matters(f.rule_name))}\n\n"
        f"**Where:** `{_md_code_span(f.file)}:{f.line}`"
    )
    if precedent_note:
        # #555: ledger-grounded citation + measured-confidence chip, as a
        # blockquote under the message. _defused() neutralizes any user text
        # that reached the note via file paths; the note itself is our own
        # rendered string.
        head += f"\n\n**Lore**\n\n> {_defused(precedent_note)}"
    # strip wrapping NEWLINES only (not spaces): GitHub commits the block
    # verbatim as the full replacement line, so leading indentation must
    # survive, but a bare "\n\n\n" suggestion must not slip through as a
    # blank-line commit (FLINT finding on #558).
    stripped_suggestion = f.suggestion.strip("\n\r") if f.suggestion else ""
    if (
        f.suggestion
        and "```" not in f.suggestion
        and "\n" not in f.suggestion.strip()
        and stripped_suggestion
    ):
        # GitHub-native committable block - one click REPLACES the single
        # anchored line. Committable ONLY when the suggestion is itself
        # single-line and fence-safe: the comment anchors one line, so a
        # multi-line suggestion applied there duplicates the following
        # original lines - confident-looking one-click corruption.
        body = (
            f"{head}\n\n**Fix** (one-click, line-exact):\n"
            f"```suggestion\n{stripped_suggestion}\n```"
        )
    elif f.suggestion:
        # Multi-line or fence-bearing: fenced prose with an explicit scope
        # label. _fenced() contains ANY payload (a suggestion containing
        # ```suggestion would otherwise render as a live committable block
        # - the sanitizer must not route the payload around itself).
        body = (
            f"{head}\n\n**Fix** "
            f"(anchored at line {f.line} - verify scope before applying):\n"
            f"{_fenced(f.suggestion)}"
        )
    else:
        body = head
    provenance = _provenance_block(f)
    if provenance:
        body += f"\n\n{provenance}"
    return f"{body}\n\n{_agent_prompt_block(f)}\n\n{rule_marker(f.rule_name)}"


# Rows of the findings table carried in the board body. Ten, because the body
# IS the email and <details> does not fold in Gmail (see _review_stack_body).
_STACK_TABLE_ROWS = 10


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


def is_real_degradation(reason: str | None) -> bool:
    return bool(reason) and reason not in BENIGN_DEGRADATIONS


def is_partial_coverage(reason: str | None) -> bool:
    """Elder saw most of the diff, not none of it."""
    return reason == PARTIAL_COVERAGE


def is_blackout(reason: str | None) -> bool:
    """Elder produced nothing usable this pass - the 'could not see' case.

    Split out from `is_real_degradation` so partial coverage stops borrowing
    the blackout vocabulary. Both are still 'news' for `worth_an_email`; they
    are simply not the same news."""
    return is_real_degradation(reason) and not is_partial_coverage(reason)


# Degradations the durable lane re-drives (rerun's `_RETRYABLE_SKIP_REASONS`
# imports this). Defined HERE because the email decision needs it and
# `_shared` is the lower layer - rerun already imports from this module.
RETRIED_DEGRADATIONS = frozenset({
    "all_failed", "parse_failed", "fetch_or_parse_failed",
})


# GitHub will not serve this diff at all (#1047): the diff media type
# answers 406 when a pull request's diff is past the API's size limit, and
# the same fetch gets the same 406 on every attempt. So this is deliberately
# NOT a member of RETRIED_DEGRADATIONS: before this reason existed, one such
# pull request took all five redrives of the durable lane into the DLQ, one
# attempt every ten minutes. The degraded check it publishes is terminal, so
# rerun treats it as self-completing.
#
# Only that 406, recognised by status AND GitHub's `too_large` error code
# (see `_is_diff_too_large`). Other 4xx on this fetch (a 403 permission, a
# 404 visibility blip, a 422, a 406 for any other reason) stay retryable: a
# retry or a DLQ page is the right outcome for those, and completing them
# with an author-facing "split the PR" would misname the cause.
DIFF_TOO_LARGE = "diff_too_large"


def worth_an_email(evaluation: CodeReviewEvaluation) -> bool:
    """Should a review that found THIS be allowed to create the board?

    Creating a comment mails the author; editing one does not. So this is the
    question "is there news?", and a clean review is not news - the green
    check-run already says so, in the place people look for it.

    A REAL degradation is news in the other direction: "Grug could not review
    this" is precisely the case where silence would be misread as approval.
    `no_diff` is not that - nothing to review is not a failure to review.

    But a degradation the durable lane will RETRY is not news yet, whatever
    else this pass happened to collect. Measured on quadseven/infra#2157: the
    LLM half was cancelled mid-flight (`all_failed`), the deterministic
    complexity scanner still produced one finding, and that finding was enough
    to create the board - mailing "Grug eyes cloudy this pass - read for self"
    plus a cyclomatic nitpick on a PR fixing a packet-mode bootstrap deadlock.
    The retry six minutes later came back clean from the Cave, but GitHub does
    not mail an EDIT, so that inbox keeps the degraded verdict permanently.

    Deterministic findings are not lost by waiting: the retry re-runs the same
    scanners and republishes them alongside whatever the LLM finds.

    The trade is explicit. If every retry also fails, the author gets no board
    at all - but the check-run still carries the degraded verdict (rerun
    fail-opens it to neutral rather than leaving it in_progress), so the
    signal survives in the place a required check is read. A false "Grug could
    not see" is worse than a quiet one, because it trains the author to
    discount the surface entirely.
    """
    if evaluation.degraded_reason in RETRIED_DEGRADATIONS:
        return False
    return bool(evaluation.findings) or is_real_degradation(
        evaluation.degraded_reason,
    )


def _stack_detail_lines(
    review_phase: str, living_range: str, suppressed_count: int,
    head_sha: str = "", base_sha: str = "",
) -> str:
    """The two facts a skeptical reader asks for: what was looked at, and what
    was withheld. Everything else that used to live here was cut as noise.

    #673: "looked at" used to appear ONLY when a Living Hunt delta existed, so
    a full base..head review - the common path, and every first review of a
    PR - answered "what did you read" with nothing. Both cases now disclose.
    """
    lines = []
    if review_phase == "tier1":
        # Not jargon for its own sake: a clear tier-1 pass is PROVISIONAL, and
        # a reader who does not know that will read it as final.
        lines.append("- Fast look only - deep look may add more.")
    # ONE implementation of "what did you read", in board.review_scope_line.
    # This branch used to re-derive it with its own wording and its own
    # `head_sha[:7]`, so the same fact appeared in two vocabularies.
    scope = board.review_scope_line(
        living_range=living_range, base_sha=base_sha, head_sha=head_sha,
    )
    if scope:
        lines.append(f"- Looked at: {scope}")
    if suppressed_count:
        lines.append(f"- Grug swallow {suppressed_count} weak thought(s).")
    return "\n".join(lines)


def _stack_status_line(
    evaluation: CodeReviewEvaluation, n: int, sev_bits: str,
) -> str:
    """The board section's one-line status.

    Extracted from `_review_stack_body` when adding the partial-coverage
    branch pushed that function to cyclomatic 17 against a cap of 15 (caught
    by Elder on grug#807). The coverage-vs-findings decision is a self
    contained question, so it reads better named than inlined."""
    if is_partial_coverage(evaluation.degraded_reason):
        return (
            f"Partial coverage - {n} marking(s) from the ground Grug walked"
            if n
            else "Partial coverage - no markings on the ground Grug walked"
        )
    if is_real_degradation(evaluation.degraded_reason):
        return f"Degraded (`{evaluation.degraded_reason}`) - advisory only"
    if n == 0:
        return "Clear - no markings published"
    return f"**{n} actionable marking(s)** ({sev_bits})"


_UNWALKED_PATHS_SHOWN = 8


def _unwalked_ground_note(evaluation: CodeReviewEvaluation) -> str:
    """Name the files a partial pass did not finish. "Some ground not
    walked" with no location gave the reader nothing to act on - they could
    neither check those files themselves nor tell whether it mattered."""
    paths = evaluation.coverage.unwalked_paths if evaluation.coverage else ()
    if not paths:
        return (
            "Some ground not walked this pass - part of the diff did not fit "
            "one look. What Grug did walk is above. Grug not say trail safe "
            "for ground Grug not walk."
        )
    shown = ", ".join(f"`{p}`" for p in paths[:_UNWALKED_PATHS_SHOWN])
    extra = len(paths) - _UNWALKED_PATHS_SHOWN
    more = f" (+{extra} more)" if extra > 0 else ""
    return (
        f"Grug did not finish walking {shown}{more}. Markings above cover "
        "the rest. Grug not say trail safe in those files - comment "
        "`/grug improve` to walk them again."
    )


def _stack_closing_note(evaluation: CodeReviewEvaluation) -> list[str]:
    """The board section's tail: what to do next, or why there is nothing.

    Exactly one of these applies, and a clean non-degraded pass gets NOTHING -
    it used to get "No agent prompt - nothing to remediate.", the fifth
    separate way one body said "no findings"."""
    agent = _consolidated_agent_prompt(evaluation)
    if agent:
        # Only when there is something to fix - empty "Address each finding"
        # shells are noise (and look broken).
        return [
            "",
            agent,
            "",
            "---",
            "",
            "Inline comments carry Fix + agent prompt on each marking. "
            "Autofix push is not enabled - apply suggestions or hand the agent "
            "prompt to your coding agent.",
            "",
        ]
    if is_partial_coverage(evaluation.degraded_reason):
        # Elder DID review - just not all of it. "Grug could not see" would
        # discard real work and read as a tool failure.
        return ["", "---", "", _unwalked_ground_note(evaluation), ""]
    if is_real_degradation(evaluation.degraded_reason):
        # Degraded with empty findings is not a clean review, and the one thing
        # that must never happen is a reader taking it for one.
        return [
            "",
            "---",
            "",
            "Review degraded - no usable findings were produced. "
            "Grug not say trail safe. Grug say Grug could not see.",
            "",
        ]
    return []


def _review_stack_body(
    evaluation: CodeReviewEvaluation,
    *,
    conclusion: CheckConclusion,
    living_range: str = "",
    suppressed_count: int = 0,
    review_phase: Literal["tier1", "deep", "dual"] = "dual",
    pr_title: str = "",
    head_sha: str = "",
    base_sha: str = "",
) -> str:
    """PR-timeline review stack comment (Markings v2 / FLINT-style shell).

    Deterministic markdown only — no extra LLM. Upserted by marker so
    synchronize edits in place rather than spamming the PR.
    """
    findings = evaluation.findings
    n = len(findings)
    by_sev: dict[str, int] = {}
    for f in findings:
        by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
    sev_bits = ", ".join(
        f"{k}={by_sev[k]}" for k in ("critical", "high", "medium", "low") if k in by_sev
    ) or "none"
    status_line = _stack_status_line(evaluation, n, sev_bits)

    # Ten, not twenty-five. <details> does NOT fold in Gmail - the raw
    # notification HTML for grug#799 carried a real <details>, and Gmail
    # rendered every child of it flat. So the fold buys nothing in the surface
    # a human actually reads, and the only thing that keeps the mail short is
    # the content being short. Each row is also one line, not a diff: the
    # evidence lives on the inline comment anchored to the actual code.
    rows = [
        "| Severity | File | Line | Rule |",
        "|---|---|---|---|",
    ]
    for f in findings[:_STACK_TABLE_ROWS]:
        rows.append(
            f"| {f.severity} | `{_md_code_span(f.file)}` | {f.line} | "
            f"`{_md_code_span(f.rule_name)}` |"
        )
    if n > _STACK_TABLE_ROWS:
        rows.append(f"| +{n - _STACK_TABLE_ROWS} more | | | |")
    table = "\n".join(rows) if findings else ""

    # The BOARD (#791). What a human receives by email is exactly this body at
    # CREATION time - GitHub never mails an edit - so the verdict leads and all
    # evidence is folded. Previously the first thing posted was a file table
    # and a diagram, which made the email a diff dump with the conclusion
    # buried under it.
    blocking = sum(1 for f in findings if f.severity in ("high", "critical"))
    advisory = len(findings) - blocking
    parts: list[str] = []
    # Phase, Status and Check-run used to live in the fold. All three were cut:
    # Status restated the summary line verbatim, Phase ("Tier-1 coder arm") is
    # internal vocabulary that means nothing to the author, and the check-run
    # name is already a row in the PR's own check list. Scope and judge
    # suppression stay - they are the two questions a skeptical reader asks:
    # what did you look at, and what did you decide not to tell me.
    #
    # BLANK LINE between table and bullets is required: a list starting on the
    # line after a table row is swallowed into the table.
    detail = "\n\n".join(p for p in [
        table,
        _stack_detail_lines(
            review_phase, living_range, suppressed_count, head_sha, base_sha,
        ),
    ] if p)
    if detail:
        parts.extend([
            "",
            board.collapse(
                f"Grug Elder - {status_line.replace('**','')} - check `{conclusion}`",
                detail,
                # Open when something needs doing, folded when it does not: a
                # clean review should cost no clicks and no scrolling.
                open_by_default=bool(blocking),
            ),
        ])
    # No detail at all (a clean pass with no scope or suppression note) gets no
    # fold. An empty <details> renders as a disclosure triangle hiding nothing,
    # which reads as a bug in the tool.
    parts.extend(_stack_closing_note(evaluation))

    # Assemble a REAL board: marker, a delimited header, and Elder's content
    # inside its own `grug-sec:elder` region.
    #
    # This used to be a flat `marker + header + content` string. It looked
    # right and was wrong: with no region delimiters,
    # `board.extract_section(body, "elder")` returned None, so
    # `_upsert_review_stack_comment` skipped the merge path entirely and
    # PATCHed the COMPLETE body - deleting Chief's section on every pass.
    # Verified against the live boards on #797, #798 and #799: all three carry
    # `grug-board` and not one `grug-sec:` region. The board_client unit tests
    # never caught it because they call the client directly; nothing asserted
    # that what Elder actually produces is something the client can merge.
    body = board.set_header(
        board.new_board(),
        board.render_header(
            pr_title, blocking, advisory,
            degraded=is_blackout(evaluation.degraded_reason),
            partial=is_partial_coverage(evaluation.degraded_reason),
        ),
    )
    return board.upsert_section(body, "elder", "\n".join(parts).strip())


def _find_stack_comment_id(
    token: str, owner: str, repo: str, pr_number: int,
) -> int | None:
    """Locate our Elder stack issue comment by marker + app id."""
    own_app_id = get_app_id()
    base = (
        f"https://api.github.com/repos/{quote(owner, safe='')}/"
        f"{quote(repo, safe='')}"
    )
    page = 1
    while page <= 20:
        resp = httpx.get(
            f"{base}/issues/{pr_number}/comments",
            params={"per_page": 100, "page": page},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=_STACK_COMMENT_TIMEOUT,
        )
        resp.raise_for_status()
        batch = resp.json()
        for c in batch:
            app = c.get("performed_via_github_app")
            if not app or str(app.get("id")) != own_app_id:
                continue
            body = c.get("body") or ""
            if _STACK_MARKER in body or _LEGACY_STACK_MARKER in body:
                return int(c["id"])
        if len(batch) < 100:
            return None
        page += 1
    return None


def _upsert_review_stack_comment(
    token: str, owner: str, repo: str, pr_number: int, body: str,
    *, create_if_absent: bool = True,
) -> None:
    """PATCH existing stack comment or POST a new one (Teller discipline).

    `create_if_absent=False` suppresses only the POST: a board already on the
    PR is still refreshed. That asymmetry is the whole email budget - POST
    mails the author, PATCH is silent - so a review with no news corrects a
    stale board without ringing anyone's phone.

    Concurrent dispatch (redelivery / race) can TOCTOU: both find nothing and
    both POST. Mitigations: re-find immediately before write; if POST fails
    or races, re-find and PATCH the winner. with_install_token_retry only
    retries 401 (not generic 5xx), so successful POSTs are not re-fired.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    base = (
        f"https://api.github.com/repos/{quote(owner, safe='')}/"
        f"{quote(repo, safe='')}"
    )

    def _patch(comment_id: int) -> None:
        httpx.patch(
            f"{base}/issues/comments/{comment_id}",
            json={"body": body},
            headers=headers,
            timeout=_STACK_COMMENT_TIMEOUT,
        ).raise_for_status()

    # MERGE, never replace. Elder used to PATCH a COMPLETE body, which deleted
    # any section another persona had written (#791 slice 4). Hand the client
    # only Elder's own region + the header; it does read-modify-write.
    section = board.extract_section(body, "elder")
    header = board.extract_header(body)
    if section is not None:
        try:
            from personas import board_client
            board_client.upsert_board_section(
                token, owner, repo, pr_number,
                key="elder", section=section, header=header,
                app_id=get_app_id(), create_if_absent=create_if_absent,
            )
            return
        except (httpx.HTTPStatusError, httpx.RequestError):
            # Fall through to the whole-body path rather than lose the review
            # entirely: a clobbered section beats no comment at all.
            log.warning(
                "board_upsert_failed_falling_back",
                extra={"pr": f"{owner}/{repo}#{pr_number}"},
            )

    existing = _find_stack_comment_id(token, owner, repo, pr_number)
    if existing is not None:
        _patch(existing)
        return
    # Second look immediately before create (shrink race window).
    existing = _find_stack_comment_id(token, owner, repo, pr_number)
    if existing is not None:
        _patch(existing)
        return
    if not create_if_absent:
        # Nothing on the PR and nothing worth mailing: stay silent. The
        # fallback path has to honour this too, or a transient board_client
        # failure would leak exactly the email the caller declined to send.
        log.info(
            "elder_stack_create_declined_nothing_to_say",
            extra={"pr": f"{owner}/{repo}#{pr_number}"},
        )
        return
    try:
        httpx.post(
            f"{base}/issues/{pr_number}/comments",
            json={"body": body},
            headers=headers,
            timeout=_STACK_COMMENT_TIMEOUT,
        ).raise_for_status()
    except httpx.HTTPStatusError:
        # Lost the race or transient create error: heal via PATCH if present.
        existing = _find_stack_comment_id(token, owner, repo, pr_number)
        if existing is not None:
            _patch(existing)
            return
        raise


def _resolve_result(
    evaluation: CodeReviewEvaluation,
    *,
    check_publish_failed: bool,
    review_publish_failed: bool = False,
    review_publish_rejected: bool = False,
) -> PersonaResultStr:
    """Pick the per-persona result string. Symmetric twin of
    `_publish_shape` (publish state ↔ verdict mapping). Centralising
    avoids the drift class where check-run says one thing and the
    persona result says another.

    Either publish surface failing → `publish_failed`. The
    `code_reviewer_dispatched` log uses this result; without
    consulting `review_publish_failed`, an inline-comment publish 5xx
    would let the log fire with `result="pass"` while comments never
    reached GitHub — DD dashboards would overstate success rate.

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
    if check_publish_failed or review_publish_failed:
        return "publish_failed"
    if evaluation.degraded_reason:
        return "skipped"
    if review_publish_rejected:
        return "review_rejected"
    return "pass" if evaluation.passed else "fail"


def _publish_shape(
    evaluation: CodeReviewEvaluation, *, mode: ReviewMode,
) -> tuple[CheckConclusion, ReviewEvent]:
    """Single source of truth for the advisory-vs-blocking gate.

    Returns (check_conclusion, review_event) — both encode the same
    mode toggle and must stay aligned. Centralising avoids the class
    of bug where the check-run says "failure" but the inline review
    says "COMMENT" (or vice-versa) because the two if/else branches
    drifted.
    """
    # Any degraded evaluation (LLM outage, parse failure, empty diff)
    # forces advisory regardless of mode — Elder cannot block a PR on
    # infrastructure flakiness or a non-reviewable shape.
    if mode == "advisory" or evaluation.degraded_reason:
        return "neutral", "COMMENT"
    if evaluation.conclusion == "failure":
        return "failure", "REQUEST_CHANGES"
    return evaluation.conclusion, "COMMENT"


def _precedent_notes_for(
    repo_full: str, findings: "tuple[Finding, ...] | list[Finding]",
) -> dict[str, str]:
    """Ledger-grounded precedent note per finding, keyed by finding_key (#555).

    Best-effort: any store/parse failure yields {} so a review is never blocked
    by a missing or slow ledger - the finding just posts without its citation.
    """
    try:
        from adapters.install_store import list_ledger_rows  # type: ignore
        from ledger import parse_row

        raw = list_ledger_rows(repo_full) or []
        rows = [r for r in (parse_row(d) for d in raw) if r is not None]
        if not rows:
            return {}
        precisions = class_precision(rows)
        out: dict[str, str] = {}
        for f in findings:
            note = render_precedent_note(
                match_precedent(
                    finding_class=f.rule_name,
                    finding_path=f.file,
                    ledger_rows=rows,
                    precisions=precisions,
                )
            )
            if note:
                out[finding_key(f.file, f.line, f.rule_name)] = note
        return out
    except Exception as e:  # noqa: BLE001 - precedent is enrichment, never load-bearing
        log.info("precedent_notes_unavailable", extra={"repo": repo_full, "kind": type(e).__name__})
        return {}


def _build_review_result(
    evaluation: CodeReviewEvaluation, *, head_sha: str, event: ReviewEvent,
    prior_keys: frozenset[str] = frozenset(),
    precedent_notes: dict[str, str] | None = None,
) -> tuple[ReviewResult | None, tuple[Finding, ...]]:
    """Build the ReviewResult, or (None, ()) if nothing NEW to post.

    Skips entirely on fully degraded responses. A partial staged review still
    publishes its validated findings, but stays advisory. `prior_keys`
    (non-empty only on a synchronize/reopened push) dedups findings already commented
    on unchanged lines (#189) — so a re-review doesn't flood the PR with
    duplicate inline comments. If every finding was already posted,
    returns (None, ()) (nothing new). NOTE: dedup affects only the inline
    REVIEW; the check-run summary/conclusion still reflect ALL current
    findings (the bugs are still there).

    The second return value is `new_findings` itself, in the exact order
    used to build `comments` below - one `InlineComment` per finding, same
    order. Callers MUST thread it into `_capture_review_comments` as
    `findings=`: that function pairs GitHub's freshly-fetched comments with
    this tuple by POSITION (grug#967), so an order mismatch here would
    mis-attribute captured comments."""
    if evaluation.degraded_reason not in (None, "", "partial_review"):
        return None, ()
    new_findings = dedup_findings(evaluation.findings, prior_keys)
    if not new_findings:
        return None, ()
    notes = precedent_notes or {}
    comments = tuple(
        InlineComment(
            path=f.file, line=f.line,
            body=_inline_comment_body(
                f, precedent_note=notes.get(finding_key(f.file, f.line, f.rule_name), ""),
            ),
        )
        for f in new_findings
    )
    return ReviewResult(
        commit_id=head_sha,
        event=event,
        body=(
            f'<img src="{_PERSONA_PORTRAIT}" width="46" align="left" alt="Grug {_PERSONA}" />'
            f"\n\n**Grug {_PERSONA}** gaze upon your PR · {len(comments)} finding(s)"
        ),
        comments=comments,
    ), new_findings


def _comment_record_origins(
    finding: Finding, review_span_context: dict | None,
) -> tuple[list[CommentFindingOrigin], dict | None]:
    """The finding's origin list plus the one span context a captured
    CommentRecord attributes a later reaction to. Split out of
    `_capture_comment_records` purely to shed one branch from that
    function's own complexity score (#967 follow-up); no behavior change."""
    finding_origins: list[CommentFindingOrigin] = [
        {
            "backend": origin.backend.value,
            "model": origin.model,
            "review_span_context": origin.review_span_context,
        }
        for origin in finding.origins
    ]
    traced_origins = [
        origin for origin in finding_origins
        if origin["review_span_context"] is not None
    ]
    if traced_origins:
        # Preserve the historical scalar for old poller versions, but use
        # an actual origin for this finding rather than the response-level
        # first success (which may be a different backend).
        return finding_origins, traced_origins[0]["review_span_context"]
    if finding.origins:
        # Provenance exists but trace export failed. Unknown is more honest
        # than attributing the reaction to another backend's span.
        return finding_origins, None
    return finding_origins, review_span_context


def _capture_comment_records(
    comments: list[dict],
    findings: tuple[Finding, ...],
    *,
    install_id: int,
    repo: str,
    pr_number: int,
    review_span_context: dict | None,
    head_sha: str,
    author_login: str,
) -> int:
    """Persist each posted inline comment as a CommentRecord for later
    reaction polling (#247). Matches each comment to the Finding that
    produced it by (file, RULE) — recovered from the comment's `path` and
    its hidden `<!-- grug-rule:NAME -->` marker (parsed by `parse_rule`,
    the same marker `dedup` uses) — never by re-deriving a line number from
    anything GitHub computes.

    grug#967: an earlier version of this function matched by
    `finding_key(file, line, rule)`, looking `line`/`original_line` up on
    the fetched comment. GitHub computes a JUST-created review comment's
    `line`/`original_line` asynchronously on its own backend - live testing
    showed a comment can carry ONLY `position`/`original_position`, with
    neither human-readable field populated, for over 30 seconds. We
    already know each comment's file+line: it is exactly what
    `_build_review_result` told GitHub to place it at when building this
    same review. Reading it back from GitHub's response was always
    redundant, and the one field this function actually needs from that
    response — the comment `id`, for reaction polling — is present
    immediately, no matter how long the human-readable fields take.

    (file, rule) can collide — two findings for the SAME rule can land in
    the same file at different lines in one batch — so each (file, rule)
    maps to a FIFO queue of findings in the order `_build_review_result`
    listed them, and each matching comment claims the next one off that
    queue. This assumes GitHub returns one review's own comments in the
    order they were submitted (true for comments belonging to a single
    review — see `get_review_comments`'s docstring), but only WITHIN a
    (file, rule) group, not across the whole list — so an unrelated extra
    comment, or a comment for a rule that already exhausted its queue,
    is simply skipped, same as a stale prior-review comment always was.
    Best-effort per comment: a single DDB blip is skipped, never raised.
    Returns count persisted."""
    queues: dict[tuple[str, str], deque[Finding]] = {}
    for f in findings:
        queues.setdefault((f.file, f.rule_name), deque()).append(f)

    persisted = 0
    for c in comments:
        cid, path = c.get("id"), c.get("path")
        if cid is None or path is None:
            continue
        rule = parse_rule(c.get("body", ""))
        if rule is None:
            continue
        queue = queues.get((path, rule))
        if not queue:
            continue
        finding = queue.popleft()
        finding_origins, fallback_span_context = _comment_record_origins(
            finding, review_span_context,
        )
        try:
            put_comment_record(
                install_id=install_id,
                comment_id=int(cid),
                repo=repo,
                pr_number=pr_number,
                review_span_context=fallback_span_context,
                finding_tags=eval_tags(finding),
                finding_origins=finding_origins,
                finding_text=finding.message,
                head_sha=head_sha,
                author_login=author_login,
                trust_reactors=True,
            )
            persisted += 1
        except Exception as e:  # noqa: BLE001 — per-comment: one DDB blip
            # (throttle) must not drop the rest of the batch.
            log.warning(
                "comment_record_put_failed",
                extra={"install_id": install_id, "comment_id": cid,
                       "kind": type(e).__name__},
            )
    return persisted


def _safe_attr(obj: object, name: str) -> object | None:
    """Read an attribute that may be a property raising instead of returning.

    grug#891: `httpx.RequestError.request` raises RuntimeError when unset, so
    `getattr(err, "request", None)` re-raises rather than yielding the default.
    Caught by a test that fed a bare ConnectError through this path."""
    try:
        return getattr(obj, name, None)
    except Exception:  # noqa: BLE001 - a property that throws is exactly the case
        return None


_ERROR_BODY_PREVIEW_CHARS = 300


def _http_error_detail(error: Exception) -> dict[str, object]:
    """Extra log fields that name WHY an HTTP call failed, not just its class.

    grug#891: three Elder jobs dead-lettered on a GitHub 403 and the only
    record was `kind=HTTPStatusError`. Diagnosing it took two days and
    succeeded only because an unrelated `httpx` INFO line happened to carry
    the URL and status. A 403 on `/compare` while `/pulls` returns 200 on the
    same token is a SECONDARY RATE LIMIT, and that is legible from one line
    only if the status, the URL and the body are recorded.

    Bounded: a GitHub error body is small, but an upstream HTML interstitial
    is not, so the preview is truncated. Same defect class as grug#881
    (`kind=TypeError`, no message) and grug#883 (non-json, no payload)."""
    detail: dict[str, object] = {"err": str(error)[:_ERROR_BODY_PREVIEW_CHARS]}
    # httpx exposes `.request`/`.response` as PROPERTIES that RAISE
    # RuntimeError when unset (a ConnectError built without a request has no
    # `.request`), so `getattr(..., None)` does NOT protect this - it re-raises.
    # A diagnostic helper that can throw inside a log call is worse than none.
    response = _safe_attr(error, "response")
    if response is not None:
        status = getattr(response, "status_code", None)
        if status is not None:
            detail["status"] = status
        # GitHub signals a secondary rate limit in headers, not the status
        # alone - a plain 403 and a throttle are the same code.
        headers = getattr(response, "headers", None) or {}
        for header in ("retry-after", "x-ratelimit-remaining", "x-ratelimit-reset"):
            value = headers.get(header) if hasattr(headers, "get") else None
            if value is not None:
                detail[header.replace("-", "_")] = value
        try:
            detail["body"] = response.text[:_ERROR_BODY_PREVIEW_CHARS]
        except Exception:  # noqa: BLE001 - a body we cannot read must not mask the error
            detail["body"] = "<unreadable>"
    request = _safe_attr(error, "request")
    url = _safe_attr(request, "url") if request is not None else None
    if url is not None:
        detail["url"] = str(url)[:200]
    return detail


def _is_diff_too_large(error: Exception) -> bool:
    """Is this GitHub's "the diff is past the API size limit" answer?

    #1047: GitHub answers the diff media type with a 406 whose JSON body
    carries `errors[].code == "too_large"` (field `diff`). Both are required.
    A 406 alone could be media-type negotiation or a proxy, and treating that
    as terminal would tell the author to split a PR that is not too big.
    Unlike `_is_permanent_rejection` this does read the body, but only the
    machine-readable error code, never GitHub's prose."""
    response = _safe_attr(error, "response")
    if getattr(response, "status_code", None) != 406:
        return False
    try:
        body = response.json()  # type: ignore[union-attr]
    except ValueError:  # unreadable or non-JSON body: not the size limit
        return False
    errors = body.get("errors") if isinstance(body, dict) else None
    if not isinstance(errors, list):
        return False
    return any(
        isinstance(item, dict) and item.get("code") == "too_large"
        for item in errors
    )


def _is_permanent_rejection(error: Exception) -> bool:
    """Is this HTTP failure a verdict on the PAYLOAD rather than on the wire?

    grug#770: GitHub 422s an inline review when any one comment's path/line
    is not part of the diff. The response is deterministic - the identical
    body is rejected identically on every attempt - yet it was classified
    like a 5xx and four Elder jobs each burned all five redrives into the
    DLQ on it. A 4xx other than a rate limit is such a verdict: 400/422 name
    the payload, 403/404 name a permission or a PR that is not there.

    Kept retryable: everything with no response (transport), 5xx, 429, and
    a 403 that carries a rate-limit signal - GitHub's secondary throttle is
    a plain 403 told apart only by its headers (the grug#891 shape).
    Deliberately does not consult the body: a classifier that parses
    GitHub's prose drifts the day the wording changes."""
    response = _safe_attr(error, "response")
    status = getattr(response, "status_code", None) if response is not None else None
    if not isinstance(status, int) or not 400 <= status < 500:
        return False
    if status == 429:
        return False
    if status == 403:
        headers = getattr(response, "headers", None) or {}
        get = headers.get if hasattr(headers, "get") else (lambda _k: None)
        if get("retry-after") is not None or get("x-ratelimit-remaining") == "0":
            return False
    return True


def dispatch_code_review(
    payload: dict[str, Any], *, blocking: bool,
    cancel_event: threading.Event | None = None,
) -> dict[str, str]:
    """Entry point — orchestrate one Elder review pass.

    `blocking` comes from RepoConfig.code_reviewer_blocking. False ⇒
    advisory mode: every publication is forced to neutral/COMMENT
    regardless of the evaluation verdict. True ⇒ blocking mode: the
    verdict survives.

    Returns a structured-log dict; never raises a wire-level
    exception — LLM outages, parse errors, and publish failures all
    degrade to advisory neutral so this persona cannot 500 the
    webhook handler.

    `cancel_event` (#635 follow-up): passed straight through to
    `review_diff`, which aborts an in-flight Cave arm call the moment it
    fires. The caller (rerun.py's `_run_hot_review`) owns setting it, via a
    background watcher that re-fetches the PR every few seconds for as long
    as this call is running.
    """
    mode: ReviewMode = "blocking" if blocking else "advisory"
    action = payload.get("action", "")
    pr = payload["pull_request"]
    repo = payload["repository"]
    installation = payload["installation"]
    owner = repo["owner"]["login"]
    repo_name = repo["name"]
    pull_number = int(pr["number"])
    head_sha = pr["head"]["sha"]
    author_login = str((pr.get("user") or {}).get("login") or "")
    installation_id = int(installation["id"])
    base_sha = str((pr.get("base") or {}).get("sha", ""))
    # Base-insensitive: every merge to the base branch used to change this
    # and cancel every in-flight review. See review_freshness_id.
    snapshot_id = review_freshness_id_from_pr(pr)
    pr_context: PrContext = {
        "installation_id": installation_id,
        "repo": f"{owner}/{repo_name}",
        "pr_number": pull_number,
        "head_sha": head_sha,
        "base_sha": base_sha,
        "title": str(pr.get("title") or ""),
        "body": str(pr.get("body") or ""),
    }

    # Living Hunt (#557): if we already finished a review on an older head
    # for this PR, scope the LLM to the delta (prior..head) instead of the
    # full PR base..head. Best-effort: store blips fall back to full review.
    living_prior_sha = ""
    living_range = ""
    try:
        from adapters.install_store import get_elder_last_reviewed

        prior = get_elder_last_reviewed(
            install_id=installation_id,
            repo=f"{owner}/{repo_name}",
            pr_number=pull_number,
        )
        if prior and prior != head_sha:
            living_prior_sha = prior
            living_range = f"{prior[:8]}..{head_sha[:8]}"
    except Exception as e:  # noqa: BLE001 - never fail a review for memory
        log.warning(
            "elder_living_hunt_lookup_failed",
            extra={
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(e).__name__,
            },
        )

    # Elder voice pack (#288/#578): sage for entitled installs that opted in
    # via repo config, caveman (the free default) otherwise. Entitlement is
    # re-checked HERE at use-time (not just at config write) so an install that
    # lost allowlist status stops getting the paid voice on its next review;
    # the allowlist lookup only fires for a repo whose config asks for sage.
    # Best-effort: a config-store hiccup must not fail a review, so any error
    # falls back to the caveman default.
    voice: VoiceSelection = "caveman"
    try:
        from adapters.install_store import get_repo_config, is_install_allowlisted

        voice = entitled_voice(
            get_repo_config(installation_id, int(repo["id"])),
            check_entitlement=lambda: is_install_allowlisted(installation_id),
        )
    except Exception as e:  # noqa: BLE001 - voice is cosmetic; never fail a review
        log.warning(
            "elder_voice_resolve_failed",
            extra={
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(e).__name__,
            },
        )

    # A queued message can already be stale when the consumer starts it. Check
    # the complete review input before spending model tokens: unchanged head
    # does not imply unchanged diff or intent when base/title/body moved.
    if action == "review":
        stale = _review_snapshot_freshness_failure(
            installation_id=installation_id,
            owner=owner,
            repo_name=repo_name,
            pull_number=pull_number,
            expected_snapshot_id=snapshot_id,
            expected_head_sha=head_sha,
        )
        if stale is not None:
            return stale

    # DiffParseError → advisory neutral so a fetcher bug or GitHub
    # format drift cannot 500 the webhook.
    try:
        if living_prior_sha:
            diff_text, used_living_compare = with_install_token_retry(
                installation_id,
                lambda token: _fetch_pr_diff_with_scope(
                    token,
                    owner,
                    repo_name,
                    pull_number,
                    base_sha=living_prior_sha,
                    head_sha=head_sha,
                ),
            )
        else:
            # Retain the established fetch seam for ordinary full reviews;
            # Guard/Smasher and focused dispatch tests share this helper.
            diff_text = with_install_token_retry(
                installation_id,
                lambda token: _fetch_pr_diff(
                    token,
                    owner,
                    repo_name,
                    pull_number,
                    base_sha=base_sha,
                    head_sha=head_sha,
                ),
            )
            used_living_compare = False
        if living_prior_sha and not used_living_compare:
            living_prior_sha = ""
            living_range = ""
        pr_context["base_sha"] = living_prior_sha or base_sha
        hunks = parse_diff(diff_text)
        # #609: drop data/generated/vendored files from the LLM's plate - a
        # big JSONL/lockfile hunk balloons the prompt into parse_failed and
        # carries no review signal. Named in the summary, never silent.
        hunks, excluded_paths = split_reviewable_hunks(hunks)
        if excluded_paths:
            log.info(
                "code_review_paths_excluded",
                extra={
                    "pr": f"{owner}/{repo_name}#{pull_number}",
                    "excluded": list(excluded_paths)[:20],
                    "count": len(excluded_paths),
                },
            )
        # A hunk bigger than a whole cohort can never be reviewed - the
        # planner will not truncate it (line anchors), so it becomes a solo
        # cohort that is auto-failed, flips the check to `partial_review`,
        # and inflates the plan into more cohorts than the wall-clock budget
        # can run. Drop it HERE, named, so the rest of the diff reviews
        # normally. Measured live: one 1,004,156-char generated
        # `.json` hunk cost four unrelated healthy cohorts.
        cohort_budget = review_cohort_char_budget()
        hunks, oversized_paths = split_oversized_hunks(hunks, cohort_budget)
        if oversized_paths:
            log.warning(
                "code_review_hunks_oversized",
                extra={
                    "pr": f"{owner}/{repo_name}#{pull_number}",
                    "oversized": list(oversized_paths)[:20],
                    "count": len(oversized_paths),
                    "cohort_budget_chars": cohort_budget,
                },
            )
        # #813: a byte-identical whole-file copy of another file already IN
        # this diff (e.g. one file added under two paths because a build
        # tool can't symlink outside its root) is provably unchanged
        # content. Reviewing it again spends full cohort budget re-reading
        # bytes Elder already judged this pass, which was measured to push
        # the genuinely novel logic in the SAME diff out of the budget
        # entirely. Drop it HERE, named against the path it duplicates, so
        # the budget goes to content nobody has looked at yet.
        hunks, duplicate_paths = split_duplicate_hunks(hunks)
        if duplicate_paths:
            log.info(
                "code_review_hunks_deduplicated",
                extra={
                    "pr": f"{owner}/{repo_name}#{pull_number}",
                    "duplicates": list(duplicate_paths)[:20],
                    "count": len(duplicate_paths),
                },
            )
    except (httpx.HTTPStatusError, httpx.RequestError, DiffParseError) as e:
        diff_too_large = _is_diff_too_large(e)
        log.warning(
            "code_review_fetch_or_parse_failed",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(e).__name__,
                "diff_too_large": diff_too_large,
                **_http_error_detail(e),
            },
        )
        # Do not publish even a degraded check for an input that changed while
        # the immutable diff was being fetched/parsed.
        if action == "review":
            stale = _review_snapshot_freshness_failure(
                installation_id=installation_id,
                owner=owner,
                repo_name=repo_name,
                pull_number=pull_number,
                expected_snapshot_id=snapshot_id,
                expected_head_sha=head_sha,
            )
            if stale is not None:
                return stale
        if diff_too_large:
            reason = DIFF_TOO_LARGE
            check_summary: str | None = (
                "GitHub refused to serve this pull request's diff (HTTP 406): "
                "it is over the API's diff size limit. Split the change into "
                "smaller pull requests to get an Elder review. Advisory "
                "neutral - PR merge is not blocked."
            )
            verdict_summary = (
                "Grug could not look - diff is over GitHub's size limit"
            )
        else:
            reason = "fetch_or_parse_failed"
            check_summary = None
            verdict_summary = "Grug could not look — diff fetch/parse failed"
        degraded = _publish_degraded(
            installation_id, owner, repo_name, pull_number, head_sha,
            reason=reason, summary=check_summary,
        )
        # Errored Activity row (PRD #301): Grug couldn't even fetch/parse the
        # diff — record it so it surfaces as `errored` (re-runnable in S3a),
        # never a silent gap. Best-effort.
        record_check_verdict(
            install_id=installation_id,
            persona_key="code_reviewer",
            repo=f"{owner}/{repo_name}",
            pr_number=pull_number,
            head_sha=head_sha,
            conclusion="neutral",
            summary=verdict_summary,
            findings_count=0,
            blocking=blocking,
            degraded_reason=reason,
        )
        return degraded

    # Full-file context (#336): fetch the whole current content of each changed
    # file at head SHA so the Elder + judge can see mitigations OUTSIDE the diff
    # hunk (the #1149 false-positive class). Best-effort + self-guarding — any
    # failure degrades to the pre-#336 diff-only review, never blocks it.
    changed_paths = tuple(dict.fromkeys(h.file_path for h in hunks))
    try:
        file_contents = with_install_token_retry(
            installation_id,
            lambda token: _fetch_file_contents(
                token, owner, repo_name, changed_paths, head_sha
            ),
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.info(
            "code_review_file_contents_unavailable",
            extra={"pr": f"{owner}/{repo_name}#{pull_number}", "error": str(e)},
        )
        file_contents = {}

    # Base revision of the SAME files, so complexity can report what THIS PR
    # did rather than the function's pre-existing debt (#767). Measured: the
    # absolute-threshold form was 85 of the last 120 findings, most replies
    # saying "pre-existing" or "outside this PR".
    #
    # Degrades to {} on any failure, which `scan_complexity` reads as "no base"
    # and falls back to the old absolute behaviour. That direction is
    # deliberate: going SILENT on a fetch error would hide real regressions,
    # whereas falling back only restores the previous noise level.
    base_file_contents = _fetch_base_contents(
        installation_id, owner, repo_name, pull_number,
        changed_paths, str(pr_context.get("base_sha") or ""),
    )

    # Cross-file context (#468): resolve the diff's changed defs + external
    # calls to the UNCHANGED files that define/call them, so the Elder can
    # catch stale callers (caller-not-updated rule). FAIL-SAFE + additive:
    # any failure degrades to {} = today's diff-only review, never blocks.
    cross_file_contents: dict[str, str] = {}
    try:
        # file_contents (the #336 full-file fetch) lets the extractor find
        # the ENCLOSING def of a body-only change even when the def line is
        # outside the diff context window (codex round 4).
        symbols = extract_symbols(hunks, file_contents)
        if symbols:
            cross_file_contents = with_install_token_retry(
                installation_id,
                lambda token: fetch_cross_file_context(
                    token, owner, repo_name, symbols,
                    head_sha=head_sha,
                    exclude_paths=frozenset(changed_paths),
                ),
            )
    except Exception as e:  # noqa: BLE001 — cross-file context is additive; never break the review
        log.info(
            "cross_file_context_degraded",
            extra={
                "stage": "dispatch",
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(e).__name__,
            },
        )

    # Production signal (#470 Omen): DD error counts for the diff's
    # files, injected as review context. FAIL-SAFE + explicit-allow: no
    # service mapping (or any failure) = None = today's review.
    runtime_context: str | None = None
    try:
        runtime_context = build_runtime_context(owner, repo_name, hunks)
    except Exception as e:  # noqa: BLE001 — omen is additive; never break the review
        log.info(
            "omen_degraded",
            extra={
                "stage": "dispatch",
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(e).__name__,
            },
        )

    # PR context supplies both trace identity and author intent. The prompt
    # treats title/body as untrusted repository data before sending it.
    llm_response: LlmReviewResponse = review_diff(
        _to_llm_hunks(hunks),
        installation_id=installation_id,
        file_contents=file_contents,
        cross_file_contents=cross_file_contents,
        runtime_context=runtime_context,
        pr_context=pr_context,
        voice=voice,
        cancel_event=cancel_event,
    )
    needs_cave_fallback = llm_response.kind == "all_failed"
    if llm_response.kind != "reviewed":
        # Without this log, a 100% LLM-outage rate looks identical to
        # "no findings" in operational dashboards — both yield
        # findings=(). Surface the degraded kind so DD can monitor
        # backend health per-install.
        log.warning(
            "code_review_llm_degraded",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": llm_response.kind,
                "error": llm_response.error,
            },
        )

    evaluation = evaluate_diff(hunks, llm_response)

    # NOTE (#466, ADR-0012): the deterministic security suite (SAST + SCA +
    # secret + IaC scans -> exploitability judge) that used to merge into
    # THIS evaluation now runs as the GUARD persona with its own check-run
    # ("Grug - Guard") - see personas/guard/dispatch.py. Elder is the LLM
    # diff review only.

    # Judge-gated publication (#467, ADR-0011): grade the findings with the
    # exploitability judge BEFORE publishing, then suppress the ones it
    # confidently calls false positives at low/medium severity. HIGH/CRITICAL
    # always publish; a judge outage grades nothing and publishes everything
    # (fail-open). ONE judge call - its verdicts drive both the gate here and
    # the DD evals below. `graded_findings` keeps the FULL set so the eval
    # denominator counts suppressed rows too.
    graded_findings = evaluation.findings
    judge_verdicts = grade_findings(
        evaluation, hunks, installation_id,
        pr_context=pr_context,
        file_contents=file_contents,
        cross_file_contents=cross_file_contents,
        runtime_context=runtime_context,
    )
    kept, suppressed = partition_findings(evaluation.findings, judge_verdicts)
    if suppressed:
        evaluation = with_findings(evaluation, kept)
        log.info(
            "judge_suppressed_findings",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "suppressed": len(suppressed),
                "published": len(kept),
            },
        )

    # Repo-grounded verification pass (#708, epic #707): the judge grades
    # plausibility from the model's own frame; this step checks the
    # load-bearing CLAIM against the fetched file contents and kills
    # contradicted findings (prose file with an execution-class claim,
    # async-family claim in a provably-sync context, suggested fix already
    # on the anchored line). Runs on the LLM findings only - the
    # deterministic sources merged below (complexity, claim-check) are
    # precise by construction. One structured log row per kill so the
    # #707 scoreboard can track precision contribution AND false kills.
    verified, killed = verify_findings(evaluation.findings, file_contents)
    if killed:
        evaluation = with_findings(evaluation, verified)
    _record_verification_kills(
        killed, installation_id=installation_id, owner=owner,
        repo_name=repo_name, pull_number=pull_number, arm="tier1",
    )
    surviving = _apply_refute_gate(
        evaluation.findings, hunks, installation_id,
        pr_context=pr_context, file_contents=file_contents,
        cross_file_contents=cross_file_contents,
        runtime_context=runtime_context, owner=owner,
        repo_name=repo_name, pull_number=pull_number, arm="tier1",
    )
    if len(surviving) != len(evaluation.findings):
        evaluation = with_findings(evaluation, surviving)

    # #532: deterministic complexity source. A changed Python function over the
    # cyclomatic/cognitive cap merges as an advisory MEDIUM finding - no LLM, no
    # judge (it is precise by construction). It rides the SAME merge rule as the
    # SAST suite; MEDIUM means it never blocks a merge on its own.
    try:
        complexity_scan = scan_complexity_full(
            hunks, file_contents,
            base_contents=base_file_contents or None,
        )
    except Exception as e:  # noqa: BLE001 - enrichment must never abort a review
        log.info(
            "code_review_complexity_scan_failed",
            extra={"pr": f"{owner}/{repo_name}#{pull_number}", "kind": type(e).__name__},
        )
        complexity_scan = ComplexityScan(findings=(), suppressed=())
    if complexity_scan.findings:
        evaluation = with_extra_findings(evaluation, complexity_scan.findings)
        log.info(
            "code_review_complexity_findings",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "count": len(complexity_scan.findings),
            },
        )
    if complexity_scan.suppressed:
        # #781: the regression gate held these back (over cap, but this PR
        # did not make them worse) - a structured count so the #707
        # scoreboard can measure how many markings the gate removes, the
        # same way judge_suppressed_findings tracks the judge's own gate.
        log.info(
            "code_review_complexity_suppressed",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "suppressed": len(complexity_scan.suppressed),
                "published": len(complexity_scan.findings),
            },
        )

    evaluation = _merge_lint_evidence(evaluation, hunks, file_contents)

    # Deterministic docs/code claim check: catch comment/env prose that
    # asserts the wrong settle cap or deep-diff bound (the LORE/CR class
    # on #664). Pure + advisory MEDIUM; never aborts the review.
    try:
        claim_file_contents = _enrich_claim_check_sources(
            installation_id, owner, repo_name, head_sha, file_contents, hunks,
        )
        claim_findings = scan_claim_checks(hunks, claim_file_contents)
        # Drop rows the LLM already published under the same rule/anchor.
        claim_findings = filter_novel_claim_findings(
            claim_findings, evaluation.findings,
        )
    except Exception as e:  # noqa: BLE001 - enrichment must never abort a review
        log.info(
            "code_review_claim_check_failed",
            extra={"pr": f"{owner}/{repo_name}#{pull_number}", "kind": type(e).__name__},
        )
        claim_findings = ()
    if claim_findings:
        evaluation = with_extra_findings(evaluation, claim_findings)
        log.info(
            "code_review_claim_check_findings",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "count": len(claim_findings),
            },
        )

    # Durable reviews can spend several minutes in inference. Re-check the
    # complete input after reasoning so changes to code, base, or intent cannot
    # publish a result for an obsolete snapshot. The durable caller enqueues the
    # freshly fetched replacement snapshot rather than assuming another event.
    if action == "review":
        stale = _review_snapshot_freshness_failure(
            installation_id=installation_id,
            owner=owner,
            repo_name=repo_name,
            pull_number=pull_number,
            expected_snapshot_id=snapshot_id,
            expected_head_sha=head_sha,
        )
        if stale is not None:
            return stale

    if needs_cave_fallback:
        # Do not enqueue fallback work until the full input snapshot has passed
        # the same freshness gate as direct publication.
        from cave_fallback import enqueue_fallback

        enqueue_fallback(
            _to_llm_hunks(hunks),
            installation_id=installation_id,
            repo=f"{owner}/{repo_name}",
            pr_number=pull_number,
            head_sha=head_sha,
        )

    # Both clients are independent — a 5xx on review post must not
    # skip the check-run post.
    conclusion, event = _publish_shape(evaluation, mode=mode)
    depth_now = os.getenv("GRUG_REVIEW_DEPTH", "tiered").strip().lower()
    tier1_phase: Literal["tier1", "deep", "dual"] = (
        "tier1" if depth_now == "tiered" else "dual"
    )
    title, summary = _summary_markdown(
        evaluation, suppressed_count=len(suppressed),
        excluded_paths=excluded_paths,
        oversized_paths=oversized_paths,
        duplicate_paths=duplicate_paths,
        living_range=living_range,
        review_phase=tier1_phase,
    )
    check_result = CheckRunResult(
        name=_CHECK_NAME,
        head_sha=head_sha,
        status="completed",
        conclusion=conclusion,
        title=title,
        summary=summary,
    )
    check_publish_failed = False
    try:
        with_install_token_retry(
            installation_id,
            lambda token: post_check_run(
                token, owner, repo_name, check_result,
                external_id=f"grug-cr:{owner}/{repo_name}#{pull_number}:{head_sha}",
            ),
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.error(
            "code_review_check_run_publish_failed",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(e).__name__,
            },
        )
        check_publish_failed = True
        # Continue to attempt the review post — independent surface.

    review_publish_failed = False
    review_publish_rejected = False
    # On a re-review (synchronize/reopened), dedup findings already
    # commented on unchanged lines so the PR isn't flooded with
    # duplicate inline comments on every push (#189). On the first pass
    # (opened/ready_for_review) there are no prior Grug comments, so
    # skip the fetch entirely.
    prior_keys: frozenset[str] = frozenset()
    dedup_degraded = False
    if action in {"synchronize", "reopened", "review"}:
        prior_keys, dedup_degraded = _prior_finding_keys(
            installation_id, owner, repo_name, pull_number,
        )
    # Only pay the ledger fetch when there is something to annotate. A fully
    # degraded eval publishes no inline review; a partial staged review does.
    precedent_notes = (
        _precedent_notes_for(f"{owner}/{repo_name}", evaluation.findings)
        if evaluation.findings
        and evaluation.degraded_reason in (None, "", "partial_review")
        else {}
    )
    review_result, posted_findings = _build_review_result(
        evaluation, head_sha=head_sha, event=event, prior_keys=prior_keys,
        precedent_notes=precedent_notes,
    )
    review_resp: dict[str, Any] | None = None
    if review_result is not None:
        try:
            review_resp = with_install_token_retry(
                installation_id,
                lambda token: post_review(
                    token, owner, repo_name,
                    pull_number=pull_number, result=review_result,
                ),
            )
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            # #770: a deterministic 4xx (422 on an out-of-diff comment) is
            # NOT a publish failure to redrive - the same payload gets the
            # same answer. Only wire/5xx/429 failures reach the retry lane.
            # The status + GitHub's error body ride on the log line so the
            # DLQ runbook can name the cause from one record (grug#891).
            permanent = _is_permanent_rejection(e)
            log.error(
                "code_review_review_publish_failed",
                extra={
                    "installation_id": installation_id,
                    "pr": f"{owner}/{repo_name}#{pull_number}",
                    "kind": type(e).__name__,
                    "permanent": permanent,
                    "inline_comments": len(review_result.comments),
                    **_http_error_detail(e),
                },
            )
            if permanent:
                review_publish_rejected = True
            else:
                review_publish_failed = True

    # Capture inline-comment IDs for later reaction polling (#247). BEST-
    # EFFORT, post-publish, own try/except — a capture failure must never
    # change the review outcome (`result` is computed below, unaffected).
    # Persist posted findings even if trace export failed: a later trusted
    # maintainer reaction can still teach the repository ledger without DD span
    # attribution.
    review_id = review_resp.get("id") if review_resp else None
    if review_id is not None:
        _capture_review_comments(
            installation_id=installation_id,
            owner=owner,
            repo_name=repo_name,
            pull_number=pull_number,
            review_id=review_id,
            findings=posted_findings,
            review_span_context=llm_response.review_span_context,
            head_sha=head_sha,
            author_login=author_login,
            failure_log="code_review_comment_capture_failed",
        )

    # Markings v2 review stack: upsert PR-timeline summary (actionable count
    # + consolidated agent prompt). Best-effort — never fails the check/review
    # that already published. Skipped when the check never landed.
    if not check_publish_failed:
        try:
            stack_body = _review_stack_body(
                evaluation,
                conclusion=conclusion,
                living_range=living_range,
                suppressed_count=len(suppressed),
                review_phase=tier1_phase,
                pr_title=str(pr_context.get("title") or ""),
                head_sha=head_sha,
                base_sha=str(pr_context.get("base_sha") or ""),
            )
            with_install_token_retry(
                installation_id,
                lambda token: _upsert_review_stack_comment(
                    token, owner, repo_name, pull_number, stack_body,
                    create_if_absent=worth_an_email(evaluation),
                ),
            )
            log.info(
                "elder_review_stack_upserted",
                extra={
                    "installation_id": installation_id,
                    "pr": f"{owner}/{repo_name}#{pull_number}",
                    "findings": len(evaluation.findings),
                },
            )
        except Exception as e:  # noqa: BLE001 - stack is cosmetic UX
            log.warning(
                "elder_review_stack_upsert_failed",
                extra={
                    "installation_id": installation_id,
                    "pr": f"{owner}/{repo_name}#{pull_number}",
                    "kind": type(e).__name__,
                },
            )

    result = _resolve_result(
        evaluation,
        check_publish_failed=check_publish_failed,
        review_publish_failed=review_publish_failed,
        review_publish_rejected=review_publish_rejected,
    )
    # Structured log carries everything needed to verify the persona
    # ran end-to-end on a real PR (operator AC). Backend + model
    # attribution lets DD LLM Obs slice metrics by which LLM produced
    # the verdict; degraded_reason correlates dispatch volume with
    # backend health.
    log.info(
        "code_reviewer_dispatched",
        extra={
            "installation_id": installation_id,
            "pr": f"{owner}/{repo_name}#{pull_number}",
            "head_sha": head_sha[:8],
            "backend": (
                llm_response.backend_used.value
                if llm_response.backend_used is not None else None
            ),
            "model": llm_response.model_name,
            "backends": [
                backend.value for backend in llm_response.backends_used
            ],
            "models": list(llm_response.models_used),
            "findings_count": len(evaluation.findings),
            "dropped_hallucinations": evaluation.dropped_hallucinations,
            "degraded_reason": evaluation.degraded_reason,
            # How much of the diff was actually walked, as a number the
            # eval harness (#645) can track directly instead of parsing
            # board prose - 1.0 for a review with no cohort plan at all
            # (single-call reviews carry no ReviewCoverage).
            "coverage_fraction": (
                evaluation.coverage.fraction if evaluation.coverage is not None else 1.0
            ),
            # True when the prior-comments fetch failed on a re-review:
            # dedup fell back to post-everything, so duplicate comments
            # this cycle are a fetch artifact, not new findings.
            "dedup_degraded": dedup_degraded,
            "result": result,
        },
    )
    # Activity feed (PRD #301): record what Elder did, best-effort. Use the
    # PUBLISHED `conclusion` (the actual PR outcome from _publish_shape), NOT
    # the raw eval severity — in advisory mode high/critical findings post
    # `neutral` (no gate), so the honest badge is `warn`, not `block`. The
    # verdict resolves to `errored` (never a fake pass/block) when the LLM
    # degraded (`evaluation.degraded_reason`) OR the check-run never reached
    # GitHub (`check_publish_failed`) — the feed must not claim a verdict for a
    # check that isn't on the PR (mirrors `_resolve_result`'s publish-failed
    # precedence; "no lies").
    record_check_verdict(
        install_id=installation_id,
        persona_key="code_reviewer",
        repo=f"{owner}/{repo_name}",
        pr_number=pull_number,
        head_sha=head_sha,
        conclusion=conclusion,
        summary=title,
        findings_count=len(evaluation.findings),
        blocking=blocking,
        degraded_reason=(
            evaluation.degraded_reason
            or ("check_publish_failed" if check_publish_failed else None)
        ),
    )
    # Living Hunt: remember this head only when the check actually landed
    # and the review was not an infra skip (so the next push can delta).
    if (
        result in {"pass", "fail", "skipped"}
        and not check_publish_failed
        and evaluation.degraded_reason in (None, "", "no_diff")
    ):
        try:
            from adapters.install_store import put_elder_last_reviewed

            _, current_head_sha, _, _ = with_install_token_retry(
                installation_id,
                lambda token: _fetch_current_review_snapshot(
                    token, owner, repo_name, pull_number,
                ),
            )
            if current_head_sha == head_sha:
                put_elder_last_reviewed(
                    install_id=installation_id,
                    repo=f"{owner}/{repo_name}",
                    pr_number=pull_number,
                    head_sha=head_sha,
                )
                if living_range:
                    log.info(
                        "elder_living_hunt_delta_done",
                        extra={
                            "pr": f"{owner}/{repo_name}#{pull_number}",
                            "range": living_range,
                        },
                    )
            else:
                log.info(
                    "elder_living_hunt_stale_anchor_skipped",
                    extra={
                        "pr": f"{owner}/{repo_name}#{pull_number}",
                        "review_head": head_sha,
                        "current_head": current_head_sha,
                    },
                )
        except Exception as e:  # noqa: BLE001 - memory must not fail publish
            log.warning(
                "elder_living_hunt_put_failed",
                extra={
                    "pr": f"{owner}/{repo_name}#{pull_number}",
                    "kind": type(e).__name__,
                },
            )

    # Async deep append (#646): Tier-1 (coder) already published the required
    # check. When tiered escalation fires, run the reasoner arm now and append
    # any new findings. Failures here never change `result` — the required
    # check already completed.
    try:
        _async_deep_append_if_needed(
            installation_id=installation_id,
            owner=owner,
            repo_name=repo_name,
            pull_number=pull_number,
            head_sha=head_sha,
            action=action,
            snapshot_id=snapshot_id,
            mode=mode,
            blocking=blocking,
            hunks=hunks,
            llm_hunks=_to_llm_hunks(hunks),
            pr_context=pr_context,
            file_contents=file_contents,
            cross_file_contents=cross_file_contents,
            runtime_context=runtime_context,
            voice=voice,
            living_range=living_range,
            evaluation=evaluation,
            prior_keys=prior_keys,
            check_publish_failed=check_publish_failed,
            cancel_event=cancel_event,
            author_login=author_login,
        )
    except Exception as e:  # noqa: BLE001 - deep append is best-effort
        log.warning(
            "elder_async_deep_append_failed",
            extra={
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(e).__name__,
            },
        )

    # LLM-as-a-judge DD evals (#190) submit AFTER the review + check-run are
    # POSTed, so recording can't delay the developer seeing the review. The
    # judge LLM CALL already ran pre-publish (grade_findings, #467) to gate
    # publication; here we only submit its verdicts to DD LLM Obs - for the
    # FULL `graded_findings` set (published AND suppressed AND
    # verification-killed, #708) so the precision denominator and the
    # learning corpus keep every judged row: these evals measure the JUDGE
    # stage, and downstream gates (judge suppression, verification kills)
    # each carry their own telemetry. `submit_evals`
    # is self-guarding (never raises); wrap anyway - evals must never affect
    # the dispatch result the developer already has.
    try:
        submit_evals(
            graded_findings, judge_verdicts,
            review_span_context=llm_response.review_span_context,
        )
    except Exception as e:  # noqa: BLE001 — defense-in-depth over submit_evals's own guard
        log.error(
            "code_review_judge_dispatch_failed",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(e).__name__,
            },
        )

    # Result shape mirrors TPM's `{persona, result}` so dispatcher can
    # treat both uniformly. The outer dispatcher wraps with `status`.
    response = {
        "persona": "code_reviewer",
        "result": result,
    }
    if evaluation.degraded_reason:
        response["degraded_reason"] = evaluation.degraded_reason
    return response



def _apply_refute_gate(
    findings, hunks, installation_id, *, pr_context, file_contents,
    cross_file_contents, runtime_context, owner, repo_name, pull_number, arm,
):
    """Refute gate (#714): adversarial evidence-check for the HIGH/CRITICAL
    findings that survived deterministic verification - the semantic-
    misreading class no grep can refute. Returns the surviving findings;
    kills are recorded through the same telemetry as verification kills
    (reason "refuted") so the #707 scoreboard attributes them and the
    false-kill hunt covers this gate too. Fail-open end to end."""
    high = tuple(f for f in findings if f.severity in ("high", "critical"))
    if not high:
        return findings
    verdicts = refute_findings(
        high, hunks, installation_id,
        pr_context=pr_context,
        file_contents=file_contents,
        cross_file_contents=cross_file_contents,
        runtime_context=runtime_context,
    )
    if not verdicts:
        return findings
    _, refuted = partition_refuted(high, verdicts)
    if not refuted:
        return findings
    from personas.code_reviewer.verify import KilledFinding
    _record_verification_kills(
        tuple(KilledFinding(finding=f, reason="refuted") for f in refuted),
        installation_id=installation_id, owner=owner,
        repo_name=repo_name, pull_number=pull_number, arm=arm,
    )
    dead = set(id(f) for f in refuted)
    return tuple(f for f in findings if id(f) not in dead)


def _record_verification_kills(
    killed, *, installation_id: int, owner: str, repo_name: str,
    pull_number: int, arm: str,
) -> None:
    """One structured log row per verification kill plus the ALWAYS-emitted
    arm-tagged gauge (#708; PR #710 reviews). The gauge fires on zero too -
    a kills-only gauge made a silently-disabled verifier indistinguishable
    from healthy zero-kill traffic. Shared by the tier-1 and deep arms so
    the telemetry contract cannot drift between them."""
    for kf in killed:
        log.info(
            "code_review_verification_killed",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "path": kf.finding.file,
                "line": kf.finding.line,
                "rule": kf.finding.rule_name,
                "severity": kf.finding.severity,
                "reason": kf.reason,
                "arm": arm,
            },
        )
    try:
        from observability import emit_gauge  # type: ignore
        emit_gauge(
            "grug.elder.verification_killed", len(killed),
            tags={"repo": f"{owner}/{repo_name}", "arm": arm},
        )
    except Exception as e:  # noqa: BLE001 - telemetry never breaks the review
        log.debug(
            "code_review_verification_gauge_failed",
            extra={"kind": type(e).__name__},
        )


def _async_deep_enabled() -> bool:
    """Async deep append on for tiered unless GRUG_DEEP_ASYNC=0."""
    raw = os.getenv("GRUG_DEEP_ASYNC", "1").strip().lower()
    return raw not in {"0", "false", "off", "no"}


def _async_deep_decision(
    *,
    check_publish_failed: bool,
    llm_hunks: list[LlmHunk],
    pr_context: PrContext,
    evaluation: CodeReviewEvaluation,
    cancel_event: threading.Event | None,
    owner: str,
    repo_name: str,
    pull_number: int,
) -> DeepEscalationDecision | None:
    """Return the escalation decision only when deep append may safely start."""
    depth = os.getenv("GRUG_REVIEW_DEPTH", "tiered").strip().lower()
    unavailable = (
        check_publish_failed
        or not _async_deep_enabled()
        or depth != "tiered"
        or evaluation.degraded_reason not in (None, "", "no_diff", "partial_review")
        or (cancel_event is not None and cancel_event.is_set())
    )
    if unavailable:
        return None
    if review_is_staged(llm_hunks):
        log.info(
            "elder_async_deep_skipped_staged_review",
            extra={"pr": f"{owner}/{repo_name}#{pull_number}"},
        )
        return None
    decision = decide_deep_escalation(list(llm_hunks), pr_context)
    return decision if decision.escalate else None


def _deep_snapshot_is_stale(
    *,
    action: str,
    installation_id: int,
    owner: str,
    repo_name: str,
    pull_number: int,
    snapshot_id: str,
    head_sha: str,
    when: str = "pre_infer",
) -> bool:
    if action != "review":
        return False
    stale = _review_snapshot_freshness_failure(
        installation_id=installation_id,
        owner=owner,
        repo_name=repo_name,
        pull_number=pull_number,
        expected_snapshot_id=snapshot_id,
        expected_head_sha=head_sha,
    )
    if stale is not None:
        log.info(
            "elder_async_deep_skipped_stale",
            extra={"pr": f"{owner}/{repo_name}#{pull_number}", "when": when},
        )
    return stale is not None


def _grade_deep_response(
    deep_llm: LlmReviewResponse,
    hunks: tuple[DiffHunk, ...],
    installation_id: int,
    *,
    pr_context: PrContext,
    file_contents: dict[str, str] | None,
    cross_file_contents: dict[str, str] | None,
    runtime_context: str | None,
    owner: str,
    repo_name: str,
    pull_number: int,
) -> tuple[CodeReviewEvaluation, tuple[Finding, ...], tuple[Any, ...], int]:
    deep_eval = evaluate_diff(hunks, deep_llm)
    deep_graded = deep_eval.findings
    deep_verdicts = grade_findings(
        deep_eval,
        hunks,
        installation_id,
        pr_context=pr_context,
        file_contents=file_contents,
        cross_file_contents=cross_file_contents,
        runtime_context=runtime_context,
    )
    deep_kept, deep_suppressed = partition_findings(deep_graded, deep_verdicts)
    if deep_suppressed:
        deep_eval = with_findings(deep_eval, deep_kept)
    deep_verified, deep_killed = verify_findings(
        deep_eval.findings,
        file_contents or {},
    )
    if deep_killed:
        deep_eval = with_findings(deep_eval, deep_verified)
    _record_verification_kills(
        deep_killed,
        installation_id=installation_id,
        owner=owner,
        repo_name=repo_name,
        pull_number=pull_number,
        arm="deep",
    )
    deep_surviving = _apply_refute_gate(
        deep_eval.findings,
        hunks,
        installation_id,
        pr_context=pr_context,
        file_contents=file_contents,
        cross_file_contents=cross_file_contents,
        runtime_context=runtime_context,
        owner=owner,
        repo_name=repo_name,
        pull_number=pull_number,
        arm="deep",
    )
    if len(deep_surviving) != len(deep_eval.findings):
        deep_eval = with_findings(deep_eval, deep_surviving)
    return deep_eval, deep_graded, deep_verdicts, len(deep_suppressed)


def _publish_deep_check(
    *,
    installation_id: int,
    owner: str,
    repo_name: str,
    pull_number: int,
    head_sha: str,
    conclusion: CheckConclusion,
    title: str,
    summary: str,
) -> None:
    try:
        with_install_token_retry(
            installation_id,
            lambda token: post_check_run(
                token,
                owner,
                repo_name,
                CheckRunResult(
                    name=_CHECK_NAME,
                    head_sha=head_sha,
                    status="completed",
                    conclusion=conclusion,
                    title=title,
                    summary=summary,
                ),
                external_id=f"grug-cr-deep:{owner}/{repo_name}#{pull_number}:{head_sha}",
            ),
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as error:
        log.warning(
            "elder_async_deep_check_publish_failed",
            extra={
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(error).__name__,
            },
        )


def _capture_review_comments(
    *,
    installation_id: int,
    owner: str,
    repo_name: str,
    pull_number: int,
    review_id: str,
    findings: tuple[Finding, ...],
    review_span_context: dict[str, Any] | None,
    head_sha: str,
    author_login: str,
    failure_log: str,
) -> None:
    """Fetch a review's inline comments and persist them for reaction polling.

    Shared by the synchronous publish path (#247) and the async deep path
    (#730). Best-effort: any failure is logged under `failure_log` and
    never re-raised - a capture failure must never change the review
    outcome.

    `findings` should be the `new_findings` tuple `_build_review_result`
    returned alongside the `ReviewResult` this review was built from (not
    the caller's full unfiltered findings set) - see
    `_capture_comment_records` for how it matches them to comments (grug#967:
    no retry here anymore, since a fresh fetch already carries everything
    this function needs).
    """
    try:
        comments = with_install_token_retry(
            installation_id,
            lambda token: get_review_comments(
                token, owner, repo_name,
                pull_number=pull_number, review_id=int(review_id),
            ),
        )
        persisted = _capture_comment_records(
            comments, findings,
            install_id=installation_id,
            repo=f"{owner}/{repo_name}",
            pr_number=pull_number,
            review_span_context=review_span_context,
            head_sha=head_sha,
            author_login=author_login,
        )
        # Observability: a 0-of-N capture (e.g. a comment<->finding shape
        # regression) silently empties the poller's batch with no other
        # signal - alarm on it; otherwise the count is recorded by
        # _capture_comment_records.
        if comments and persisted == 0:
            log.warning(
                "code_review_comment_capture_zero",
                extra={
                    "installation_id": installation_id,
                    "pr": f"{owner}/{repo_name}#{pull_number}",
                    "fetched": len(comments),
                },
            )
    except Exception as error:  # noqa: BLE001 - capture is best-effort
        log.warning(
            failure_log,
            extra={
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(error).__name__,
            },
        )


def _publish_deep_review(
    deep_eval: CodeReviewEvaluation,
    *,
    novel_deep: tuple[Finding, ...],
    head_sha: str,
    event: ReviewEvent,
    all_prior: frozenset[str],
    installation_id: int,
    owner: str,
    repo_name: str,
    pull_number: int,
    deep_llm: LlmReviewResponse,
    author_login: str,
    mode: ReviewMode,
    living_range: str,
    deep_suppressed_count: int,
    combined_eval: CodeReviewEvaluation,
    pr_title: str = "",
    base_sha: str = "",
) -> None:
    """Publish the async deep review's novel findings as a GitHub review.

    No-op if `novel_deep` is empty (deep found nothing new beyond Tier-1).
    On a successful post, also captures the review's inline-comment IDs
    (#730, via `_capture_review_comments`, best-effort) and refreshes the
    PR-timeline stack comment using `combined_eval` (Tier-1 + deep) so the
    canonical projection carries every finding, not just the deep ones.
    """
    if not novel_deep:
        return
    review_result, posted_findings = _build_review_result(
        deep_eval,
        head_sha=head_sha,
        event=event,
        prior_keys=all_prior,
        precedent_notes={},
    )
    if review_result is None:
        return
    review_resp: dict[str, Any] | None = None
    try:
        review_resp = with_install_token_retry(
            installation_id,
            lambda token: post_review(
                token,
                owner,
                repo_name,
                pull_number=pull_number,
                result=review_result,
            ),
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as error:
        log.warning(
            "elder_async_deep_review_publish_failed",
            extra={
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(error).__name__,
            },
        )
        return

    # #730: capture the deep review's inline-comment IDs so the hardest-won
    # findings are visible to the reaction/reply learning loops - the same
    # durable contract the synchronous path uses (#247). Best-effort: a
    # capture failure must never change the deep review outcome.
    review_id = review_resp.get("id") if review_resp else None
    if review_id is not None:
        _capture_review_comments(
            installation_id=installation_id,
            owner=owner,
            repo_name=repo_name,
            pull_number=pull_number,
            review_id=review_id,
            findings=posted_findings,
            review_span_context=deep_llm.review_span_context,
            head_sha=head_sha,
            author_login=author_login,
            failure_log="elder_async_deep_comment_capture_failed",
        )

    # #730: refresh the PR-timeline stack comment so the deep findings appear
    # in the canonical review projection alongside the Tier-1 findings.
    # Use combined_eval (Tier-1 + deep) so the stack preserves all findings,
    # severity, and actionable counts - not just novel_deep.
    try:
        conclusion, _ = _publish_shape(combined_eval, mode=mode)
        stack_body = _review_stack_body(
            combined_eval,
            conclusion=conclusion,
            living_range=living_range,
            suppressed_count=deep_suppressed_count,
            review_phase="deep",
            pr_title=pr_title,
            head_sha=head_sha,
            base_sha=base_sha,
        )
        with_install_token_retry(
            installation_id,
            lambda token: _upsert_review_stack_comment(
                token, owner, repo_name, pull_number, stack_body,
                create_if_absent=worth_an_email(combined_eval),
            ),
        )
    except Exception as error:  # noqa: BLE001 - stack is cosmetic UX
        log.warning(
            "elder_async_deep_stack_upsert_failed",
            extra={
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(error).__name__,
            },
        )


def _submit_deep_evals(
    deep_graded: tuple[Finding, ...],
    deep_verdicts: tuple[Any, ...],
    deep_llm: LlmReviewResponse,
    *,
    owner: str,
    repo_name: str,
    pull_number: int,
) -> None:
    try:
        submit_evals(
            deep_graded,
            deep_verdicts,
            review_span_context=deep_llm.review_span_context,
        )
    except Exception as error:  # noqa: BLE001
        log.warning(
            "elder_async_deep_evals_failed",
            extra={
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(error).__name__,
            },
        )


def _async_deep_append_if_needed(
    *,
    installation_id: int,
    owner: str,
    repo_name: str,
    pull_number: int,
    head_sha: str,
    action: str,
    snapshot_id: str,
    mode: ReviewMode,
    blocking: bool,
    hunks: tuple[DiffHunk, ...],
    llm_hunks: list[LlmHunk],
    pr_context: PrContext,
    file_contents: dict[str, str] | None,
    cross_file_contents: dict[str, str] | None,
    runtime_context: str | None,
    voice: VoiceSelection,
    living_range: str,
    evaluation: CodeReviewEvaluation,
    prior_keys: frozenset[str],
    check_publish_failed: bool,
    cancel_event: threading.Event | None,
    author_login: str,
) -> None:
    """Run reasoner after Tier-1 publish when tiered escalation fires (#646).

    Posts additional inline comments and a second completed check-run summary
    that includes deep findings. Never raises to the caller.
    """
    decision = _async_deep_decision(
        check_publish_failed=check_publish_failed,
        llm_hunks=llm_hunks,
        pr_context=pr_context,
        evaluation=evaluation,
        cancel_event=cancel_event,
        owner=owner,
        repo_name=repo_name,
        pull_number=pull_number,
    )
    if decision is None:
        return

    log.info(
        "elder_async_deep_start",
        extra={
            "pr": f"{owner}/{repo_name}#{pull_number}",
            "reasons": list(decision.reasons),
            "added_lines": decision.added_lines,
        },
    )

    if _deep_snapshot_is_stale(
        action=action,
        installation_id=installation_id,
        owner=owner,
        repo_name=repo_name,
        pull_number=pull_number,
        snapshot_id=snapshot_id,
        head_sha=head_sha,
    ):
        return

    deep_llm = review_reasoner_diff(
        llm_hunks,
        installation_id=installation_id,
        file_contents=file_contents,
        cross_file_contents=cross_file_contents,
        runtime_context=runtime_context,
        pr_context=pr_context,
        voice=voice,
        cancel_event=cancel_event,
    )
    arm_ran = deep_llm.kind == "reviewed"
    if not arm_ran:
        log.info(
            "elder_async_deep_arm_empty",
            extra={
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": deep_llm.kind,
                "error": deep_llm.error,
            },
        )
        if not is_blackout(deep_llm.kind):
            # `no_diff`: nothing for the reasoner to look at is not a
            # failure to look (BENIGN_DEGRADATIONS) - nothing to qualify.
            return
        # grug#848: the reasoner did NOT run (transport or parse failure).
        # This used to `return` here, leaving Tier-1's already-published
        # `success` standing over a promise its own check-run text made
        # ("reasoner may append later if escalated") and never kept - the
        # author could not tell "the reasoner looked and agreed" from "the
        # reasoner never looked". Nothing to grade, so build the deep
        # arm's evaluation directly: `evaluate_diff` turns a failed
        # response into an empty pass carrying `degraded_reason=kind`, and
        # `with_degradation` below folds that into Tier-1 through the same
        # `_derive_conclusion` seam #844 established for partial coverage.
        # The deep check-run then completes `neutral`, naming the arm that
        # never ran, in place of the unqualified Tier-1 `success`.
        deep_eval = evaluate_diff(hunks, deep_llm)
        deep_graded: tuple[Finding, ...] = ()
        deep_verdicts: tuple[Any, ...] = ()
        deep_suppressed_count = 0
    else:
        deep_eval, deep_graded, deep_verdicts, deep_suppressed_count = _grade_deep_response(
            deep_llm,
            hunks,
            installation_id,
            pr_context=pr_context,
            file_contents=file_contents,
            cross_file_contents=cross_file_contents,
            runtime_context=runtime_context,
            owner=owner,
            repo_name=repo_name,
            pull_number=pull_number,
        )

    # Supersession after long reasoner/judge work (#646 FLINT).
    if cancel_event is not None and cancel_event.is_set():
        log.info(
            "elder_async_deep_skipped_cancelled",
            extra={"pr": f"{owner}/{repo_name}#{pull_number}"},
        )
        return
    if _deep_snapshot_is_stale(
        action=action,
        installation_id=installation_id,
        owner=owner,
        repo_name=repo_name,
        pull_number=pull_number,
        snapshot_id=snapshot_id,
        head_sha=head_sha,
        when="post_infer",
    ):
        return

    # Dedupe against already-posted Tier-1 + prior-push comments AND drop
    # reasoner duplicates from the combined check summary.
    tier1_keys = frozenset(
        finding_key(f.file, f.line, f.rule_name) for f in evaluation.findings
    )
    all_prior = prior_keys | tier1_keys
    novel_deep = tuple(
        f
        for f in deep_eval.findings
        if finding_key(f.file, f.line, f.rule_name) not in tier1_keys
    )
    if novel_deep:
        deep_eval = with_findings(deep_eval, novel_deep)
        combined = with_extra_findings(evaluation, novel_deep)
    else:
        log.info(
            "elder_async_deep_no_findings",
            extra={"pr": f"{owner}/{repo_name}#{pull_number}"},
        )
        combined = evaluation

    # `combined` so far only carries TIER-1's degraded_reason - the merge
    # above never looked at deep_eval's own. The deep arm is an INDEPENDENT
    # review pass with its own cohort plan and its own budget; if IT hit
    # partial coverage (#813: budget burned on byte-identical content) while
    # Tier-1 was clean, that fact must not vanish just because Tier-1 had
    # nothing to report it. Without this, a deep pass that ran out of
    # budget on a diff Tier-1 already judged clean published `success` over
    # ground the deep arm never finished walking.
    combined = with_degradation(combined, deep_eval)

    conclusion, event = _publish_shape(combined, mode=mode)
    title, summary = _summary_markdown(
        combined,
        suppressed_count=deep_suppressed_count,
        living_range=living_range,
        review_phase="deep",
    )
    title = f"{title} (deep append)"
    # The author-facing distinction #848 asks for: "the reasoner ran and
    # found nothing" and "the reasoner did not run" are different news, and
    # this preamble says which one this is even when `_summary_markdown`'s
    # wording is owned by a Tier-1 degradation (first-degradation-wins in
    # `with_degradation`). Only `kind` is quoted - the raw error text stays
    # in the log, since a parse failure's text can be model prose.
    reasons = ", ".join(decision.reasons)
    preamble = (
        f"_Deep reasoner arm appended after Tier-1 completed ({reasons})._"
        if arm_ran
        else (
            f"_Deep reasoner arm did not run after Tier-1 completed "
            f"({reasons}): `{deep_llm.kind}`. Nothing was appended._"
        )
    )
    summary = f"{preamble}\n\n{summary}"
    _publish_deep_check(
        installation_id=installation_id,
        owner=owner,
        repo_name=repo_name,
        pull_number=pull_number,
        head_sha=head_sha,
        conclusion=conclusion,
        title=title,
        summary=summary,
    )
    _publish_deep_review(
        deep_eval,
        novel_deep=novel_deep,
        head_sha=head_sha,
        event=event,
        all_prior=all_prior,
        installation_id=installation_id,
        owner=owner,
        repo_name=repo_name,
        pull_number=pull_number,
        deep_llm=deep_llm,
        author_login=author_login,
        mode=mode,
        living_range=living_range,
        deep_suppressed_count=deep_suppressed_count,
        combined_eval=combined,
        pr_title=str(pr_context.get("title") or ""),
        base_sha=str(pr_context.get("base_sha") or ""),
    )
    _submit_deep_evals(
        deep_graded,
        deep_verdicts,
        deep_llm,
        owner=owner,
        repo_name=repo_name,
        pull_number=pull_number,
    )

    log.info(
        "elder_async_deep_done",
        extra={
            "pr": f"{owner}/{repo_name}#{pull_number}",
            "deep_findings": len(novel_deep),
            "reasons": list(decision.reasons),
            "arm_kind": deep_llm.kind,
            "conclusion": conclusion,
        },
    )


def _publish_degraded(
    installation_id: int, owner: str, repo_name: str, pull_number: int,
    head_sha: str, *, reason: str, summary: str | None = None,
) -> dict[str, str]:
    """Post the "skipped" check-run when fetch/parse fails. Best-effort —
    a publish failure here is also swallowed since we can't do anything
    useful with it (would need its own degraded publish, etc)."""
    title = f"WARN Elder review skipped ({reason})"
    summary = summary or (
        f"Elder could not run this pass: `{reason}`. Advisory neutral "
        "— PR merge is not blocked."
    )
    publish_failed = False
    try:
        with_install_token_retry(
            installation_id,
            lambda token: post_check_run(
                token, owner, repo_name,
                CheckRunResult(
                    name=_CHECK_NAME, head_sha=head_sha, status="completed",
                    conclusion="neutral", title=title, summary=summary,
                ),
                external_id=(
                    f"grug-cr:{owner}/{repo_name}#{pull_number}:{head_sha}"
                ),
            ),
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        # No recovery path beyond logging — but a silent miss here
        # means the PR shows NO check-run at all, indistinguishable
        # from persona-disabled. Surface as a discrete signal so the
        # dispatcher result reflects it.
        log.error(
            "code_review_degraded_publish_failed",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(e).__name__,
                "reason": reason,
            },
        )
        publish_failed = True
    return {
        "persona": "code_reviewer",
        "result": "publish_failed" if publish_failed else "skipped",
        "degraded_reason": reason,
    }
