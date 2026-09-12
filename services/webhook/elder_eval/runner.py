"""Live replay runner for the Elder eval (#361 slice 2, #537).

Fetches each corpus case's REAL PR diff from GitHub and drives it through
Elder's actual review path - `_build_messages` (Elder's exact prompt,
production variant) and `_parse_response` (Elder's exact parser) - via the
SAST benchmark's backend transport, so the catch/noise numbers are Elder's
real behavior, never a reimplementation's.

This module makes network calls at runtime - `fetch_pr_diff` and live
`run_case` never run in the per-PR CI suite. Its pure pieces
(`classes_for_findings`, `diff_to_hunks`, `run_eval`/`run_production_eval`/
`diff_for_case` with injected fetch + anchor-resolution callables) ARE
unit-tested there. Live runs happen only from the on-demand
`benchmark.elder-eval.yml` job or a manual `python -m elder_eval`.

`diff_for_case` (#545) resolves each case's pre-fix snapshot when the
corpus row(s) carry a usable anchor (`EvalCase.anchored`) - see
`corpus.py` and `resolve_anchor_sha`/`fetch_anchored_diff` below. It is
the follow-up to the KNOWN METHODOLOGY BIAS in specs/DESIGN.md: without
it, every case replays the PR's FINAL merged diff, so a `fixed` ledger
row whose bug was fixed inside that same PR is graded against code where
the bug no longer exists.

`run_case` (#859 follow-up) STAGES an oversized diff into multiple bounded
cohort calls via the real `plan_review` packer, the same one dispatch.py
and `review_diff` pack against - instead of the pre-#859 behavior of
sending the whole diff as one monolithic backend call. A small diff still
gets exactly one call (`plan_review` returns a single cohort and this is
byte-identical to the old path), so only genuinely oversized cases change
methodology. Deliberately UNBOUNDED cohort count (`max_cohorts=None`,
unlike live production's webhook-latency-bound cap): a batch baseline
needs COMPLETENESS, not the per-request latency guarantee that cap
exists for, so nothing here reports "too big to review in full". A hunk
too large for even one cohort on its own still cannot be reviewed (the
planner refuses to truncate it - see `split_oversized_hunks`'s
docstring), and a staged case is scored ALL-OR-NOTHING: any cohort
failure errors the WHOLE case, mirroring `run_production_case`'s refusal
to score incomplete `review_diff` coverage - a partial replay's misses
would be amputation, not Elder. `CaseReplay.staged` rides into the report
and baseline (`staged_cases`) so a reader can see exactly which cases
were reviewed via multiple calls instead of one - the comparability
caveat non-negotiable in #859: those cases' numbers describe a different
methodology than an unstaged replay, not a like-for-like rerun.
"""

from __future__ import annotations

import logging
from typing import Callable, Iterable, Sequence

import httpx

from code_review_prompt import RULES

# Elder's exact prompt + parser - deliberate private imports, same
# rationale + caveat as the SAST runner: if their signatures change, this
# runner must follow (measuring the shipped prompt is the whole point).
#
# `_review_cohort_paths` is the same kind of deliberate private import: it
# is the live GRUG_REVIEW_COHORT_FILES-tunable cap the real cohort planner
# packs against (see `review_cohort_char_budget`'s own docstring on why
# the public/private split exists - "so the two layers cannot drift apart
# on a config change"). Bench mode's staging (below) reuses it for the
# same reason: measuring Elder's real behavior means packing against the
# same numbers Elder's planner packs against, not a second guess at them.
from llm_client import (
    Finding,
    FindingJudgement,
    Hunk,
    LlmReviewResponse,
    _build_messages,
    _parse_response,
    _review_cohort_paths,
    review_cohort_char_budget,
    review_diff,
)
from personas.code_reviewer.diff_parser import parse_diff
from personas.code_reviewer.judge import (
    grade_findings,
    partition_findings,
    partition_refuted,
    refute_findings,
)
from personas.code_reviewer.persona import (
    Finding as PublishedFinding,
    evaluate_diff,
)
from personas.code_reviewer.verify import verify_findings
from review_pipeline import ReviewPlan, plan_review, render_review_map
from sast_benchmark.backends import BenchBackend
from sast_benchmark.runner import _post

from .corpus import EvalCase, normalize_class
from .scoring import CaseReplay

