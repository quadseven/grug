# MIRRORED — sibling at services/api/personas/code_reviewer/sast.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
"""SAST detection tracer for Elder — clear-text-secret-log (#400, ADR-0006).

The vendor-neutral RECALL layer: `scan_candidates` finds candidate vuln sites
in the diff deterministically (a builtin detector for ONE class here — the
heavy Semgrep OSS engine slots in behind this same boundary at #401). The
PRECISION layer (the LLM exploitability judge that keeps/suppresses each
candidate with a reason) lives in `sast_judge.py`; kept candidates become
ordinary `Finding`s that flow through the EXISTING anti-hallucination filter
(`evaluate_diff`) + advisory/blocking publish path — no parallel posting.

Design split (ADR-0006 "SAST recall + LLM precision"): the detector is
deliberately LIBERAL — it flags BOTH a real secret reaching a log sink AND the
#391 public-config-path log (logging an SSM PARAM NAME). Discriminating those
is the judge's job, not the detector's. A detector miss is unrecoverable
(no candidate -> no finding); a detector over-flag is recovered by the judge.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

from llm_client import Hunk, JudgeFindingRepr, PrContext, judge_findings

from .diff_parser import DiffHunk
from .persona import Finding

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.code_reviewer.sast")

# The one class this tracer covers. Kept as a constant so the judge + tests +
# the (future) engine share one spelling.
CLEARTEXT_SECRET_LOG = "cleartext-secret-log"

# A logging / print SINK on an added line. Covers the stdlib + common logger
# idioms (`logging.info`, `log.warning`, `logger.debug`, `self.log.error`) and
# bare `print(`. Deliberately broad — the judge filters non-exploitable hits.
_SINK_RE = re.compile(
    r"(?:\b(?:logging|log|logger|_log|self\.log|LOG)\s*\.\s*"
    r"(?:debug|info|warning|warn|error|exception|critical)\s*\()"
    r"|(?:\bprint\s*\()"
)

# A SECRET-ish token: a name/keyword that suggests a credential is in scope on
# the line. Matches identifiers AND substrings inside an f-string/format. The
# judge decides whether the secret VALUE (vs a public name/path) actually
# reaches the sink.
_SECRET_TOKEN_RE = re.compile(
    r"(?i)(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|"
    r"secret[_-]?key|private[_-]?key|credential|client[_-]?secret|auth[_-]?token)"
)


@dataclass(frozen=True, slots=True)
class Candidate:
    """One detected candidate vuln site, pre-judgement. `(file, line)` map to a
    real added diff line (so a kept candidate passes the anti-hallucination
    filter). `snippet` is the offending added line (for the judge + the finding
    message). `vuln_class` groups it (benchmark + future multi-class)."""

    vuln_class: str
    file: str
    line: int
    snippet: str


def _added_lines(hunk: DiffHunk) -> list[tuple[int, str]]:
    """Walk a hunk body -> [(new_side_line_number, added_text)] for each ADDED
    line. New-side number starts at `new_start` and advances on added/context
    lines, not on removed lines (unified-diff semantics). The first body line
    is the `@@` header (skipped)."""
    out: list[tuple[int, str]] = []
    lineno = hunk.new_start
    for raw in hunk.body.splitlines():
        if raw.startswith("@@"):
            continue
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("+"):
            out.append((lineno, raw[1:]))
            lineno += 1
        elif raw.startswith("-"):
            continue  # removed line: no new-side advance
        else:
            lineno += 1  # context line
    return out


def _is_cleartext_secret_log(text: str) -> bool:
    """True when an added line both calls a log/print SINK and references a
    secret-ish token — the recall heuristic for this class."""
    return bool(_SINK_RE.search(text) and _SECRET_TOKEN_RE.search(text))


def scan_candidates(hunks: tuple[DiffHunk, ...]) -> tuple[Candidate, ...]:
    """Pure builtin detector for clear-text-secret-log over the diff hunks.

    Returns one Candidate per added line that looks like a secret reaching a
    log sink. LIBERAL by design (see module docstring) — the judge does the
    exploitability discrimination. Line numbers are real new-side numbers in
    the hunk's `new_lines`, so a kept candidate survives `evaluate_diff`'s
    anti-hallucination filter. No IO, no logging — deterministic + unit-tested.
    """
    candidates: list[Candidate] = []
    for hunk in hunks:
        for lineno, text in _added_lines(hunk):
            if _is_cleartext_secret_log(text):
                candidates.append(
                    Candidate(
                        vuln_class=CLEARTEXT_SECRET_LOG,
                        file=hunk.file_path,
                        line=lineno,
                        snippet=text.strip(),
                    )
                )
    return tuple(candidates)


# Secret-leak severity. "high" lives in the blocking partition, so an operator
# who flips code_reviewer_blocking gets it as a merge gate; advisory by default.
_SAST_SEVERITY = "high"


def _candidate_to_repr(c: Candidate) -> JudgeFindingRepr:
    """Provisional finding handed to the exploitability judge. The message
    states the candidate hypothesis; the judge decides if the secret VALUE
    (vs a public name/path, the #391 case) actually reaches the sink."""
    return {
        "rule_name": c.vuln_class,
        "file": c.file,
        "line": c.line,
        "severity": _SAST_SEVERITY,
        "message": f"Possible clear-text logging of a secret: {c.snippet}",
    }


def judge_candidates(
    candidates: tuple[Candidate, ...],
    hunks: tuple[DiffHunk, ...],
    installation_id: int,
    *,
    pr_context: Optional[PrContext] = None,
    file_contents: dict[str, str] | None = None,
) -> tuple[Finding, ...]:
    """Precision layer: LLM-judge each candidate for real exploitability and
    return Findings for the KEPT (exploitable) ones, suppressing the rest.

    Reuses the existing `llm_client.judge_findings` (is_real_bug) as the
    exploitability gate — `reasoning` becomes the finding's source->sink
    rationale. A judged-FALSE candidate (e.g. the #391 public-config-path log)
    is suppressed (dropped), so the FP never reaches the PR.

    FAIL-CLOSED on a judge outage: if the judge can't run (LLM down, parse
    error, count mismatch), candidates are SUPPRESSED, not posted raw — an
    un-triaged candidate flood is exactly the noisy-SAST failure precision
    exists to prevent (a real vuln re-surfaces on the next push's re-review).
    """
    if not candidates:
        return ()
    reprs = [_candidate_to_repr(c) for c in candidates]
    llm_hunks = [Hunk(path=h.file_path, body=h.body) for h in hunks]
    try:
        judgements = judge_findings(
            reprs, llm_hunks, installation_id,
            pr_context=pr_context, file_contents=file_contents,
        )
    except Exception as e:  # noqa: BLE001 — judge failure must not crash the review
        log.warning(
            "sast_judge_unavailable_candidates_suppressed",
            extra={"installation_id": installation_id, "candidates": len(candidates),
                   "kind": type(e).__name__},
        )
        return ()

    by_index = {j.finding_index: j for j in judgements}
    kept: list[Finding] = []
    for i, c in enumerate(candidates):
        j = by_index.get(i)
        if j is None:
            # The judge returned no verdict for this candidate -> can't confirm
            # exploitability -> suppress (fail-closed, same as an outage).
            log.info(
                "sast_candidate_unjudged_suppressed",
                extra={"installation_id": installation_id, "file": c.file, "line": c.line},
            )
            continue
        if not j.is_real_bug:
            log.info(
                "sast_candidate_suppressed_not_exploitable",
                extra={"file": c.file, "line": c.line, "reason": j.reasoning},
            )
            continue
        kept.append(
            Finding(
                file=c.file,
                line=c.line,
                severity=_SAST_SEVERITY,
                rule_name=c.vuln_class,
                message=(
                    f"Clear-text logging of a secret. {j.reasoning} "
                    f"(line: `{c.snippet}`)"
                ),
                suggestion=None,
            )
        )
    return tuple(kept)
