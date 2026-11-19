"""Elder (code-reviewer) persona dispatch orchestration.

End-to-end for one `pull_request` event:
  1. Fetch the PR's unified diff via the GitHub API.
  2. parse_diff → DiffHunks.
  3. assemble_evidence(...) once -> review_diff(evidence, installation_id)
     -> LlmReviewResponse; the judge grades against the same evidence.
  4. evaluate_diff(hunks, llm_response) → CodeReviewEvaluation.
  5. Build the inline-comment ReviewResult + the summary CheckRunResult.
  6. Publish via post_check_run + (optionally) post_review.

Advisory-first contract: when `blocking=False` (default per
RepoConfig.code_reviewer_blocking) the check-run conclusion is forced to
`neutral`, even when the evaluation itself returned `failure`. In
blocking mode a degraded review concludes `failure` (grug#1212,
ADR-0023): a required neutral check passes. Operator flips to blocking
via dashboard once trust is established.

The review state is the verdict in both modes (#1758, ADR-0028): the
Tier-1 review is APPROVE on a clean head, REQUEST_CHANGES while a
blocking finding stands, and COMMENT only for a degraded or superseded
run (`review_publish.verdict_event`). Merge gates read that state.

Independent from TPM dispatch — the caller (dispatcher.py) calls this
in sequence with TPM but catches exceptions per-persona so one
failing does not skip the other.
"""
from __future__ import annotations

import functools
import logging
from math import ceil
import os
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from dataclasses import dataclass, replace
from typing import Any, Literal
from urllib.parse import quote

import httpx

from activity_log import record_check_verdict
from model_family import app_author_family, model_family
from github_app_auth import (
    get_app_id, installation_permission_summary_note, with_install_token_retry,
)
from github_checks_client import (
    CheckConclusion,
    CheckRunResult,
    list_check_runs_for_ref,
    post_check_run,
)
from github_reviews_client import (  # noqa: F401 - InlineComment, ReviewResult re-exported
    InlineComment, ReviewEvent, ReviewResult, dismiss_review, get_review_comments,
    list_reviews, post_review, update_review_body,
)
from llm_client import (
    DeepEscalationDecision,
    Hunk as LlmHunk,
    LlmReviewResponse,
    JUDGE_BATCH_SIZE,
    PrContext,
    _review_deadline,
    _staged_review_budget_s,
    decide_deep_escalation,
    review_cohort_char_budget,
    review_diff,
    review_is_staged,
    review_reasoner_diff,
    review_second_lineage,
)
from ledger import SECOND_LINEAGE_REVIEWER
from redact import redact_secrets
from voice_pack import VoiceSelection, entitled_voice
from review_evidence import ElderEvidence, assemble_evidence
from personas.review_publish import (
    DIFF_FETCH_TIMEOUT, PARTIAL_COVERAGE, PUBLISHABLE_DEGRADATIONS, PublishOutcome, ReviewMode,
    append_neutral_reason, fetch_file_contents, fetch_pr_diff, fetch_pr_diff_with_scope,
    fetch_review_comment_body, is_blackout, is_real_degradation,
    neutral_check_title, neutral_reason, plan_publish, prior_finding_keys, verdict_event,
)
from personas.code_reviewer.dedup import (
    finding_key, parse_rule,
)
from personas.code_reviewer.diff_parser import (
    added_lines,
    DiffHunk, DiffParseError, file_changes, is_tree_copy_candidate, new_file_blob_ids,
    parse_diff, split_duplicate_hunks, split_large_hunks, split_oversized_hunks,
    split_reviewable_hunks, split_tree_copy_hunks,
)
from personas.code_reviewer.lock_sources import (
    LockCheck, is_lock_file_path, is_unchecked_lock_path, lock_source_check,
)
from personas.code_reviewer.move_detection import (
    SubtreeImport, split_move_hunks, subtree_imports,
)
from personas.code_reviewer.precedent import (
    ClassPrecision, class_precision, match_precedent, muted_classes, render_precedent_note,
)
from personas.code_reviewer.claim_check import (
    filter_novel_claim_findings,
    scan_claim_checks,
)
from personas.code_reviewer.model_metrics import emit_finding_posted
from personas import board
from personas.code_reviewer.complexity import ComplexityScan, scan_complexity_full
from personas.code_reviewer.analyzers import (
    RepoAnalyzerConfig, configured_engines, merge_analyzer_findings,
    run_analyzers,
)
from personas.code_reviewer.repo_ruff_policy import parse_repo_ruff_policy
from personas.code_reviewer.cross_file import (
    extract_symbols, fetch_cross_file_context,
)
from personas.code_reviewer.ci_status import build_ci_status_context
from personas.code_reviewer.repo_config import (
    CONFIG_PATH as _REPO_CONFIG_PATH,
    RepoConfig,
    config_note as _repo_config_note,
    parse_repo_config,
    skip_pr_rule,
    skip_summary,
    split_ignored_hunks,
    with_instructions,
)
from personas.code_reviewer.criteria_judge import criteria_judge_for
from personas.code_reviewer.deferred_ruff import (
    MAX_DEFERRED, MAX_SETTLED_TICKETS, DeferredLint, parse_deferred_lint, rule_code,
    unmet_findings,
)
from personas.code_reviewer.lint import ruff_hits
from personas.code_reviewer.lane_work import is_lane_pr, lane_work_findings
from personas.code_reviewer.merge_hold import (
    MAX_HOLDS, MAX_ISSUE_READS, find_holds, hold_findings,
)
from personas.code_reviewer.linked_issue import (
    IssueDetailsFetcher, build_linked_issue_context, linked_issue_numbers,
    rendered_criteria, rendered_criteria_with_state, rendered_health_issues,
)
from personas.code_reviewer.pinned_review import pin_review
from personas.tpm.dor_checks import loom_footer_model
from personas.tpm.persona import is_trusted_operator
from personas.tpm.ticket_compliance import closes_refs
from personas.code_reviewer.repo_docs import build_repo_docs_context
from personas.tpm.issue_fetcher import (
    build_issue_details_fetcher, build_merged_siblings_fetcher,
)
from personas.code_reviewer.omen import build_runtime_context
from personas.code_reviewer.judge import (
    advisory_when_unrefuted, eval_tags, falsification_verifier_enabled,
    falsify_with_outcome, grade_findings,
    partition_refuted, refute_with_outcome, submit_evals, verifier_rows,
)
from personas.code_reviewer.comment_budget import (
    budget_enabled, drip_fed, held_lines, is_model_blocking, split_for_budget,
)
from personas.code_reviewer.quarantine import (
    SECOND_LINEAGE_MARK,
    SINGLE_LINEAGE_NOTE,
    UNAVAILABLE_NOTE as QUARANTINE_UNAVAILABLE_NOTE,
    merge_by_fingerprint,
    quarantined_evidence,
    quarantined_pass_enabled,
    high_risk_paths,
    second_lineage_enabled,
    sensitive_paths,
)
from personas.code_reviewer.persona import (
    CodeReviewEvaluation, Finding, evaluate_diff, with_degradation,
    with_extra_findings, with_findings,
)
from personas.code_reviewer.removed_files import (
    REMOVED_REFERENCE_CHECK_FAILED, apply_removal_acceptances,
    find_referrer_candidates, removed_paths, removed_reference_findings,
    scan_guarded_deletions,
)
from personas.code_reviewer.finding_funnel import (
    CORRECTNESS_ONLY_SCOPE, FunnelResult,
    drop_non_correctness_findings, drop_unreachable_defensive_findings,
    filter_checked_shell_commands, filter_intent_mismatches, publish_funnel,
)
from personas.code_reviewer.elder_review import review_freshness_id_from_pr
from personas.code_reviewer import carry_forward, carry_verdict, hunt_link, living_review
from personas.hunt_recorder import PrRef, now as hunt_now
from personas.code_reviewer.outage_advisory import (
    ADVISORY_TITLE, advisory_summary, observe as _observe_outage,
)
from personas.code_reviewer.degraded_gate import (
    alert_operator_could_not_review, could_not_review_sentence,
    could_not_review_title, degraded_conclusion, gate_sentence,
    lead_with_degraded_line,
)
from personas.code_reviewer.publish_retry import (
    DeliveryOutcome, PendingPublication, publication_key, queue_publication,
    review_carrying_key, with_publish_marker,
)
from personas.code_reviewer.thread_resolver import is_grug, resolve_fixed_threads
from personas.code_reviewer.verify import (
    CheckEvidence,
    downgrade_bound_names,
    verify_findings,
)
from personas.tribe import CHECK_ELDER
from adapters.install_store import (  # type: ignore
    CommentFindingOrigin,
    put_comment_record,
)
from personas.code_reviewer.markings import FinishedPass, Markings, project, review_run_started
from personas.code_reviewer.markings import (  # noqa: F401 - re-exported: callers and tests import these from dispatch
    _activity_families,
    _AGENT_META_PREAMBLE,
    _agent_prompt_block,
    _category_for_finding,
    _category_for_rule,
    _checked_comment_body,
    _chip_vocabulary_drift,
    _clean_review_scope,
    _consolidated_agent_prompt,
    _CONSOLIDATED_PROMPT_BUDGET,
    _coverage_file_lines,
    _coverage_lines,
    _COVERAGE_PATHS_SHOWN,
    _defensive_drop_note,
    _defused,
    _details_block,
    _docstring_offer,
    _docstring_offer_lines,
    _EFFORT_CHIP,
    _effort_chip,
    _family_line,
    _family_note,
    _fenced,
    _findings_table,
    _hunt_section,
    _hunt_title,
    _inline_comment_body,
    _intent_paragraph,
    _is_complexity_finding,
    _lead_if_degraded,
    _LEGACY_STACK_MARKER,
    _maintainability_notes,
    _md_code_span,
    _md_table_cell,
    _move_note,
    _muted_lines,
    _path_list,
    _path_tail,
    _PERSONA,
    _PERSONA_PORTRAIT,
    _provenance_block,
    _REVIEW_PERSONA_TAG,
    _review_phase_line,
    _review_stack_body,
    _review_transparency,
    _RULES_BY_NAME,
    _SAME_FAMILY_NOTE,
    _SEVERITY_CHIP,
    _stack_closing_note,
    _stack_detail_lines,
    _STACK_MARKER,
    _stack_status_line,
    _STACK_TABLE_ROWS,
    _summary_markdown,
    _tier_served_note,
    _tier_served_text,
    _TITLE_MODELS_SHOWN,
    _tried_models_note,
    _tried_models_title,
    _unwalked_ground_note,
    _UNWALKED_PATHS_SHOWN,
    _WHY_IT_MATTERS,
    _why_it_matters,
    build_review_result,
    RETRIED_DEGRADATIONS,
    severity_chip,
    worth_an_email,
)


_drop_unreachable_defensive_findings = drop_unreachable_defensive_findings
_drop_non_correctness_findings = drop_non_correctness_findings

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.code_reviewer")


_CHECK_NAME = CHECK_ELDER
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
        timeout=DIFF_FETCH_TIMEOUT,
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
            lambda token: fetch_file_contents(
                token, owner, repo_name, changed_paths, base_ref
            ),
        ) or {}
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.info(
            "code_review_base_contents_unavailable",
            extra={"pr": f"{owner}/{repo_name}#{pull_number}", "error": str(e)},
        )
        return {}