log = logging.getLogger("grug.elder_eval")

# Measure the SHIPPED prompt (see sast_benchmark.runner._BENCH_PROMPT_VARIANT).
_PROMPT_VARIANT = "v2"
_GITHUB_API = "https://api.github.com"
_DIFF_TIMEOUT_SECONDS = 30.0
# Bound the replay context like the live review path bounds its own input.
_MAX_DIFF_BYTES = 200_000

# Finding.rule -> normalized bug class, from Elder's own RULES table.
_RULE_TO_CLASS: dict[str, str] = {
    r.name: normalize_class(r.bug_class) for r in RULES
}
# A duplicate rule name would silently keep the last entry - fail at import.
if len(_RULE_TO_CLASS) != len(RULES):
    raise ValueError("duplicate ReviewRule.name in RULES")


def classes_for_findings(
    findings: Iterable[Finding | PublishedFinding],
) -> dict[str, int]:
    """ELDER-normalized class -> finding count. A rule outside the RULES
    table (the model improvised a name) falls back to its own normalized
    name - it can never match an expected cell, so it only widens the
    noise denominator honestly."""
    out: dict[str, int] = {}
    for f in findings:
        rule = f.rule if isinstance(f, Finding) else f.rule_name
        cls = _RULE_TO_CLASS.get(rule, normalize_class(rule))
        out[cls] = out.get(cls, 0) + 1
    return out


def _published_findings(
    response: LlmReviewResponse,
    hunks,
    pr_context,
    *,
    grade: Callable[..., tuple[FindingJudgement, ...]] = grade_findings,
    refute: Callable[..., tuple[FindingJudgement, ...]] = refute_findings,
) -> tuple[PublishedFinding, ...]:
    """Replay publication gates available from the corpus diff alone.

    Full-file-dependent verifier rules remain inconclusive until the evaluator
    fetches immutable head contents; the judge and refute gates still receive
    the exact scoped diff evidence used by this replay.
    """
    diff_hunks = tuple(hunks)
    evaluation = evaluate_diff(diff_hunks, response)
    verdicts = grade(
        evaluation,
        diff_hunks,
        0,
        pr_context=pr_context,
        file_contents={},
        cross_file_contents={},
        runtime_context=None,
    )
    kept, _ = partition_findings(evaluation.findings, verdicts)
    verified, _ = verify_findings(kept, {})
    high = tuple(
        finding for finding in verified if finding.severity in ("high", "critical")
    )
    refuted_verdicts = refute(
        high,
        diff_hunks,
        0,
        pr_context=pr_context,
        file_contents={},
        cross_file_contents={},
        runtime_context=None,
    )
    _, refuted = partition_refuted(high, refuted_verdicts)
    refuted_ids = {id(finding) for finding in refuted}
    return tuple(finding for finding in verified if id(finding) not in refuted_ids)


def diff_to_hunks(diff_text: str) -> list[Hunk]:
    """Unified diff -> Elder's Hunk units, via the production diff parser."""
    return [Hunk(path=h.file_path, body=h.body) for h in parse_diff(diff_text)]


def bounded_hunks(
    diff_text: str, budget: int = _MAX_DIFF_BYTES
) -> tuple[list[Hunk], bool]:
    """Bound the replay context at WHOLE-HUNK boundaries. A flat character
    slice corrupts the final hunk (Elder reviews garbage) and, worse,
    silently amputates expected findings - the two heaviest corpus PRs
    exceed the budget TODAY, so truncation is steady-state, not an edge.
    Returns (kept_hunks, truncated); the caller must surface `truncated`
    all the way into the report, never just a log line. A single hunk
    larger than the whole budget is kept alone (an empty replay would be
    a worse lie than an oversized prompt)."""
    hunks = diff_to_hunks(diff_text)
    kept: list[Hunk] = []
    used = 0
    for h in hunks:
        if kept and used + len(h.body) > budget:
            return kept, True
        kept.append(h)
        used += len(h.body)
    return kept, False


