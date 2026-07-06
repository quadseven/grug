"""Few-shot exemplar injection for Elder's prompt (#361 slice 3, #538).

The team's own highest-signal ACCEPTED findings, per class, rendered as a
bounded EXAMPLES section appended to Elder's system prompt. Distinct from
the #527 best-practices block: RULES state the norms maintainers enforce;
EXAMPLES teach the SHAPE of a finding worth reporting. Both derive from
the slice-1 ledger corpus and both are refreshed by `ingest_ledger`.

Pure module: selection comes from `ledger.accepted_findings_by_class`
(FPs excluded, severity-ranked - slice 1's exemplar function); this module
converts to its own dedicated `Exemplar` carrier (never a placeholder
LedgerRow - a fabricated verdict/repo would lie to any future consumer),
serializes for the cache, and renders the bounded block. The store
adapter persists under sk='EXEMPLARS' (`put_repo_exemplars` /
`get_repo_exemplars`); `llm_client._few_shot_block` fetches best-effort at
review time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

# Deliberate private import: the sibling's sanitizer strips control chars
# and caps item length (the #541 lesson - this text rides the SYSTEM
# prompt to a third-party backend). One sanitizer, not two drifting copies.
from best_practices import _sanitize
from ledger import SEVERITY_ORDER, LedgerRow

log = logging.getLogger("grug.few_shot")

DEFAULT_MAX_CLASSES = 6
DEFAULT_PER_CLASS = 2

_HEADER = (
    "EXAMPLES OF ACCEPTED FINDINGS from this repo's review history - real "
    "findings maintainers acted on. Match this bar and shape; do not "
    "re-report these exact items:"
)




@dataclass(frozen=True)
class Exemplar:
    """One cached few-shot exemplar - exactly the fields the block renders.
    A dedicated carrier (not a placeholder-filled LedgerRow) so nothing
    downstream can read a fabricated verdict/repo/reviewer off it."""

    finding_class: str
    severity: str
    finding: str
    pr: int


def exemplars_from_rows(by_class: dict[str, list[LedgerRow]]) -> list[Exemplar]:
    """Flatten the slice-1 aggregation (already FP-excluded and
    severity-ranked within each class) into Exemplars."""
    return [
        Exemplar(
            finding_class=cls, severity=r.severity, finding=r.finding, pr=r.pr
        )
        for cls, rows in by_class.items()
        for r in rows
    ]


def exemplars_to_dicts(exemplars: list[Exemplar]) -> list[dict]:
    """Cacheable dicts. The wire key is `class` (matching the ledger JSONL
    vocabulary), not the Python-side `finding_class` field name."""
    return [
        {
            "class": e.finding_class,
            "severity": e.severity,
            "finding": e.finding,
            "pr": e.pr,
        }
        for e in exemplars
    ]


def exemplars_from_dicts(data: list[dict]) -> list[Exemplar]:
    """Cached dicts back to Exemplars. Malformed entries are skipped (a
    stale cache row must not break a review) but COUNTED: a
    present-but-rotten cache decoding to [] must not be indistinguishable
    from 'no exemplars derived' - that failure would be permanent and
    invisible."""
    out: list[Exemplar] = []
    skipped = 0
    for d in data:
        if not isinstance(d, dict):
            skipped += 1
            continue
        try:
            out.append(
                Exemplar(
                    finding_class=str(d["class"]),
                    severity=str(d["severity"]),
                    finding=str(d["finding"]),
                    pr=int(d["pr"]),
                )
            )
        except (KeyError, ValueError, TypeError):
            skipped += 1
    if skipped:
        log.warning(
            "exemplar_cache_rows_skipped skipped=%d total=%d", skipped, len(data)
        )
    return out


def _class_rank(exemplars: list[Exemplar]) -> tuple[int, int]:
    """Strongest-first class ordering: best severity, then depth. Which
    classes survive the max_classes cut must be deterministic strength,
    never corpus-insertion order."""
    best = min(SEVERITY_ORDER.get(e.severity, 4) for e in exemplars)
    return (best, -len(exemplars))


def exemplars_block(
    exemplars: list[Exemplar],
    *,
    max_classes: int = DEFAULT_MAX_CLASSES,
    per_class: int = DEFAULT_PER_CLASS,
    max_chars: int = 1400,
) -> str:
    """The bounded prompt section, or "" when there is nothing to inject.
    Never unbounded: capped at max_classes x per_class exemplars AND
    max_chars (same discipline as `best_practices.practices_block`;
    per-item length is capped by the shared sanitizer, so one oversized
    finding can never blank the whole block)."""
    if not exemplars:
        return ""
    by_class: dict[str, list[Exemplar]] = {}
    for e in exemplars:
        by_class.setdefault(e.finding_class, []).append(e)
    ranked_classes = sorted(by_class, key=lambda c: _class_rank(by_class[c]))
    lines = [_HEADER]
    for cls in ranked_classes[:max_classes]:
        for e in by_class[cls][:per_class]:
            line = (
                f"- [{_sanitize(cls)}/{_sanitize(e.severity)}] "
                f"{_sanitize(e.finding)} (PR #{e.pr})"
            )
            if sum(len(x) + 1 for x in lines) + len(line) > max_chars:
                return "\n".join(lines) if len(lines) > 1 else ""
            lines.append(line)
    return "\n".join(lines) if len(lines) > 1 else ""
