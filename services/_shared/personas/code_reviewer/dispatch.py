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
    review_diff,
    review_is_staged,
    review_reasoner_diff,
)
from voice_pack import VoiceSelection, entitled_voice
from review_types import EFFORTS
from personas.code_reviewer.dedup import (
    dedup_findings, finding_key, parse_rule, prior_keys_from_comments,
    rule_marker,
)
from personas.code_reviewer.diff_parser import (
    DiffHunk, DiffParseError, parse_diff, split_reviewable_hunks,
)
from personas.code_reviewer.precedent import (
    class_precision, match_precedent, render_precedent_note,
)
from personas.code_reviewer.claim_check import (
    filter_novel_claim_findings,
    scan_claim_checks,
)
from personas.code_reviewer.complexity import scan_complexity
from personas.code_reviewer.cross_file import (
    extract_symbols, fetch_cross_file_context,
)
from personas.code_reviewer.omen import build_runtime_context
from personas.code_reviewer.judge import (
    eval_tags, grade_findings, partition_findings, partition_refuted,
    refute_findings, submit_evals,
)
from personas.code_reviewer.persona import (
    CodeReviewEvaluation, Finding, evaluate_diff, with_extra_findings, with_findings,
)
from personas.code_reviewer.snapshot import review_snapshot_id_from_pr
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
PersonaResultStr = Literal[
    "pass", "fail", "skipped", "publish_failed", "unhandled_error",
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
        review_snapshot_id_from_pr(body),
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


def _review_transparency(
    evaluation: CodeReviewEvaluation,
    suppressed_count: int,
    excluded_paths: tuple[str, ...],
) -> str:
    lines = (
        f"\n\nGrug held back {suppressed_count} weak finding(s) his judge doubted."
        if suppressed_count
        else ""
    )
    coverage = evaluation.coverage
    if coverage is not None:
        failed = (
            f"; failed: {', '.join(str(index) for index in coverage.failed_cohorts)}"
            if coverage.failed_cohorts
            else ""
        )
        lines += (
            f"\n\nCoverage: {coverage.completed_cohorts}/{coverage.total_cohorts} "
            f"cohorts completed{failed}."
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
    if excluded_paths:
        shown = ", ".join(
            f"`{path.replace(chr(96), '')}`" for path in excluded_paths[:10]
        )
        more = (
            f" (+{len(excluded_paths) - 10} more)" if len(excluded_paths) > 10 else ""
        )
        lines += (
            f"\n\nGrug not read {len(excluded_paths)} data/generated "
            f"file(s) - no meat for review there: {shown}{more}."
        )
    return lines


def _clean_review_scope(living_range: str, excluded_paths: tuple[str, ...]) -> str:
    if living_range:
        return (
            "Elder walked the delta diff (full file + cross-file + Omen "
            "runtime signal when mapped). No markings survived the judge. "
            "Code walk steady."
        )
    if excluded_paths:
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
    held = _review_transparency(evaluation, suppressed_count, excluded_paths)
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

    if evaluation.degraded_reason == "partial_review":
        title = "WARN Elder review coverage partial"
        return hunt_title(title), (
            "Grug reviewed part of the diff, but one or more bounded cohorts "
            "did not return usable output. Validated markings from completed "
            "cohorts are still published below; this check stays advisory."
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
        scope = _clean_review_scope(living_range, excluded_paths)
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


# Derived from the shared vocabulary so a new effort level can never
# silently drop its chip (the Severity-partition-assert drift class).
_EFFORT_LABELS = {e: e.replace("-", " ") for e in EFFORTS}

# Closed caveman-inspired chrome chips (CodeRabbit-density scanability).
# Identifiers and check *names* stay plain ASCII; Markings surface uses these.
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
_STACK_MARKER = "<!-- grug-elder-stack -->"
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


def _review_stack_body(
    evaluation: CodeReviewEvaluation,
    *,
    conclusion: CheckConclusion,
    living_range: str = "",
    suppressed_count: int = 0,
    review_phase: Literal["tier1", "deep", "dual"] = "dual",
) -> str:
    """PR-timeline review stack comment (Markings v2 / CodeRabbit-style shell).

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
    phase_label = {
        "tier1": "Tier-1 coder arm (deep may append later)",
        "deep": "Deep reasoner append",
        "dual": "Dual-arm Cave review",
    }.get(review_phase, review_phase)
    hunt = f"\n- Living Hunt range: `{living_range}`" if living_range else ""
    held = (
        f"\n- Judge held back {suppressed_count} weak finding(s)"
        if suppressed_count
        else ""
    )
    if evaluation.degraded_reason:
        status_line = f"Degraded (`{evaluation.degraded_reason}`) — advisory only"
    elif n == 0:
        status_line = "Clear — no markings published"
    else:
        status_line = f"**{n} actionable marking(s)** ({sev_bits})"

    rows = [
        "| Severity | Category | File | Line | Rule |",
        "|---|---|---|---|---|",
    ]
    for f in findings[:25]:
        rows.append(
            f"| {f.severity} | {_md_code_span(_category_for_rule(f.rule_name))} | "
            f"`{_md_code_span(f.file)}` | {f.line} | "
            f"`{_md_code_span(f.rule_name)}` |"
        )
    if n > 25:
        rows.append(f"| … | +{n - 25} more | | | |")
    table = "\n".join(rows) if findings else "_No inline markings this pass._"

    parts = [
        _STACK_MARKER,
        "",
        f'<img src="{_PERSONA_PORTRAIT}" width="46" align="left" alt="Grug Elder" />',
        "",
        f"**Grug Elder** review stack · check conclusion `{conclusion}`",
        "",
        "### Review stack",
        f"- Phase: {phase_label}",
        f"- Status: {status_line}{held}{hunt}",
        f"- Check-run: `{_CHECK_NAME}`",
        "",
        "### Markings",
        "",
        table,
    ]
    # Only when there is something to fix — empty "Address each finding"
    # shells are noise (and look broken).
    agent = _consolidated_agent_prompt(evaluation)
    if agent:
        parts.extend([
            "",
            "### Prompt for AI agents",
            "",
            agent,
            "",
            "---",
            "",
            "Inline comments carry Fix + agent prompt on each marking. "
            "Autofix push is not enabled — apply suggestions or hand the agent "
            "prompt to your coding agent.",
            "",
        ])
    else:
        # Degraded (all_failed / parse_failed / ...) with empty findings is
        # not a clean review - say so instead of "nothing to remediate".
        if evaluation.degraded_reason:
            status = (
                "No agent prompt — review degraded; no usable findings were produced."
            )
        else:
            status = "No agent prompt — nothing to remediate."
        parts.extend(["", "---", "", status, ""])
    return "\n".join(parts)


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
            if _STACK_MARKER in (c.get("body") or ""):
                return int(c["id"])
        if len(batch) < 100:
            return None
        page += 1
    return None


def _upsert_review_stack_comment(
    token: str, owner: str, repo: str, pr_number: int, body: str,
) -> None:
    """PATCH existing stack comment or POST a new one (Teller discipline).

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

    existing = _find_stack_comment_id(token, owner, repo, pr_number)
    if existing is not None:
        _patch(existing)
        return
    # Second look immediately before create (shrink race window).
    existing = _find_stack_comment_id(token, owner, repo, pr_number)
    if existing is not None:
        _patch(existing)
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
    """
    if check_publish_failed or review_publish_failed:
        return "publish_failed"
    if evaluation.degraded_reason:
        return "skipped"
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
) -> ReviewResult | None:
    """Build the ReviewResult, or None if nothing NEW to post.

    Skips entirely on fully degraded responses. A partial staged review still
    publishes its validated findings, but stays advisory. `prior_keys`
    (non-empty only on a synchronize/reopened push) dedups findings already commented
    on unchanged lines (#189) — so a re-review doesn't flood the PR with
    duplicate inline comments. If every finding was already posted,
    returns None (nothing new). NOTE: dedup affects only the inline
    REVIEW; the check-run summary/conclusion still reflect ALL current
    findings (the bugs are still there)."""
    if evaluation.degraded_reason not in (None, "", "partial_review"):
        return None
    new_findings = dedup_findings(evaluation.findings, prior_keys)
    if not new_findings:
        return None
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
    )


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
    produced it by (file, line, RULE) so the stored `finding_tags` are the
    SAME `eval_tags` the judge used — the poller's `human_verdict` then
    shares finding identity with the judge's `is_real_bug`.

    Keying includes the rule (not just file+line): two distinct rules can
    post two comments on the SAME line (dedup keys on rule for exactly this
    reason), so a (file, line) map would collapse them and mis-tag one. The
    rule is recovered from each comment body's hidden `<!-- grug-rule:NAME -->`
    marker (the same marker dedup parses), so a comment with no marker (not
    ours) or no matching finding is skipped. Best-effort per comment: a
    malformed dict or a single DDB blip is skipped, never raised. Returns
    count persisted.
    """
    by_key: dict[str, Finding] = {
        finding_key(f.file, f.line, f.rule_name): f for f in findings
    }
    persisted = 0
    for c in comments:
        cid, path, line = c.get("id"), c.get("path"), c.get("line")
        if cid is None or path is None or line is None:
            continue
        rule = parse_rule(c.get("body", ""))
        if rule is None:
            continue
        try:
            finding = by_key.get(finding_key(path, int(line), rule))
        except (TypeError, ValueError):
            continue
        if finding is None:
            continue
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
            fallback_span_context = traced_origins[0]["review_span_context"]
        elif finding.origins:
            # Provenance exists but trace export failed. Unknown is more honest
            # than attributing the reaction to another backend's span.
            fallback_span_context = None
        else:
            fallback_span_context = review_span_context
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
    snapshot_id = review_snapshot_id_from_pr(pr)
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
    except (httpx.HTTPStatusError, httpx.RequestError, DiffParseError) as e:
        log.warning(
            "code_review_fetch_or_parse_failed",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(e).__name__,
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
        degraded = _publish_degraded(
            installation_id, owner, repo_name, pull_number, head_sha,
            reason="fetch_or_parse_failed",
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
            summary="Grug could not look — diff fetch/parse failed",
            findings_count=0,
            blocking=blocking,
            degraded_reason="fetch_or_parse_failed",
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
        complexity_findings = scan_complexity(hunks, file_contents)
    except Exception as e:  # noqa: BLE001 - enrichment must never abort a review
        log.info(
            "code_review_complexity_scan_failed",
            extra={"pr": f"{owner}/{repo_name}#{pull_number}", "kind": type(e).__name__},
        )
        complexity_findings = ()
    if complexity_findings:
        evaluation = with_extra_findings(evaluation, complexity_findings)
        log.info(
            "code_review_complexity_findings",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "count": len(complexity_findings),
            },
        )

    # Deterministic docs/code claim check: catch comment/env prose that
    # asserts the wrong settle cap or deep-diff bound (the Qodo/CR class
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
    review_result = _build_review_result(
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
            log.error(
                "code_review_review_publish_failed",
                extra={
                    "installation_id": installation_id,
                    "pr": f"{owner}/{repo_name}#{pull_number}",
                    "kind": type(e).__name__,
                },
            )
            review_publish_failed = True

    # Capture inline-comment IDs for later reaction polling (#247). BEST-
    # EFFORT, post-publish, own try/except — a capture failure must never
    # change the review outcome (`result` is computed below, unaffected).
    # Persist posted findings even if trace export failed: a later trusted
    # maintainer reaction can still teach the repository ledger without DD span
    # attribution.
    if review_resp is not None and not review_publish_failed:
        review_id = review_resp.get("id")
        if review_id is not None:
            try:
                comments = with_install_token_retry(
                    installation_id,
                    lambda token: get_review_comments(
                        token, owner, repo_name,
                        pull_number=pull_number, review_id=int(review_id),
                    ),
                )
                persisted = _capture_comment_records(
                    comments, evaluation.findings,
                    install_id=installation_id,
                    repo=f"{owner}/{repo_name}",
                    pr_number=pull_number,
                    review_span_context=llm_response.review_span_context,
                    head_sha=head_sha,
                    author_login=author_login,
                )
                # Observability: a 0-of-N capture (e.g. a comment↔finding
                # shape regression) silently empties the poller's batch with
                # no other signal — alarm on it; otherwise record the count.
                if comments and persisted == 0:
                    log.warning(
                        "code_review_comment_capture_zero",
                        extra={
                            "installation_id": installation_id,
                            "pr": f"{owner}/{repo_name}#{pull_number}",
                            "fetched": len(comments),
                        },
                    )
                else:
                    log.info(
                        "code_review_comments_captured",
                        extra={
                            "installation_id": installation_id,
                            "pr": f"{owner}/{repo_name}#{pull_number}",
                            "fetched": len(comments),
                            "persisted": persisted,
                        },
                    )
            except Exception as e:  # noqa: BLE001 — capture is best-effort; a
                # GH 5xx (get_review_comments) OR a DDB error (put_comment_record)
                # must never 500 dispatch. Broad like run_judge's guard below.
                log.error(
                    "code_review_comment_capture_failed",
                    extra={
                        "installation_id": installation_id,
                        "pr": f"{owner}/{repo_name}#{pull_number}",
                        "kind": type(e).__name__,
                    },
                    exc_info=True,
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
            )
            with_install_token_retry(
                installation_id,
                lambda token: _upsert_review_stack_comment(
                    token, owner, repo_name, pull_number, stack_body,
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
) -> None:
    if not novel_deep:
        return
    review_result = _build_review_result(
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
        try:
            comments = with_install_token_retry(
                installation_id,
                lambda token: get_review_comments(
                    token, owner, repo_name,
                    pull_number=pull_number, review_id=int(review_id),
                ),
            )
            _capture_comment_records(
                comments, deep_eval.findings,
                install_id=installation_id,
                repo=f"{owner}/{repo_name}",
                pr_number=pull_number,
                review_span_context=deep_llm.review_span_context,
                head_sha=head_sha,
                author_login=author_login,
            )
        except Exception as error:  # noqa: BLE001 - capture is best-effort
            log.warning(
                "elder_async_deep_comment_capture_failed",
                extra={
                    "pr": f"{owner}/{repo_name}#{pull_number}",
                    "kind": type(error).__name__,
                },
            )

    # #730: refresh the PR-timeline stack comment so the deep findings appear
    # in the canonical review projection alongside the Tier-1 findings.
    try:
        conclusion, _ = _publish_shape(deep_eval, mode=mode)
        stack_body = _review_stack_body(
            deep_eval,
            conclusion=conclusion,
            living_range=living_range,
            suppressed_count=deep_suppressed_count,
            review_phase="deep",
        )
        with_install_token_retry(
            installation_id,
            lambda token: _upsert_review_stack_comment(
                token, owner, repo_name, pull_number, stack_body,
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
    if deep_llm.kind != "reviewed":
        log.info(
            "elder_async_deep_arm_empty",
            extra={
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": deep_llm.kind,
                "error": deep_llm.error,
            },
        )
        return

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

    # Supersession after long reasoner/judge work (#646 CodeRabbit).
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

    conclusion, event = _publish_shape(combined, mode=mode)
    title, summary = _summary_markdown(
        combined,
        suppressed_count=deep_suppressed_count,
        living_range=living_range,
        review_phase="deep",
    )
    title = f"{title} (deep append)"
    summary = (
        f"_Deep reasoner arm appended after Tier-1 completed "
        f"({', '.join(decision.reasons)})._\n\n{summary}"
    )
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
        },
    )


def _publish_degraded(
    installation_id: int, owner: str, repo_name: str, pull_number: int,
    head_sha: str, *, reason: str,
) -> dict[str, str]:
    """Post the "skipped" check-run when fetch/parse fails. Best-effort —
    a publish failure here is also swallowed since we can't do anything
    useful with it (would need its own degraded publish, etc)."""
    title = f"WARN Elder review skipped ({reason})"
    summary = (
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
