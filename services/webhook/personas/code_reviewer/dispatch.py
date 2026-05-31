# MIRRORED — sibling at services/api/personas/code_reviewer/dispatch.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
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
from personas.code_reviewer.dedup import (
    dedup_findings, prior_keys_from_comments, rule_marker,
)
from personas.code_reviewer.diff_parser import (
    DiffHunk, DiffParseError, parse_diff,
)
from personas.code_reviewer.judge import run_judge
from personas.code_reviewer.persona import (
    CodeReviewEvaluation, Finding, evaluate_diff,
)

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.code_reviewer")

_CHECK_NAME = "Grug — Code Review"
_DIFF_FETCH_TIMEOUT = 30

# Literal (not bool) so a future "degraded"/"experimental" mode can't
# silently invert `if not blocking` call sites.
ReviewMode = Literal["advisory", "blocking"]

# Closed set so a new return site can't introduce an undocumented value.
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


def _fetch_pr_review_comments(
    install_token: str, owner: str, repo: str, pull_number: int,
) -> list[dict]:
    """GET the PR's inline review comments (paginated). Used to dedup
    findings already posted on a prior review pass (#189). Returns the
    raw comment dicts (each carries `path`, `line`, `body`)."""
    out: list[dict] = []
    page = 1
    while True:
        resp = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pull_number}/comments",
            params={"per_page": 100, "page": page},
            headers={
                "Authorization": f"Bearer {install_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=_DIFF_FETCH_TIMEOUT,
        )
        resp.raise_for_status()
        body = resp.json()
        batch = body if isinstance(body, list) else []
        out.extend(batch)
        # GitHub returns a short (<per_page) final page; stop there.
        if len(batch) < 100:
            break
        page += 1
    return out


def _prior_finding_keys(
    installation_id: int, owner: str, repo_name: str, pull_number: int,
) -> frozenset[str]:
    """Fetch prior Grug review comments and build the dedup key set.
    Best-effort: a fetch failure returns an empty set (so we fall back
    to posting everything — a duplicate comment is a lesser evil than
    skipping the whole review)."""
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
        return frozenset()
    return frozenset(prior_keys_from_comments(comments))


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
    """Format one finding as an inline-comment Markdown body.

    Appends a hidden `grug-rule` marker (rendered invisibly by GitHub)
    so a later `synchronize` push can recognise this comment as a Grug
    finding for dedup (#189) — see dedup.parse_rule."""
    head = f"**{f.severity.upper()} · `{f.rule_name}`**\n\n{f.message}"
    body = f"{head}\n\n**Suggested fix:**\n{f.suggestion}" if f.suggestion else head
    return f"{body}\n\n{rule_marker(f.rule_name)}"


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


def _build_review_result(
    evaluation: CodeReviewEvaluation, *, head_sha: str, event: ReviewEvent,
    prior_keys: frozenset[str] = frozenset(),
) -> ReviewResult | None:
    """Build the ReviewResult, or None if nothing NEW to post.

    Skips entirely on degraded responses. `prior_keys` (non-empty only
    on a synchronize push) dedups findings already commented on
    unchanged lines (#189) — so a re-review doesn't flood the PR with
    duplicate inline comments. If every finding was already posted,
    returns None (nothing new). NOTE: dedup affects only the inline
    REVIEW; the check-run summary/conclusion still reflect ALL current
    findings (the bugs are still there)."""
    if evaluation.degraded_reason:
        return None
    new_findings = dedup_findings(evaluation.findings, prior_keys)
    if not new_findings:
        return None
    comments = tuple(
        InlineComment(path=f.file, line=f.line, body=_inline_comment_body(f))
        for f in new_findings
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
    action = payload.get("action", "")
    pr = payload["pull_request"]
    repo = payload["repository"]
    installation = payload["installation"]
    owner = repo["owner"]["login"]
    repo_name = repo["name"]
    pull_number = int(pr["number"])
    head_sha = pr["head"]["sha"]
    installation_id = int(installation["id"])

    # DiffParseError → advisory neutral so a fetcher bug or GitHub
    # format drift cannot 500 the webhook.
    try:
        diff_text = with_install_token_retry(
            installation_id,
            lambda token: _fetch_pr_diff(token, owner, repo_name, pull_number),
        )
        hunks = parse_diff(diff_text)
    except (httpx.HTTPStatusError, httpx.RequestError, DiffParseError) as e:
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

    # `pr_context` flows into DD LLM Obs span tags so traces are
    # filterable by repo / PR / installation in the LLM Obs UI.
    llm_response: LlmReviewResponse = review_diff(
        _to_llm_hunks(hunks),
        installation_id=installation_id,
        pr_context={
            "installation_id": installation_id,
            "repo": f"{owner}/{repo_name}",
            "pr_number": pull_number,
            "head_sha": head_sha,
        },
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

    evaluation = evaluate_diff(hunks, llm_response)

    # Both clients are independent — a 5xx on review post must not
    # skip the check-run post.
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
    if action in {"synchronize", "reopened"}:
        prior_keys = _prior_finding_keys(
            installation_id, owner, repo_name, pull_number,
        )
    review_result = _build_review_result(
        evaluation, head_sha=head_sha, event=event, prior_keys=prior_keys,
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
            "findings_count": len(evaluation.findings),
            "dropped_hallucinations": evaluation.dropped_hallucinations,
            "degraded_reason": evaluation.degraded_reason,
            "result": result,
        },
    )
    # LLM-as-a-judge (#190) runs AFTER the review + check-run are POSTed
    # to GitHub, so the developer sees the review immediately regardless
    # of the judge. NOTE the webhook HANDLER, however, still blocks on
    # the judge's LLM round-trip before returning to GitHub — this is a
    # deliberate tradeoff for v1: true async (Lambda self-invoke / SQS)
    # is deferred to #245's scheduled-poller infra. The judge inherits
    # the 30s `_TIMEOUT_SECONDS` and the `_JUDGE_MAX_FINDINGS` cost guard
    # bounds the worst case; revisit if webhook-delivery timeouts appear
    # in DD. `run_judge` is fully self-guarding (never raises), but wrap
    # it anyway — the judge is pure observability and must never affect
    # the dispatch result the developer already has.
    try:
        run_judge(
            evaluation, hunks, installation_id=installation_id,
            review_span_context=llm_response.review_span_context,
            pr_context={
                "installation_id": installation_id,
                "repo": f"{owner}/{repo_name}",
                "pr_number": pull_number,
                "head_sha": head_sha,
            },
        )
    except Exception as e:  # noqa: BLE001 — defense-in-depth over run_judge's own guard
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
    return {
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
    }
