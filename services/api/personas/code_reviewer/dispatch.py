# MIRRORED — sibling at services/webhook/personas/code_reviewer/dispatch.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
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
from typing import Any, Literal

import httpx

from github_app_auth import with_install_token_retry
from github_checks_client import CheckConclusion, CheckRunResult, post_check_run
from github_reviews_client import (
    InlineComment, ReviewEvent, ReviewResult, post_review,
)
from llm_client import Hunk as LlmHunk, LlmReviewResponse, review_diff
from personas.code_reviewer.diff_parser import (
    DiffHunk, DiffParseError, parse_diff,
)
from personas.code_reviewer.persona import (
    CodeReviewEvaluation, Finding, evaluate_diff,
)

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.code_reviewer")

_CHECK_NAME = "Grug — Code Review"
_DIFF_FETCH_TIMEOUT = 30

# Advisory vs blocking mode threaded through publish-shape decisions.
# Promoted from a bare `bool` so future "degraded" or "experimental"
# modes can be added without an inversion bug at every call site (e.g.
# `if not blocking` flipping wrong when a third mode appears).
ReviewMode = Literal["advisory", "blocking"]

# Per-persona result string. Promoted to Literal so a new return site
# can't silently introduce an undocumented value (e.g. dispatcher.py's
# `unhandled_error` was previously documented only by source-grep).
PersonaResultStr = Literal[
    "pass", "fail", "skipped", "publish_failed", "unhandled_error",
]


