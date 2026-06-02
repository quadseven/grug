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
    InlineComment, ReviewEvent, ReviewResult, get_review_comments, post_review,
)
from llm_client import Hunk as LlmHunk, LlmReviewResponse, review_diff
from personas.code_reviewer.dedup import (
    dedup_findings, finding_key, parse_rule, prior_keys_from_comments,
    rule_marker,
)
from personas.code_reviewer.diff_parser import (
    DiffHunk, DiffParseError, parse_diff,
)
from personas.code_reviewer.judge import eval_tags, run_judge
from personas.code_reviewer.persona import (
    CodeReviewEvaluation, Finding, evaluate_diff,
)
from adapters.install_store import put_comment_record  # type: ignore

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.code_reviewer")

# Grug's face on the review comment. This dispatch IS the Elder (code-reviewer)
# persona, so it leads with the Elder portrait — hosted at grug.lol/assets and
# rendered via an <img> (GitHub markdown allows width/align) so it's a little
# face, not a giant banner.
_PERSONA = "Elder"
_PERSONA_PORTRAIT = "https://grug.lol/assets/grug_elder.png"

_CHECK_NAME = "Grug — Code Review"
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
    for page in range(1, _MAX_COMMENT_PAGES + 1):
        resp = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pull_number}/comments",
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


def _summary_markdown(evaluation: CodeReviewEvaluation) -> tuple[str, str]:
    """Render a (title, summary) pair for the check-run output.

    Title is a one-liner status; summary is a Markdown table of findings
    by severity. Operators read this when triaging in GH's Checks tab.
    """
    if evaluation.degraded_reason:
        title = f"⚠️ Grug eyes clouded ({evaluation.degraded_reason})"
        return title, (
            "Grug Elder could not see the diff this pass. The mist: "
            f"`{evaluation.degraded_reason}`. Grug stay his club — this "
            "only counsel, merge not blocked."
        )
    if not evaluation.findings:
        title = "✅ Grug find nothing — code good"
        return title, (
            "Grug Elder look long upon the diff and find nothing to fear. "
            "Code walk steady. Grug nod."
        )

    severity_icon = {
        "critical": "🛑", "high": "❌", "medium": "⚠️", "low": "ℹ️",
    }
    blocking = sum(
        1 for f in evaluation.findings if f.severity in ("high", "critical")
    )
    title = (
        f"❌ Grug see trouble — {blocking} blocking · "
        f"{len(evaluation.findings)} finding(s) in all"
    )
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
    on a synchronize/reopened push) dedups findings already commented
    on unchanged lines (#189) — so a re-review doesn't flood the PR with
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
    review_span_context: dict,
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
        try:
            put_comment_record(
                install_id=install_id,
                comment_id=int(cid),
                repo=repo,
                pr_number=pr_number,
                review_span_context=review_span_context,
                finding_tags=eval_tags(finding),
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
    dedup_degraded = False
    if action in {"synchronize", "reopened"}:
        prior_keys, dedup_degraded = _prior_finding_keys(
            installation_id, owner, repo_name, pull_number,
        )
    review_result = _build_review_result(
        evaluation, head_sha=head_sha, event=event, prior_keys=prior_keys,
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
    # Gated on: the review actually posted, AND a review span exists to
    # attach future `human_verdict` annotations to (else the poller would
    # skip the record anyway, so persisting it just wastes the poll batch).
    if (
        review_resp is not None
        and not review_publish_failed
        and llm_response.review_span_context is not None
    ):
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
            # True when the prior-comments fetch failed on a re-review:
            # dedup fell back to post-everything, so duplicate comments
            # this cycle are a fetch artifact, not new findings.
            "dedup_degraded": dedup_degraded,
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