def _fetch_head_blobs(
    installation_id: int, owner: str, repo_name: str, pull_number: int,
    head_sha: str,
) -> dict[str, str]:
    """`{path: blob sha}` for every file in the head commit's tree (#813).

    One recursive git-trees call. Best-effort: any failure or malformed
    body returns {}, which `split_tree_copy_hunks` reads as "no proof of a
    copy" and reviews everything - a missing tree can only cost budget,
    never hide a change. A truncated tree is still used: every entry it
    does list is a real blob at head."""
    if not head_sha:
        return {}

    def _get(token: str) -> Any:
        resp = httpx.get(
            f"https://api.github.com/repos/{quote(owner, safe='')}/"
            f"{quote(repo_name, safe='')}/git/trees/{quote(head_sha, safe='')}",
            params={"recursive": "1"},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=DIFF_FETCH_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    try:
        data = with_install_token_retry(installation_id, _get)
    except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as e:
        log.info(
            "code_review_head_tree_unavailable",
            extra={"pr": f"{owner}/{repo_name}#{pull_number}", "kind": type(e).__name__},
        )
        return {}
    tree = data.get("tree") if isinstance(data, dict) else None
    if not isinstance(tree, list):
        return {}
    return {
        entry["path"]: entry["sha"]
        for entry in tree
        if isinstance(entry, dict)
        and entry.get("type") == "blob"
        and isinstance(entry.get("path"), str)
        and isinstance(entry.get("sha"), str)
    }


# Most subtree imports whose split trees are fetched for one pull request.
_MAX_SUBTREE_IMPORTS = 5


# GitHub's pull request commits endpoint serves at most 250 commits, so at
# 100 per page the walk needs 3 requests and the last page comes back short.
_MAX_COMMIT_PAGES = 3


def _fetch_pr_commit_messages(
    installation_id: int, owner: str, repo_name: str, pull_number: int,
) -> tuple[str, ...]:
    """Commit messages of the pull request, every page the API serves (#772).

    Best-effort: a failed page stops the walk and keeps the messages read so
    far, so the worst case is fewer trailers seen and more files reviewed."""
    messages: list[str] = []
    for page in range(1, _MAX_COMMIT_PAGES + 1):
        batch = _fetch_commit_page(
            installation_id, owner, repo_name, pull_number, page,
        )
        if batch is None:
            break
        messages.extend(batch)
        if len(batch) < _COMMITS_PER_PAGE:
            break
    return tuple(messages)


_COMMITS_PER_PAGE = 100


def _fetch_commit_page(
    installation_id: int, owner: str, repo_name: str, pull_number: int,
    page: int,
) -> list[str] | None:
    """One page of commit messages, or None when the page cannot be read."""

    def _get(token: str) -> Any:
        resp = httpx.get(
            f"https://api.github.com/repos/{quote(owner, safe='')}/"
            f"{quote(repo_name, safe='')}/pulls/{pull_number}/commits",
            params={"per_page": str(_COMMITS_PER_PAGE), "page": str(page)},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=DIFF_FETCH_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    try:
        data = with_install_token_retry(installation_id, _get)
    except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as e:
        log.info(
            "code_review_pr_commits_unavailable",
            extra={
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "page": page,
                "kind": type(e).__name__,
            },
        )
        return None
    if not isinstance(data, list):
        return None
    return [
        str(c["commit"]["message"])
        for c in data
        if isinstance(c, dict) and isinstance(c.get("commit"), dict)
        and isinstance(c["commit"].get("message"), str)
    ]


def _fetch_subtree_context(
    installation_id: int, owner: str, repo_name: str, pull_number: int,
) -> tuple[tuple[SubtreeImport, ...], dict[str, dict[str, str]]]:
    """The pull request's subtree imports and each split commit's tree (#772).

    A split commit whose tree cannot be fetched is left out of the result
    (`_fetch_head_blobs` returns {}), so its files are reviewed in full."""
    imports = subtree_imports(
        _fetch_pr_commit_messages(installation_id, owner, repo_name, pull_number)
    )[:_MAX_SUBTREE_IMPORTS]
    trees = {
        imp.split_sha: _fetch_head_blobs(
            installation_id, owner, repo_name, pull_number, imp.split_sha,
        )
        for imp in imports
    }
    return imports, {sha: tree for sha, tree in trees.items() if tree}


def _split_copied_hunks(
    hunks: tuple[DiffHunk, ...], diff_text: str, installation_id: int,
    owner: str, repo_name: str, pull_number: int, head_sha: str,
) -> tuple[
    tuple[DiffHunk, ...], tuple[tuple[str, str], ...], tuple[tuple[str, str], ...],
]:
    """Drop byte-identical copies and moves before cohort planning.

    Returns `(hunks, duplicates, moves)`. Duplicates (#813) are copies of a
    file already at head, or one file added under two paths in the diff.
    Moves (#772) are 100% renames, a whole-file delete plus an add of the
    same blob, and `git subtree add` imports proven against the split
    commit's tree. Each is named in the summary, never silent. The trees
    and commit list are fetched only when the diff adds a file, so most
    PRs pay nothing."""
    blob_ids = new_file_blob_ids(diff_text)
    tree_copies: tuple[tuple[str, str], ...] = ()
    if any(is_tree_copy_candidate(h, blob_ids) for h in hunks):
        head_blobs = _fetch_head_blobs(
            installation_id, owner, repo_name, pull_number, head_sha,
        )
        hunks, tree_copies = split_tree_copy_hunks(hunks, blob_ids, head_blobs)
    hunks, same_diff = split_duplicate_hunks(hunks)
    imports, split_trees = (
        _fetch_subtree_context(installation_id, owner, repo_name, pull_number)
        if blob_ids else ((), {})
    )
    hunks, moves = split_move_hunks(hunks, diff_text, imports, split_trees)
    return hunks, tree_copies + same_diff, moves


def _load_repo_config(
    installation_id: int, owner: str, repo_name: str, base_ref: str,
) -> RepoConfig:
    """Parse the repo's optional `.grug.yaml` as it exists at the PR BASE.

    `base_ref` must be the base commit, never the head: a PR that edits
    `.grug.yaml` is reviewed under the OLD file, so it cannot loosen its own
    review. Absent file, empty ref, or any fetch failure = `RepoConfig()`
    (today's behavior); parsing never raises.
    """
    if not base_ref:
        return RepoConfig()
    try:
        fetched = with_install_token_retry(
            installation_id,
            lambda token: fetch_file_contents(
                token, owner, repo_name, (_REPO_CONFIG_PATH,), base_ref,
            ),
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.info(
            "repo_config_fetch_failed",
            extra={"repo": f"{owner}/{repo_name}", "error": str(e)},
        )
        return RepoConfig()
    return parse_repo_config((fetched or {}).get(_REPO_CONFIG_PATH))


def _load_repo_docs(
    token: str, owner: str, repo_name: str, base_ref: str,
    hunks: Sequence[DiffHunk],
) -> str | None:
    """CONTEXT.md and the relevant ADRs at the PR BASE. The model reads these
    as the repo's source of truth, so a PR must not be able to write them
    (#1574). The diff still shows the model any change a PR makes to them."""
    if not base_ref:
        return None
    return build_repo_docs_context(
        fetch_file=lambda path: fetch_file_contents(
            token, owner, repo_name, (path,), base_ref,
        ).get(path),
        list_dir=lambda path: _list_dir_contents(
            token, owner, repo_name, path, base_ref,
        ),
        hunks=hunks,
    )


_RUFF_POLICY_PATHS = ("ruff.toml", ".ruff.toml", "pyproject.toml")


def _load_repo_ruff_policy(installation_id: int, owner: str, repo_name: str, base_ref: str):
    """Load the first Ruff config present at the PR base revision."""
    if not base_ref:
        return parse_repo_ruff_policy(None)
    try:
        fetched = with_install_token_retry(
            installation_id,
            lambda token: fetch_file_contents(
                token, owner, repo_name, _RUFF_POLICY_PATHS, base_ref,
            ),
        ) or {}
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        log.info("repo_ruff_policy_fetch_failed", extra={"kind": type(exc).__name__})
        return parse_repo_ruff_policy(None)
    return parse_repo_ruff_policy(next((fetched[path] for path in _RUFF_POLICY_PATHS if path in fetched), None))


def steering_base_ref(
    pr: dict[str, Any], repository: dict[str, Any] | None,
    installation_id: int, owner: str, repo_name: str,
) -> str:
    """The ref every steering file is read at: `.grug.yaml`, guideline files,
    repo docs, Ruff policy and the ignore list (grug#1578).

    A PR into the default branch uses its own base commit, the protected,
    maintainer-reviewed state. A PR into any other branch uses the default
    branch head instead, since its base may be a branch the PR's author can
    push to. The head is resolved once, so every file in a pass reads one
    commit; a failed lookup falls back to the default branch name, which the
    contents API resolves itself, never to the author-writable base.

    A payload that names the base branch but not the default branch asks
    GitHub for it; if that fails too the result is "", so the pass runs with
    no steering files rather than ones its author may have written.
    """
    base = pr.get("base") or {}
    own_base = str(base.get("sha") or base.get("ref") or "")
    base_ref = str(base.get("ref") or "")
    if not base_ref:
        return own_base
    default = str(
        (repository or {}).get("default_branch")
        or (base.get("repo") or {}).get("default_branch")
        or ""
    )
    if not default:
        try:
            default = with_install_token_retry(
                installation_id,
                lambda token: _repo_default_branch(token, owner, repo_name),
            )
        except (httpx.HTTPStatusError, httpx.RequestError, KeyError, TypeError, ValueError) as e:
            log.info("steering_default_branch_unknown", extra={
                "repo": f"{owner}/{repo_name}", "base_ref": base_ref,
                "kind": type(e).__name__,
            })
            return ""
    if base_ref == default:
        return own_base
    try:
        sha = with_install_token_retry(
            installation_id,
            lambda token: _default_branch_head(token, owner, repo_name, default),
        )
    except (httpx.HTTPStatusError, httpx.RequestError, KeyError, TypeError, ValueError) as e:
        log.info("steering_ref_lookup_failed", extra={
            "repo": f"{owner}/{repo_name}", "base_ref": base_ref,
            "default_branch": default, "kind": type(e).__name__,
        })
        return default
    log.info("steering_ref_default_branch", extra={
        "repo": f"{owner}/{repo_name}", "base_ref": base_ref,
        "default_branch": default,
    })
    return sha


def _repo_default_branch(token: str, owner: str, repo_name: str) -> str:
    """The repo's default branch name, via the repos API."""
    resp = httpx.get(
        f"https://api.github.com/repos/{owner}/{repo_name}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=DIFF_FETCH_TIMEOUT,
    )
    resp.raise_for_status()
    name = str(resp.json()["default_branch"])
    if not name:
        raise ValueError("empty default branch")
    return name


def _default_branch_head(
    token: str, owner: str, repo_name: str, branch: str,
) -> str:
    """The commit `branch` points at, via the refs API."""
    from urllib.parse import quote

    resp = httpx.get(
        f"https://api.github.com/repos/{owner}/{repo_name}/git/ref/heads/"
        f"{quote(branch, safe='/')}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=DIFF_FETCH_TIMEOUT,
    )
    resp.raise_for_status()
    sha = str(resp.json()["object"]["sha"])
    if not sha:
        raise ValueError("empty sha")
    return sha


def _load_pr_repo_config(
    pr: dict[str, Any], installation_id: int, owner: str, repo_name: str,
    *, steering_ref: str | None = None,
    repository: dict[str, Any] | None = None,
) -> RepoConfig:
    """`.grug.yaml` at the PR's steering ref (`steering_base_ref`), never the
    head and never the living-hunt baseline. `steering_ref`, when given, is
    the one already resolved for this pass; otherwise it is resolved from the
    PR and the webhook `repository`."""
    if steering_ref is None:
        steering_ref = steering_base_ref(
            pr, repository, installation_id, owner, repo_name,
        )
    return _load_repo_config(installation_id, owner, repo_name, steering_ref)


def pr_skip_rule(
    pr: dict[str, Any], installation_id: int, owner: str, repo_name: str,
    config: RepoConfig | None = None,
    repository: dict[str, Any] | None = None,
) -> str | None:
    """The base-ref `.grug.yaml` `skip_prs` rule this PR matches, or None
    (grug#1439). Shared by Elder and Teller; Chief and Guard never call it.
    `repository` is the webhook's, which names the default branch (#1578)."""
    if config is None:
        config = _load_pr_repo_config(
            pr, installation_id, owner, repo_name, repository=repository,
        )
    return skip_pr_rule(
        config,
        author=str((pr.get("user") or {}).get("login") or ""),
        labels=_pr_labels(pr),
        title=str(pr.get("title") or ""),
    )


def _publish_config_skip(
    installation_id: int, owner: str, repo_name: str, pull_number: int,
    head_sha: str, *, rule: str,
) -> dict[str, str]:
    """Complete the Elder check `success` for a PR skipped by `skip_prs`.

    No review, no inline comment, no board: the maintainer opted this PR
    out on the base branch, so the check passes and says why. A publish
    failure returns `publish_failed` so the durable consumer redrives
    instead of leaving the enqueue's in_progress check behind."""
    summary = skip_summary(rule)
    try:
        with_install_token_retry(
            installation_id,
            lambda token: post_check_run(
                token, owner, repo_name,
                CheckRunResult(
                    name=_CHECK_NAME, head_sha=head_sha, status="completed",
                    conclusion="success", title="Elder skipped this PR",
                    summary=summary,
                ),
                external_id=(
                    f"grug-cr:{owner}/{repo_name}#{pull_number}:{head_sha}"
                ),
            ),
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.error(
            "code_review_config_skip_publish_failed",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(e).__name__,
            },
        )
        return {"persona": "code_reviewer", "result": "publish_failed"}
    log.info(
        "code_review_skipped_by_config",
        extra={"pr": f"{owner}/{repo_name}#{pull_number}", "rule": rule},
    )
    return {"persona": "code_reviewer", "result": "pass", "skipped_by": rule}


def _apply_repo_config(
    hunks: tuple[DiffHunk, ...], pr: dict[str, Any], installation_id: int,
    owner: str, repo_name: str, config: RepoConfig | None = None,
) -> tuple[tuple[DiffHunk, ...], RepoConfig, str, tuple[str, ...]]:
    """Load `.grug.yaml` from the PR base and drop `ignore`d hunks.

    Returns (kept hunks, config, summary note, ignored paths). `config`, when given, is
    the one already loaded for this pass, so the file is fetched once."""
    if config is None:
        config = _load_pr_repo_config(pr, installation_id, owner, repo_name)
    kept, ignored = split_ignored_hunks(hunks, config)
    return kept, config, _repo_config_note(config, ignored), ignored


def _list_dir_contents(
    install_token: str,
    owner: str,
    repo: str,
    dir_path: str,
    ref: str,
) -> list[str] | None:
    """List entry names under `dir_path` at `ref` via the contents API
    (a directory listing returns a JSON array, not raw). Best-effort: any
    failure returns None — the caller degrades, never raises."""
    from urllib.parse import quote

    try:
        resp = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/contents/{quote(dir_path, safe='/')}",
            params={"ref": ref},
            headers={
                "Authorization": f"Bearer {install_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=DIFF_FETCH_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            return None
        return [e["name"] for e in data if isinstance(e, dict) and e.get("name")]
    except (httpx.HTTPStatusError, httpx.RequestError, ValueError):
        return None


# When a PR only retouches k8s/docs comments about settle/deep policy, the
# implementation source of truth may not be in `changed_paths`. Pull these
# known helpers (best-effort) so the claim-check detector can still compare
# numbers. Harmless no-ops on foreign repos (404 -> skip).
_CLAIM_CHECK_POLICY_PATHS: tuple[str, ...] = (
    "services/_shared/personas/code_reviewer/elder_review.py",
    "services/_shared/llm_client.py",
)
_CLAIM_HINT_RE = re.compile(
    r"(?i)(?:settle|steady\s+hunt|swift\s+hunt|deep[_\s-]?diff|"
    r"GRUG_DEEP_DIFF|GRUG_ELDER_SETTLE|min\(\s*base)",
)


def _merge_lint_evidence(
    evaluation, hunks, file_contents, ruff_policy=None, policy_dropped=None,
    *, semgrepignore=None, skipped=None, engines=None,
):
    """Fold the configured OSS analyzers' findings into the evaluation
    (#681 / #1254, epic #707).

    RECALL work: ruff (Python) and opengrep over grug's own rule pack
    (JavaScript/TypeScript) catch the mechanical class the model skims past,
    and cannot hallucinate because the findings are measured rather than
    generated. Merged with the other deterministic sources - i.e. AFTER the
    verification pass and refute gate, both of which exist to police MODEL
    judgment and should not second-guess a linter. An analyzer finding on the
    same line as one of the model's own findings is folded into it rather
    than posted twice (`merge_analyzer_findings`).

    Extracted rather than inlined so it costs `dispatch_code_review` ONE
    branch: that function's decomposition is #725's job, not this slice's.
    Inert unless GRUG_ELDER_ANALYZERS (or the older GRUG_LINT_EVIDENCE=ruff)
    names an engine. `skipped`, when given, collects (engine, reason) for the
    check summary.
    """
    engines = configured_engines() if engines is None else engines
    if not engines:
        return evaluation
    try:
        run = run_analyzers(
            hunks, file_contents or {}, engines,
            RepoAnalyzerConfig(ruff_policy=ruff_policy, semgrepignore=semgrepignore),
        )
    except Exception:  # noqa: BLE001 - lint evidence must never break a review
        log.warning("code_review_lint_scan_failed", exc_info=True)
        return evaluation
    if policy_dropped is not None and run.policy_dropped:
        policy_dropped.append(run.policy_dropped)
    if skipped is not None:
        skipped.extend(run.skipped)
    if not run.findings:
        return evaluation
    existing, extra = merge_analyzer_findings(evaluation.findings, run.findings)
    log.info("code_review_lint_findings", extra={
        "count": len(extra), "folded": len(run.findings) - len(extra),
        "engines": ",".join(run.ran),
    })
    return with_extra_findings(with_findings(evaluation, existing), extra)


def _analyzer_skip_note(skipped: list[tuple[str, str]]) -> str:
    """One summary line naming each analyzer that degraded to a skip."""
    if not skipped:
        return ""
    return "\n\nAnalyzers skipped: " + ", ".join(
        f"{engine} ({reason})" for engine, reason in skipped
    ) + ". The review went on without them."


_SEMGREPIGNORE_PATH = ".semgrepignore"


def _load_repo_semgrepignore(
    installation_id: int, owner: str, repo_name: str, base_ref: str,
) -> str | None:
    """The repo's `.semgrepignore` at the PR base, or None. Read at the BASE
    so a PR cannot widen its own ignore list to hide its own findings."""
    if not base_ref:
        return None
    try:
        fetched = with_install_token_retry(
            installation_id,
            lambda token: fetch_file_contents(
                token, owner, repo_name, (_SEMGREPIGNORE_PATH,), base_ref,
            ),
        ) or {}
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        log.info("repo_semgrepignore_fetch_failed", extra={"kind": type(exc).__name__})
        return None
    return fetched.get(_SEMGREPIGNORE_PATH)


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
            lambda token: fetch_file_contents(
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


# "rerun" is a `/grug improve` re-review (rerun._dispatch_elder_rerun). It
# re-reads a PR that already carries Elder threads, so it carries earlier
# findings forward and withdraws a block it clears like any re-review
# (grug#1581: without it a clean `/grug improve` left the block standing).
_RE_REVIEW_ACTIONS = {"synchronize", "reopened", "review", "rerun"}


def observe_outage(
    evaluation: CodeReviewEvaluation, *, blocking: bool, record: bool = True,
):
    """ADR-0027: is this degraded BLOCK-mode review part of a sustained
    backend outage? `record` moves the install's outage streak; the deep
    append only reads it."""
    return _observe_outage(
        evaluation.degraded_reason, evaluation.coverage,
        blocking=blocking, record=record,
    )


def _carry_into_check(
    *, action: str, evaluation: CodeReviewEvaluation, mode: ReviewMode,
    installation_id: int, owner: str, repo_name: str, pull_number: int,
    head_sha: str, conclusion: CheckConclusion, title: str, summary: str,
    threads: list[dict] | None = None,
) -> tuple[CheckConclusion, str, str, list[dict] | None, bool]:
    """Fold Elder's earlier findings into the check verdict: still-open ones
    via carry-forward, and ones this push addressed as a title note (#1187),
    which the Activity feed records as the review's summary.

    One thread listing per re-review, returned so the thread resolver and
    living review reuse it. A listing failure leaves the check as it was.
    The last value says a carried high/critical finding still stands, in
    either mode, for the review verdict (#1758). Never raises.
    """
    if action not in _RE_REVIEW_ACTIONS or evaluation.degraded_reason:
        return conclusion, title, summary, None, False
    if threads is None:
        threads = carry_forward.list_threads(
            installation_id, owner, repo_name, pull_number,
        )
    if threads is None:
        return conclusion, title, summary, None, False
    carried = carry_forward.select_carried(threads, evaluation.findings)
    new_conclusion, title, summary = carry_forward.apply_to_check(
        carried, conclusion=conclusion, title=title, summary=summary,
        blocking_mode=mode == "blocking",
    )
    addressed = living_review.count_addressed(
        threads, living_review.ReviewEvidence(head_sha, evaluation.findings),
    )
    title = living_review.note_on_title(title, addressed, head_sha)
    carried_blocking = any(c.blocking for c in carried)
    return new_conclusion, title, summary, threads, carried_blocking  # type: ignore[return-value]


def _head_is_same_repo(pr: dict, repo: dict) -> bool:
    """True when the PR's head branch lives in the base repository, so the
    pusher has write access. A fork, or a deleted fork's null head repo, is
    not; living review lets only a trusted push teach the ledger (#1187)."""
    head_repo = (pr.get("head") or {}).get("repo") or {}
    return head_repo.get("id") is not None and head_repo.get("id") == repo.get("id")


def _tidy_fixed_threads(
    *, action: str, review_published: bool, evaluation: CodeReviewEvaluation | None,
    installation_id: int, owner: str, repo_name: str, pull_number: int,
    head_sha: str, threads: list[dict] | None = None,
    pr_author: str | None = None, trusted_push: bool = False,
) -> int:
    """Resolve Elder's own fixed threads after a complete re-review (#1102),
    then edit each Elder finding in place to its current state (#1187).

    Only a complete, published re-review may close threads or mark a
    finding addressed: a degraded or partial review has no findings because
    it did not look, and its silence must not read as "fixed". Living review
    reuses the listing taken before the resolver ran, so a thread the
    resolver just closed still reads as changed, and reads addressed; with
    no listing it is skipped (the failure is already logged).
    """
    # Any re-review of a PR that may already carry Elder threads. Production
    # reviews arrive through the durable queue as action="review"
    # (rerun._review_payload), so "synchronize" alone never matched live;
    # a `/grug improve` re-review arrives as action="rerun".
    if (
        action not in _RE_REVIEW_ACTIONS
        or not review_published
        or evaluation is None
        or evaluation.degraded_reason
    ):
        return 0
    resolved = 0
    try:
        resolved = resolve_fixed_threads(
            installation_id, owner, repo_name, pull_number,
            head_sha=head_sha, findings=evaluation.findings, threads=threads,
            pr_author=pr_author,
        )
    except Exception as e:  # noqa: BLE001 - tidying never fails a review
        log.warning(
            "elder_thread_resolve_crashed",
            extra={
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(e).__name__,
            },
        )
    if threads is not None:
        living_review.sync_pr(
            installation_id, owner, repo_name, pull_number, threads=threads,
            review=living_review.ReviewEvidence(
                head_sha, evaluation.findings, trusted_push=trusted_push,
            ),
        )
    return resolved


def _to_llm_hunks(hunks: tuple[DiffHunk, ...]) -> list[LlmHunk]:
    """DiffHunks (parser shape) → llm_client.Hunk (review-input shape).
    The LLM only needs the per-file body; line-number bookkeeping is
    already in `new_lines` for the post-hoc hallucination filter."""
    return [LlmHunk(path=h.file_path, body=h.body) for h in hunks]


def _author_family(pr: Mapping[str, Any]) -> str | None:
    """The family of the model a trusted operator's Loom footer names, or None.

    For trusted operator humans, uses the Loom footer. For App authors (bots),
    uses a trusted source mapping App logins to model families (grug#1553).
    Both are "trusted source" but differ in how the family is derived.
    """
    if is_trusted_operator(pr):
        return model_family(loom_footer_model(str(pr.get("body") or "")))

    user = pr.get("user")
    if isinstance(user, Mapping) and user.get("type") == "Bot":
        app_login = user.get("login")
        return app_author_family(str(app_login) if app_login else None)

    return None


_STACK_COMMENT_TIMEOUT = 10.0


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
            found = board_client.find_board(
                token, owner, repo, pr_number, app_id=get_app_id(),
            )
            if found is not None:
                _, existing_board = found
                existing_elder = board.extract_section(existing_board, "elder") or ""
                if "**Closed threads**" in existing_elder and "**Closed threads**" not in section:
                    closed_part = existing_elder[existing_elder.index("**Closed threads**"):]
                    section = f"{section.rstrip()}\n\n{closed_part.strip()}"
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


def _muted_classes_for(
    repo_full: str, repo_config: RepoConfig | None,
) -> dict[str, ClassPrecision]:
    """Finding classes muted by their measured precision (#1436).

    Best-effort like the precedent notes: a ledger failure mutes nothing, so
    the review posts every finding inline rather than hiding any."""
    cfg = repo_config or RepoConfig()
    try:
        from adapters.install_store import list_ledger_rows  # type: ignore
        from ledger import parse_row

        raw = list_ledger_rows(repo_full) or []
        rows = [r for r in (parse_row(d) for d in raw) if r is not None]
        return muted_classes(
            class_precision(rows),
            min_labeled=cfg.elder_mute_min_labeled,
            below_precision=cfg.elder_mute_below_precision,
            locked=frozenset(cfg.elder_mute_locked_classes),
        )
    except Exception as e:  # noqa: BLE001 - muting is tuning, never load-bearing
        log.info("muted_classes_unavailable", extra={"repo": repo_full, "kind": type(e).__name__})
        return {}


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


def _pair_comments(
    comments: list[dict], findings: tuple[Finding, ...],
) -> list[tuple[int, Finding]]:
    """Match each posted inline comment to the Finding that produced it, by
    (file, RULE) - recovered from the comment's `path` and its hidden
    `<!-- grug-rule:NAME -->` marker (parsed by `parse_rule`, the same marker
    `dedup` uses) - never by re-deriving a line number from anything GitHub
    computes. See `_capture_comment_records` for why, and for the FIFO rule
    that settles two findings of one rule in one file."""
    queues: dict[tuple[str, str], deque[Finding]] = {}
    for f in findings:
        queues.setdefault((f.file, f.rule_name), deque()).append(f)
    pairs: list[tuple[int, Finding]] = []
    for c in comments:
        cid, path = c.get("id"), c.get("path")
        if cid is None or path is None:
            continue
        rule = parse_rule(c.get("body", ""))
        if rule is None:
            continue
        queue = queues.get((path, rule))
        if queue:
            pairs.append((int(cid), queue.popleft()))
    return pairs


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
    `build_review_result` told GitHub to place it at when building this
    same review. Reading it back from GitHub's response was always
    redundant, and the one field this function actually needs from that
    response — the comment `id`, for reaction polling — is present
    immediately, no matter how long the human-readable fields take.

    (file, rule) can collide — two findings for the SAME rule can land in
    the same file at different lines in one batch — so each (file, rule)
    maps to a FIFO queue of findings in the order `build_review_result`
    listed them, and each matching comment claims the next one off that
    queue. This assumes GitHub returns one review's own comments in the
    order they were submitted (true for comments belonging to a single
    review — see `get_review_comments`'s docstring), but only WITHIN a
    (file, rule) group, not across the whole list — so an unrelated extra
    comment, or a comment for a rule that already exhausted its queue,
    is simply skipped, same as a stale prior-review comment always was.
    Best-effort per comment: a single DDB blip is skipped, never raised.
    Returns count persisted."""
    persisted = 0
    for cid, finding in _pair_comments(comments, findings):
        finding_origins, fallback_span_context = _comment_record_origins(
            finding, review_span_context,
        )
        try:
            put_comment_record(
                install_id=install_id,
                comment_id=cid,
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
            emit_finding_posted(finding_origins)
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


def _repo_complexity_caps(
    installation_id: int, repo_id: int,
) -> tuple[int | None, int | None]:
    """Per-repo (cyclomatic, cognitive) caps from repo config (grug#1041).

    None means "use the scanner's env default". Best-effort: a config-store
    hiccup must not fail a review, so any error returns (None, None)."""
    try:
        from adapters.install_store import get_repo_config

        cfg = get_repo_config(installation_id, repo_id)
        return (
            cfg.get("complexity_cyclomatic_cap"),
            cfg.get("complexity_cognitive_cap"),
        )
    except Exception as e:  # noqa: BLE001 - enrichment must never abort a review
        log.info(
            "code_review_complexity_caps_unavailable",
            extra={"kind": type(e).__name__},
        )
        return None, None


# GitHub's answers for an issue that does not exist or was deleted.
_ISSUE_ABSENT = (httpx.codes.NOT_FOUND, httpx.codes.GONE)


def _settled_tickets(body: str, *, owner: str, repo_name: str) -> list[int]:
    """Tickets this PR must settle: those it closes, every link of a lane PR."""
    closes = set(closes_refs(body))
    lane = is_lane_pr(body)
    return [
        n for n in linked_issue_numbers(body, owner=owner, repo=repo_name, limit=None)
        if lane or n in closes
    ]


def _note_deferred_lint(
    pr_context: PrContext, fetch_issue: IssueDetailsFetcher, *,
    owner: str, repo_name: str,
) -> None:
    """Put the deferred ruff findings of the tickets this PR settles on
    `pr_context` (#1477), and why that list is incomplete. An issue GitHub
    does not have defers nothing; any other failure is recorded, so the
    re-check degrades the review. Bounded here: the criteria block's cap on
    linked issues would skip a ticket silently."""
    body = str(pr_context.get("body") or "")
    unfinished: list[str] = []
    deferred: list[DeferredLint] = []
    try:
        settled = _settled_tickets(body, owner=owner, repo_name=repo_name)
        if len(settled) > MAX_SETTLED_TICKETS:
            unfinished.append("too_many_tickets")
        for number in settled[:MAX_SETTLED_TICKETS]:
            try:
                issue = fetch_issue(number)
            except httpx.HTTPStatusError as e:
                if e.response.status_code not in _ISSUE_ABSENT:
                    unfinished.append("ticket_unread")
                continue
            except httpx.RequestError:
                unfinished.append("ticket_unread")
                continue
            text = issue if isinstance(issue, str) else issue.get("body") or ""
            deferred.extend(parse_deferred_lint(
                text, issue=number, owner=owner, repo=repo_name,
            ))
    except Exception as e:  # noqa: BLE001 - recorded: the review degrades on it
        log.warning("code_review_deferred_tickets_failed", extra={"kind": type(e).__name__})
        unfinished.append("ticket_unread")
    if len(deferred) > MAX_DEFERRED:
        unfinished.append("too_many_findings")
    if deferred:
        pr_context["deferred_lint"] = tuple(deferred[:MAX_DEFERRED])
    if unfinished:
        pr_context["deferred_lint_unfinished"] = tuple(dict.fromkeys(unfinished))


def _read_issue_or_none(fetch_issue: IssueDetailsFetcher, number: int) -> dict | None:
    """One issue read for the merge-hold check; a failed read is None, so
    an unreadable issue holds nothing rather than blocking on a guess."""
    try:
        issue = fetch_issue(number)
    except Exception as e:  # noqa: BLE001 - the IO boundary only: a read that fails holds nothing
        log.info(
            "code_review_merge_hold_read_failed",
            extra={"issue": number, "kind": type(e).__name__},
        )
        return None
    return issue if isinstance(issue, dict) else None


def _note_open_merge_holds(
    pr_context: PrContext, fetch_issue: IssueDetailsFetcher, *,
    owner: str, repo_name: str,
) -> None:
    """Put the merge holds still waiting on an open issue on `pr_context`
    (grug#1766): the PR body's and its linked tickets'. The linked tickets
    are the work itself, open until this PR lands, so they never count as a
    hold. A failed GitHub read holds nothing (`_read_issue_or_none`); the
    parsing is pure, so any other error is a bug and is left to raise."""
    body = str(pr_context.get("body") or "")
    linked = linked_issue_numbers(body, owner=owner, repo=repo_name)
    holds = list(find_holds(body, source="the PR body", owner=owner, repo=repo_name))
    for number in linked:
        issue = _read_issue_or_none(fetch_issue, number) or {}
        holds.extend(find_holds(
            str(issue.get("body") or ""), source=f"ticket #{number}",
            owner=owner, repo=repo_name,
        ))
    cited = list(dict.fromkeys(
        n for hold in holds[:MAX_HOLDS] for n in hold.refs if n not in linked
    ))
    open_refs = frozenset(
        n for n in cited[:MAX_ISSUE_READS]
        if (_read_issue_or_none(fetch_issue, n) or {}).get("state") == "open"
    )
    unchecked = max(0, len(cited) - MAX_ISSUE_READS)
    if open_refs or unchecked:
        pr_context["merge_holds"] = tuple(holds[:MAX_HOLDS])
        pr_context["merge_hold_open_refs"] = open_refs
        pr_context["merge_hold_unchecked"] = unchecked


def _merge_open_merge_holds(
    evaluation: CodeReviewEvaluation, hunks: tuple[DiffHunk, ...],
    pr_context: PrContext,
) -> CodeReviewEvaluation:
    """Add a blocking finding per merge hold still waiting on an open issue
    (grug#1766). Pure over what `_note_open_merge_holds` recorded."""
    found = hold_findings(
        tuple(pr_context.get("merge_holds") or ()),
        frozenset(pr_context.get("merge_hold_open_refs") or ()),
        hunks,
        unchecked=int(pr_context.get("merge_hold_unchecked") or 0),
    )
    return with_extra_findings(evaluation, found)


def _log_omit_markings(
    evaluation: CodeReviewEvaluation, *, pr: str, head_sha: str,
) -> None:
    """One line per marking dropped because its own text says to omit it
    (grug#1529), to measure how often the model skips the structured verdict."""
    for rule in evaluation.omit_dropped_rules:
        log.info(
            "code_review_omit_marking_dropped",
            extra={"pr": pr, "head_sha": head_sha[:8], "rule": rule},
        )


def _attach_linked_issue_context(
    pr_context: PrContext, *, installation_id: int, owner: str, repo_name: str,
) -> None:
    """Put the linked-issue criteria block on `pr_context` (#904) so every
    cohort and the deep pass see it. Any failure leaves the block off =
    today's review. The tickets this PR settles are read first, under their
    own bound, and the block reuses those reads (#1477)."""
    try:
        fetch_issue = functools.lru_cache(maxsize=None)(build_issue_details_fetcher(
            installation_id=installation_id, owner=owner, repo=repo_name,
        ))
        _note_deferred_lint(pr_context, fetch_issue, owner=owner, repo_name=repo_name)
        _note_open_merge_holds(pr_context, fetch_issue, owner=owner, repo_name=repo_name)
        live_criteria_count: list[int] = []
        linked = build_linked_issue_context(
            str(pr_context.get("body") or ""),
            fetch_issue=fetch_issue,
            owner=owner,
            repo=repo_name,
            fetch_siblings=build_merged_siblings_fetcher(
                installation_id=installation_id, owner=owner, repo=repo_name,
                pr_number=int(pr_context.get("pr_number") or 0),
            ),
            live_criteria_count=live_criteria_count,
        )
        if sum(live_criteria_count):
            pr_context["linked_issue_live_criteria_count"] = sum(live_criteria_count)
            pr_context["linked_issue_numbers"] = linked_issue_numbers(
                str(pr_context.get("body") or ""), owner=owner, repo=repo_name,
            )
        if linked:
            pr_context["linked_issue_context"] = linked
    except Exception as e:  # noqa: BLE001 - additive; never break the review
        log.info(
            "linked_issue_context_degraded",
            extra={
                "stage": "dispatch",
                "pr": f"{owner}/{repo_name}#{pr_context.get('pr_number')}",
                "kind": type(e).__name__,
            },
        )
        # No fetcher means no ticket was read. A PR that settles none still
        # has nothing to measure; one that settles any must not pass unmeasured.
        if pr_context.get("deferred_lint") is None and _settled_tickets(
            str(pr_context.get("body") or ""), owner=owner, repo_name=repo_name,
        ):
            pr_context["deferred_lint_unfinished"] = ("ticket_unread",)


def _pinned_issues(
    pr_context: "PrContext | dict[str, Any]", owner: str, repo: str,
) -> tuple[int, ...]:
    """The issue(s) the PR links, for the review's pin line (grug#1429)."""
    return tuple(linked_issue_numbers(
        str(pr_context.get("body") or ""), owner=owner, repo=repo,
    ))


def _linked_issue_summary_note(
    linked_context: str, live_count: int = 0, linked_numbers: tuple[int, ...] = (),
) -> str:
    """Summarize which linked issue criteria were included in the review."""
    criteria = rendered_criteria(linked_context)
    if not criteria and not live_count:
        return ""
    issue_numbers = list(dict.fromkeys(
        linked_numbers or [issue for issue, _, _ in criteria],
    ))
    summary = (
        "\n\nLinked issue criteria read: "
        + ", ".join(f"#{number}" for number in issue_numbers)
        + f" ({len(criteria)} criteria"
    )
    if live_count:
        summary += f"; {live_count} live criteria left for after deploy"
    return summary + ")."


def _pr_labels(pr: dict[str, Any]) -> frozenset[str]:
    """The PR's label names, trimmed and lowercased; non-dict entries skipped."""
    return frozenset(
        str(label.get("name") or "").strip().lower()
        for label in (pr.get("labels") or [])
        if isinstance(label, dict)
    )


def _is_documentation_path(path: str) -> bool:
    path = path.lower()
    return path.endswith((".md", ".txt")) or path.startswith("docs/") or "/docs/" in path


def _is_test_path(path: str) -> bool:
    path = path.lower()
    name = path.split("/")[-1]
    return (
        "/tests/" in f"/{path}/"
        or path.startswith(("tests/", "test/"))
        or name.startswith("test_")
        or name.endswith("_test.py")
    )


def _correctness_only_scope(paths: tuple[str, ...], labels: frozenset[str]) -> str:
    """The review scope note for a docs/tests change, or "" for a full review.

    A diff touching only documentation and tests, or a PR labelled
    class:docs / class:tests, gets correctness findings only (#1172)."""
    docs_or_tests_only = bool(paths) and all(
        _is_documentation_path(path) or _is_test_path(path) for path in paths
    )
    if docs_or_tests_only or "class:docs" in labels or "class:tests" in labels:
        return CORRECTNESS_ONLY_SCOPE
    return ""


def _accept_guarded_removals(
    guarded: tuple[Finding, ...], *, installation_id: int, owner: str,
    repo_name: str, pull_number: int, head_sha: str,
) -> tuple[tuple[Finding, ...], str]:
    """Clear each guard finding a trusted maintainer accepted on this head
    with `/grug accept-removal <path>` (#1405). The store row is keyed by the
    head SHA, so a new head finds nothing. A store that cannot be read
    clears nothing. Returns the findings that still stand and a summary
    note naming who accepted which path on which head."""
    if not guarded:
        return guarded, ""
    pr = f"{owner}/{repo_name}#{pull_number}"
    try:
        from adapters.install_store import get_removal_acceptances  # type: ignore

        accepted = get_removal_acceptances(
            install_id=installation_id, repo=f"{owner}/{repo_name}",
            pr_number=pull_number, head_sha=head_sha,
        )
    except Exception as e:  # noqa: BLE001 - an unread acceptance clears nothing
        log.warning(
            "code_review_removal_acceptances_unreadable",
            extra={"pr": pr, "kind": type(e).__name__},
        )
        return guarded, (
            "\n\nGrug could not read accepted removals for this head, so every "
            "deleted guard still blocks."
        )
    kept, cleared = apply_removal_acceptances(guarded, accepted or {})
    if not cleared:
        return kept, ""
    log.info(
        "code_review_removal_accepted",
        extra={"pr": pr, "head_sha": head_sha[:8], "cleared": len(cleared)},
    )
    lines = "".join(
        f"\n- `{path}`: removal accepted by `{by}` on `{head_sha[:8]}`."
        for path, by in cleared
    )
    return kept, (
        "\n\nAccepted removals on this head (a new push needs a new "
        "`/grug accept-removal`):" + lines
    )


def _delta_is_empty(diff_text: str) -> bool:
    """A Living Hunt delta with no change at all: no hunk and no deleted file.

    grug#1644: an empty commit since the last completed Elder head leaves
    such a delta. Reviewed as is it publishes neutral no_diff, which never
    clears a blocking gate, so the caller reviews the full PR diff on this
    head instead: the same review `/grug improve` runs. A deletion-only
    delta is not empty; it keeps its own path (#1401)."""
    return not parse_diff(diff_text) and not removed_paths(diff_text)


def _delta_is_excluded_metadata_only(diff_text: str) -> bool:
    """A nonempty delta whose hunks the canonical metadata filter excludes (#1730)."""
    hunks = parse_diff(diff_text)
    return bool(hunks) and not removed_paths(diff_text) and not split_reviewable_hunks(hunks)[0]


def _is_lock_only(diff_text: str) -> bool:
    """Every changed file, both sides of a rename included, is a listed
    lock file (#1649). An empty diff is not lock-only."""
    paths = [p for c in file_changes(diff_text) for p in (c.path, c.previous_path) if p]
    return bool(paths) and all(is_lock_file_path(p) for p in paths)


def _keep_unchecked_locks(
    parsed_hunks: tuple[DiffHunk, ...], kept: tuple[DiffHunk, ...],
    excluded_paths: tuple[str, ...],
) -> tuple[tuple[DiffHunk, ...], tuple[str, ...]]:
    """Put back the lock files the source check cannot parse, so the model
    reviews them in a mixed diff too (#1649). Diff order is kept."""
    kept_ids = {id(h) for h in kept}
    hunks = tuple(
        h for h in parsed_hunks
        if id(h) in kept_ids or is_unchecked_lock_path(h.file_path)
    )
    return hunks, tuple(p for p in excluded_paths if not is_unchecked_lock_path(p))


_UNREVIEWED_NAMED = 20


def _unreviewed_paths(
    *, excluded: tuple[str, ...], oversized: tuple[str, ...], ignored: tuple[str, ...],
) -> tuple[str, ...]:
    """Changed paths no model read on this head (#1781): data, generated or
    vendored files, hunks too big for any cohort, and `.grug.yaml` ignores.
    Lock files are left to the lock check (`lock_check.unreviewed`). Proven
    copies and moves are not here: their content is byte-identical to code
    already at head. Deduped, in order."""
    return tuple(dict.fromkeys(
        p for p in (*excluded, *oversized, *ignored) if not is_lock_file_path(p)
    ))


def _unreviewed_note(paths: tuple[str, ...]) -> str:
    """The summary paragraph that names the unreviewed paths, or ""."""
    if not paths:
        return ""
    named = ", ".join(f"`{p}`" for p in paths[:_UNREVIEWED_NAMED])
    more = (
        f", and {len(paths) - _UNREVIEWED_NAMED} more"
        if len(paths) > _UNREVIEWED_NAMED else ""
    )
    return (
        f"\n\nElder does not approve this head: {len(paths)} changed path(s) "
        f"were not reviewed: {named}{more}."
    )


def _lock_findings(
    parsed_hunks: tuple[DiffHunk, ...], *, installation_id: int, owner: str,
    repo_name: str, head_sha: str, base_sha: str,
    reviewed: frozenset[str],
) -> LockCheck:
    """The deterministic lock-source check for this diff (#1649): a second
    layer on top of the model review, not the gate."""
    return lock_source_check(
        parsed_hunks,
        fetch=lambda paths, ref: with_install_token_retry(
            installation_id,
            lambda token: fetch_file_contents(
                token, owner, repo_name, paths, ref, only_404_skips=True,
            ),
        ),
        head_sha=head_sha, base_sha=base_sha,
        reviewed=reviewed,
    )


def _fetch_full_pr_diff(
    installation_id: int, owner: str, repo_name: str, pull_number: int, *,
    base_sha: str, head_sha: str,
) -> str:
    """The whole PR diff (base..head), as a review with no stored delta reads it."""
    return with_install_token_retry(
        installation_id,
        lambda token: fetch_pr_diff(
            token, owner, repo_name, pull_number,
            base_sha=base_sha, head_sha=head_sha,
        ),
    )


def _review_deletion_only(
    evaluation: CodeReviewEvaluation, diff_text: str,
    parsed_hunks: tuple[DiffHunk, ...], *, installation_id: int, owner: str, repo_name: str, pull_number: int,
    head_sha: str,
) -> tuple[CodeReviewEvaluation, str]:
    """A pull request that only deletes files gets a verdict, not no_diff (#1401).

    `parse_diff` keeps no hunk for a deleted file, so a pure revert reached
    the model with nothing and published a neutral "eyes clouded
    (no_diff)" that never clears a blocking gate. When the diff deletes
    files and has no other hunk, the verdict comes from the deterministic
    checks in `removed_files`: a deleted guard path is a high finding with
    no ticket downgrade (a linked issue can be the PR author's own) unless
    a trusted maintainer accepted that path on this head (#1405), and a
    file at head that still names a removed path is a finding too. A referrer check that cannot finish
    degrades to `REMOVED_REFERENCE_CHECK_FAILED`, never a pass. Returns the
    evaluation and a summary note; any other diff is returned unchanged
    with no note."""
    if evaluation.degraded_reason != "no_diff" or parsed_hunks:
        return evaluation, ""
    removed = removed_paths(diff_text)
    if not removed:
        return evaluation, ""
    pr = f"{owner}/{repo_name}#{pull_number}"
    guarded, accepted_note = _accept_guarded_removals(
        scan_guarded_deletions(removed, head_sha), installation_id=installation_id,
        owner=owner, repo_name=repo_name, pull_number=pull_number,
        head_sha=head_sha,
    )
    try:
        referenced = removed_reference_findings(
            removed,
            search=lambda paths: with_install_token_retry(
                installation_id,
                lambda token: find_referrer_candidates(token, owner, repo_name, paths),
            ),
            fetch_at_head=lambda paths: with_install_token_retry(
                installation_id,
                lambda token: fetch_file_contents(token, owner, repo_name, paths, head_sha),
            ),
        )
        degraded: str | None = None
    except Exception as e:  # noqa: BLE001 - an unfinished check degrades, never passes
        log.warning(
            "code_review_removed_reference_check_failed",
            extra={"pr": pr, "kind": type(e).__name__, "error": str(e)[:200]},
        )
        referenced, degraded = (), REMOVED_REFERENCE_CHECK_FAILED
    findings = guarded + referenced
    log.info(
        "code_review_deletion_only",
        extra={
            "pr": pr, "removed": len(removed), "guarded": len(guarded),
            "referenced": len(referenced), "degraded": degraded,
        },
    )
    base = CodeReviewEvaluation(
        findings=(), conclusion="neutral" if degraded else "success",
        degraded_reason=degraded,
    )
    referrers = (
        "could not finish checking whether any file at this head still names "
        "them, so this is not a pass" if degraded
        else "checked whether any file at this head still names them"
    )
    note = (
        f"\n\nDeletion-only diff: {len(removed)} deleted file(s). Grug "
        f"checked them for removed guards and {referrers}."
        + (" See the markings above." if findings else "")
        + accepted_note
    )
    return with_extra_findings(base, findings), note


def _merge_lane_work(
    evaluation: CodeReviewEvaluation, hunks: tuple[DiffHunk, ...],
    diff_text: str, file_contents: dict[str, str],
    base_contents: dict[str, str], pr_context: PrContext, *,
    correctness_only: bool,
) -> CodeReviewEvaluation:
    """Merge the deterministic lane-work findings (#1179, #1363).

    The criteria in scope are those of an issue the PR closes, every linked
    criterion of a lane PR (the station closes its ticket on merge), and a
    box the body claims (`lane_work.criteria_in_scope`). Dead-code findings
    follow the correctness-only scope the LLM's do."""
    body = str(pr_context.get("body") or "")
    pr_title = pr_context.get("title")
    if callable(pr_title):
        pr_title = pr_title()
    pr_title = str(pr_title or "")
    linked_context = pr_context.get("linked_issue_context")
    if callable(linked_context):
        linked_context = linked_context()
    linked_context = str(linked_context or "")
    found = lane_work_findings(
        diff_text=diff_text,
        hunks=hunks,
        file_contents=file_contents,
        base_line_counts={
            path: len(text.splitlines()) for path, text in base_contents.items()
        },
        boxes=rendered_criteria_with_state(linked_context),
        pr_title=pr_title,
        pr_body=body,
        health_issues=rendered_health_issues(linked_context),
        criteria_judge=criteria_judge_for(pr_context),
    )
    if correctness_only:
        found = tuple(f for f in found if f.rule_name != "dead-code")
    return with_extra_findings(
        evaluation, filter_novel_claim_findings(found, evaluation.findings),
    )


def _deferred_rule_codes(
    refs: tuple[DeferredLint, ...], installation_id: int, owner: str, repo_name: str,
) -> tuple[list[tuple[DeferredLint, str]], list[str]]:
    """(finding, ruff code) for each deferred line, plus why any line has
    none: its thread is gone, or is not Elder's lint finding, so the rule it
    names cannot be measured. A read failure raises."""
    named: list[tuple[DeferredLint, str]] = []
    unfinished: list[str] = []
    for ref in refs:
        try:
            author, text = with_install_token_retry(
                installation_id,
                lambda token, comment_id=ref.comment_id: fetch_review_comment_body(
                    token, owner, repo_name, comment_id,
                ),
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code != httpx.codes.NOT_FOUND:
                raise
            unfinished.append("thread_gone")
            continue
        code = rule_code(text) if is_grug(author) else None
        if code:
            named.append((ref, code))
        else:
            unfinished.append("thread_names_no_rule")
    return named, unfinished


def _measure_deferred_ruff(
    evaluation: CodeReviewEvaluation, hunks: tuple[DiffHunk, ...],
    file_contents: dict[str, str], pr_context: PrContext,
) -> tuple[CodeReviewEvaluation, list[str]]:
    """The evaluation with each named rule still firing, and what stopped
    the measurement from covering everything the tickets name."""
    refs = tuple(pr_context.get("deferred_lint") or ())
    owner, _, repo_name = str(pr_context.get("repo") or "").partition("/")
    installation_id = int(pr_context.get("installation_id") or 0)
    head_sha = str(pr_context.get("head_sha") or "")
    named, unfinished = _deferred_rule_codes(refs, installation_id, owner, repo_name)
    paths = tuple(dict.fromkeys(ref.path for ref, _ in named))
    contents = {p: file_contents[p] for p in paths if p in file_contents}
    missing = tuple(p for p in paths if p not in contents)
    if missing:
        contents.update(with_install_token_retry(
            installation_id,
            lambda token: fetch_file_contents(
                token, owner, repo_name, missing, head_sha, only_404_skips=True,
            ),
        ) or {})
    found = unmet_findings(named, ruff_hits(contents), hunks)
    return with_extra_findings(evaluation, found), unfinished


def _merge_deferred_ruff(
    evaluation: CodeReviewEvaluation, hunks: tuple[DiffHunk, ...],
    file_contents: dict[str, str], pr_context: PrContext,
) -> CodeReviewEvaluation:
    """Measure the ruff findings the linked tickets defer, at the head
    (#1477). A file the PR did not change is read here. Anything that stops
    it covering them all degrades the review: its silence must not read as
    "fixed". Never raises."""
    unfinished = list(pr_context.get("deferred_lint_unfinished") or ())
    if not pr_context.get("deferred_lint") and not unfinished:
        return evaluation
    try:
        evaluation, more = _measure_deferred_ruff(
            evaluation, hunks, file_contents, pr_context,
        )
        unfinished += more
    except Exception as e:  # noqa: BLE001 - degrades, never passes
        unfinished.append(type(e).__name__)
    if not unfinished:
        return evaluation
    log.warning(
        "code_review_deferred_ruff_unfinished",
        extra={"pr": f"{pr_context.get('repo')}#{pr_context.get('pr_number')}",
               "reasons": sorted(set(unfinished))},
    )
    return with_degradation(evaluation, CodeReviewEvaluation(
        findings=(), conclusion="neutral", degraded_reason=PARTIAL_COVERAGE,
    ))


@dataclass(frozen=True)
class _LaneWorkInputs:
    """The whole pull request, as the lane-work checks read it."""

    diff_text: str
    hunks: tuple[DiffHunk, ...]
    file_contents: dict[str, str]
    base_contents: dict[str, str]


def _full_pr_lane_work_inputs(
    installation_id: int, owner: str, repo_name: str, pull_number: int, *,
    base_sha: str, head_sha: str, known_contents: dict[str, str],
) -> _LaneWorkInputs | None:
    """The full PR diff (merge base..head) and its file texts, for a delta
    review's lane-work checks (#1343). The model still reviews only the
    delta. Head texts the delta already fetched are reused. None when the
    full diff cannot be fetched: the checks then skip this pass, logged,
    as they did before."""
    try:
        diff_text = with_install_token_retry(
            installation_id,
            lambda token: fetch_pr_diff(
                token, owner, repo_name, pull_number,
                base_sha=base_sha, head_sha=head_sha,
            ),
        )
        hunks = parse_diff(diff_text)
    except (httpx.HTTPStatusError, httpx.RequestError, DiffParseError) as e:
        log.info(
            "code_review_lane_work_full_diff_unavailable",
            extra={"pr": f"{owner}/{repo_name}#{pull_number}", "kind": type(e).__name__},
        )
        return None
    paths = tuple(dict.fromkeys(h.file_path for h in hunks))
    missing = tuple(p for p in paths if p not in known_contents)
    contents = {p: known_contents[p] for p in paths if p in known_contents}
    if missing:
        try:
            contents.update(with_install_token_retry(
                installation_id,
                lambda token: fetch_file_contents(
                    token, owner, repo_name, missing, head_sha,
                ),
            ) or {})
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            log.info(
                "code_review_file_contents_unavailable",
                extra={"pr": f"{owner}/{repo_name}#{pull_number}", "error": str(e)},
            )
    rename_sources = tuple(
        c.previous_path for c in file_changes(diff_text)
        if c.status == "renamed" and c.previous_path
    )
    base_contents = _fetch_base_contents(
        installation_id, owner, repo_name, pull_number,
        tuple(dict.fromkeys(paths + rename_sources)), base_sha,
    )
    return _LaneWorkInputs(diff_text, hunks, contents, base_contents)


def _fetch_pr_title_and_body(
    installation_id: int, owner: str, repo_name: str, pull_number: int,
) -> tuple[str, str] | None:
    """Read PR title and body from GitHub at review time (#1457)."""
    if not (installation_id and owner and repo_name and pull_number):
        return None
    try:
        def _get(token: str) -> tuple[str, str]:
            resp = httpx.get(
                f"https://api.github.com/repos/{quote(owner, safe='')}/"
                f"{quote(repo_name, safe='')}/pulls/{pull_number}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=DIFF_FETCH_TIMEOUT,
            )
            resp.raise_for_status()
            body = resp.json() or {}
            return str(body.get("title") or ""), str(body.get("body") or "")

        return with_install_token_retry(installation_id, _get)
    except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as e:
        log.info(
            "code_review_pr_refresh_failed",
            extra={"pr": f"{owner}/{repo_name}#{pull_number}", "kind": type(e).__name__},
        )
        return None


def _merge_lane_work_for_pass(
    evaluation: CodeReviewEvaluation, this_pass: _LaneWorkInputs,
    pr_context: PrContext, *, living_range: str, base_sha: str,
    correctness_only: bool, action: str = "",
) -> CodeReviewEvaluation:
    """Merge the lane-work findings for one review pass. A full review
    judges the diff it reviewed; a living delta hides callers and the
    commits that met a criterion, so a delta review judges the full PR diff
    instead (#1343). The deferred ruff re-check runs apart, so a lane-work
    failure cannot skip it (#1477). Never aborts the review."""
    hunks, contents = this_pass.hunks, this_pass.file_contents
    owner, _, repo_name = str(pr_context.get("repo") or "").partition("/")
    pull_number = int(pr_context.get("pr_number") or 0)
    installation_id = int(pr_context.get("installation_id") or 0)
    try:
        inputs: _LaneWorkInputs | None = this_pass
        if living_range:
            inputs = _full_pr_lane_work_inputs(
                installation_id, owner, repo_name,
                pull_number, base_sha=base_sha,
                head_sha=str(pr_context.get("head_sha") or ""),
                known_contents=this_pass.file_contents,
            )
        if living_range or action in _RE_REVIEW_ACTIONS:
            # Re-read PR title and linked issue criteria at review time (#1457):
            # prevents carrying forward stale title or criteria from the first pass.
            pr_details = _fetch_pr_title_and_body(
                installation_id, owner, repo_name, pull_number,
            )
            if pr_details is not None:
                cur_title, cur_body = pr_details
                if cur_title:
                    pr_context["title"] = cur_title
                if cur_body:
                    pr_context["body"] = cur_body
            _attach_linked_issue_context(
                pr_context, installation_id=installation_id, owner=owner, repo_name=repo_name,
            )
        if inputs is not None:
            hunks, contents = inputs.hunks, inputs.file_contents
            evaluation = _merge_lane_work(
                evaluation, inputs.hunks, inputs.diff_text, inputs.file_contents,
                inputs.base_contents, pr_context, correctness_only=correctness_only,
            )
    except Exception as e:  # noqa: BLE001 - enrichment must never abort a review
        log.info(
            "code_review_criteria_coverage_failed",
            extra={"pr": f"{owner}/{repo_name}#{pull_number}", "kind": type(e).__name__},
        )
    evaluation = _merge_open_merge_holds(evaluation, hunks, pr_context)
    return _merge_deferred_ruff(evaluation, hunks, contents, pr_context)


def _filter_checked_shell_commands(
    evaluation: CodeReviewEvaluation, file_contents: dict[str, str], *,
    installation_id: int, owner: str, repo_name: str, pull_number: int,
) -> CodeReviewEvaluation:
    """The funnel's checked-shell filter (#771) under dispatch's PR naming."""
    return filter_checked_shell_commands(
        evaluation, file_contents, installation_id=installation_id,
        pr=f"{owner}/{repo_name}#{pull_number}",
    )


def _filter_intent_mismatches(
    evaluation: CodeReviewEvaluation, *, installation_id: int,
    owner: str, repo_name: str, pull_number: int, pr_context: PrContext,
) -> CodeReviewEvaluation:
    """The funnel's intent-mismatch filter (#1170, #1218)."""
    return filter_intent_mismatches(
        evaluation, pr_context, installation_id=installation_id,
        pr=f"{owner}/{repo_name}#{pull_number}",
    )


def _repo_private(payload: dict[str, Any]) -> bool | None:
    """The payload's repo visibility, or None when it does not say (grug#1269).

    Elder tells the spark-gateway whether a review is public work, and an
    unknown repo counts as private, so only a real bool is trusted."""
    private = (payload.get("repository") or {}).get("private")
    if isinstance(private, bool):
        return private
    base_repo = ((payload.get("pull_request") or {}).get("base") or {}).get("repo") or {}
    private = base_repo.get("private")
    return private if isinstance(private, bool) else None


@dataclass(frozen=True)
class _ReviewRun:
    pr: dict[str, Any]
    repo: dict[str, Any]
    installation_id: int
    owner: str
    repo_name: str
    pull_number: int
    head_sha: str
    base_sha: str
    snapshot_id: str
    action: str
    author_login: str
    labels: frozenset[str]
    pr_context: PrContext
    blocking: bool
    mode: ReviewMode
    cancel_event: threading.Event | None
    run_started_at: datetime
    run_started_monotonic: float
    started_at: str

    @property
    def full_repo(self) -> str:
        return f"{self.owner}/{self.repo_name}"

    @property
    def pr_name(self) -> str:
        return f"{self.full_repo}#{self.pull_number}"


@dataclass(frozen=True)
class _LivingRange:
    prior_review_head: str
    prior_sha: str = ""
    range: str = ""
    rejected: str = ""
    empty_range: str = ""
    metadata_range: str = ""


@dataclass(frozen=True)
class _PreparedDiff:
    text: str
    hunks: tuple[DiffHunk, ...]
    parsed_hunks: tuple[DiffHunk, ...]
    repo_config: RepoConfig
    config_note: str
    excluded_paths: tuple[str, ...]
    oversized_paths: tuple[str, ...]
    ignored_paths: tuple[str, ...]
    duplicate_paths: tuple[tuple[str, str], ...]
    move_paths: tuple[tuple[str, str], ...]
    lock_only: bool
    living: _LivingRange

    @property
    def unreviewed_paths(self) -> tuple[str, ...]:
        """Changed paths no model read on this head (#1781)."""
        return _unreviewed_paths(
            excluded=self.excluded_paths, oversized=self.oversized_paths,
            ignored=self.ignored_paths,
        )


@dataclass(frozen=True)
class _ReviewEvidence:
    evidence: ElderEvidence
    file_contents: dict[str, str]
    base_file_contents: dict[str, str]
    head_check_runs: list[dict[str, Any]]


@dataclass(frozen=True)
class _ReviewAssessment:
    inputs: _ReviewEvidence
    model_response: LlmReviewResponse
    funnel: FunnelResult
    evaluation: CodeReviewEvaluation
    review_threads: list[dict[str, Any]] | None
    quarantine_gap: str
    config_note: str
    lock_check: LockCheck
    deletion_note: str
    ruff_policy_dropped: list[int]
    analyzer_skips: list[tuple[str, str]]


@dataclass(frozen=True)
class _ReviewComments:
    muted: dict[str, ClassPrecision]
    budget: _BudgetOutcome
    budget_on: bool
    prior_keys: frozenset[str]
    dedup_degraded: bool


@dataclass(frozen=True)
class _Tier1Publication:
    conclusion: CheckConclusion
    title: str
    summary: str
    outcome: PublishOutcome
    event: ReviewEvent
    phase: Literal["tier1", "dual"]
    pinned_issues: tuple[int, ...]
    listed_threads: list[dict[str, Any]] | None
    deep_plan: DeepEscalationDecision | None
    deferred_approval: bool


@dataclass(frozen=True)
class _PostedCheck:
    failed: bool = False
    run_id: int | None = None
    url: str = ""


@dataclass(frozen=True)
class _PostedReview:
    response: dict[str, Any] | None = None
    failed: bool = False
    rejected: bool = False


def dispatch_code_review(
    payload: dict[str, Any], *, blocking: bool,
    cancel_event: threading.Event | None = None,
    rerun_pending: bool = False,
) -> dict[str, str]:
    """Entry point: one Elder review pass (`_review_pass`), then, however it
    ended (approved, skipped, carried, stale, degraded, or raised), withdraw
    Elder's approvals on commits other than the reviewed head (#1781). An
    approval on another commit never vouches for this head; one on a newer
    head is safe, since the withdrawal stands down when the head has moved.
    """
    try:
        return _review_pass(
            payload, blocking=blocking, cancel_event=cancel_event,
            rerun_pending=rerun_pending,
        )
    finally:
        _withdraw_other_commit_approvals(payload)


def _withdraw_other_commit_approvals(payload: dict[str, Any]) -> None:
    """Best-effort and silent on failure: it must never mask the pass's own
    result or exception."""
    try:
        pr = payload["pull_request"]
        repo = payload["repository"]
        _dismiss_superseded_reviews(
            int(payload["installation"]["id"]), repo["owner"]["login"],
            repo["name"], int(pr["number"]), pr["head"]["sha"], _run_clock(),
            state="APPROVED", other_commits_only=True,
        )
    except Exception as e:  # noqa: BLE001 - never mask the review's outcome
        log.warning("elder_final_approval_withdrawal_failed", extra={"kind": type(e).__name__})


def _review_pass(
    payload: dict[str, Any], *, blocking: bool,
    cancel_event: threading.Event | None = None,
    rerun_pending: bool = False,
) -> dict[str, str]:
    """Orchestrate one Elder review pass.

    `blocking` comes from RepoConfig.code_reviewer_blocking. False means
    advisory mode: the check run is forced to neutral regardless of the
    evaluation verdict. True means blocking mode: the verdict survives on the
    check. The review state is the verdict in both modes (#1758).

    Returns a structured-log dict; never raises a wire-level
    exception - LLM outages, parse errors, and publish failures all
    degrade to a published degraded check (neutral in advisory mode,
    failure in blocking mode, grug#1212 / ADR-0023) so this persona
    cannot 500 the webhook handler.

    `cancel_event` (#635 follow-up): passed straight through to
    `review_diff`, which aborts an in-flight Cave arm call the moment it
    fires. The caller (rerun.py's `_run_hot_review`) owns setting it, via a
    background watcher that re-fetches the PR every few seconds for as long
    as this call is running.

    `rerun_pending` (grug#1494): the caller will run this review again if it
    degrades to a reason the queue retries (`RETRIED_DEGRADATIONS`: every
    model rung failed, unparseable output, a failed diff fetch), or to a
    partial or low-confidence review with nothing blocking in it. The check
    then stays `in_progress` - pending, so a required check still blocks the
    merge - instead of completing for a retry that is already queued. The
    retried reasons wait in WARN mode too (grug#818), so no author reads
    "Grug could not review" before the retries run out; the partial and
    low-confidence ones wait only on a blocking repo (`_hold_for_rerun`).
    Never set for the last scheduled attempt.
    """
    run = _start_review_run(payload, blocking, cancel_event)
    living = _living_review_range(run)
    voice = _resolve_review_voice(run)

    # #1781, fail closed: a re-review may still exit early (stale, skipped,
    # carried, degraded, re-run pending) without vouching for this head, so
    # Elder's earlier approvals are withdrawn before any exit. A clean
    # finish approves again.
    if run.action in _RE_REVIEW_ACTIONS:
        _dismiss_superseded_reviews(
            run.installation_id, run.owner, run.repo_name, run.pull_number,
            run.head_sha, run.run_started_at, state="APPROVED",
        )

    # Queued work can already be stale before spending model tokens.
    stale = _stale_queued_review(run)
    if stale is not None:
        return stale

    # Load policy from the trusted ref before fetching the diff.
    base_repo_config = _load_pr_repo_config(
        run.pr, run.installation_id, run.owner, run.repo_name,
        steering_ref=run.pr_context["pr_base_sha"],
    )
    skip_rule = pr_skip_rule(
        run.pr, run.installation_id, run.owner, run.repo_name, config=base_repo_config,
    )
    if skip_rule is not None:
        return _publish_config_skip(
            run.installation_id, run.owner, run.repo_name, run.pull_number,
            run.head_sha, rule=skip_rule,
        )

    carry_request = _carry_request(run, base_repo_config)
    # AI-REVIEW 2026-11-12 [gpt-6.1-sol/code-review] 1730-R1 (PR #1731; issue #1730):
    # The carry probe and fallback share this invocation's immutable pinned diff.
    # Cache successes only; a failed fetch can still retry through normal handling.
    full_pr_diff = functools.cache(functools.partial(
        _fetch_full_pr_diff, run.installation_id, run.owner, run.repo_name,
        run.pull_number, base_sha=run.base_sha, head_sha=run.head_sha,
    ))
    carried = carry_verdict.carry_if_unchanged(carry_request, fetch_pr_diff=full_pr_diff)
    if carried is not None:
        return carried

    try:
        diff_text, living = _fetch_review_diff(run, living, full_pr_diff)
        diff = _prepare_review_diff(run, diff_text, living, base_repo_config)
    except (httpx.HTTPStatusError, httpx.RequestError, DiffParseError) as e:
        return _handle_diff_failure(run, e, rerun_pending=rerun_pending)

    assessment = _assess_review(run, diff, voice)

    # A durable review can spend minutes in inference. Share this gate
    # between direct publication and enqueuing fallback work.
    stale = _stale_queued_review(run)
    if stale is not None:
        return stale
    if assessment.model_response.kind == "all_failed":
        _enqueue_review_fallback(run, diff.hunks)
    evaluation = assessment.evaluation
    if rerun_pending and _hold_for_rerun(evaluation, blocking):
        return _publish_rerun_pending(
            run.installation_id, run.owner, run.repo_name, run.pull_number, run.head_sha,
            str(evaluation.degraded_reason), blocking=blocking,
        )

    comments = _plan_review_comments(run, diff, assessment)
    plan = _plan_tier1_publication(run, diff, assessment, comments)
    # Check and review publication fail independently.
    check = _post_tier1_check(run, plan)
    comments = _read_prior_comments(run, comments)
    markings = _project_tier1(run, diff, assessment, comments, plan)
    review = _post_tier1_review(run, markings.review)
    _record_tier1_publication(run, diff, assessment, plan, check, review, markings)
    outcome = _record_dispatch_outcome(run, assessment, plan, check, review, comments)
    _remember_review_baseline(
        run, diff, assessment, comments.budget, plan.conclusion, outcome,
        carry_request, full_pr_diff,
    )
    _finish_tier1_review(run, diff, assessment, voice, comments, plan, check, review, markings)

    response = {"persona": "code_reviewer", "result": outcome.result}
    if evaluation.degraded_reason:
        response["degraded_reason"] = evaluation.degraded_reason
    return response


def _start_review_run(
    payload: dict[str, Any], blocking: bool, cancel_event: threading.Event | None,
) -> _ReviewRun:
    # #1350: when this run began, so a clean verdict can tell Elder's own
    # earlier blocks on this head from one another run posts mid-flight.
    run_started_at = _run_clock()
    run_started_monotonic = time.monotonic()
    mode: ReviewMode = "blocking" if blocking else "advisory"
    started_at = hunt_now()
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
    # grug#1578: steering files come from the default branch when the PR's
    # base is another branch, one its author may be able to push to.
    steering_ref = steering_base_ref(pr, repo, installation_id, owner, repo_name)
    pr_context: PrContext = {
        "installation_id": installation_id,
        "repo": f"{owner}/{repo_name}",
        "pr_number": pull_number,
        "head_sha": head_sha,
        "base_sha": base_sha,
        "pr_base_sha": steering_ref,
        "title": str(pr.get("title") or ""),
        "body": str(pr.get("body") or ""),
    }
    private = _repo_private(payload)
    if private is not None:
        pr_context["repo_private"] = private
    author_family = _author_family(pr)
    if author_family:
        pr_context["author_family"] = author_family
    labels = _pr_labels(pr)
    pr_context["sensitive_review"] = "class:sensitive" in labels
    return _ReviewRun(
        pr=pr, repo=repo, installation_id=installation_id, owner=owner, repo_name=repo_name,
        pull_number=pull_number, head_sha=head_sha, base_sha=base_sha, snapshot_id=snapshot_id,
        action=action, author_login=author_login, labels=labels, pr_context=pr_context,
        blocking=blocking, mode=mode, cancel_event=cancel_event, run_started_at=run_started_at,
        run_started_monotonic=run_started_monotonic, started_at=started_at,
    )


def _living_review_range(run: _ReviewRun) -> _LivingRange:
    # Living Hunt (#557): if we already finished a review on an older head
    # for this PR, scope the LLM to the delta (prior..head) instead of the
    # full PR base..head. Best-effort: store blips fall back to full review.
    prior_review_head = ""
    living_prior_sha = ""
    living_range = ""
    try:
        from adapters.install_store import get_elder_last_reviewed

        prior = get_elder_last_reviewed(
            install_id=run.installation_id,
            repo=run.full_repo,
            pr_number=run.pull_number,
        )
        prior_review_head = prior or ""
        if prior and prior != run.head_sha:
            living_prior_sha = prior
            living_range = f"{prior[:8]}..{run.head_sha[:8]}"
    except Exception as e:  # noqa: BLE001 - never fail a review for memory
        log.warning(
            "elder_living_hunt_lookup_failed",
            extra={
                "pr": run.pr_name,
                "kind": type(e).__name__,
            },
        )
    return _LivingRange(prior_review_head, living_prior_sha, living_range)


def _resolve_review_voice(run: _ReviewRun) -> VoiceSelection:
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
            get_repo_config(run.installation_id, int(run.repo["id"])),
            check_entitlement=lambda: is_install_allowlisted(run.installation_id),
        )
    except Exception as e:  # noqa: BLE001 - voice is cosmetic; never fail a review
        log.warning(
            "elder_voice_resolve_failed",
            extra={
                "pr": run.pr_name,
                "kind": type(e).__name__,
            },
        )
    return voice


def _stale_queued_review(run: _ReviewRun) -> dict[str, str] | None:
    if run.action != "review":
        return None
    return _review_snapshot_freshness_failure(
        installation_id=run.installation_id,
        owner=run.owner,
        repo_name=run.repo_name,
        pull_number=run.pull_number,
        expected_snapshot_id=run.snapshot_id,
        expected_head_sha=run.head_sha,
    )


def _carry_request(run: _ReviewRun, base_repo_config: RepoConfig) -> carry_verdict.CarryRequest:
    # grug#1648: a new head whose own PR diff matches a passed head's.
    return carry_verdict.CarryRequest(
        action=run.action, installation_id=run.installation_id, owner=run.owner,
        repo_name=run.repo_name, pull_number=run.pull_number, head_sha=run.head_sha,
        blocking=run.blocking, title=run.pr_context["title"], body=run.pr_context["body"],
        labels=run.labels, author_family=run.pr_context.get("author_family") or "",
        config=repr(base_repo_config),
    )


def _fetch_review_diff(
    run: _ReviewRun, living: _LivingRange, full_pr_diff: Callable[[], str],
) -> tuple[str, _LivingRange]:
    if not living.prior_sha:
        # Retain the established fetch seam for ordinary full reviews;
        # Guard/Smasher and focused dispatch tests share this helper.
        diff_text = with_install_token_retry(
            run.installation_id,
            lambda token: fetch_pr_diff(
                token,
                run.owner,
                run.repo_name,
                run.pull_number,
                base_sha=run.base_sha,
                head_sha=run.head_sha,
            ),
        )
        return diff_text, living

    diff_text, used_living_compare, living_baseline_was_rejected = with_install_token_retry(
        run.installation_id,
        lambda token: fetch_pr_diff_with_scope(
            token, run.owner, run.repo_name, run.pull_number,
            base_sha=living.prior_sha, head_sha=run.head_sha,
        ),
    )
    if not used_living_compare:
        # The delta was rejected (#845): keep the rejected SHA for the
        # summary note, but clear the range so the title doesn't name a
        # delta that was never reviewed.
        return diff_text, replace(
            living, prior_sha="", range="",
            rejected=living.prior_sha[:8] if living_baseline_was_rejected else "",
        )
    if _delta_is_empty(diff_text):
        return full_pr_diff(), replace(living, prior_sha="", range="", empty_range=living.range)
    if _is_lock_only(diff_text):
        # #1649: a lock-only delta is judged on the whole base..head
        # diff, never on the increment alone.
        log.info(
            "elder_living_hunt_lock_delta_full_review",
            extra={"pr": run.pr_name, "range": living.range},
        )
        return full_pr_diff(), replace(living, prior_sha="", range="")
    if _delta_is_excluded_metadata_only(diff_text):
        return full_pr_diff(), replace(living, prior_sha="", range="", metadata_range=living.range)
    return diff_text, living


def _prepare_review_diff(
    run: _ReviewRun, diff_text: str, living: _LivingRange, base_repo_config: RepoConfig,
) -> _PreparedDiff:
    run.pr_context["base_sha"] = living.prior_sha or run.base_sha
    hunks = parse_diff(diff_text)
    # The lane-work checks (#1179, #1363) judge the WHOLE diff, so they
    # keep every parsed hunk, including those dropped below (excluded,
    # ignored, oversized, duplicate) from the model's plate.
    parsed_hunks = hunks
    review_scope = _correctness_only_scope(
        tuple(hunk.file_path for hunk in hunks), run.labels,
    )
    if review_scope:
        run.pr_context["review_scope"] = review_scope
    # #609: drop data/generated/vendored files from the LLM's plate - a
    # big JSONL/lockfile hunk balloons the prompt into parse_failed and
    # carries no review signal. Named in the summary, never silent.
    hunks, excluded_paths = split_reviewable_hunks(hunks)
    # #1649: no verdict may come from a skip. When every changed file
    # is a lock file, the model reviews them like any other file; the
    # size rules below still apply.
    lock_only_review = bool(parsed_hunks) and _is_lock_only(diff_text)
    if lock_only_review:
        hunks, excluded_paths = parsed_hunks, ()
    else:
        hunks, excluded_paths = _keep_unchecked_locks(
            parsed_hunks, hunks, excluded_paths,
        )
    if excluded_paths:
        log.info(
            "code_review_paths_excluded",
            extra={
                "pr": run.pr_name,
                "excluded": list(excluded_paths)[:20],
                "count": len(excluded_paths),
            },
        )
    # `.grug.yaml` from the BASE ref: `ignore` globs are dropped here,
    # before anything below (full-file fetch, prompt, judge) can see them.
    hunks, repo_config, config_note, ignored_paths = _apply_repo_config(
        hunks, run.pr, run.installation_id, run.owner, run.repo_name,
        config=base_repo_config,
    )
    if living.metadata_range:
        config_note += (
            "\n\nLiving Hunt: the delta contained only excluded metadata in `"
            + living.metadata_range
            + "`, so Elder reviewed the full PR diff on this head instead."
        )
    # A hunk bigger than a whole cohort can never be reviewed - the
    # planner will not truncate it (line anchors), so it becomes a solo
    # cohort that is auto-failed, flips the check to `partial_review`,
    # and inflates the plan into more cohorts than the wall-clock budget
    # can run. Drop it HERE, named, so the rest of the diff reviews
    # normally. Measured live: one 1,004,156-char generated
    # `.json` hunk cost four unrelated healthy cohorts.
    cohort_budget = review_cohort_char_budget()
    # #1747: compress first. A hunk that splits into a few line-anchored
    # pieces is read in full; only what still does not fit is dropped and
    # named below, so the author sees exactly what was compressed or unread.
    hunks, split_paths = split_large_hunks(hunks, cohort_budget)
    if split_paths:
        config_note += (
            "\n\nCompressed: the change to "
            + ", ".join(f"`{p.replace(chr(96), '')}`" for p in split_paths[:10])
            + (" and more" if len(split_paths) > 10 else "")
            + " was larger than one review pass, so Elder split it at line"
            " boundaries and read every line in separate pieces."
        )
    hunks, oversized_paths = split_oversized_hunks(hunks, cohort_budget)
    if oversized_paths:
        log.warning(
            "code_review_hunks_oversized",
            extra={
                "pr": run.pr_name,
                "oversized": list(oversized_paths)[:20],
                "count": len(oversized_paths),
                "cohort_budget_chars": cohort_budget,
            },
        )
    # #813: a byte-identical whole-file copy - of a file already in the
    # repo at head, or of another file IN this diff (one file added
    # under two paths because a build tool can't symlink outside its
    # root) - is provably unchanged content. Reviewing it spends full
    # cohort budget on bytes nobody wrote here, which was measured to
    # push the genuinely novel logic in the SAME diff out of the budget
    # entirely. Drop it HERE, before cohort planning, named against the
    # path it duplicates, so the budget goes to content nobody has
    # looked at yet.
    hunks, duplicate_paths, move_paths = _split_copied_hunks(
        hunks, diff_text, run.installation_id, run.owner, run.repo_name,
        run.pull_number, run.head_sha,
    )
    if duplicate_paths:
        log.info(
            "code_review_hunks_deduplicated",
            extra={
                "pr": run.pr_name,
                "duplicates": list(duplicate_paths)[:20],
                "count": len(duplicate_paths),
            },
        )
    return _PreparedDiff(
        text=diff_text, hunks=hunks, parsed_hunks=parsed_hunks, repo_config=repo_config,
        config_note=config_note, excluded_paths=excluded_paths, oversized_paths=oversized_paths,
        ignored_paths=ignored_paths,
        duplicate_paths=duplicate_paths, move_paths=move_paths, lock_only=lock_only_review,
        living=living,
    )


def _handle_diff_failure(
    run: _ReviewRun, e: Exception, *, rerun_pending: bool,
) -> dict[str, str]:
    diff_too_large = _is_diff_too_large(e)
    log.warning(
        "code_review_fetch_or_parse_failed",
        extra={
            "installation_id": run.installation_id,
            "pr": run.pr_name,
            "kind": type(e).__name__,
            "diff_too_large": diff_too_large,
            **_http_error_detail(e),
        },
    )
    # Do not publish even a degraded check for an input that changed while
    # the immutable diff was being fetched/parsed.
    stale = _stale_queued_review(run)
    if stale is not None:
        return stale
    if diff_too_large:
        reason = DIFF_TOO_LARGE
        check_summary: str | None = (
            "GitHub refused to serve this pull request's diff (HTTP 406): "
            "it is over the API's diff size limit. Split the change into "
            "smaller pull requests to get an Elder review. "
            + gate_sentence(run.blocking)
        )
        verdict_summary = (
            "Grug could not look - diff is over GitHub's size limit"
        )
    else:
        reason = "fetch_or_parse_failed"
        check_summary = None
        verdict_summary = "Grug could not look \u2014 diff fetch/parse failed"
        if rerun_pending:
            # grug#818: the queue re-runs this reason, so a completed
            # "could not review" now would be read before the retry lands.
            return _publish_rerun_pending(
                run.installation_id, run.owner, run.repo_name, run.pull_number, run.head_sha,
                reason, blocking=run.blocking,
            )
    degraded = _publish_degraded(
        run.installation_id, run.owner, run.repo_name, run.pull_number, run.head_sha,
        reason=reason, summary=check_summary, blocking=run.blocking,
    )
    # Errored Activity row (PRD #301): Grug couldn't even fetch/parse the
    # diff - record it so it surfaces as `errored` (re-runnable in S3a),
    # never a silent gap. Best-effort.
    record_check_verdict(
        install_id=run.installation_id,
        persona_key="code_reviewer",
        repo=run.full_repo,
        pr_number=run.pull_number,
        head_sha=run.head_sha,
        conclusion=degraded_conclusion(run.blocking),
        summary=verdict_summary,
        findings_count=0,
        blocking=run.blocking,
        degraded_reason=reason,
    )
    return degraded


def _assemble_review_evidence(run: _ReviewRun, diff: _PreparedDiff) -> _ReviewEvidence:
    # Full-file context (#336): fetch the whole current content of each changed
    # file at head SHA so the Elder + judge can see mitigations OUTSIDE the diff
    # hunk (the #1149 false-positive class). Best-effort + self-guarding - any
    # failure degrades to the pre-#336 diff-only review, never blocks it.
    changed_paths = tuple(dict.fromkeys(h.file_path for h in diff.hunks))
    try:
        file_contents = with_install_token_retry(
            run.installation_id,
            lambda token: fetch_file_contents(
                token, run.owner, run.repo_name, changed_paths, run.head_sha
            ),
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.info(
            "code_review_file_contents_unavailable",
            extra={"pr": run.pr_name, "error": str(e)},
        )
        file_contents = {}

    # Base revision of the SAME files, so complexity can report what THIS PR
    # did rather than the function's pre-existing debt (#767). Measured: the
    # absolute-threshold form was 85 of the last 120 findings, most replies
    # saying "pre-existing" or "outside this PR".
    #
    # Degrades to {} on any failure, which `scan_complexity` reads as "no base"
    # and falls back to the old absolute behavior. That direction is
    # deliberate: going SILENT on a fetch error would hide real regressions,
    # whereas falling back only restores the previous noise level.
    # A renamed file's base text lives at its old path (#1365).
    rename_sources = tuple(
        c.previous_path for c in file_changes(diff.text)
        if c.status == "renamed" and c.previous_path
    )
    base_file_contents = _fetch_base_contents(
        run.installation_id, run.owner, run.repo_name, run.pull_number,
        tuple(dict.fromkeys(changed_paths + rename_sources)),
        str(run.pr_context.get("base_sha") or ""),
    )

    cross_file_contents = _review_cross_file_context(run, diff.hunks, file_contents, changed_paths)
    runtime_context, ci_context, head_check_runs = _review_runtime_and_ci(run, diff.hunks)
    repo_docs_context = _review_docs_and_intent(run, diff, changed_paths)

    # The reviewer, judge, refute gate, and deep arm use the same evidence.
    evidence = assemble_evidence(
        _to_llm_hunks(diff.hunks),
        pr_context=run.pr_context,
        file_contents=file_contents,
        cross_file_contents=cross_file_contents,
        runtime_context=runtime_context,
        ci_context=ci_context,
        repo_docs_context=repo_docs_context,
    )
    return _ReviewEvidence(evidence, file_contents, base_file_contents, head_check_runs)


def _review_cross_file_context(
    run: _ReviewRun, hunks: tuple[DiffHunk, ...], file_contents: dict[str, str],
    changed_paths: tuple[str, ...],
) -> dict[str, str]:
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
                run.installation_id,
                lambda token: fetch_cross_file_context(
                    token, run.owner, run.repo_name, symbols,
                    head_sha=run.head_sha,
                    exclude_paths=frozenset(changed_paths),
                ),
            )
    except Exception as e:  # noqa: BLE001 - cross-file context is additive; never break the review
        log.info(
            "cross_file_context_degraded",
            extra={
                "stage": "dispatch",
                "pr": run.pr_name,
                "kind": type(e).__name__,
            },
        )
    return cross_file_contents


def _review_runtime_and_ci(
    run: _ReviewRun, hunks: tuple[DiffHunk, ...],
) -> tuple[str | None, str | None, list[dict[str, Any]]]:
    # Production signal (#470 Omen): DD error counts for the diff's
    # files, injected as review context. FAIL-SAFE + explicit-allow: no
    # service mapping (or any failure) = None = today's review.
    runtime_context: str | None = None
    try:
        runtime_context = build_runtime_context(run.owner, run.repo_name, hunks)
    except Exception as e:  # noqa: BLE001 - omen is additive; never break the review
        log.info(
            "omen_degraded",
            extra={
                "stage": "dispatch",
                "pr": run.pr_name,
                "kind": type(e).__name__,
            },
        )

    # CI status (#902): the PR's OWN check-run status on the reviewed head
    # SHA, injected as review context so Elder can see whether the PR's own
    # tests passed. FAIL-SAFE + additive: any failure degrades to None =
    # today's review, never blocks. The check-runs already ran in isolated
    # CI; nothing new executes here.
    ci_context: str | None = None
    # The same listing feeds the covered_by_passing_check kill below (#763),
    # so a caller-not-updated claim that a passing check on this head
    # already covers is dropped. Empty on any failure: the kill never fires.
    head_check_runs: list[dict[str, Any]] = []

    def _fetch_head_check_runs(token: str) -> list[dict[str, Any]]:
        runs = list(list_check_runs_for_ref(token, run.owner, run.repo_name, run.head_sha) or [])
        head_check_runs[:] = runs
        return runs

    try:
        ci_context = with_install_token_retry(
            run.installation_id,
            lambda token: build_ci_status_context(
                fetch_check_runs=lambda: _fetch_head_check_runs(token),
                head_sha=run.head_sha,
                hunks=hunks,
            ),
        )
    except Exception as e:  # noqa: BLE001 - ci status is additive; never break the review
        log.info(
            "ci_status_degraded",
            extra={
                "stage": "dispatch",
                "pr": run.pr_name,
                "kind": type(e).__name__,
            },
        )
    return runtime_context, ci_context, head_check_runs


def _review_docs_and_intent(
    run: _ReviewRun, diff: _PreparedDiff, changed_paths: tuple[str, ...],
) -> str | None:
    # Repo docs (#903): CONTEXT.md + path-relevant docs/adr/ entries as a
    # DISTINCT review-context block (not merged into team-practices/Lore).
    # FAIL-SAFE + additive: any failure degrades to None = today's review,
    # never blocks. These docs already exist versioned in the repo; nothing
    # new executes.
    repo_docs_context: str | None = None
    try:
        repo_docs_context = with_install_token_retry(
            run.installation_id,
            lambda token: _load_repo_docs(
                token, run.owner, run.repo_name, str(run.pr_context.get("pr_base_sha") or ""), diff.hunks,
            ),
        )
    except Exception as e:  # noqa: BLE001 - repo docs are additive; never break the review
        log.info(
            "repo_docs_degraded",
            extra={
                "stage": "dispatch",
                "pr": run.pr_name,
                "kind": type(e).__name__,
            },
        )

    # `.grug.yaml` path_instructions: only entries matching a file in THIS
    # diff reach the prompt, riding the repo-docs block so every review arm
    # (tier-1, deep, cohorts) carries them without new plumbing.
    repo_docs_context = with_instructions(
        repo_docs_context, changed_paths, diff.repo_config,
    )

    # Linked-issue acceptance criteria (#904): additive, never blocks.
    _attach_linked_issue_context(
        run.pr_context, installation_id=run.installation_id, owner=run.owner, repo_name=run.repo_name,
    )
    return repo_docs_context


def _review_and_adjudicate_tier1(
    run: _ReviewRun, diff: _PreparedDiff, inputs: _ReviewEvidence, voice: VoiceSelection,
) -> tuple[LlmReviewResponse, FunnelResult]:
    evidence = inputs.evidence
    file_contents = inputs.file_contents
    llm_response: LlmReviewResponse = review_diff(
        evidence,
        installation_id=run.installation_id,
        voice=voice,
        cancel_event=run.cancel_event,
    )
    if llm_response.kind != "reviewed":
        # Without this log, a 100% LLM-outage rate looks identical to
        # "no findings" in operational dashboards - both yield
        # findings=(). Surface the degraded kind so DD can monitor
        # backend health per-install.
        log.warning(
            "code_review_llm_degraded",
            extra={
                "installation_id": run.installation_id,
                "pr": run.pr_name,
                "kind": llm_response.kind,
                "error": llm_response.error,
            },
        )

    evaluation = evaluate_diff(
        diff.hunks, llm_response, cross_file_contents=evidence.cross_file_contents,
    )
    _log_omit_markings(
        evaluation, pr=run.pr_name, head_sha=run.head_sha,
    )
    # NOTE (#466, ADR-0012): the deterministic security suite (SAST + SCA +
    # secret + IaC scans -> exploitability judge) that used to merge into
    # THIS evaluation now runs as the GUARD persona with its own check-run
    # ("Grug - Guard") - see personas/guard/dispatch.py. Elder is the LLM
    # diff review only.

    # The one finding funnel (#1586): the deterministic filters (correctness
    # scope, unreachable defensive, checked shell #771, intent #1170), then
    # the judge (#467, ADR-0011: low/medium false positives suppressed, a
    # judge outage grades nothing), the repo-grounded verifier (#708) and
    # the refute gate or falsification verifier (#714, #1567). The deep and
    # quarantined passes and the eval run the same funnel. `graded_findings`
    # keeps the FULL judged set so the eval denominator counts suppressed
    # rows too.
    def _verify_tier1(current: CodeReviewEvaluation) -> CodeReviewEvaluation:
        verified, killed = verify_findings(
            current.findings, file_contents,
            checks=_check_evidence(inputs.head_check_runs), head_sha=run.head_sha,
        )
        if killed:
            current = with_findings(current, verified)
        _record_verification_kills(
            killed, installation_id=run.installation_id, owner=run.owner,
            repo_name=run.repo_name, pull_number=run.pull_number, arm="tier1",
        )
        return _apply_bound_name_downgrade(
            current, file_contents, installation_id=run.installation_id,
            owner=run.owner, repo_name=run.repo_name, pull_number=run.pull_number, arm="tier1",
        )

    funnel = publish_funnel(
        evaluation, file_contents, run.pr_context,
        grade=lambda current: grade_findings(current, evidence, run.installation_id),
        verify=_verify_tier1,
        falsify=lambda findings, plain, verdicts: _apply_refute_gate(
            findings, evidence, run.installation_id, owner=run.owner,
            repo_name=run.repo_name, pull_number=run.pull_number, arm="tier1",
            plain_findings=plain, plain_verdicts=verdicts,
        ),
        installation_id=run.installation_id,
        pr=run.pr_name,
    )
    if funnel.suppressed:
        log.info(
            "judge_suppressed_findings",
            extra={
                "installation_id": run.installation_id,
                "pr": run.pr_name,
                "suppressed": len(funnel.suppressed),
                "published": len(funnel.graded) - len(funnel.suppressed),
            },
        )
    return llm_response, funnel


def _review_additional_lineages(
    run: _ReviewRun, diff: _PreparedDiff, evidence: ElderEvidence, voice: VoiceSelection,
    llm_response: LlmReviewResponse, evaluation: CodeReviewEvaluation,
) -> tuple[CodeReviewEvaluation, str, str]:
    config_note = diff.config_note
    evaluation, quarantine_gap = _run_quarantined_pass(
        evaluation, diff.hunks, evidence, run.installation_id,
        repo_config=diff.repo_config, started=run.run_started_monotonic,
        voice=voice, cancel_event=run.cancel_event,
        owner=run.owner, repo_name=run.repo_name, pull_number=run.pull_number,
        head_sha=run.head_sha,
        skip=(
            llm_response.kind != "reviewed"
            or (run.cancel_event is not None and run.cancel_event.is_set())
        ),
    )
    if quarantine_gap:
        config_note = f"{config_note}\n\n{quarantine_gap}".strip()
    # #1781: a changed path no model read leaves the head unvouched.
    config_note += _unreviewed_note(diff.unreviewed_paths)
    evaluation, lineage_gap = _run_second_lineage_pass(
        evaluation, diff.hunks, evidence, run.installation_id,
        primary_models=llm_response.models_used,
        repo_config=diff.repo_config, started=run.run_started_monotonic,
        cancel_event=run.cancel_event, owner=run.owner, repo_name=run.repo_name,
        pull_number=run.pull_number, head_sha=run.head_sha,
        skip=(
            llm_response.kind != "reviewed"
            or (run.cancel_event is not None and run.cancel_event.is_set())
        ),
    )
    if lineage_gap:
        config_note = f"{config_note}\n\n{lineage_gap}".strip()
    return evaluation, quarantine_gap, config_note


def _review_complexity(
    run: _ReviewRun, diff: _PreparedDiff, inputs: _ReviewEvidence,
    evaluation: CodeReviewEvaluation, *, correctness_only: bool,
) -> CodeReviewEvaluation:
    # #532: deterministic complexity source. A changed Python function over the
    # cyclomatic/cognitive cap merges as an advisory MEDIUM finding - no LLM, no
    # judge (it is precise by construction). It rides the SAME merge rule as the
    # SAST suite; MEDIUM means it never blocks a merge on its own.
    try:
        cyclomatic_cap, cognitive_cap = _repo_complexity_caps(
            run.installation_id, int(run.repo["id"]),
        )
        complexity_scan = (
            ComplexityScan(findings=(), suppressed=())
            if correctness_only else scan_complexity_full(
                diff.hunks, inputs.file_contents,
                cyclomatic_cap=cyclomatic_cap,
                cognitive_cap=cognitive_cap,
                base_contents=inputs.base_file_contents or None,
            )
        )
    except Exception as e:  # noqa: BLE001 - enrichment must never abort a review
        log.info(
            "code_review_complexity_scan_failed",
            extra={"pr": run.pr_name, "kind": type(e).__name__},
        )
        complexity_scan = ComplexityScan(findings=(), suppressed=())
    if complexity_scan.findings:
        evaluation = with_extra_findings(evaluation, complexity_scan.findings)
        log.info(
            "code_review_complexity_findings",
            extra={
                "installation_id": run.installation_id,
                "pr": run.pr_name,
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
                "installation_id": run.installation_id,
                "pr": run.pr_name,
                "suppressed": len(complexity_scan.suppressed),
                "published": len(complexity_scan.findings),
            },
        )
    return evaluation


def _review_documented_claims(
    run: _ReviewRun, diff: _PreparedDiff, file_contents: dict[str, str],
    evaluation: CodeReviewEvaluation,
) -> CodeReviewEvaluation:
    # Deterministic docs/code claim check: catch comment/env prose that
    # asserts the wrong settle cap or deep-diff bound (the LORE/CR class
    # on #664). Pure + advisory MEDIUM; never aborts the review.
    try:
        claim_file_contents = _enrich_claim_check_sources(
            run.installation_id, run.owner, run.repo_name, run.head_sha, file_contents, diff.hunks,
        )
        claim_findings = scan_claim_checks(diff.hunks, claim_file_contents)
        # Drop rows the LLM already published under the same rule/anchor.
        claim_findings = filter_novel_claim_findings(
            claim_findings, evaluation.findings,
        )
    except Exception as e:  # noqa: BLE001 - enrichment must never abort a review
        log.info(
            "code_review_claim_check_failed",
            extra={"pr": run.pr_name, "kind": type(e).__name__},
        )
        claim_findings = ()
    if claim_findings:
        evaluation = with_extra_findings(evaluation, claim_findings)
        log.info(
            "code_review_claim_check_findings",
            extra={
                "installation_id": run.installation_id,
                "pr": run.pr_name,
                "count": len(claim_findings),
            },
        )
    return evaluation


def _assess_review(run: _ReviewRun, diff: _PreparedDiff, voice: VoiceSelection) -> _ReviewAssessment:
    inputs = _assemble_review_evidence(run, diff)
    model_response, funnel = _review_and_adjudicate_tier1(run, diff, inputs, voice)
    correctness_only = bool(run.pr_context.get("review_scope"))
    evaluation, quarantine_gap, config_note = _review_additional_lineages(
        run, diff, inputs.evidence, voice, model_response, funnel.evaluation,
    )

    # Deterministic evidence is added after the model finding funnel.
    evaluation = _review_complexity(run, diff, inputs, evaluation, correctness_only=correctness_only)
    ruff_policy_dropped: list[int] = []
    analyzer_skips: list[tuple[str, str]] = []
    policy_base = str(run.pr_context.get("pr_base_sha") or "")
    ruff_policy = _load_repo_ruff_policy(run.installation_id, run.owner, run.repo_name, policy_base)
    analyzer_engines = configured_engines()
    if not correctness_only and analyzer_engines:
        evaluation = _merge_lint_evidence(
            evaluation, diff.hunks, inputs.file_contents, ruff_policy, ruff_policy_dropped,
            semgrepignore=(
                _load_repo_semgrepignore(run.installation_id, run.owner, run.repo_name, policy_base)
                if "opengrep" in analyzer_engines else None
            ),
            skipped=analyzer_skips, engines=analyzer_engines,
        )

    evaluation = _review_documented_claims(run, diff, inputs.file_contents, evaluation)
    evaluation, deletion_note = _review_deletion_only(
        evaluation, diff.text, diff.parsed_hunks,
        installation_id=run.installation_id, owner=run.owner, repo_name=run.repo_name,
        pull_number=run.pull_number, head_sha=run.head_sha,
    )
    lock_check = _lock_findings(
        diff.parsed_hunks, installation_id=run.installation_id, owner=run.owner,
        repo_name=run.repo_name, head_sha=run.head_sha, base_sha=run.base_sha,
        # The final plate: after the ignore, size, copy and move filters.
        reviewed=frozenset(h.file_path for h in diff.hunks),
    )
    lock_findings = lock_check.findings
    if lock_findings:
        evaluation = with_extra_findings(evaluation, lock_findings)

    # Deterministic lane-work checks (#1179, #1363): a new function nothing
    # calls, linked criteria no hunk can satisfy, a file mostly deleted that
    # nobody named. They judge the WHOLE pull request on every pass (#1343).
    evaluation = _merge_lane_work_for_pass(
        evaluation,
        _LaneWorkInputs(diff.text, diff.parsed_hunks, inputs.file_contents, inputs.base_file_contents),
        run.pr_context, living_range=diff.living.range, base_sha=run.base_sha,
        correctness_only=correctness_only,
        action=run.action,
    )
    evaluation, review_threads = _filter_resolved_findings(run, evaluation)
    return _ReviewAssessment(
        inputs=inputs, model_response=model_response, funnel=funnel,
        evaluation=evaluation, review_threads=review_threads, quarantine_gap=quarantine_gap,
        config_note=config_note, lock_check=lock_check, deletion_note=deletion_note,
        ruff_policy_dropped=ruff_policy_dropped, analyzer_skips=analyzer_skips,
    )


def _filter_resolved_findings(
    run: _ReviewRun, evaluation: CodeReviewEvaluation,
) -> tuple[CodeReviewEvaluation, list[dict[str, Any]] | None]:
    # Fetch review threads once for re-reviews (#1457): shared between the
    # lane-work filter and the carry-forward verdict so we only list once.
    review_threads: list[dict[str, Any]] | None = None
    if run.action in _RE_REVIEW_ACTIONS and not evaluation.degraded_reason:
        review_threads = carry_forward.list_threads(
            run.installation_id, run.owner, run.repo_name, run.pull_number,
        )

    # Filter lane-work findings that match resolved or dismissed Elder threads
    # (#1457, #1582): prevents re-raising weakened-test findings on re-reviews
    # when the thread was resolved by another maintainer. A resolve by the PR
    # author or an App must not hide the finding.
    if review_threads is not None:
        filtered = carry_forward.filter_findings_against_resolved_threads(
            evaluation.findings,
            run.installation_id, run.owner, run.repo_name, run.pull_number,
            threads=review_threads,
            pr_author=run.author_login,
        )
        evaluation = with_findings(evaluation, filtered)
    return evaluation, review_threads


def _enqueue_review_fallback(run: _ReviewRun, hunks: tuple[DiffHunk, ...]) -> None:
    from cave_fallback import enqueue_fallback

    enqueue_fallback(
        _to_llm_hunks(hunks),
        installation_id=run.installation_id,
        repo=run.full_repo,
        pr_number=run.pull_number,
        head_sha=run.head_sha,
    )


def _plan_review_comments(
    run: _ReviewRun, diff: _PreparedDiff, assessment: _ReviewAssessment,
) -> _ReviewComments:
    evaluation = assessment.evaluation
    muted = (
        _muted_classes_for(run.full_repo, diff.repo_config)
        if evaluation.findings else {}
    )
    # On a re-review (synchronize/reopened), dedup findings already
    # commented on unchanged lines so the PR isn't flooded with
    # duplicate inline comments on every push (#189). On the first pass
    # (opened/ready_for_review) there are no prior Grug comments, so
    # skip the fetch entirely. The comment budget reads the same keys
    # before publishing, and a rerun needs them too.
    prior_keys: frozenset[str] = frozenset()
    dedup_degraded = False
    budget_on = budget_enabled() and bool(evaluation.findings)
    prior_keys_read = False
    if budget_on and run.action in {"synchronize", "reopened", "review", "rerun"}:
        prior_keys, dedup_degraded = prior_finding_keys(
            run.installation_id, run.owner, run.repo_name, run.pull_number,
        )
        prior_keys_read = not dedup_degraded
    budget = _BudgetOutcome()
    if budget_on:
        budget = _plan_comment_budget(
            evaluation, prior_keys=prior_keys,
            changed_lines=_round_changed_lines(
                diff.hunks, prior_review_head=diff.living.prior_review_head, head_sha=run.head_sha,
                delta_reviewed=bool(diff.living.range), keys_known=prior_keys_read,
            ),
            open_threads=assessment.review_threads or [],
            repo_config=diff.repo_config,
            confidence=_judge_confidence(assessment.funnel.graded, assessment.funnel.verdicts),
            pr=run.pr_name,
        )
    return _ReviewComments(muted, budget, budget_on, prior_keys, dedup_degraded)


def _render_tier1_summary(
    run: _ReviewRun, diff: _PreparedDiff, assessment: _ReviewAssessment,
    comments: _ReviewComments, *, conclusion: CheckConclusion,
    tier1_phase: Literal["tier1", "dual"], outage_advisory: bool,
) -> tuple[CheckConclusion, str, str]:
    evaluation = assessment.evaluation
    title, summary = _summary_markdown(
        evaluation, suppressed_count=len(assessment.funnel.suppressed),
        excluded_paths=diff.excluded_paths,
        oversized_paths=diff.oversized_paths,
        duplicate_paths=diff.duplicate_paths,
        move_paths=diff.move_paths,
        living_range=diff.living.range,
        living_baseline_rejected=diff.living.rejected,
        living_delta_empty=diff.living.empty_range,
        review_phase=tier1_phase,
        defensive_dropped_count=assessment.funnel.defensive_dropped,
        blocking=run.blocking and not outage_advisory,
        muted=comments.muted,
    )
    if assessment.lock_check.unreviewed and conclusion == "success":
        # #1649: a lock file neither machine-checked nor read by the model
        # never passes.
        names = ", ".join(f"`{n}`" for n in assessment.lock_check.unreviewed)
        conclusion = "neutral"
        title = _hunt_title("WARN Elder did not read lock file(s)", diff.living.range)
        summary += (
            f"\n\nLock files {names} were kept from Elder's review (size or "
            "ignore rules) and have no machine check, so this pass is "
            "neutral, not success."
        )
    if evaluation.reviewability_note:
        summary += "\n\n## Reviewability assessment\n\n" + evaluation.reviewability_note
    summary = _intent_paragraph(run.pr_context) + summary
    if assessment.ruff_policy_dropped:
        summary += (
            "\n\nRuff policy: " + str(sum(assessment.ruff_policy_dropped))
            + " finding(s) were omitted because the repository configuration "
            "ignores or explicitly selects those rules for the affected paths."
        )
    summary += _analyzer_skip_note(assessment.analyzer_skips)
    summary += f"\n\n{comments.budget.note}" if comments.budget.note else ""
    summary += assessment.config_note
    summary += assessment.deletion_note
    summary += assessment.lock_check.note
    summary += _linked_issue_summary_note(
        str(run.pr_context.get("linked_issue_context") or ""),
        int(run.pr_context.get("linked_issue_live_criteria_count") or 0),
        tuple(run.pr_context.get("linked_issue_numbers") or ()),
    )
    summary += installation_permission_summary_note(run.installation_id)
    return conclusion, title, summary


def _plan_tier1_publication(
    run: _ReviewRun, diff: _PreparedDiff, assessment: _ReviewAssessment,
    comments: _ReviewComments,
) -> _Tier1Publication:
    evaluation = assessment.evaluation
    outage = observe_outage(evaluation, blocking=run.blocking)
    outcome = plan_publish(
        evaluation, mode=run.mode, degraded_fails_closed=True,
        outage_advisory=outage.advisory,
    )
    depth_now = os.getenv("GRUG_REVIEW_DEPTH", "tiered").strip().lower()
    tier1_phase: Literal["tier1", "dual"] = (
        "tier1" if depth_now == "tiered" else "dual"
    )
    conclusion, title, summary = _render_tier1_summary(
        run, diff, assessment, comments,
        conclusion=outcome.conclusion, tier1_phase=tier1_phase,
        outage_advisory=outage.advisory,
    )
    conclusion, title, summary, listed_threads, carried_blocking = _carry_into_check(
        action=run.action, evaluation=evaluation, mode=run.mode,
        installation_id=run.installation_id, owner=run.owner, repo_name=run.repo_name,
        pull_number=run.pull_number, head_sha=run.head_sha, conclusion=conclusion,
        title=title, summary=summary,
        threads=assessment.review_threads,
    )
    # #1758: the review state is the verdict, whatever the mode. A clean
    # pass vouches for the head only when the carry-forward fold ran (or
    # there was nothing earlier to fold), every lock file was read, and the
    # sensitive-path security pass finished.
    verdict = _own_pr_event(run.author_login, verdict_event(
        evaluation, conclusion=conclusion, carried_blocking=carried_blocking,
        vouched=(
            not assessment.lock_check.unreviewed
            and not assessment.quarantine_gap
            and not diff.unreviewed_paths
            and (run.action not in _RE_REVIEW_ACTIONS or listed_threads is not None)
        ),
    ))
    # Decided once, before anything is posted (the escalation may sample):
    # Tier-1 must know whether a deep pass will follow on this head.
    deep_plan = _async_deep_decision(
        check_publish_failed=False,
        llm_hunks=list(assessment.inputs.evidence.hunks),
        pr_context=run.pr_context,
        evaluation=evaluation,
        cancel_event=run.cancel_event,
        owner=run.owner,
        repo_name=run.repo_name,
        pull_number=run.pull_number,
    )
    # #1758: an APPROVE posted while the deep pass is still looking would let
    # a gate merge before that pass reports a blocking finding. So Tier-1
    # defers its approval to the deep append, which approves only if it
    # finishes clean; if it fails or degrades the head stays unapproved.
    deferred_approval = verdict == "APPROVE" and deep_plan is not None
    event = "COMMENT" if deferred_approval else verdict
    if event == "APPROVE" and _pr_head_state(
        run.installation_id, run.owner, run.repo_name, run.pull_number, run.head_sha,
    ) != "current":
        # #1781: a push landed after this run read the head (or the head
        # could not be confirmed). Never approve a head that is not current.
        event = "COMMENT"
    # grug#1429 pin + closure, then grug#1212's degraded lead: a degraded
    # pass still says so before anything else, the pin line right after.
    pinned_issues = _pinned_issues(run.pr_context, run.owner, run.repo_name)
    summary = pin_review(
        summary, evaluation, head_sha=run.head_sha, linked_issues=pinned_issues,
    )
    summary = _lead_if_degraded(summary, evaluation)
    if outage.advisory:
        title = ADVISORY_TITLE
        summary = advisory_summary(outage, summary)
    if conclusion == "neutral":
        # grug#1753: a neutral cannot clear a blocking gate, so say why.
        # The lock-file rule above changed the conclusion after the gate,
        # so its cause is passed alongside the ones the gate read.
        why = neutral_reason(
            evaluation, mode=run.mode,
            outage_advisory=outage.advisory,
            unreviewed_lock_files=assessment.lock_check.unreviewed,
        )
        summary = append_neutral_reason(summary, why)
        title = neutral_check_title(title, why)
    _alert_if_no_review(evaluation, run.pr_name)
    return _Tier1Publication(
        conclusion=conclusion, title=title, summary=summary, outcome=outcome,
        event=event, phase=tier1_phase, pinned_issues=pinned_issues, listed_threads=listed_threads,
        deep_plan=deep_plan, deferred_approval=deferred_approval,
    )


def _post_tier1_check(run: _ReviewRun, plan: _Tier1Publication) -> _PostedCheck:
    check_result = CheckRunResult(
        name=_CHECK_NAME,
        head_sha=run.head_sha,
        status="completed",
        conclusion=plan.conclusion,
        title=plan.title,
        summary=plan.summary,
    )
    try:
        check_resp = with_install_token_retry(
            run.installation_id,
            lambda token: post_check_run(
                token, run.owner, run.repo_name, check_result,
                external_id=f"grug-cr:{run.pr_name}:{run.head_sha}",
            ),
        )
        return _PostedCheck(
            run_id=(check_resp or {}).get("id"),
            url=str((check_resp or {}).get("html_url") or ""),
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.error(
            "code_review_check_run_publish_failed",
            extra={
                "installation_id": run.installation_id,
                "pr": run.pr_name,
                "kind": type(e).__name__,
            },
        )
        # Continue to attempt the independent review post.
        return _PostedCheck(failed=True)


def _read_prior_comments(run: _ReviewRun, comments: _ReviewComments) -> _ReviewComments:
    if not comments.budget_on and run.action in {"synchronize", "reopened", "review"}:
        prior_keys, dedup_degraded = prior_finding_keys(
            run.installation_id, run.owner, run.repo_name, run.pull_number,
        )
        return replace(comments, prior_keys=prior_keys, dedup_degraded=dedup_degraded)
    return comments


def _project_tier1(
    run: _ReviewRun, diff: _PreparedDiff, assessment: _ReviewAssessment,
    comments: _ReviewComments, plan: _Tier1Publication,
) -> Markings:
    evaluation = assessment.evaluation
    # Only pay the ledger fetch when there is something to annotate. A fully
    # degraded eval publishes no inline review; a partial staged review does.
    precedent_notes = (
        _precedent_notes_for(run.full_repo, evaluation.findings)
        if evaluation.findings
        and evaluation.degraded_reason in (None, "", *PUBLISHABLE_DEGRADATIONS)
        else {}
    )
    return project(FinishedPass(
        board_evaluation=evaluation,
        review_evaluation=evaluation,
        conclusion=plan.conclusion,
        event=plan.event,
        review_phase=plan.phase,
        head_sha=run.head_sha,
        base_sha=str(run.pr_context.get("base_sha") or ""),
        pr_title=str(run.pr_context.get("title") or ""),
        living_range=diff.living.range,
        suppressed_count=len(assessment.funnel.suppressed),
        file_contents=assessment.inputs.file_contents or {},
        diff_text=diff.text or "",
        linked_issues=plan.pinned_issues,
        precedent_notes=precedent_notes,
        muted=comments.muted,
        prior_keys=comments.prior_keys,
        held_back=comments.budget.held_keys,
        held_note=comments.budget.note,
        repo_config=diff.repo_config,
        posts_verdict=True,
        run_started_at=run.run_started_at,
    ))


def _post_tier1_review(run: _ReviewRun, review_result: ReviewResult | None) -> _PostedReview:
    if review_result is None:
        return _PostedReview()
    try:
        response = with_install_token_retry(
            run.installation_id,
            lambda token: post_review(
                token, run.owner, run.repo_name,
                pull_number=run.pull_number, result=review_result,
            ),
        )
        return _PostedReview(response=response)
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
                "installation_id": run.installation_id,
                "pr": run.pr_name,
                "kind": type(e).__name__,
                "permanent": permanent,
                "inline_comments": len(review_result.comments),
                **_http_error_detail(e),
            },
        )
        return _PostedReview(failed=not permanent, rejected=permanent)


def _dismiss_tier1_superseded_reviews(
    run: _ReviewRun, plan: _Tier1Publication, check: _PostedCheck, review: _PostedReview,
) -> None:
    # A blocking review from an earlier commit stays GitHub's standing verdict
    # until it is dismissed; a clean re-review alone does not clear it.
    # Withdraw it when this run APPROVES the head (grug#1581, #1758): the
    # dismissal follows the verdict, so advisory findings never keep a block
    # alive, in either mode. `verdict_event` already refuses to approve a
    # degraded run, a head with an earlier finding still open
    # (carry-forward), or a re-review whose fold could not run; the
    # `listed_threads` guard keeps a first review from listing reviews.
    if (
        plan.event == "APPROVE"
        and plan.listed_threads is not None
        and not check.failed
    ):
        _dismiss_superseded_reviews(
            run.installation_id, run.owner, run.repo_name, run.pull_number, run.head_sha,
            run.run_started_at, state="CHANGES_REQUESTED",
            clean_url=check.url or str((review.response or {}).get("html_url") or ""),
        )
    elif plan.event == "COMMENT" and run.action in _RE_REVIEW_ACTIONS:
        # #1758, fail closed: this run cannot vouch for the head (or has
        # deferred its verdict to the deep pass), so an approval Elder gave
        # earlier no longer stands.
        _dismiss_superseded_reviews(
            run.installation_id, run.owner, run.repo_name, run.pull_number, run.head_sha,
            run.run_started_at, state="APPROVED",
        )


def _record_tier1_publication(
    run: _ReviewRun, diff: _PreparedDiff, assessment: _ReviewAssessment, plan: _Tier1Publication,
    check: _PostedCheck, review: _PostedReview, markings: Markings,
) -> None:
    _dismiss_tier1_superseded_reviews(run, plan, check, review)
    evaluation = assessment.evaluation

    # Capture inline-comment IDs for later reaction polling (#247). BEST-
    # EFFORT, post-publish, own try/except - a capture failure must never
    # change the review outcome (`result` is computed below, unaffected).
    # Persist posted findings even if trace export failed: a later trusted
    # maintainer reaction can still teach the repository ledger without DD span
    # attribution.
    review_resp = review.response
    review_id = review_resp.get("id") if review_resp else None
    comment_ids: hunt_link.CommentIds = {}
    if review_id is not None:
        comment_ids = _capture_review_comments(
            installation_id=run.installation_id,
            owner=run.owner,
            repo_name=run.repo_name,
            pull_number=run.pull_number,
            review_id=review_id,
            findings=markings.posted_findings,
            review_span_context=assessment.model_response.review_span_context,
            head_sha=run.head_sha,
            author_login=run.author_login,
            failure_log="code_review_comment_capture_failed",
        )

    # Hunt Board (#1207): the canonical session learns this pass, and the
    # board's `hunt` section is refreshed from it. Before the stack comment so
    # the two projections read the same record. A delta review (living range)
    # only speaks for the files it read.
    _record_hunt_run(
        installation_id=run.installation_id, owner=run.owner, repo_name=run.repo_name,
        pull_number=run.pull_number, head_sha=run.head_sha, trigger=run.action,
        started_at=run.started_at, llm=assessment.model_response, findings=evaluation.findings,
        file_contents=assessment.inputs.file_contents,
        diff_paths=frozenset(h.file_path for h in diff.hunks),
        delta=bool(diff.living.range), comment_ids=comment_ids,
        check_run_id=check.run_id, degraded_reason=evaluation.degraded_reason,
    )

    # Markings v2 review stack: upsert PR-timeline summary (actionable count
    # + consolidated agent prompt). Best-effort - never fails the check/review
    # that already published. Skipped when the check never landed.
    if check.failed:
        return
    stack_body = markings.stack_body
    if stack_body is None:
        log.warning(
            "elder_review_stack_upsert_failed",
            extra={
                "installation_id": run.installation_id,
                "pr": run.pr_name,
                "kind": markings.stack_error,
            },
        )
    else:
        try:
            with_install_token_retry(
                run.installation_id,
                lambda token: _upsert_review_stack_comment(
                    token, run.owner, run.repo_name, run.pull_number, stack_body,
                    create_if_absent=markings.stack_create_if_absent,
                ),
            )
            log.info(
                "elder_review_stack_upserted",
                extra={
                    "installation_id": run.installation_id,
                    "pr": run.pr_name,
                    "findings": len(evaluation.findings),
                },
            )
        except Exception as e:  # noqa: BLE001 - stack is cosmetic UX
            log.warning(
                "elder_review_stack_upsert_failed",
                extra={
                    "installation_id": run.installation_id,
                    "pr": run.pr_name,
                    "kind": type(e).__name__,
                },
            )


def _record_dispatch_outcome(
    run: _ReviewRun, assessment: _ReviewAssessment, plan: _Tier1Publication,
    check: _PostedCheck, review: _PostedReview, comments: _ReviewComments,
) -> PublishOutcome:
    evaluation = assessment.evaluation
    llm_response = assessment.model_response
    outcome = plan.outcome.settled(
        check_publish_failed=check.failed,
        review_publish_failed=review.failed,
        review_publish_rejected=review.rejected,
    )
    result = outcome.result
    # Structured log carries everything needed to verify the persona
    # ran end-to-end on a real PR (operator AC). Backend + model
    # attribution lets DD LLM Obs slice metrics by which LLM produced
    # the verdict; degraded_reason correlates dispatch volume with
    # backend health.
    log.info(
        "code_reviewer_dispatched",
        extra={
            "installation_id": run.installation_id,
            "pr": run.pr_name,
            "head_sha": run.head_sha[:8],
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
            "dropped_withdrawn": evaluation.dropped_withdrawn,
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
            "dedup_degraded": comments.dedup_degraded,
            "result": result,
        },
    )
    # Activity feed (PRD #301): record what Elder did, best-effort. Use the
    # PUBLISHED `conclusion` (the actual PR outcome from plan_publish), NOT
    # the raw eval severity - in advisory mode high/critical findings post
    # `neutral` (no gate), so the honest badge is `warn`, not `block`. The
    # degraded reason comes from the same `outcome` as `result` above, so the
    # feed resolves to `errored` (never a fake pass/block) exactly when the
    # LLM degraded or the check-run never reached GitHub ("no lies").
    record_check_verdict(
        install_id=run.installation_id,
        persona_key="code_reviewer",
        repo=run.full_repo,
        pr_number=run.pull_number,
        head_sha=run.head_sha,
        conclusion=plan.conclusion,
        summary=plan.title,
        findings_count=len(evaluation.findings),
        blocking=run.blocking,
        degraded_reason=outcome.feed_degraded_reason,
        **_activity_families(evaluation.families),
    )
    return outcome


def _remember_review_baseline(
    run: _ReviewRun, diff: _PreparedDiff, assessment: _ReviewAssessment,
    budget: _BudgetOutcome, conclusion: CheckConclusion, outcome: PublishOutcome,
    carry_request: carry_verdict.CarryRequest,
    full_pr_diff: Callable[[], str],
) -> None:
    result = outcome.result
    lock_check = assessment.lock_check
    # Living Hunt: remember this head only when the check actually landed
    # and the review was not an infra skip (so the next push can delta).
    if (
        result in {"pass", "fail", "skipped"}
        and not outcome.check_publish_failed
        and assessment.evaluation.degraded_reason in (None, "", "no_diff")
        and not budget.keep_baseline
        # #1649: a failed lock review keeps the baseline, so the next push
        # judges the lock change again rather than only what follows it.
        and not ((diff.lock_only or lock_check.findings) and result != "pass")
        and not lock_check.unreviewed
        # #1781: a path no model read keeps the baseline (and stores no
        # patch-id to carry), so the next push reviews it again.
        and not diff.unreviewed_paths
    ):
        try:
            _, current_head_sha, _, _ = with_install_token_retry(
                run.installation_id,
                lambda token: _fetch_current_review_snapshot(
                    token, run.owner, run.repo_name, run.pull_number,
                ),
            )
            if current_head_sha == run.head_sha:
                carry_verdict.store_record(carry_request, diff_id=carry_verdict.passed_diff_id(
                    conclusion, full_diff=None if diff.living.prior_sha else diff.text,
                    fetch_pr_diff=full_pr_diff,
                ))
                if diff.living.range:
                    log.info(
                        "elder_living_hunt_delta_done",
                        extra={
                            "pr": run.pr_name,
                            "range": diff.living.range,
                        },
                    )
            else:
                log.info(
                    "elder_living_hunt_stale_anchor_skipped",
                    extra={
                        "pr": run.pr_name,
                        "review_head": run.head_sha,
                        "current_head": current_head_sha,
                    },
                )
        except Exception as e:  # noqa: BLE001 - memory must not fail publish
            log.warning(
                "elder_living_hunt_put_failed",
                extra={
                    "pr": run.pr_name,
                    "kind": type(e).__name__,
                },
            )


def _note_resolved_threads(
    run: _ReviewRun, review_result: ReviewResult | None, review: _PostedReview, resolved: int,
) -> None:
    """Say in the review body how many earlier Elder threads this run resolved."""
    noun = "thread" if resolved == 1 else "threads"
    note = f"Resolved {resolved} earlier Elder {noun}."
    review_id = (review.response or {}).get("id")
    try:
        if review_result is None:
            with_install_token_retry(
                run.installation_id,
                lambda token: post_review(
                    token, run.owner, run.repo_name, pull_number=run.pull_number,
                    result=ReviewResult(run.head_sha, "COMMENT", note, ()),
                ),
            )
        elif isinstance(review_id, int):
            with_install_token_retry(
                run.installation_id,
                lambda token: update_review_body(
                    token, run.owner, run.repo_name, pull_number=run.pull_number,
                    review_id=review_id, body=f"{review_result.body}\n\n{note}",
                ),
            )
    except Exception as e:  # noqa: BLE001 - the review is already complete
        log.warning(
            "elder_thread_resolution_note_failed",
            extra={"pr": run.pr_name, "kind": type(e).__name__},
        )


def _finish_tier1_review(
    run: _ReviewRun, diff: _PreparedDiff, assessment: _ReviewAssessment, voice: VoiceSelection,
    comments: _ReviewComments, plan: _Tier1Publication, check: _PostedCheck, review: _PostedReview,
    markings: Markings,
) -> None:
    evaluation = assessment.evaluation
    # Async deep append (#646): Tier-1 (coder) already published the required
    # check. When tiered escalation fires, run the reasoner arm now and append
    # any new findings. Failures here never change `result` - the required
    # check already completed.
    finished_evaluation = None
    try:
        finished_evaluation = _async_deep_append_if_needed(
            installation_id=run.installation_id,
            owner=run.owner,
            repo_name=run.repo_name,
            pull_number=run.pull_number,
            head_sha=run.head_sha,
            action=run.action,
            snapshot_id=run.snapshot_id,
            mode=run.mode,
            blocking=run.blocking,
            hunks=diff.hunks,
            diff_text=diff.text,
            evidence=assessment.inputs.evidence,
            pr_context=run.pr_context,
            voice=voice,
            living_range=diff.living.range,
            living_baseline_rejected=diff.living.rejected,
            living_delta_empty=diff.living.empty_range,
            evaluation=evaluation,
            prior_keys=comments.prior_keys,
            check_publish_failed=check.failed,
            cancel_event=run.cancel_event,
            author_login=run.author_login,
            repo_config=diff.repo_config,
            config_note=assessment.config_note,
            tier1_check_run_id=check.run_id,
            deep_plan=plan.deep_plan,
            deferred_approval=plan.deferred_approval,
            vouched=not (assessment.quarantine_gap or diff.unreviewed_paths),
            run_started_at=run.run_started_at,
        )
    except Exception as e:  # noqa: BLE001 - deep append is best-effort
        log.warning(
            "elder_async_deep_append_failed",
            extra={
                "pr": run.pr_name,
                "kind": type(e).__name__,
            },
        )

    # Reconcile threads against the complete set of findings, including
    # those re-raised by the deep append (#1102, #1803).
    resolved_threads = _tidy_fixed_threads(
        action=run.action,
        review_published=not (review.failed or review.rejected),
        evaluation=finished_evaluation,
        installation_id=run.installation_id, owner=run.owner, repo_name=run.repo_name,
        pull_number=run.pull_number, head_sha=run.head_sha, threads=plan.listed_threads,
        pr_author=run.author_login, trusted_push=_head_is_same_repo(run.pr, run.repo),
    )
    if resolved_threads:
        _note_resolved_threads(run, markings.review, review, resolved_threads)

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
            assessment.funnel.graded, assessment.funnel.verdicts,
            review_span_context=assessment.model_response.review_span_context,
        )
    except Exception as e:  # noqa: BLE001 - defense-in-depth over submit_evals's own guard
        log.error(
            "code_review_judge_dispatch_failed",
            extra={
                "installation_id": run.installation_id,
                "pr": run.pr_name,
                "kind": type(e).__name__,
            },
        )


def _run_quarantined_pass(
    evaluation: CodeReviewEvaluation,
    hunks: tuple[DiffHunk, ...],
    evidence: ElderEvidence,
    installation_id: int,
    *,
    repo_config: RepoConfig,
    started: float,
    voice: VoiceSelection,
    cancel_event: threading.Event | None,
    owner: str,
    repo_name: str,
    pull_number: int,
    head_sha: str,
    skip: bool,
) -> tuple[CodeReviewEvaluation, str]:
    """The code-only security pass (grug#1569): reviews the sensitive paths
    without author text, adjudicates with the same quarantined evidence, and
    merges one way into `evaluation`. Returns the evaluation and a summary
    note, empty unless the pass did not finish (failed, or skipped on a
    sensitive path). A non-empty note means the run never approves. The pass is additive, so a failure
    leaves the normal review standing and names the gap in the summary. It
    shares the review's wall-clock budget, so it cannot run past the job
    timeout. One `elder_quarantined_pass` row per run reports calls per PR."""
    if not quarantined_pass_enabled():
        return evaluation, ""
    paths = sensitive_paths(
        dict.fromkeys(h.path for h in evidence.hunks), repo_config,
    )
    if not paths:
        return evaluation, ""
    if skip:
        # Sensitive paths with no security pass: not finished (fail closed).
        return evaluation, QUARANTINE_UNAVAILABLE_NOTE
    quarantined = quarantined_evidence(evidence, paths)
    pr = f"{owner}/{repo_name}#{pull_number}"
    outcome = "unavailable"
    merged = None
    model_calls = 0
    try:
        with _review_deadline(started + _staged_review_budget_s()):
            response = review_diff(
                quarantined, installation_id=installation_id, voice=voice,
                cancel_event=cancel_event,
            )
            coverage = response.coverage
            model_calls = coverage.total_cohorts if coverage else 1
            if response.kind == "reviewed":
                graded, judged, verdicts, _ = _grade_deep_response(
                    response, hunks, quarantined, installation_id,
                    pr_context=quarantined.pr_context or {}, owner=owner,
                    repo_name=repo_name, pull_number=pull_number,
                    head_sha=head_sha, arm="quarantined",
                )
                submit_evals(
                    judged, verdicts,
                    review_span_context=response.review_span_context,
                )
                merged = merge_by_fingerprint(evaluation.findings, graded.findings)
                outcome = "partial" if response.error else "reviewed"
                model_calls += _adjudication_calls(response)
    except Exception as e:  # noqa: BLE001 - additive; the normal review stands
        log.warning(
            "elder_quarantined_pass_failed",
            extra={"pr": pr, "kind": type(e).__name__}, exc_info=True,
        )
    log.info(
        "elder_quarantined_pass",
        extra={
            "installation_id": installation_id,
            "pr": pr,
            "outcome": outcome,
            "sensitive_paths": len(paths),
            "model_calls": model_calls,
            "added": merged.added if merged else 0,
            "raised": merged.raised if merged else 0,
        },
    )
    if merged is not None and merged.findings != evaluation.findings:
        evaluation = with_findings(evaluation, merged.findings)
    return evaluation, "" if outcome == "reviewed" else QUARANTINE_UNAVAILABLE_NOTE


def _is_second_lineage(
    response: LlmReviewResponse, primary_models: tuple[str, ...],
    author_family: str | None,
) -> bool:
    """A finished reply from a family named in neither the primary pass nor
    the PR's author. Fails closed: an unknown family on either side is not
    proof of a second lineage."""
    primary = {model_family(model) for model in primary_models}
    answered = {model_family(model) for model in response.models_used}
    if not primary or not answered or None in primary or None in answered:
        return False
    return (
        response.kind == "reviewed" and not response.error
        and not answered & primary and author_family not in answered
    )


def _second_lineage_rows(
    outcome: str, findings: tuple[Finding, ...], *, repo: str, pull_number: int,
    head_sha: str, model_calls: int,
) -> list[dict]:
    """Ledger rows: one per blocking finding only this pass raised, then one
    for the run. They carry no reaction label."""
    base = {
        "repo": repo, "pr": pull_number, "reviewer": SECOND_LINEAGE_REVIEWER,
        "ts": datetime.now(timezone.utc).isoformat(),
        "commit": head_sha or None, "head_sha": head_sha or None,
    }
    rows = [
        {
            **base, "severity": f.severity, "class": f.rule_name,
            "category": f.category,
            "finding": redact_secrets(f.message.removeprefix(SECOND_LINEAGE_MARK)),
            "verdict": "second-only",
            # No head here: one finding is one row across pushes.
            "evidence": f"second-lineage:{f.file}:{f.line}:{f.rule_name}",
        }
        for f in findings if f.severity in ("high", "critical")
    ]
    rows.append({
        **base, "severity": "", "class": "second-lineage", "finding": "run",
        "verdict": outcome, "evidence": f"second-lineage:{head_sha}",
        "model_calls": model_calls,
    })
    return rows


def put_second_lineage_rows(rows: list[dict]) -> None:
    put_verifier_verdicts(rows)


def _run_second_lineage_pass(
    evaluation: CodeReviewEvaluation,
    hunks: tuple[DiffHunk, ...],
    evidence: ElderEvidence,
    installation_id: int,
    *,
    primary_models: tuple[str, ...],
    repo_config: RepoConfig,
    started: float,
    cancel_event: threading.Event | None,
    owner: str,
    repo_name: str,
    pull_number: int,
    head_sha: str,
    skip: bool,
) -> tuple[CodeReviewEvaluation, str]:
    """A second review of the high-risk paths on the subscription lane
    (#1573), in a fresh context with the quarantined evidence. Findings merge
    one way into `evaluation`, and one only this pass raised is marked, so its
    incremental value can be measured. It never breaks a tie on the primary
    pass's findings. Returns a single-lineage note when no second lineage
    answered, so the gap is never silent."""
    if skip or not second_lineage_enabled():
        return evaluation, ""
    paths = high_risk_paths(
        dict.fromkeys(h.path for h in evidence.hunks), repo_config,
    )
    if not paths:
        return evaluation, ""
    fresh = quarantined_evidence(evidence, paths)
    pr = f"{owner}/{repo_name}#{pull_number}"
    author_family = (fresh.pr_context or {}).get("author_family")
    merged = None
    outcome = "single-lineage"
    model_calls = 0
    try:
        with _review_deadline(started + _staged_review_budget_s()):
            response = review_second_lineage(
                fresh, installation_id, primary_models, cancel_event=cancel_event,
            )
            # A lane call happened only if the lane tier answered.
            model_calls = 1 if response.tier_served == "subscription" else 0
            if _is_second_lineage(response, primary_models, author_family):
                graded, judged, verdicts, _ = _grade_deep_response(
                    response, hunks, fresh, installation_id,
                    pr_context=fresh.pr_context or {}, owner=owner,
                    repo_name=repo_name, pull_number=pull_number,
                    head_sha=head_sha, arm="second-lineage",
                )
                model_calls += _adjudication_calls(response)
                submit_evals(
                    judged, verdicts,
                    review_span_context=response.review_span_context,
                )
                merged = merge_by_fingerprint(
                    evaluation.findings, graded.findings,
                    mark=SECOND_LINEAGE_MARK, second_opinion=True,
                )
                outcome = "second-lineage-run"
    except Exception as e:  # noqa: BLE001 - additive; the normal review stands
        merged = None
        outcome = "single-lineage"
        log.warning(
            "elder_second_lineage_failed",
            extra={"pr": pr, "kind": type(e).__name__}, exc_info=True,
        )
    second_only = merged.added_findings if merged else ()
    put_second_lineage_rows(_second_lineage_rows(
        outcome, second_only, repo=f"{owner}/{repo_name}",
        pull_number=pull_number, head_sha=head_sha, model_calls=model_calls,
    ))
    log.info(
        "elder_second_lineage",
        extra={
            "installation_id": installation_id, "pr": pr, "outcome": outcome,
            "sensitive_paths": len(paths), "model_calls": model_calls,
            "added": merged.added if merged else 0,
        },
    )
    if merged is not None and merged.findings != evaluation.findings:
        evaluation = with_findings(evaluation, merged.findings)
    return evaluation, "" if merged is not None else SINGLE_LINEAGE_NOTE


def _adjudication_calls(response: LlmReviewResponse) -> int:
    """Judge and refute batches for what the model returned."""
    blocking = sum(1 for f in response.findings if f.severity in ("high", "critical"))
    return (
        ceil(len(response.findings) / JUDGE_BATCH_SIZE)
        + ceil(blocking / JUDGE_BATCH_SIZE)
    )


@dataclass(frozen=True)
class _BudgetOutcome:
    held_keys: frozenset[str] = frozenset()
    note: str = ""
    # A held blocking finding has no thread, so the next push must see it.
    keep_baseline: bool = False


def _deep_held_keys(
    novel: tuple[Finding, ...],
    combined_eval: CodeReviewEvaluation,
    prior_keys: frozenset[str],
    repo_config: RepoConfig | None,
) -> frozenset[str]:
    """The deep arm's share of the budget: what tier-1 and earlier rounds
    already spent comes off the cap (#1570). Its findings show on the board."""
    if not budget_enabled():
        return frozenset()
    spent = sum(
        1 for f in combined_eval.findings
        if is_model_blocking(f)
        and finding_key(f.file, f.line, f.rule_name) in prior_keys
    )
    split = split_for_budget(
        novel, cap=(repo_config or RepoConfig()).elder_blocking_cap,
        open_blocking=spent, confidence={},
    )
    return frozenset(finding_key(f.file, f.line, f.rule_name) for f in split.held)


def _judge_confidence(
    findings: tuple[Finding, ...], verdicts: tuple[Any, ...],
) -> dict[str, tuple[bool, float]]:
    """The plain judge's view of each graded finding, by finding key."""
    return {
        finding_key(findings[v.finding_index].file, findings[v.finding_index].line,
                    findings[v.finding_index].rule_name): (v.is_real_bug, v.confidence)
        for v in verdicts if 0 <= v.finding_index < len(findings)
    }


def _round_changed_lines(
    hunks: tuple[DiffHunk, ...], *, prior_review_head: str, head_sha: str,
    delta_reviewed: bool, keys_known: bool,
) -> frozenset[tuple[str, int]] | None:
    """The lines this round changed, for the drip-feed rule. None on a first
    round, after rewritten history, and when the prior markings could not be
    read: the rule then marks nothing late."""
    if not prior_review_head or not keys_known:
        return None
    if prior_review_head == head_sha:
        return frozenset()
    if not delta_reviewed:
        return None
    return frozenset(
        (hunk.file_path, line) for hunk in hunks for line, _ in added_lines(hunk)
    )


def _plan_comment_budget(
    evaluation: CodeReviewEvaluation,
    *,
    prior_keys: frozenset[str],
    changed_lines: frozenset[tuple[str, int]] | None,
    open_threads: list[dict],
    repo_config: RepoConfig | None,
    confidence: dict[str, tuple[bool, float]],
    pr: str,
) -> _BudgetOutcome:
    """Pick what stays off the inline surface (#1570). The evaluation is left
    whole, so nothing here can turn a failing check green. One
    `elder_comment_budget` row per review."""
    config = repo_config or RepoConfig()
    new: list[Finding] = []
    posted: list[Finding] = []
    for finding in evaluation.findings:
        key = finding_key(finding.file, finding.line, finding.rule_name)
        (posted if key in prior_keys else new).append(finding)
    late: tuple[Finding, ...] = ()
    fresh = tuple(new)
    if config.elder_drip_feed:
        fresh, late = drip_fed(
            fresh, prior_keys=prior_keys, changed_lines=changed_lines,
        )
    split = split_for_budget(
        tuple(f for f in fresh if f.outside_diff_reason is None),
        cap=config.elder_blocking_cap,
        open_blocking=max(
            carry_forward.open_blocking_count(open_threads),
            sum(1 for f in posted if is_model_blocking(f)),
        ),
        confidence=confidence,
    )
    held = split.held + late
    log.info(
        "elder_comment_budget",
        extra={"pr": pr, "held": len(split.held), "late": len(late),
               "inline": len(split.inline), "cap": config.elder_blocking_cap},
    )
    return _BudgetOutcome(
        frozenset(finding_key(f.file, f.line, f.rule_name) for f in held),
        held_lines(held, confidence),
        any(is_model_blocking(f) for f in split.held),
    )


def put_verifier_verdicts(rows: list[dict]) -> None:
    """Store verifier verdicts in the findings ledger. Best-effort per row: a
    store failure never changes what publishes."""
    from adapters.install_store import put_ledger_row

    for row in rows:
        try:
            put_ledger_row(row)
        except Exception as e:  # noqa: BLE001 - telemetry never breaks the review
            log.warning("elder_verifier_ledger_failed", extra={"kind": type(e).__name__})


def _apply_refute_gate(
    findings, evidence, installation_id, *, owner, repo_name, pull_number, arm,
    plain_findings=(), plain_verdicts=(),
):
    """Refute gate (#714): adversarial evidence-check for the HIGH/CRITICAL
    findings that survived deterministic verification - the semantic-
    misreading class no grep can refute. Returns the surviving findings;
    kills are recorded through the same telemetry as verification kills
    (reason "refuted") so the #707 scoreboard attributes them and the
    false-kill hunt covers this gate too.

    Fail-open, softened (#1400): a HIGH whose refute batch no backend
    answered still publishes, but as an advisory medium when the plain judge
    (`plain_verdicts`, indexed into `plain_findings`) already called it not
    real at the refute floor. A critical never softens. One
    `code_review_refute_unverified_downgraded` row per downgrade, plus the
    `grug.elder.refute_unverified_downgraded` gauge for the #707 scoreboard."""
    high = tuple(f for f in findings if f.severity in ("high", "critical"))
    if not high:
        return findings
    cost = None
    if falsification_verifier_enabled():
        outcome, cost = falsify_with_outcome(high, evidence, installation_id)
    else:
        outcome = refute_with_outcome(high, evidence, installation_id)
    verdicts = outcome.verdicts
    _, refuted = partition_refuted(high, verdicts) if verdicts else ((), ())
    if cost is not None:
        head_sha = str((evidence.pr_context or {}).get("head_sha") or "")
        put_verifier_verdicts(verifier_rows(
            high, outcome, refuted, repo=f"{owner}/{repo_name}",
            pull_number=pull_number, head_sha=head_sha, arm=arm,
        ))
        log.info(
            "elder_verifier",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "arm": arm,
                "findings": len(high),
                "refuted": len(refuted),
                "unavailable": len(outcome.unavailable),
                "model_calls": cost.model_calls,
                "estimated_prompt_tokens": cost.estimated_prompt_tokens,
            },
        )
    if refuted:
        from personas.code_reviewer.verify import KilledFinding
        _record_verification_kills(
            tuple(KilledFinding(finding=f, reason="refuted") for f in refuted),
            installation_id=installation_id, owner=owner,
            repo_name=repo_name, pull_number=pull_number, arm=arm,
        )
    softened, downgraded = advisory_when_unrefuted(
        high, outcome.unavailable,
        plain_findings=tuple(plain_findings), plain_verdicts=tuple(plain_verdicts),
    )
    for original, verdict in downgraded:
        log.info(
            "code_review_refute_unverified_downgraded",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "path": original.file,
                "line": original.line,
                "rule": original.rule_name,
                "severity": original.severity,
                "confidence": verdict.confidence,
                "arm": arm,
            },
        )
    if downgraded:
        try:
            from observability import emit_gauge  # type: ignore
            emit_gauge(
                "grug.elder.refute_unverified_downgraded", len(downgraded),
                tags={"repo": f"{owner}/{repo_name}", "arm": arm},
            )
        except Exception as e:  # noqa: BLE001 - telemetry never breaks the review
            log.debug("refute_downgrade_gauge_failed", extra={"kind": type(e).__name__})
    if not refuted and not downgraded:
        return findings
    dead = {id(f) for f in refuted}
    replacement = {
        id(orig): new for orig, new in zip(high, softened, strict=True) if new is not orig
    }
    return tuple(
        replacement.get(id(f), f) for f in findings if id(f) not in dead
    )


def _apply_bound_name_downgrade(
    evaluation: CodeReviewEvaluation, file_contents, *, installation_id: int,
    owner: str, repo_name: str, pull_number: int, arm: str,
) -> CodeReviewEvaluation:
    """Bound-name downgrade (#1227): a blocking "is not defined" marking
    whose every named identifier is bound in an enclosing scope of the
    post-change file becomes a non-blocking note. Runs after the
    verification kills and before the refute gate, so a downgraded finding
    no longer spends a refute call. One structured row per downgrade."""
    findings, downgraded = downgrade_bound_names(
        evaluation.findings, file_contents or {},
    )
    if not downgraded:
        return evaluation
    for d in downgraded:
        log.info(
            "code_review_verification_downgraded",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "path": d.finding.file,
                "line": d.finding.line,
                "rule": d.finding.rule_name,
                "severity": d.finding.severity,
                "reason": "bound_name",
                "names": list(d.names),
                "arm": arm,
            },
        )
    return with_findings(evaluation, findings)


def _check_evidence(runs: Any) -> tuple[CheckEvidence, ...]:
    """Check-run dicts as verification evidence (#763). Never raises: a
    malformed row is skipped, so a weird payload only means no evidence."""
    evidence: list[CheckEvidence] = []
    for run in runs or ():
        if not isinstance(run, dict):
            continue
        try:
            evidence.append(CheckEvidence.from_check_run(run))
        except Exception as e:  # noqa: BLE001 - evidence is additive; skip the row
            log.info(
                "code_review_check_evidence_row_skipped",
                extra={"kind": type(e).__name__},
            )
    return tuple(evidence)


def _fetch_check_evidence(
    findings: tuple[Finding, ...], *, installation_id: int, owner: str,
    repo_name: str, pull_number: int, head_sha: str,
) -> tuple[CheckEvidence, ...]:
    """The head's check runs for the deep arm's coverage kill (#763).

    One listing, and only when a caller-not-updated finding could use it.
    Fails open: any error is no evidence, so every finding is kept.
    """
    if not head_sha or not any(
        f.rule_name == "caller-not-updated" for f in findings
    ):
        return ()
    try:
        runs = with_install_token_retry(
            installation_id,
            lambda token: list_check_runs_for_ref(token, owner, repo_name, head_sha),
        )
    except Exception as e:  # noqa: BLE001 - evidence is additive; keep findings
        log.info(
            "code_review_check_evidence_degraded",
            extra={
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(e).__name__,
            },
        )
        return ()
    return _check_evidence(runs)


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
                "detail": getattr(kf, "detail", ""),
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
        or evaluation.degraded_reason
        not in (None, "", "no_diff", *PUBLISHABLE_DEGRADATIONS)
        or (cancel_event is not None and cancel_event.is_set())
    )
    if unavailable:
        return None
    if review_is_staged(llm_hunks) and not pr_context.get("sensitive_review"):
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
    evidence: ElderEvidence,
    installation_id: int,
    *,
    pr_context: PrContext,
    owner: str,
    repo_name: str,
    pull_number: int,
    head_sha: str = "",
    arm: str = "deep",
) -> tuple[CodeReviewEvaluation, tuple[Finding, ...], tuple[Any, ...], int]:
    deep_eval = evaluate_diff(
        hunks, deep_llm, cross_file_contents=evidence.cross_file_contents,
    )
    _log_omit_markings(
        deep_eval, pr=f"{owner}/{repo_name}#{pull_number}", head_sha=head_sha,
    )
    # The deep, quarantined and second-lineage passes publish through the
    # same funnel as tier-1 (#1586), so every filter runs on them too.
    def _verify_deep(current: CodeReviewEvaluation) -> CodeReviewEvaluation:
        verified, killed = verify_findings(
            current.findings,
            evidence.file_contents,
            checks=_fetch_check_evidence(
                current.findings, installation_id=installation_id, owner=owner,
                repo_name=repo_name, pull_number=pull_number, head_sha=head_sha,
            ),
            head_sha=head_sha,
        )
        if killed:
            current = with_findings(current, verified)
        _record_verification_kills(
            killed,
            installation_id=installation_id,
            owner=owner,
            repo_name=repo_name,
            pull_number=pull_number,
            arm=arm,
        )
        return _apply_bound_name_downgrade(
            current, evidence.file_contents, installation_id=installation_id,
            owner=owner, repo_name=repo_name, pull_number=pull_number, arm=arm,
        )

    funnel = publish_funnel(
        deep_eval, evidence.file_contents, pr_context,
        grade=lambda current: grade_findings(current, evidence, installation_id),
        verify=_verify_deep,
        falsify=lambda findings, plain, verdicts: _apply_refute_gate(
            findings, evidence, installation_id, owner=owner,
            repo_name=repo_name, pull_number=pull_number, arm=arm,
            plain_findings=plain, plain_verdicts=verdicts,
        ),
        installation_id=installation_id,
        pr=f"{owner}/{repo_name}#{pull_number}",
    )
    return funnel.evaluation, funnel.graded, funnel.verdicts, len(funnel.suppressed)


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
) -> int | None:
    """Post the deep append's check run; its id, or None when it failed."""
    try:
        response = with_install_token_retry(
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
        return None
    check_id = (response or {}).get("id")
    return check_id if isinstance(check_id, int) else None


def _newer_elder_verdict_on_head(
    *,
    installation_id: int,
    owner: str,
    repo_name: str,
    pull_number: int,
    head_sha: str,
    tier1_check_run_id: int | None,
) -> bool:
    """True when a newer review's verdict owns this head's Elder check (#1344).

    Every Elder publish creates a new check run, and GitHub shows the newest
    one per name. The deep append lands minutes after its own Tier-1, so a
    review of the same head that started and finished in between already
    posted a newer `Grug - Elder` run; appending on top of it would replace
    that newer verdict with this superseded one. Check-run ids rise with
    creation, so a run above this run's Tier-1 id is a later one.

    Counts only a run this app created (another app or an Actions job can
    name a run `Grug - Elder`) that completed with a real verdict, success
    or failure. An in-progress marker, a neutral or skipped run, or a
    cancelled one decided nothing, so the deep append still publishes.

    Fails open: with no Tier-1 id, no app id, or a failed listing nothing
    proves a newer verdict, and the deep append publishes as before.
    """
    if not isinstance(tier1_check_run_id, int):
        return False
    pr = f"{owner}/{repo_name}#{pull_number}"
    try:
        own_app_id = str(get_app_id() or "")
        runs = with_install_token_retry(
            installation_id,
            lambda token: list_check_runs_for_ref(token, owner, repo_name, head_sha),
        )
    except Exception as e:  # noqa: BLE001 - lookup failure keeps the old publish
        log.warning(
            "elder_async_deep_supersession_check_failed",
            extra={"pr": pr, "kind": type(e).__name__},
        )
        return False
    if not own_app_id:
        return False
    newer = [
        run["id"]
        for run in runs or ()
        if isinstance(run, dict)
        and run.get("name") == _CHECK_NAME
        and str((run.get("app") or {}).get("id")) == own_app_id
        and run.get("status") == "completed"
        and run.get("conclusion") in ("success", "failure")
        and isinstance(run.get("id"), int)
        and run["id"] > tier1_check_run_id
    ]
    if not newer:
        return False
    log.info(
        "elder_async_deep_check_superseded",
        extra={
            "pr": pr,
            "tier1_check_run_id": tier1_check_run_id,
            "newer_check_run_id": max(newer),
        },
    )
    return True


def _submitted_before(review: dict[str, Any], moment: datetime) -> bool:
    """True when the review's `submitted_at` parses and precedes `moment`."""
    raw = review.get("submitted_at")
    if not isinstance(raw, str):
        return False
    try:
        submitted = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if submitted.tzinfo is None:
        return False
    return submitted < moment


def _run_clock() -> datetime:
    """When an Elder run starts: the run identity its reviews carry."""
    return datetime.now(timezone.utc)


def _from_earlier_run(
    review: dict[str, Any], run_started_at: datetime, *, unmarked_is_earlier: bool = False,
) -> bool:
    """True when the review came from an Elder run that started before this
    one. Recency is the run start in the review's marker, not the post
    time: a slow earlier run can post after a later run started.

    A review with no marker (posted before markers existed) cannot prove
    its run. `unmarked_is_earlier` says how to fail closed: True when
    withdrawing an approval (an unproven approval never outlives a block),
    otherwise its `submitted_at` decides (#1350), so an unproven block is
    kept."""
    started = review_run_started(review.get("body"))
    if started is None:
        return unmarked_is_earlier or _submitted_before(review, run_started_at)
    return started < run_started_at


def _from_later_run(review: dict[str, Any], run_started_at: datetime) -> bool:
    """True only when the review's marker proves its run started after this
    one. A review with no marker never counts as later."""
    started = review_run_started(review.get("body"))
    return started is not None and started > run_started_at


def _is_superseded(
    review: dict[str, Any], *, state: str, head_sha: str,
    run_started_at: datetime, other_commits_only: bool,
) -> bool:
    """Whether `_dismiss_superseded_reviews` withdraws `review`: Elder's own,
    in `state`, and on another commit (approvals only, #1781) or on this
    head from an earlier-started run (unless `other_commits_only`)."""
    if review.get("state") != state or not _is_elder_review(review):
        return False
    if review.get("commit_id") != head_sha:
        return state == "APPROVED"
    return not other_commits_only and _from_earlier_run(
        review, run_started_at, unmarked_is_earlier=state == "APPROVED",
    )


_DISMISS_LOGS: dict[str, tuple[str, str]] = {
    "CHANGES_REQUESTED": ("elder_stale_block_dismiss_failed", "elder_stale_blocks_dismissed"),
    "APPROVED": ("elder_stale_approval_dismiss_failed", "elder_stale_approvals_dismissed"),
}


def _is_elder_review(review: dict[str, Any]) -> bool:
    """A review the Grug app wrote as Elder: a Guard or Smasher review, a
    human's, and another bot quoting the tag all fail this."""
    return (
        is_grug((review.get("user") or {}).get("login"))
        and _REVIEW_PERSONA_TAG.format("elder") in (review.get("body") or "")
    )


def _pr_head_state(
    installation_id: int, owner: str, repo_name: str, pull_number: int,
    head_sha: str,
) -> Literal["current", "moved", "unknown"]:
    """Whether `head_sha` is still the pull request's head on GitHub (#1781).

    `moved` means a push landed after this run read the head: a newer run
    owns the verdict. `unknown` (the lookup failed) proves nothing, so a
    caller about to approve treats it as not current (fail closed)."""
    try:
        _, current, _, _ = with_install_token_retry(
            installation_id,
            lambda token: _fetch_current_review_snapshot(
                token, owner, repo_name, pull_number,
            ),
        )
    except Exception as e:  # noqa: BLE001 - no proof; the caller fails closed
        log.warning(
            "elder_head_check_failed",
            extra={"pr": f"{owner}/{repo_name}#{pull_number}", "kind": type(e).__name__},
        )
        return "unknown"
    return "current" if current == head_sha else "moved"


def _dismiss_superseded_reviews(
    installation_id: int, owner: str, repo_name: str, pull_number: int,
    head_sha: str, run_started_at: datetime, *,
    state: Literal["CHANGES_REQUESTED", "APPROVED"], clean_url: str = "",
    other_commits_only: bool = False,
) -> int:
    """Dismiss Elder's own reviews in `state` that this run supersedes.

    `CHANGES_REQUESTED`: this run approved the head, so Elder's blocks on
    that head from runs that started earlier are withdrawn. A block on any
    other commit is never dismissed (#1781): it may come from a newer push,
    and Elder's newer review already replaces an older one as its standing
    state on GitHub.

    `APPROVED` (#1758): this run cannot vouch for the head, so Elder's older
    approvals are withdrawn (fail closed): every one on another commit, plus
    one on this head from an earlier-started run unless `other_commits_only`.
    When a push has moved the head past `head_sha`, nothing is dismissed: a
    newer run owns the verdict, and an approval on the new head is not this
    run's to withdraw (#1781).

    Same-head recency is the run marker (`_from_earlier_run`), so an earlier
    run's review that landed mid-flight is still superseded, while a
    later-started run's review stays. A same-head block with no marker
    counts as earlier only when submitted before `run_started_at` (#1350); a
    same-head approval with no marker is always withdrawn (fail closed).

    A block's dismissal message links `clean_url`, the clean check run (or
    the approving review), so the PR timeline names what superseded it.

    Best-effort: a failure is logged and never changes the review outcome.
    Matches only Elder's own reviews (`_is_elder_review`). Returns how many
    were dismissed.
    """
    pr = f"{owner}/{repo_name}#{pull_number}"
    failed_log, done_log = _DISMISS_LOGS[state]
    dismissed = 0
    if state == "CHANGES_REQUESTED":
        message = (
            f"Superseded: Elder approved {head_sha[:7]} with nothing blocking"
            + (f": {clean_url}" if clean_url else ".")
        )
    else:
        message = (
            f"Withdrawn: Elder's newest review of {head_sha[:7]} could not "
            "vouch for it, so this approval no longer stands."
        )
    if state == "APPROVED" and _pr_head_state(
        installation_id, owner, repo_name, pull_number, head_sha,
    ) == "moved":
        log.info("elder_approval_withdrawal_head_moved", extra={"pr": pr})
        return 0
    try:
        reviews = with_install_token_retry(
            installation_id,
            lambda token: list_reviews(
                token, owner, repo_name, pull_number=pull_number,
            ),
        )
        for review in reviews:
            if not _is_superseded(
                review, state=state, head_sha=head_sha,
                run_started_at=run_started_at, other_commits_only=other_commits_only,
            ):
                continue
            review_id = review["id"]
            with_install_token_retry(
                installation_id,
                lambda token, rid=review_id: dismiss_review(
                    token, owner, repo_name, pull_number=pull_number,
                    review_id=rid, message=message,
                ),
            )
            dismissed += 1
    except Exception as e:  # noqa: BLE001 - best-effort; never changes the outcome
        log.warning(
            failed_log,
            extra={"pr": pr, "kind": type(e).__name__, **_http_error_detail(e)},
        )
    if dismissed:
        log.info(done_log, extra={"pr": pr, "count": dismissed})
    return dismissed


def _own_pr_event(author_login: str, event: ReviewEvent) -> ReviewEvent:
    """GitHub refuses an APPROVE or REQUEST_CHANGES review from the pull
    request's own author (422), so a PR the Grug app opened gets COMMENT."""
    return "COMMENT" if is_grug(author_login) else event


def _newer_elder_verdict_review(
    *, installation_id: int, owner: str, repo_name: str, pull_number: int,
    head_sha: str, run_started_at: datetime, on_error: bool = False,
) -> bool:
    """True when an Elder run that STARTED after this one has posted a
    verdict review on this head (#1758).

    The check-run test (`_newer_elder_verdict_on_head`) counts only success
    or failure, and an advisory-mode check always concludes neutral, so it
    cannot see a newer advisory run. Recency is the run start in the
    review's marker (`_from_later_run`), never the post time: a slow earlier
    run's verdict that lands after this run started is not newer, so it
    cannot make this run stand down.

    A failed listing returns `on_error`: the caller passes True when it is
    about to approve (an approval is never posted unproven) and False when
    about to block (as the check-run test, it fails open).
    """
    try:
        reviews = with_install_token_retry(
            installation_id,
            lambda token: list_reviews(token, owner, repo_name, pull_number=pull_number),
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.warning(
            "elder_async_deep_review_supersession_check_failed",
            extra={"pr": f"{owner}/{repo_name}#{pull_number}", "kind": type(e).__name__},
        )
        return on_error
    return any(
        isinstance(review, dict)
        and review.get("state") in ("APPROVED", "CHANGES_REQUESTED")
        and review.get("commit_id") == head_sha
        and _is_elder_review(review)
        and _from_later_run(review, run_started_at)
        for review in reviews or ()
    )


def _newer_run_started(
    *, installation_id: int, owner: str, repo_name: str, head_sha: str,
    run_started_at: datetime, own_check_ids: frozenset[int],
) -> bool:
    """True unless the check list proves no other Elder run has started on
    this head since this run did (#1758).

    The latest `Grug - Elder` check this app holds on the head is checked:
    one that is not this run's own and was started at or after
    `run_started_at` belongs to another run (a newer one, or an older one
    that is still publishing). Either way this run cannot vouch that its
    approval is the newest word. Fails closed: a failed listing, a missing
    app id or an unreadable start time all count as a newer run.
    """
    try:
        own_app_id = str(get_app_id() or "")
        runs = with_install_token_retry(
            installation_id,
            lambda token: list_check_runs_for_ref(token, owner, repo_name, head_sha),
        )
    except Exception as e:  # noqa: BLE001 - no proof means no approval
        log.warning(
            "elder_deferred_approval_run_check_failed",
            extra={"pr": f"{owner}/{repo_name}", "kind": type(e).__name__},
        )
        return True
    if not own_app_id:
        return True
    for run in runs or ():
        if (
            not isinstance(run, dict)
            or run.get("name") != _CHECK_NAME
            or str((run.get("app") or {}).get("id")) != own_app_id
            or run.get("id") in own_check_ids
        ):
            continue
        raw = run.get("started_at")
        try:
            started = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return True
        if started.tzinfo is None or started >= run_started_at:
            return True
    return False


def _post_deferred_approval(
    *, installation_id: int, owner: str, repo_name: str, pull_number: int,
    head_sha: str, run_started_at: datetime, own_check_ids: frozenset[int],
    vouched: bool,
) -> None:
    """Post the APPROVE Tier-1 deferred to the deep pass (#1758), then
    withdraw Elder's own older blocks, as a clean Tier-1 would.

    Right before posting, re-checks that the PR head is still `head_sha`
    (#1781), that no other run has started on the head
    (`_newer_run_started`), and that no later-started run has posted a
    verdict; any of them stands this approval down.

    Body-only and posted once: a failed post is logged and never queued for
    retry, so a late approval cannot land over a newer verdict. The head
    then stays unapproved (fail closed) until the next review.
    """
    pr = f"{owner}/{repo_name}#{pull_number}"
    if not vouched:
        log.info("elder_deferred_approval_not_vouched", extra={"pr": pr})
        return
    if _pr_head_state(
        installation_id, owner, repo_name, pull_number, head_sha,
    ) != "current" or _newer_run_started(
        installation_id=installation_id, owner=owner, repo_name=repo_name,
        head_sha=head_sha, run_started_at=run_started_at,
        own_check_ids=own_check_ids,
    ) or _newer_elder_verdict_review(
        installation_id=installation_id, owner=owner, repo_name=repo_name,
        pull_number=pull_number, head_sha=head_sha,
        run_started_at=run_started_at, on_error=True,
    ):
        log.info("elder_deferred_approval_stood_down", extra={"pr": pr})
        return
    review, _ = build_review_result(
        CodeReviewEvaluation(findings=(), conclusion="success"),
        head_sha=head_sha, event="APPROVE", posts_verdict=True,
        run_started_at=run_started_at,
    )
    if review is None:
        return
    try:
        response = with_install_token_retry(
            installation_id,
            lambda token: post_review(
                token, owner, repo_name, pull_number=pull_number, result=review,
            ),
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.warning(
            "elder_deferred_approval_publish_failed",
            extra={"pr": pr, "kind": type(e).__name__, **_http_error_detail(e)},
        )
        return
    log.info("elder_deferred_approval_posted", extra={"pr": pr})
    _dismiss_superseded_reviews(
        installation_id, owner, repo_name, pull_number, head_sha,
        run_started_at, state="CHANGES_REQUESTED",
        clean_url=str((response or {}).get("html_url") or ""),
    )


def _record_hunt_run(
    *,
    installation_id: int,
    owner: str,
    repo_name: str,
    pull_number: int,
    head_sha: str,
    trigger: str,
    started_at: str,
    llm: LlmReviewResponse,
    findings: tuple[Finding, ...],
    file_contents: dict[str, str],
    diff_paths: frozenset[str],
    delta: bool,
    comment_ids: hunt_link.CommentIds,
    check_run_id: int | None,
    degraded_reason: str | None,
) -> None:
    """Record this pass in the PR's canonical review session (#1207) and
    refresh the Hunt Board. Best-effort: never raises, never changes the
    review outcome. A benign degradation (`no_diff`) is a clean pass."""
    real = degraded_reason if is_real_degradation(degraded_reason) else ""
    hunt_link.record_elder_run(
        PrRef(installation_id, owner, repo_name, pull_number),
        head_sha=head_sha,
        trigger=trigger,
        started_at=started_at,
        backend=llm.backend_used.value if llm.backend_used is not None else "",
        model=llm.model_name or "",
        findings=findings,
        file_contents=file_contents,
        diff_paths=diff_paths,
        delta=delta,
        comment_ids=comment_ids,
        check_run_id=check_run_id,
        degraded_reason=real,
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
) -> hunt_link.CommentIds:
    """Fetch a review's inline comments and persist them for reaction polling.

    Shared by the synchronous publish path (#247) and the async deep path
    (#730). Best-effort: any failure is logged under `failure_log` and
    never re-raised - a capture failure must never change the review
    outcome.

    `findings` should be the `new_findings` tuple `build_review_result`
    returned alongside the `ReviewResult` this review was built from (not
    the caller's full unfiltered findings set) - see
    `_capture_comment_records` for how it matches them to comments (grug#967:
    no retry here anymore, since a fresh fetch already carries everything
    this function needs).

    Returns the inline comment id each posted finding got, keyed (file, rule,
    line), for the Hunt Board to link its records to; {} when the fetch failed.
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
        return {
            (f.file, f.rule_name, f.line): cid
            for cid, f in _pair_comments(comments, findings)
        }
    except Exception as error:  # noqa: BLE001 - capture is best-effort
        log.warning(
            failure_log,
            extra={
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(error).__name__,
            },
        )
        return {}


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
    repo_config: RepoConfig | None = None,
    file_contents: dict[str, str] | None = None,
    action: str = "review",
    linked_issues: tuple[int, ...] = (),
    diff_text: str = "",
    run_started_at: datetime | None = None,
) -> None:
    """Publish the async deep review's novel findings as a GitHub review.

    No-op if `novel_deep` is empty (deep found nothing new beyond Tier-1).
    On a successful post, also captures the review's inline-comment IDs
    (#730, via `_capture_review_comments`, best-effort) and refreshes the
    PR-timeline stack comment using `combined_eval` (Tier-1 + deep) so the
    canonical projection carries every finding, not just the deep ones.

    #738: the review body carries its publication key, and a transient
    publish failure (transport, 5xx, 429) stores the finished review for
    the poller to retry instead of dropping the model's work. A permanent
    4xx (#770) is not queued: the same payload gets the same answer.
    """
    if not novel_deep:
        return
    precedent_notes = _precedent_notes_for(
        f"{owner}/{repo_name}", deep_eval.findings,
    )
    muted = _muted_classes_for(f"{owner}/{repo_name}", repo_config)
    pr = f"{owner}/{repo_name}#{pull_number}"
    markings = project(FinishedPass(
        board_evaluation=combined_eval,
        review_evaluation=deep_eval,
        conclusion=plan_publish(
            combined_eval, mode=mode, degraded_fails_closed=True,
            outage_advisory=observe_outage(
                combined_eval, blocking=mode == "blocking", record=False,
            ).advisory,
        ).conclusion,
        event=event,
        review_phase="deep",
        head_sha=head_sha,
        base_sha=base_sha,
        pr_title=pr_title,
        living_range=living_range,
        suppressed_count=deep_suppressed_count,
        file_contents=file_contents or {},
        diff_text=diff_text,
        linked_issues=linked_issues,
        precedent_notes=precedent_notes,
        muted=muted,
        prior_keys=all_prior,
        held_back=_deep_held_keys(novel_deep, combined_eval, all_prior, repo_config),
        held_note="",
        repo_config=repo_config,
        posts_verdict=False,
        run_started_at=run_started_at,
    ))
    review_result = markings.review
    if review_result is None:
        return
    if markings.stack_body is None:
        log.warning(
            "elder_async_deep_stack_upsert_failed",
            extra={"pr": pr, "kind": markings.stack_error},
        )
    key = publication_key(
        review_result, installation_id=installation_id, owner=owner,
        repo_name=repo_name, pull_number=pull_number,
    )
    pending = PendingPublication(
        key=key,
        installation_id=installation_id,
        owner=owner,
        repo_name=repo_name,
        pull_number=pull_number,
        head_sha=head_sha,
        result=with_publish_marker(review_result, key),
        findings=markings.posted_findings,
        review_span_context=deep_llm.review_span_context,
        author_login=author_login,
        stack_body=markings.stack_body,
        stack_create_if_absent=markings.stack_create_if_absent,
    )
    review_resp: dict[str, Any] | None = None
    try:
        review_resp = with_install_token_retry(
            installation_id,
            lambda token: post_review(
                token,
                owner,
                repo_name,
                pull_number=pull_number,
                result=pending.result,
            ),
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as error:
        permanent = _is_permanent_rejection(error)
        log.warning(
            "elder_async_deep_review_publish_failed",
            extra={
                "pr": pending.pr,
                "kind": type(error).__name__,
                "permanent": permanent,
            },
        )
        if not permanent:
            _queue_deep_publication(pending)
        return

    comment_ids = _capture_deep_comments(
        pending, review_resp.get("id") if review_resp else None,
    )

    # Hunt Board (#1207): the deep pass is a second Elder run on this head.
    # It reads only what Tier-1 already fetched, so it is recorded as a
    # delta: it adds findings, it never closes one.
    _record_hunt_run(
        installation_id=installation_id, owner=owner, repo_name=repo_name,
        pull_number=pull_number, head_sha=head_sha, trigger=f"{action}:deep",
        started_at=hunt_now(), llm=deep_llm, findings=combined_eval.findings,
        file_contents=file_contents or {}, diff_paths=frozenset(),
        delta=True, comment_ids=comment_ids, check_run_id=None,
        degraded_reason=combined_eval.degraded_reason,
    )

    _upsert_deep_stack(pending)


def _queue_deep_publication(pending: PendingPublication) -> None:
    """Store a failed deep publish for the poller's retry (#738). A store
    failure is logged as an error, not raised: the deep append never raises
    to its caller, and the review is then lost exactly as before #738."""
    try:
        queue_publication(pending)
    except Exception as error:  # noqa: BLE001 - deep append never raises
        log.error(
            "elder_deep_publish_retry_queue_failed",
            extra={"pr": pending.pr, "key": pending.key, "kind": type(error).__name__},
        )


def _capture_deep_comments(
    pending: PendingPublication, review_id: Any,
) -> hunt_link.CommentIds:
    """#730: capture the deep review's inline-comment IDs so the hardest-won
    findings are visible to the reaction/reply learning loops - the same
    durable contract the synchronous path uses (#247). Best-effort: a
    capture failure must never change the deep review outcome."""
    if review_id is None:
        return {}
    return _capture_review_comments(
        installation_id=pending.installation_id,
        owner=pending.owner,
        repo_name=pending.repo_name,
        pull_number=pending.pull_number,
        review_id=review_id,
        findings=pending.findings,
        review_span_context=pending.review_span_context,
        head_sha=pending.head_sha,
        author_login=pending.author_login,
        failure_log="elder_async_deep_comment_capture_failed",
    )


def _upsert_deep_stack(pending: PendingPublication) -> None:
    """#730: refresh the PR-timeline stack comment so the deep findings
    appear in the canonical review projection alongside the Tier-1 findings.
    The body was built from combined_eval (Tier-1 + deep), so the stack
    keeps every finding, not just the novel deep ones."""
    stack_body = pending.stack_body
    if stack_body is None:
        return
    try:
        with_install_token_retry(
            pending.installation_id,
            lambda token: _upsert_review_stack_comment(
                token, pending.owner, pending.repo_name, pending.pull_number,
                stack_body, create_if_absent=pending.stack_create_if_absent,
            ),
        )
    except Exception as error:  # noqa: BLE001 - stack is cosmetic UX
        log.warning(
            "elder_async_deep_stack_upsert_failed",
            extra={"pr": pending.pr, "kind": type(error).__name__},
        )


def deliver_pending_publication(pending: PendingPublication) -> DeliveryOutcome:
    """Retry one queued deep publish (#738); the poller's `deliver` seam.

    Drops the retry when the pull request closed or its head moved: a newer
    review owns that head, and a late stack refresh would replace its
    projection with this older one. Otherwise a review already carrying the
    publication key is adopted instead of posted again (the original POST
    landed but its response was lost). Comment capture and the stack
    refresh then run as they would have after the original POST. A
    transient GitHub failure raises, so the row stays queued.
    """
    _, live_head, state, _ = with_install_token_retry(
        pending.installation_id,
        lambda token: _fetch_current_review_snapshot(
            token, pending.owner, pending.repo_name, pending.pull_number,
        ),
    )
    if state != "open" or live_head != pending.head_sha:
        log.info(
            "elder_deep_publish_retry_superseded",
            extra={"pr": pending.pr, "key": pending.key, "state": state},
        )
        return "superseded"
    reviews = with_install_token_retry(
        pending.installation_id,
        lambda token: list_reviews(
            token, pending.owner, pending.repo_name, pull_number=pending.pull_number,
        ),
    )
    existing = review_carrying_key(reviews, pending.key)
    if existing is not None:
        outcome: DeliveryOutcome = "already_posted"
        review_id = existing.get("id")
    else:
        try:
            response = with_install_token_retry(
                pending.installation_id,
                lambda token: post_review(
                    token, pending.owner, pending.repo_name,
                    pull_number=pending.pull_number, result=pending.result,
                ),
            )
        except httpx.HTTPStatusError as error:
            if not _is_permanent_rejection(error):
                raise
            log.warning(
                "elder_deep_publish_retry_rejected",
                extra={"pr": pending.pr, "key": pending.key, **_http_error_detail(error)},
            )
            return "rejected"
        outcome = "posted"
        review_id = response.get("id") if response else None
    _capture_deep_comments(pending, review_id)
    _upsert_deep_stack(pending)
    return outcome


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
    diff_text: str,
    evidence: ElderEvidence,
    pr_context: PrContext,
    voice: VoiceSelection,
    living_range: str,
    living_baseline_rejected: str,
    evaluation: CodeReviewEvaluation,
    living_delta_empty: str = "",
    prior_keys: frozenset[str],
    check_publish_failed: bool,
    cancel_event: threading.Event | None,
    author_login: str,
    repo_config: RepoConfig | None = None,
    config_note: str = "",
    tier1_check_run_id: int | None = None,
    deep_plan: DeepEscalationDecision | None = None,
    deferred_approval: bool = False,
    vouched: bool,
    run_started_at: datetime,
) -> CodeReviewEvaluation | None:
    """Run reasoner after Tier-1 publish when tiered escalation fires (#646).

    Posts additional inline comments and a second completed check-run summary
    that includes deep findings. Returns the evaluation for thread
    reconciliation, or None if a planned pass could not finish.

    `deep_plan` is the escalation Tier-1 decided before it published, so
    Tier-1 knew a deep pass would follow. On such a head the deep append
    owns the formal verdict (#1758): Tier-1 held back its APPROVE
    (`deferred_approval`), and this pass posts APPROVE only when it finishes
    clean, or REQUEST_CHANGES on a blocking finding. If it fails, degrades,
    is cancelled or is superseded, the head gets no approval (fail closed).
    `vouched` requires a complete security pass and no unreviewed paths.
    """
    decision = deep_plan
    if (
        check_publish_failed
        or (cancel_event is not None and cancel_event.is_set())
    ):
        return None
    if decision is None:
        return evaluation

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
        return None

    deep_llm = review_reasoner_diff(
        evidence,
        installation_id=installation_id,
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
            return evaluation
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
        deep_eval = evaluate_diff(
            hunks, deep_llm, cross_file_contents=evidence.cross_file_contents,
        )
        deep_graded: tuple[Finding, ...] = ()
        deep_verdicts: tuple[Any, ...] = ()
        deep_suppressed_count = 0
    else:
        deep_eval, deep_graded, deep_verdicts, deep_suppressed_count = _grade_deep_response(
            deep_llm,
            hunks,
            evidence,
            installation_id,
            pr_context=pr_context,
            owner=owner,
            repo_name=repo_name,
            pull_number=pull_number,
            head_sha=head_sha,
        )

    # Supersession after long reasoner/judge work (#646 FLINT).
    if cancel_event is not None and cancel_event.is_set():
        log.info(
            "elder_async_deep_skipped_cancelled",
            extra={"pr": f"{owner}/{repo_name}#{pull_number}"},
        )
        return None
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
        return None

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

    outage = observe_outage(combined, blocking=blocking, record=False)
    outcome = plan_publish(
        combined, mode=mode, degraded_fails_closed=True,
        outage_advisory=outage.advisory,
    )
    conclusion = outcome.conclusion
    title, summary = _summary_markdown(
        combined,
        suppressed_count=deep_suppressed_count,
        living_range=living_range,
        living_baseline_rejected=living_baseline_rejected,
        living_delta_empty=living_delta_empty,
        review_phase="deep",
        blocking=blocking and not outage.advisory,
        muted=_muted_classes_for(f"{owner}/{repo_name}", repo_config),
    )
    summary = _intent_paragraph(pr_context) + summary
    summary += config_note
    summary += _linked_issue_summary_note(
        str(pr_context.get("linked_issue_context") or ""),
        int(pr_context.get("linked_issue_live_criteria_count") or 0),
        tuple(pr_context.get("linked_issue_numbers") or ()),
    )
    title = f"{ADVISORY_TITLE if outage.advisory else title} (deep append)"
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
    pinned_issues = _pinned_issues(pr_context, owner, repo_name)
    summary = pin_review(
        summary, combined, head_sha=head_sha, linked_issues=pinned_issues,
    )
    summary = _lead_if_degraded(summary, combined)
    if outage.advisory:
        summary = advisory_summary(outage, summary)
    if conclusion == "neutral":
        # grug#1753: the deep append is an Elder check too, so a neutral
        # one names its cause the same way Tier-1 does.
        why = neutral_reason(
            combined, mode=mode, outage_advisory=outage.advisory,
        )
        summary = append_neutral_reason(summary, why)
        title = neutral_check_title(title, why)
    # #1344: a later-POSTED Elder check owns the check surface. Post order is
    # not run order, so this only decides the check and refuses approvals;
    # it never silences a block (below).
    check_superseded = _newer_elder_verdict_on_head(
        installation_id=installation_id, owner=owner, repo_name=repo_name,
        pull_number=pull_number, head_sha=head_sha,
        tier1_check_run_id=tier1_check_run_id,
    )
    verdict = verdict_event(combined, conclusion=conclusion, vouched=vouched)
    # #1758: Tier-1 deferred its APPROVE to this pass; approve only if the
    # combined review is still clean (not degraded, nothing blocking) and
    # no later check exists (fail closed).
    approves = deferred_approval and verdict == "APPROVE" and not check_superseded
    blocks = verdict == "REQUEST_CHANGES"
    if blocks or approves:
        # Recency is the run start: only a verdict from a run that STARTED
        # after this one supersedes it. An earlier run's approval that landed
        # mid-flight does not; a block dismisses it instead (below).
        newer_verdict = _newer_elder_verdict_review(
            installation_id=installation_id, owner=owner, repo_name=repo_name,
            pull_number=pull_number, head_sha=head_sha,
            run_started_at=run_started_at, on_error=approves,
        )
        blocks = blocks and not newer_verdict
        approves = approves and not newer_verdict
    # A superseded run posts its findings as a COMMENT review. The inline
    # threads stay open, so the next review carries a still-standing high
    # finding into its own verdict (carry_forward), while a superseded run
    # never re-blocks a pull request a later-started run cleared, and it
    # never approves.
    event: ReviewEvent = _own_pr_event(
        author_login, "REQUEST_CHANGES" if blocks else "COMMENT",
    )
    if event == "REQUEST_CHANGES":
        # An approval from an earlier-started run (on this head or an
        # earlier commit) no longer stands once this run found a block.
        _dismiss_superseded_reviews(
            installation_id, owner, repo_name, pull_number, head_sha,
            run_started_at, state="APPROVED",
        )
    deep_check_id: int | None = None
    if blocks or not check_superseded:
        deep_check_id = _publish_deep_check(
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
        repo_config=repo_config,
        file_contents=evidence.file_contents,
        action=action,
        linked_issues=pinned_issues,
        diff_text=diff_text,
        run_started_at=run_started_at,
    )
    if approves:
        _post_deferred_approval(
            installation_id=installation_id, owner=owner, repo_name=repo_name,
            pull_number=pull_number, head_sha=head_sha,
            run_started_at=run_started_at,
            own_check_ids=frozenset(
                i for i in (tier1_check_run_id, deep_check_id) if isinstance(i, int)
            ),
            vouched=vouched,
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
    return combined


def _hold_for_rerun(evaluation: CodeReviewEvaluation, blocking: bool) -> bool:
    """Should this degraded review leave the check pending for the scheduled
    re-run instead of publishing now?

    grug#818: a reason the queue always re-runs (`RETRIED_DEGRADATIONS`:
    every model failed, unparseable output) waits in BOTH modes. Publishing
    it first put "Grug could not review" in front of the author - a neutral
    in WARN mode, a failure in BLOCK mode - for a pass the very next delivery
    usually repaired.

    A partial review (a cohort failed or was never walked) and a
    low-confidence review publish real findings, so they wait only where
    their degraded check would fail closed (#1212, BLOCK mode), and only
    when the pass found nothing blocking. A blocking finding is a real
    verdict, which a full review would publish too, so it fails now and
    nothing is re-run."""
    reason = evaluation.degraded_reason
    if reason in RETRIED_DEGRADATIONS:
        return True
    return (
        blocking
        and reason in PUBLISHABLE_DEGRADATIONS
        and evaluation.conclusion != "failure"
    )


def _alert_if_no_review(evaluation: CodeReviewEvaluation, pr: str) -> None:
    """Page an operator before a check saying no review happened goes out.

    Only a blackout says that; a partial or low-confidence review published
    findings, and `no_diff` had nothing to review (`is_blackout`)."""
    if is_blackout(evaluation.degraded_reason):
        alert_operator_could_not_review(
            str(evaluation.degraded_reason), pr=pr, surface="dispatch",
        )


def _rerun_pending_summary(reason: str, *, blocking: bool) -> str:
    """What the pending check says about the pass that is being re-run."""
    if reason == "all_failed":
        what = f"Every model failed this pass (`{reason}`)."
    elif reason in RETRIED_DEGRADATIONS:
        what = f"This pass did not finish (`{reason}`)."
    else:
        what = (
            f"This pass was degraded (`{reason}`) and found nothing blocking, "
            "so it is not a full review yet."
        )
    merge = (
        "Merge stays blocked until a full review lands; if the re-runs also "
        "fail this check fails."
        if blocking
        else "This check is advisory, so merge is not blocked meanwhile."
    )
    return (
        f"{what} Elder re-runs the review automatically. {merge} "
        "Comment `/grug improve` to re-run sooner."
    )


def _rerun_pending_title(reason: str) -> str:
    """The pending check's title while a queued re-run retries."""
    if reason == "all_failed":
        return "Elder waiting on a model - re-run scheduled"
    if reason in RETRIED_DEGRADATIONS:
        return "Elder re-running - first pass did not finish"
    return "Elder review degraded - re-run scheduled"


def _publish_rerun_pending(
    installation_id: int, owner: str, repo_name: str, pull_number: int,
    head_sha: str, reason: str, *, blocking: bool,
) -> dict[str, str]:
    """Leave the Elder check pending while a queued re-run retries (grug#1494).

    `in_progress` is not a pass: a required check that has not concluded keeps
    the merge blocked, exactly as the check the enqueue posted. Best-effort -
    a failed post leaves that earlier pending check in place.

    The result carries `rerun_scheduled` so the durable lane raises for the
    queue's re-run even for a reason it would otherwise treat as a completed
    degraded check (partial or low-confidence review)."""
    try:
        with_install_token_retry(
            installation_id,
            lambda token: post_check_run(
                token, owner, repo_name,
                CheckRunResult(
                    name=_CHECK_NAME, head_sha=head_sha, status="in_progress",
                    conclusion=None,
                    title=_rerun_pending_title(reason),
                    summary=_rerun_pending_summary(reason, blocking=blocking),
                ),
                external_id=(
                    f"grug-cr:{owner}/{repo_name}#{pull_number}:{head_sha}"
                ),
            ),
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        log.warning(
            "code_review_rerun_pending_publish_failed",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(e).__name__,
            },
        )
    return {
        "persona": "code_reviewer", "result": "skipped", "degraded_reason": reason,
        "rerun_scheduled": "true",
    }


def _publish_degraded(
    installation_id: int, owner: str, repo_name: str, pull_number: int,
    head_sha: str, *, reason: str, summary: str | None = None,
    blocking: bool = False,
) -> dict[str, str]:
    """Post the terminal "could not review" check-run when fetch/parse fails
    (grug#818: one voice with the other no-review surfaces). Best-effort -
    a publish failure here is also swallowed since we can't do anything
    useful with it (would need its own degraded publish, etc).

    grug#1212: concludes `failure` when the repo's Elder is blocking (no
    review happened, and a neutral required check would pass), `neutral`
    in WARN mode; the summary leads with the degraded line either way."""
    pr = f"{owner}/{repo_name}#{pull_number}"
    if reason == DIFF_TOO_LARGE:
        # The author can act on this one (split the change); it is no fault
        # of Grug's, so nobody is paged and the check says what to do.
        title = "Grug could not review this pass - diff over GitHub's size limit"
    else:
        title = could_not_review_title(reason)
        alert_operator_could_not_review(reason, pr=pr, surface="fetch_or_parse")
    summary = lead_with_degraded_line(summary or (
        could_not_review_sentence(reason) + " " + gate_sentence(blocking)
    ), reason)
    conclusion = degraded_conclusion(blocking)
    publish_failed = False
    try:
        with_install_token_retry(
            installation_id,
            lambda token: post_check_run(
                token, owner, repo_name,
                CheckRunResult(
                    name=_CHECK_NAME, head_sha=head_sha, status="completed",
                    conclusion=conclusion, title=title, summary=summary,
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