def fetch_pr_diff(repo: str, pr: int, token: str = "") -> str:
    """One PR's current unified diff from the GitHub API. Public repos work
    tokenless; `token` lifts the rate limit."""
    headers = {"Accept": "application/vnd.github.v3.diff"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = httpx.get(
        f"{_GITHUB_API}/repos/{repo}/pulls/{pr}",
        headers=headers,
        timeout=_DIFF_TIMEOUT_SECONDS,
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.text


def _github_headers(token: str, accept: str) -> dict[str, str]:
    headers = {"Accept": accept}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_pr_base_sha(repo: str, pr: int, token: str = "") -> str:
    """The PR's base-branch SHA (#545) - the immutable start point for an
    anchored compare. A separate JSON call from `fetch_pr_diff`: that one
    requests the diff media type and returns text, this needs the `base`
    object out of the default JSON representation."""
    resp = httpx.get(
        f"{_GITHUB_API}/repos/{repo}/pulls/{pr}",
        headers=_github_headers(token, "application/vnd.github+json"),
        timeout=_DIFF_TIMEOUT_SECONDS,
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.json()["base"]["sha"]


def fetch_commit_parent_sha(repo: str, commit_sha: str, token: str = "") -> str | None:
    """The first parent of `commit_sha` (#545), or None if it has none (a
    root commit - degenerate, cannot derive a pre-fix state from it). Used
    to turn a historical ledger row's `commit` (the FIX commit, per
    `EvalCase.anchor_fix_commit`) into the pre-fix snapshot: the state just
    before the fix landed still carries the bug."""
    resp = httpx.get(
        f"{_GITHUB_API}/repos/{repo}/commits/{commit_sha}",
        headers=_github_headers(token, "application/vnd.github+json"),
        timeout=_DIFF_TIMEOUT_SECONDS,
        follow_redirects=True,
    )
    resp.raise_for_status()
    parents = resp.json().get("parents") or []
    return parents[0]["sha"] if parents else None


def resolve_anchor_sha(
    case: EvalCase,
    token: str = "",
    *,
    fetch_parent: Callable[[str, str, str], str | None] = fetch_commit_parent_sha,
) -> str | None:
    """The SHA to diff the PR's base against for case (#545), or None when
    the case carries no usable anchor (final-diff replay, unanchored)."""
    if case.anchor_head_sha:
        return case.anchor_head_sha
    if case.anchor_fix_commit:
        return fetch_parent(case.repo, case.anchor_fix_commit, token)
    return None


def fetch_anchored_diff(
    repo: str,
    pr: int,
    anchor_sha: str,
    token: str = "",
    *,
    fetch_base: Callable[[str, int, str], str] = fetch_pr_base_sha,
) -> str:
    """The unified diff from the PR's base to `anchor_sha` (#545) - the
    pre-fix snapshot the finding was actually recorded against, immutable
    (a real commit SHA, unlike the PR's mutable current head)."""
    base_sha = fetch_base(repo, pr, token)
    resp = httpx.get(
        f"{_GITHUB_API}/repos/{repo}/compare/{base_sha}...{anchor_sha}",
        headers=_github_headers(token, "application/vnd.github.v3.diff"),
        timeout=_DIFF_TIMEOUT_SECONDS,
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.text


def diff_for_case(
    case: EvalCase,
    token: str = "",
    *,
    fetch_final: Callable[[str, int, str], str] = fetch_pr_diff,
    resolve_anchor: Callable[..., str | None] = resolve_anchor_sha,
    fetch_anchored: Callable[..., str] = fetch_anchored_diff,
) -> tuple[str, bool]:
    """The diff to replay `case` against, plus whether it is anchored to the
    pre-fix snapshot (#545).

    An anchored case tries `base...anchor_sha` first - the state the
    finding was actually recorded against, still carrying the bug. Anything
    that keeps that from resolving (no usable anchor on the case, the
    fix-commit's parent lookup fails, the compare call fails) falls back to
    `fetch_final` (the PR's current merged diff) and reports unanchored -
    resolution trouble must never turn a replayable case into an errored
    one, it only widens the KNOWN METHODOLOGY BIAS slice honestly."""
    if case.anchored:
        try:
            anchor_sha = resolve_anchor(case, token)
            if anchor_sha:
                return fetch_anchored(case.repo, case.pr, anchor_sha, token), True
        except Exception as e:  # noqa: BLE001 - fall back, never abort the case
            log.warning(
                "eval_case_anchor_resolution_failed case=%s kind=%s",
                case.case_id, type(e).__name__,
            )
    return fetch_final(case.repo, case.pr, token), False


def _bench_plan(hunks: list[Hunk]) -> ReviewPlan:
    """Pack `hunks` into bounded cohorts via the REAL planner Elder's own
    `review_diff` packs against - same char/path budgets
    (`review_cohort_char_budget`/`_review_cohort_paths`), so a bench replay
    stages a diff exactly where production would.

    `max_cohorts=None` (unbounded) is the one deliberate divergence from
    the live webhook path, which caps cohort count to protect a single
    request's wall-clock budget. A batch baseline recording is not on that
    clock - what it needs is COMPLETENESS, and `--record` already refuses
    a baseline built from any incomplete case (see `run_case` below), so
    there is no honest number to be had from truncating the plan instead
    of the diff. If a future corpus case needs so many cohorts that this
    becomes a real wall-clock problem, that will show up as `errored` (a
    cohort call timing out) rather than as a silently truncated plan."""
    return plan_review(
        hunks,
        max_cohort_chars=review_cohort_char_budget(),
        max_cohort_paths=_review_cohort_paths(),
        max_cohorts=None,
    )


def run_case(
    backend: BenchBackend,
    case: EvalCase,
    diff_text: str,
    *,
    team_practices: str = "",
    few_shot: str = "",
    anchored: bool = False,
) -> CaseReplay:
    """Replay ONE case. Any failure (empty/unparseable diff, transport,
    parse) returns errored=True + logs - it must never abort the sweep, and
    a non-run must never read as "Elder found nothing" (honest-zero rule).
    `anchored` (#545) just rides straight into the CaseReplay - the caller
    (`run_eval`, via `diff_for_case`) already decided whether `diff_text`
    is the pre-fix snapshot or the PR's final merged diff.

    A diff too big for one bounded cohort is STAGED (#859 follow-up) into
    several `_post` calls via `_bench_plan`, instead of the pre-existing
    behavior of sending the whole thing as one monolithic call - the
    latter is what made a 56-file/5236-line corpus case (grug#494)
    unrecordable even at a 900s per-request ceiling: not slow because the
    model is slow, but because nothing this large should ever have been
    one request. A diff that already fits in one cohort takes the exact
    same single-call path as before (`plan_review` returns one cohort
    covering every hunk in its original order), so this changes nothing
    for the rest of the corpus. `CaseReplay.staged` reports which cases
    took the multi-call path, so the baseline says so rather than the
    change being invisible.

    A hunk too large for even a lone cohort cannot be reviewed at all (see
    `ReviewCohort.oversized` / `plan_review`'s docstring: the planner
    refuses to truncate it, unlike the cross-hunk budget above, because
    truncation corrupts line anchors) - staging never fabricates a partial
    answer for it, and neither does an ordinary parse failure on any one
    cohort: the WHOLE case errors, mirroring `run_production_case`'s
    refusal to score incomplete `review_diff` coverage. A partial staged
    replay's misses would be amputation, not Elder, exactly like a partial
    production cohort run - never a case ELDER SCORED WORSE ON, only ever
    a case that did not finish running."""
    try:
        hunks, truncated = bounded_hunks(diff_text)
        if truncated:
            # Truncation changes what is measured - findings in the dropped
            # hunks become misses. It rides the CaseReplay into the report
            # and baseline, not just this log line.
            log.warning(
                "eval_diff_truncated case=%s original=%d budget=%d",
                case.case_id, len(diff_text), _MAX_DIFF_BYTES,
            )
        if not hunks:
            log.warning("eval_case_empty_diff case=%s", case.case_id)
            return CaseReplay(case_id=case.case_id, emitted={}, errored=True)

        plan = _bench_plan(hunks)
        oversized = next(
            (c for c in plan.concerns if c.kind == "oversized-hunk"), None
        )
        if oversized is not None:
            log.warning(
                "eval_case_hunks_oversized case=%s count=%d paths=%s",
                case.case_id, len(oversized.paths), ",".join(oversized.paths[:10]),
            )
            return CaseReplay(case_id=case.case_id, emitted={}, errored=True)

        review_map = render_review_map(plan) if plan.staged else ""
        findings: list[Finding] = []
        for cohort in plan.cohorts:
            cohort_hunks = [hunks[index] for index in cohort.hunk_indexes]
            messages = _build_messages(
                cohort_hunks, _PROMPT_VARIANT, None, None, None,
                team_practices=team_practices,
                few_shot_examples=few_shot,
                review_map=review_map,
            )
            resp = _post(backend, messages)
            cohort_findings, _model, err = _parse_response(resp)
            if err and not cohort_findings:
                # ALL-OR-NOTHING (see docstring): one bad cohort must not
                # silently shrink a staged case's findings into a fake
                # complete-looking replay.
                log.warning(
                    "eval_case_parse_failed case=%s err=%s", case.case_id, err
                )
                return CaseReplay(case_id=case.case_id, emitted={}, errored=True)
            findings.extend(cohort_findings)
    except Exception as e:  # noqa: BLE001 - one case must not abort the sweep
        # grug#881: a run that logged only `kind=TypeError` (no message, no
        # offending field) cost 90 minutes of CI to diagnose by inference.
        # `str(e)` is bounded (some exceptions embed large payloads) and
        # kept on the SAME line as `kind` so a log search on either still
        # finds the real cause, not just the exception class.
        log.warning(
            "eval_case_errored case=%s kind=%s err=%s",
            case.case_id, type(e).__name__, str(e)[:300],
        )
        return CaseReplay(case_id=case.case_id, emitted={}, errored=True)
    return CaseReplay(
        case_id=case.case_id,
        emitted=classes_for_findings(findings),
        errored=False,
        truncated=truncated,
        anchored=anchored,
        staged=plan.staged,
    )


def _response_backends(response: LlmReviewResponse) -> tuple[str, ...]:
    """The backend(s) that ACTUALLY answered this review (grug#916).

    `review_diff`'s chain degrades WITHOUT raising: with
    `GRUG_REVIEW_BACKEND_PRIORITY=cloud`, a tier-1 timeout is caught,
    logged, and control falls through to the Cave arms - the response
    still comes back `kind="reviewed"`, so the case scores normally and
    nothing in the replay says a different backend produced it. Reading
    such a run as a measurement of the CONFIGURED backend is exactly the
    mistake grug#916 was opened to correct: two `--production` runs, one
    per priority setting, reported near-identical per-class catch rates
    because both were in fact served by the same fallback.

    Prefers the plural `backends_used` (deep/staged reviews merge several
    arms) and falls back to the singular legacy field, so an older
    response shape still attributes rather than reporting nothing."""
    used = response.backends_used or (
        (response.backend_used,) if response.backend_used else ()
    )
    return tuple(sorted({b.value if hasattr(b, "value") else str(b) for b in used}))


def run_production_case(
    case: EvalCase,
    diff_text: str,
    *,
    review: Callable[..., LlmReviewResponse] = review_diff,
    published: bool = False,
    grade: Callable[..., tuple[FindingJudgement, ...]] = grade_findings,
    refute: Callable[..., tuple[FindingJudgement, ...]] = refute_findings,
    anchored: bool = False,
) -> CaseReplay:
    """Replay one case through production's staged discovery path.

    Unlike the historical backend bakeoff, this deliberately sends every
    parsed hunk to ``review_diff`` so its real cohort planner, specialist
    routing, merge, and coverage contract are measured. Partial coverage is a
    non-run for scoring: its apparent misses cannot honestly be compared with
    a complete baseline. `anchored` (#545): see `run_case`.
    """
    try:
        diff_hunks = parse_diff(diff_text)
        hunks = [Hunk(path=hunk.file_path, body=hunk.body) for hunk in diff_hunks]
        if not diff_hunks:
            return CaseReplay(case_id=case.case_id, emitted={}, errored=True)
        pr_context = {
            "repo": case.repo,
            "pr_number": case.pr,
            "review_phase": "eval-production",
        }
        response = review(
            hunks,
            installation_id=0,
            pr_context=pr_context,
        )
    except Exception as e:  # noqa: BLE001 - one case must not abort the sweep
        log.warning(
            "eval_production_case_errored case=%s kind=%s",
            case.case_id,
            type(e).__name__,
        )
        return CaseReplay(case_id=case.case_id, emitted={}, errored=True)

    incomplete = (
        response.coverage is not None and not response.coverage.complete
    ) or response.error.startswith("partial review:")
    if response.kind != "reviewed" or incomplete:
        log.warning(
            "eval_production_case_incomplete case=%s kind=%s error=%s",
            case.case_id,
            response.kind,
            response.error,
        )
        return CaseReplay(case_id=case.case_id, emitted={}, errored=True)
    findings: Iterable[Finding | PublishedFinding] = response.findings
    if published:
        findings = _published_findings(
            response,
            diff_hunks,
            pr_context,
            grade=grade,
            refute=refute,
        )
    return CaseReplay(
        case_id=case.case_id,
        emitted=classes_for_findings(findings),
        errored=False,
        anchored=anchored,
        backends=_response_backends(response),
    )


def run_production_eval(
    cases: Sequence[EvalCase],
    *,
    fetch: Callable[[str, int, str], str] = fetch_pr_diff,
    token: str = "",
    review: Callable[..., LlmReviewResponse] = review_diff,
    published: bool = False,
) -> dict[str, CaseReplay]:
    """Replay the corpus through the shipped staged discovery pipeline.
    `fetch` is the FINAL-diff fallback (injectable for tests, same as
    `run_eval`); `diff_for_case` (#545) tries each case's anchored pre-fix
    snapshot first and falls back to it."""
    replays: dict[str, CaseReplay] = {}
    for case in cases:
        try:
            diff, anchored = diff_for_case(case, token, fetch_final=fetch)
        except Exception as e:  # noqa: BLE001 - fetch failure is an errored case
            log.warning(
                "eval_diff_fetch_failed case=%s kind=%s",
                case.case_id,
                type(e).__name__,
            )
            replays[case.case_id] = CaseReplay(
                case_id=case.case_id,
                emitted={},
                errored=True,
            )
            continue
        replays[case.case_id] = run_production_case(
            case, diff, review=review, published=published, anchored=anchored,
        )
    return replays


def run_eval(
    backend: BenchBackend,
    cases: Sequence[EvalCase],
    *,
    fetch: Callable[[str, int, str], str] = fetch_pr_diff,
    token: str = "",
    team_practices: str = "",
    few_shot: str = "",
) -> dict[str, CaseReplay]:
    """Replay the whole corpus through one backend. `fetch` is the
    FINAL-diff fallback and is injectable for tests; `diff_for_case` (#545)
    tries each case's anchored pre-fix snapshot first and falls back to it
    on any resolution trouble. Returns case_id -> CaseReplay for
    `scoring.score`."""
    log.info("eval_start backend=%s cases=%d", backend.name, len(cases))
    replays: dict[str, CaseReplay] = {}
    for case in cases:
        try:
            diff, anchored = diff_for_case(case, token, fetch_final=fetch)
        except httpx.HTTPStatusError as e:
            # Transient (401/403/429: auth or rate limit - the rest of the
            # sweep is probably doomed too) vs PERMANENT (404: the corpus
            # references a deleted PR and should be pruned) must read
            # differently, or corpus rot silently shrinks denominators
            # run after run.
            status = e.response.status_code
            if status in (404, 406):
                # 404: deleted/inaccessible PR. 406: GitHub refuses the
                # diff media type over 20k lines / 300 files. Both are
                # PERMANENT corpus rot, not transient failures.
                log.warning(
                    "eval_corpus_pr_unfetchable case=%s status=%d - the ledger "
                    "references a PR whose diff cannot be fetched; prune or "
                    "annotate it",
                    case.case_id, status,
                )
            else:
                log.warning(
                    "eval_diff_fetch_failed case=%s status=%d", case.case_id, status
                )
            replays[case.case_id] = CaseReplay(
                case_id=case.case_id, emitted={}, errored=True
            )
            continue
        except Exception as e:  # noqa: BLE001 - fetch failure = errored case
            log.warning(
                "eval_diff_fetch_failed case=%s kind=%s",
                case.case_id, type(e).__name__,
            )
            replays[case.case_id] = CaseReplay(
                case_id=case.case_id, emitted={}, errored=True
            )
            continue
        replays[case.case_id] = run_case(
            backend, case, diff, team_practices=team_practices,
            few_shot=few_shot, anchored=anchored,
        )
    errored = sum(1 for r in replays.values() if r.errored)
    if errored:
        log.warning(
            "eval_errors backend=%s errors=%d total=%d",
            backend.name, errored, len(cases),
        )
    return replays
