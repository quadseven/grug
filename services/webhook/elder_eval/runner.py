"""Live replay runner for the Elder eval (#361 slice 2, #537).

Fetches each corpus case's REAL PR diff from GitHub and drives it through
Elder's actual review path - `_build_messages` (Elder's exact prompt,
production variant) and `_parse_response` (Elder's exact parser) - via the
SAST benchmark's backend transport, so the catch/noise numbers are Elder's
real behavior, never a reimplementation's.

This module makes network calls - it is NOT imported by the pure-scoring
tests and never runs in the per-PR CI suite. It runs only from the
on-demand `benchmark.elder-eval.yml` job or a manual `python -m elder_eval`.
"""

from __future__ import annotations

import logging
from typing import Callable, Iterable, Sequence

import httpx

from code_review_prompt import RULES

# Elder's exact prompt + parser - deliberate private imports, same
# rationale + caveat as the SAST runner: if their signatures change, this
# runner must follow (measuring the shipped prompt is the whole point).
from llm_client import Finding, Hunk, _build_messages, _parse_response
from personas.code_reviewer.diff_parser import parse_diff
from sast_benchmark.backends import BenchBackend
from sast_benchmark.runner import _post

from .corpus import EvalCase, normalize_class
from .scoring import CaseReplay

log = logging.getLogger("grug.elder_eval")

# Measure the SHIPPED prompt (see sast_benchmark.runner._BENCH_PROMPT_VARIANT).
_PROMPT_VARIANT = "v1"
_GITHUB_API = "https://api.github.com"
_DIFF_TIMEOUT_SECONDS = 30.0
# Bound the replay context like the live review path bounds its own input.
_MAX_DIFF_BYTES = 200_000

# Finding.rule -> normalized bug class, from Elder's own RULES table.
_RULE_TO_CLASS: dict[str, str] = {
    r.name: normalize_class(r.bug_class) for r in RULES
}


def classes_for_findings(findings: Iterable[Finding]) -> dict[str, int]:
    """ELDER-normalized class -> finding count. A rule outside the RULES
    table (the model improvised a name) falls back to its own normalized
    name - it can never match an expected cell, so it only widens the
    noise denominator honestly."""
    out: dict[str, int] = {}
    for f in findings:
        cls = _RULE_TO_CLASS.get(f.rule, normalize_class(f.rule))
        out[cls] = out.get(cls, 0) + 1
    return out


def diff_to_hunks(diff_text: str) -> list[Hunk]:
    """Unified diff -> Elder's Hunk units, via the production diff parser."""
    return [Hunk(path=h.file_path, body=h.body) for h in parse_diff(diff_text)]


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


def run_case(
    backend: BenchBackend,
    case: EvalCase,
    diff_text: str,
    *,
    team_practices: str = "",
) -> CaseReplay:
    """Replay ONE case. Any failure (empty/unparseable diff, transport,
    parse) returns errored=True + logs - it must never abort the sweep, and
    a non-run must never read as "Elder found nothing" (honest-zero rule)."""
    try:
        hunks = diff_to_hunks(diff_text[:_MAX_DIFF_BYTES])
        if not hunks:
            log.warning(
                "eval_case_empty_diff",
                extra={"case": case.case_id},
            )
            return CaseReplay(case_id=case.case_id, emitted={}, errored=True)
        messages = _build_messages(
            hunks, _PROMPT_VARIANT, None, None, None,
            team_practices=team_practices,
        )
        resp = _post(backend, messages)
        findings, _model, err = _parse_response(resp)
    except Exception as e:  # noqa: BLE001 - one case must not abort the sweep
        log.warning(
            "eval_case_errored",
            extra={"case": case.case_id, "kind": type(e).__name__},
        )
        return CaseReplay(case_id=case.case_id, emitted={}, errored=True)
    if err and not findings:
        log.warning(
            "eval_case_parse_failed",
            extra={"case": case.case_id, "err": err},
        )
        return CaseReplay(case_id=case.case_id, emitted={}, errored=True)
    return CaseReplay(
        case_id=case.case_id,
        emitted=classes_for_findings(findings),
        errored=False,
    )


def run_eval(
    backend: BenchBackend,
    cases: Sequence[EvalCase],
    *,
    fetch: Callable[[str, int, str], str] = fetch_pr_diff,
    token: str = "",
    team_practices: str = "",
) -> dict[str, CaseReplay]:
    """Replay the whole corpus through one backend. `fetch` is injectable
    for tests. Returns case_id -> CaseReplay for `scoring.score`."""
    log.info(
        "eval_start",
        extra={"backend": backend.name, "cases": len(cases)},
    )
    replays: dict[str, CaseReplay] = {}
    for case in cases:
        try:
            diff = fetch(case.repo, case.pr, token)
        except Exception as e:  # noqa: BLE001 - fetch failure = errored case
            log.warning(
                "eval_diff_fetch_failed",
                extra={"case": case.case_id, "kind": type(e).__name__},
            )
            replays[case.case_id] = CaseReplay(
                case_id=case.case_id, emitted={}, errored=True
            )
            continue
        replays[case.case_id] = run_case(
            backend, case, diff, team_practices=team_practices
        )
    errored = sum(1 for r in replays.values() if r.errored)
    if errored:
        log.warning(
            "eval_errors",
            extra={"backend": backend.name, "errors": errored, "total": len(cases)},
        )
    return replays
