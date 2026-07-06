"""Auto-derived per-repo best-practices from the review-findings ledger
(#527, epic #522). Distinct from #361's few-shot EXAMPLES: this distills
the ledger's ACCEPTED findings into a compact ranked RULES block that
steers Elder's prompt ("this team consistently requires X").

Pure derivation over the ledger corpus (services/_shared/ledger). The
store caches the result per repo; the ingest/poller pass refreshes it;
`build_system_prompt(extra_rules=...)` injects it. Decay: a practice not
reinforced within `decay_prs` of the newest PR drops off, so the block
tracks current team norms instead of ossifying.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass

from ledger import LedgerRow
from redact import redact_secrets

# Keep the injected block bounded - it rides on every review prompt.
DEFAULT_TOP_N = 8
DEFAULT_DECAY_PRS = 200
_MAX_EXAMPLE_PRS = 3
_MAX_RULE_CHARS = 220


def _sanitize(text: str) -> str:
    """Neutralize a ledger finding for SYSTEM-prompt injection (#541 Qodo):
    flatten newlines (no fake message boundaries), drop control chars, and
    cap length. Ledger text is operator-authored but still DATA, not
    instructions."""
    flat = " ".join(str(text).split())
    flat = "".join(c for c in flat if c.isprintable())
    return flat[:_MAX_RULE_CHARS]


@dataclass(frozen=True)
class Practice:
    finding_class: str
    rule: str            # a representative accepted finding for the class
    hits: int            # how many accepted findings reinforce it
    example_prs: list[int]
    last_pr: int         # newest PR that reinforced it (drives decay + rank)


def derive_practices(
    rows: list[LedgerRow],
    *,
    decay_prs: int = DEFAULT_DECAY_PRS,
) -> list[Practice]:
    """Distil ACCEPTED ledger findings into ranked, decayed practices - one
    per finding class. `rows` is the repo's ledger corpus. A class is kept
    only if it was reinforced within `decay_prs` of the newest accepted PR
    (recency decay). Ranked by hits desc, then recency desc."""
    accepted = [r for r in rows if r.accepted]
    if not accepted:
        return []
    newest_pr = max(r.pr for r in accepted)
    cutoff = newest_pr - decay_prs

    by_class: dict[str, list[LedgerRow]] = defaultdict(list)
    for r in accepted:
        by_class[r.finding_class].append(r)

    practices: list[Practice] = []
    for cls, items in by_class.items():
        last_pr = max(r.pr for r in items)
        if last_pr < cutoff:
            continue  # decayed: not reinforced recently enough
        # Representative rule = the most-recent accepted finding in the
        # class (freshest phrasing of the recurring requirement).
        rep = max(items, key=lambda r: r.pr)
        example_prs = sorted({r.pr for r in items}, reverse=True)[:_MAX_EXAMPLE_PRS]
        practices.append(Practice(
            finding_class=cls, rule=rep.finding, hits=len(items),
            example_prs=example_prs, last_pr=last_pr,
        ))
    practices.sort(key=lambda p: (p.hits, p.last_pr), reverse=True)
    return practices


def practices_to_dicts(practices: list[Practice]) -> list[dict]:
    return [asdict(p) for p in practices]


def practices_from_dicts(data: list[dict]) -> list[Practice]:
    return [
        Practice(
            finding_class=d["finding_class"], rule=d["rule"], hits=int(d["hits"]),
            example_prs=list(d.get("example_prs", [])), last_pr=int(d["last_pr"]),
        )
        for d in data
    ]


def practices_block(
    practices: list[Practice], *, top_n: int = DEFAULT_TOP_N, max_chars: int = 1400,
) -> str:
    """The bounded prompt section, or "" when there's nothing to inject.
    Never unbounded: capped at top_n practices AND max_chars."""
    if not practices:
        return ""
    header = (
        "TEAM-LEARNED PRACTICES (distilled from this repo's ACCEPTED review "
        "history - weight these; they reflect what maintainers here actually "
        "enforce):"
    )
    lines = [header]
    for p in practices[:top_n]:
        refs = ", ".join(f"#{n}" for n in p.example_prs)
        # REDACT BEFORE SANITIZE (#546 peer review): the sanitizer caps at
        # 220 chars; a mid-body truncation defeats the PEM BEGIN...END
        # redaction pattern and would leak partial key material.
        line = (
            f"- [{_sanitize(p.finding_class)} x{p.hits}] "
            f"{_sanitize(redact_secrets(p.rule))} (e.g. {refs})"
        )
        if sum(len(x) + 1 for x in lines) + len(line) > max_chars:
            break
        lines.append(line)
    return "\n".join(lines)
