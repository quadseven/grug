"""Few-shot exemplar injection for Elder's prompt (#361 slice 3, #538).

The team's own highest-signal ACCEPTED findings, per class, rendered as a
bounded EXAMPLES section appended to Elder's system prompt. Distinct from
the #527 best-practices block: RULES state the norms maintainers enforce;
EXAMPLES teach the SHAPE of a finding worth reporting. Both derive from
the slice-1 ledger corpus and both are refreshed by `ingest_ledger`.

Pure module: selection comes from `ledger.accepted_findings_by_class`
(FPs excluded, severity-ranked - slice 1's exemplar function); this module
only serializes for the cache and renders the bounded block. The store
adapter persists under sk='EXEMPLARS' (`put_repo_exemplars` /
`get_repo_exemplars`); `llm_client._few_shot_block` fetches best-effort at
review time.
"""

from __future__ import annotations

from ledger import LedgerRow

DEFAULT_MAX_CLASSES = 6
DEFAULT_PER_CLASS = 2

_HEADER = (
    "EXAMPLES OF ACCEPTED FINDINGS from this repo's review history - real "
    "findings maintainers acted on. Match this bar and shape; do not "
    "re-report these exact items:"
)


def _sanitize(text: str) -> str:
    """One exemplar = one line: collapse whitespace/newlines so a finding
    can never fork the block or smuggle prompt structure."""
    return " ".join(str(text).split())


def exemplars_to_dicts(by_class: dict[str, list[LedgerRow]]) -> list[dict]:
    """Flatten the slice-1 aggregation into cacheable dicts (class order
    then rank order preserved by list position)."""
    out: list[dict] = []
    for cls, rows in by_class.items():
        for r in rows:
            out.append(
                {
                    "class": cls,
                    "severity": r.severity,
                    "finding": r.finding,
                    "pr": r.pr,
                }
            )
    return out


def exemplars_from_dicts(data: list[dict]) -> dict[str, list[LedgerRow]]:
    """Cached dicts back to the by-class shape `exemplars_block` renders.
    Malformed entries are skipped (a stale cache row must not break a
    review)."""
    by_class: dict[str, list[LedgerRow]] = {}
    for d in data:
        if not isinstance(d, dict):
            continue
        try:
            row = LedgerRow(
                repo="",
                pr=int(d["pr"]),
                reviewer="",
                severity=str(d["severity"]),
                finding_class=str(d["class"]),
                finding=str(d["finding"]),
                verdict="fixed",
            )
        except (KeyError, ValueError, TypeError):
            continue
        by_class.setdefault(row.finding_class, []).append(row)
    return by_class


def exemplars_block(
    by_class: dict[str, list[LedgerRow]],
    *,
    max_classes: int = DEFAULT_MAX_CLASSES,
    per_class: int = DEFAULT_PER_CLASS,
    max_chars: int = 1400,
) -> str:
    """The bounded prompt section, or "" when there is nothing to inject.
    Never unbounded: capped at max_classes x per_class exemplars AND
    max_chars (same discipline as `best_practices.practices_block`)."""
    if not by_class:
        return ""
    lines = [_HEADER]
    for cls in list(by_class)[:max_classes]:
        for r in by_class[cls][:per_class]:
            line = (
                f"- [{_sanitize(cls)}/{_sanitize(r.severity)}] "
                f"{_sanitize(r.finding)} (PR #{r.pr})"
            )
            if sum(len(x) + 1 for x in lines) + len(line) > max_chars:
                return "\n".join(lines) if len(lines) > 1 else ""
            lines.append(line)
    return "\n".join(lines) if len(lines) > 1 else ""