def _fetch_pr_diff(
    install_token: str, owner: str, repo: str, pull_number: int,
) -> str:
    """GET the PR unified diff. `Accept: application/vnd.github.diff`
    returns the raw diff body rather than the JSON metadata."""
    resp = httpx.get(
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pull_number}",
        headers={
            "Authorization": f"Bearer {install_token}",
            "Accept": "application/vnd.github.diff",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=_DIFF_FETCH_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.text


def _to_llm_hunks(hunks: tuple[DiffHunk, ...]) -> list[LlmHunk]:
    """DiffHunks (parser shape) → llm_client.Hunk (review-input shape).
    The LLM only needs the per-file body; line-number bookkeeping is
    already in `new_lines` for the post-hoc hallucination filter."""
    return [LlmHunk(path=h.file_path, body=h.body) for h in hunks]


def _summary_markdown(evaluation: CodeReviewEvaluation) -> tuple[str, str]:
    """Render a (title, summary) pair for the check-run output.

    Title is a one-liner status; summary is a Markdown table of findings
    by severity. Operators read this when triaging in GH's Checks tab.
    """
    if evaluation.degraded_reason:
        title = f"⚠️ Code review skipped ({evaluation.degraded_reason})"
        return title, (
            "Elder code-reviewer could not run this pass. Reason: "
            f"`{evaluation.degraded_reason}`. Advisory neutral — PR "
            "merge is not blocked."
        )
    if not evaluation.findings:
        title = "✅ Code review pass — no findings"
        return title, "Elder reviewed the diff and found nothing actionable."

    severity_icon = {
        "critical": "🛑", "high": "❌", "medium": "⚠️", "low": "ℹ️",
    }
    blocking = sum(
        1 for f in evaluation.findings if f.severity in ("high", "critical")
    )
    title = f"❌ {blocking} blocking · {len(evaluation.findings)} total findings"
    rows = ["| Severity | File | Line | Rule | Message |", "|---|---|---|---|---|"]
    for f in evaluation.findings:
        icon = severity_icon.get(f.severity, "•")
        rows.append(
            f"| {icon} {f.severity} | `{f.file}` | {f.line} | "
            f"`{f.rule_name}` | {f.message} |"
        )
    return title, "\n".join(rows)


def _inline_comment_body(f: Finding) -> str:
    """Format one finding as an inline-comment Markdown body."""
    head = f"**{f.severity.upper()} · `{f.rule_name}`**\n\n{f.message}"
    if f.suggestion:
        return f"{head}\n\n**Suggested fix:**\n{f.suggestion}"
    return head


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
    # Degraded LLM responses always degrade to advisory regardless of
    # mode — Elder cannot block a PR on infrastructure flakiness.
    if mode == "advisory" or evaluation.degraded_reason:
        return "neutral", "COMMENT"
    if evaluation.conclusion == "failure":
        return "failure", "REQUEST_CHANGES"
    return evaluation.conclusion, "COMMENT"


def _build_review_result(
    evaluation: CodeReviewEvaluation, *, head_sha: str, event: ReviewEvent,
) -> ReviewResult | None:
    """Build the ReviewResult, or None if nothing to post.

    Skips entirely on empty findings OR degraded responses (no point
    posting an empty review)."""
    if evaluation.degraded_reason or not evaluation.findings:
        return None
    comments = tuple(
        InlineComment(path=f.file, line=f.line, body=_inline_comment_body(f))
        for f in evaluation.findings
    )
    return ReviewResult(
        commit_id=head_sha,
        event=event,
        body=f"Grug code review · {len(comments)} finding(s)",
        comments=comments,
    )


def dispatch_code_review(
    payload: dict[str, Any], *, blocking: bool,
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
    """
    mode: ReviewMode = "blocking" if blocking else "advisory"
    pr = payload["pull_request"]
    repo = payload["repository"]
    installation = payload["installation"]
    owner = repo["owner"]["login"]
    repo_name = repo["name"]
    pull_number = int(pr["number"])
    head_sha = pr["head"]["sha"]
    installation_id = int(installation["id"])

    # 1+2. Fetch + parse. DiffParseError → advisory neutral.
    try:
        diff_text = with_install_token_retry(
            installation_id,
            lambda token: _fetch_pr_diff(token, owner, repo_name, pull_number),
        )
        hunks = parse_diff(diff_text)
    except (httpx.HTTPError, DiffParseError) as e:
        log.warning(
            "code_review_fetch_or_parse_failed",
            extra={
                "installation_id": installation_id,
                "pr": f"{owner}/{repo_name}#{pull_number}",
                "kind": type(e).__name__,
            },
        )
        return _publish_degraded(
            installation_id, owner, repo_name, pull_number, head_sha,
            reason="fetch_or_parse_failed",
        )

    # 3. LLM. review_diff already swallows backend failures into
    # discriminated `LlmReviewResponse.kind` values; no extra try
    # needed.
    llm_response: LlmReviewResponse = review_diff(
        _to_llm_hunks(hunks), installation_id=installation_id,
    )
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

    # 4. Pure evaluate.
    evaluation = evaluate_diff(hunks, llm_response)

    # 5+6. Publish. Both clients are independent — a 5xx on review
    # post must not skip the check-run post.
    conclusion, event = _publish_shape(evaluation, mode=mode)
    title, summary = _summary_markdown(evaluation)
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
    except (httpx.HTTPError,) as e:
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

    review_result = _build_review_result(
        evaluation, head_sha=head_sha, event=event,
    )
    if review_result is not None:
        try:
            with_install_token_retry(
                installation_id,
                lambda token: post_review(
                    token, owner, repo_name,
                    pull_number=pull_number, result=review_result,
                ),
            )
        except (httpx.HTTPError,) as e:
            log.error(
                "code_review_review_publish_failed",
                extra={
                    "installation_id": installation_id,
                    "pr": f"{owner}/{repo_name}#{pull_number}",
                    "kind": type(e).__name__,
                },
            )

    if check_publish_failed:
        # Check-run is the load-bearing GH surface (the one that flips
        # mergeability under blocking mode). If THAT failed, the
        # operator needs to see "publish_failed" not "pass" regardless
        # of the underlying evaluation.
        result: PersonaResultStr = "publish_failed"
    elif evaluation.degraded_reason:
        result = "skipped"
    else:
        result = "pass" if evaluation.passed else "fail"
    return {
        "status": "dispatched",
        "persona": "code_reviewer",
        "result": result,
    }


def _publish_degraded(
    installation_id: int, owner: str, repo_name: str, pull_number: int,
    head_sha: str, *, reason: str,
) -> dict[str, str]:
    """Post the "skipped" check-run when fetch/parse fails. Best-effort —
    a publish failure here is also swallowed since we can't do anything
    useful with it (would need its own degraded publish, etc)."""
    title = f"⚠️ Code review skipped ({reason})"
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
    except (httpx.HTTPError,) as e:
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
        "status": "dispatched", "persona": "code_reviewer",
        "result": "publish_failed" if publish_failed else "skipped",
    }
