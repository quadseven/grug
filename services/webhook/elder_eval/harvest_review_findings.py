"""Build a review-findings eval corpus from PR review history (#595).

githumps PRs accumulate line-anchored inline review findings from several
reviewer accounts (human and automated). This module collects that history
into ``logs/review-findings.jsonl`` and scores Grug's reviewer against it:
"of the findings recorded on our PRs, how many did Elder also flag?" -- the
recall half of Grug's review-quality scoreboard (#361/#594).

Two layers, mirroring the rest of elder_eval:

- PURE parsers/join (unit-tested, no network): ``parse_inline_header_comment``
  (bold-title findings under an italic category|severity header),
  ``parse_summary_block_comment`` (HTML summary blocks with star-graded
  relevance and ``[path[Rn-m]]`` anchors), ``parse_grug_comment`` (Elder's own
  format), and ``recall_report``.
- A thin network ``main`` (NOT imported by tests, mirrors sast_benchmark's
  posture) that pages GitHub's repo-wide comment endpoints with a
  ``GITHUB_TOKEN`` and writes the corpus.

Which reviewer accounts to collect is runtime configuration, not code:
``GRUG_HARVEST_SRC_A_LOGIN`` (inline-header format, corpus label ``src-a``)
and ``GRUG_HARVEST_SRC_B_LOGIN`` (summary-block format, label ``src-b``).

The recall join is corpus-vs-Grug on the SAME surface: Elder publishes his own
inline comments as ``grug-tribe[bot]`` on the identical endpoint (the
review-ledger's ``evidence`` field is free text, no path/line, so it cannot
anchor this join). One row vocabulary for all sources: ``{source, repo, pr,
path, line, severity, category, finding, outdated, ts, url}``; ``severity``
normalized to low/medium/high/critical.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any

log = logging.getLogger("grug.elder_eval.harvest")

# Reviewer accounts to collect are runtime configuration (env), keeping the
# harvester generic over whoever reviews our PRs; a slot left unset is skipped.
import os as _os

_SRC_A_LOGIN = _os.getenv("GRUG_HARVEST_SRC_A_LOGIN", "")
_SRC_B_LOGIN = _os.getenv("GRUG_HARVEST_SRC_B_LOGIN", "")
_BOT_GRUG = "grug-tribe[bot]"

# Inline-header finding format: `_<emoji> Category_ | _<emoji> Severity_ ...`
# Replies/acks/summaries carry no such header and are skipped.
_INLINE_HEADER_RE = re.compile(
    r"^_[^\w\s]*\s*(?P<category>[^_]+?)_\s*\|\s*_[^\w\s]*\s*(?P<severity>Critical|Major|Minor|Trivial)_",
    re.MULTILINE,
)
_INLINE_TITLE_RE = re.compile(r"\*\*(?P<title>[^*]+)\*\*")

_INLINE_SEVERITY_MAP = {
    "critical": "critical",
    "major": "high",
    "minor": "medium",
    "trivial": "low",
}

# Summary-block finding format (issue-level): a <summary> title line with optional
# star relevance, plus a file link like `[services/x.py[R12-34]](url)`.
_SUMMARY_BLOCK_RE = re.compile(
    r"<summary>\s*(?:\d+\.\s*)?(?P<title>[^<]{4,120}?)\s*<code>", re.DOTALL
)
_SUMMARY_STARS_RE = re.compile(r"`(?P<stars>⭐{1,3})[^`]*`")
_SUMMARY_FILELINK_RE = re.compile(
    r"\[(?P<path>[\w./_-]+\.[A-Za-z0-9]+)\[R(?P<line>\d+)(?:-\d+)?\]\]"
)

_SUMMARY_STARS_MAP = {1: "low", 2: "medium", 3: "high"}


@dataclass(frozen=True, slots=True)
class BotFinding:
    """One labeled review finding, line-anchored where the reviewer anchored it."""

    source: str          # "src-a" | "src-b" | "grug"
    repo: str            # owner/name
    pr: int
    path: str
    line: int | None     # None when the bot's anchor was not line-shaped
    severity: str        # low|medium|high|critical (normalized)
    category: str        # bot's own category label, kebab-normalized
    finding: str         # short human text (title or first line)
    outdated: bool       # position no longer on the live diff (code moved)
    ts: str              # ISO-8601 created_at
    url: str


_NORM_RE = re.compile(r"[^a-z0-9]+")


def _kebab(label: str) -> str:
    return _NORM_RE.sub("-", label.lower()).strip("-")


# Secret-class findings quote the credential material they flag; committing
# that verbatim republishes it (observed on #608: a webhook token from a
# PRIVATE repo's review landed in this PUBLIC corpus - Critical). Mask
# credential-shaped runs at generation time: long hex everywhere, plus any
# long token run inside secret-class findings. Masked form keeps a 4-char
# prefix + length so rows stay matchable without being usable.
_HEX_RUN_RE = re.compile(r"\b[0-9a-fA-F]{16,}\b")
_TOKEN_RUN_RE = re.compile(r"\b[A-Za-z0-9+/_\-]{20,}\b")
_SECRET_CATEGORY_RE = re.compile(r"secret|credential|token|api-?key", re.IGNORECASE)


def _mask(match: re.Match) -> str:
    t = match.group(0)
    return f"{t[:4]}\u2026REDACTED[{len(t)}]"


def redact_finding_text(text: str, category: str) -> str:
    """Mask credential-shaped substrings in a finding's text (pure).

    Long hex runs are masked unconditionally; inside secret-class categories
    every long token run is masked (those findings exist to point AT a
    credential, so any long run is presumed material)."""
    out = _HEX_RUN_RE.sub(_mask, text)
    if _SECRET_CATEGORY_RE.search(category or ""):
        out = _TOKEN_RUN_RE.sub(_mask, out)
    return out


def _pr_number(pull_request_url: str) -> int | None:
    m = re.search(r"/pulls/(\d+)$", pull_request_url or "")
    return int(m.group(1)) if m else None


def parse_inline_header_comment(repo: str, raw: dict[str, Any]) -> BotFinding | None:
    """One repo-wide `/pulls/comments` row (inline-header format) -> a
    finding, or None.

    None for anything that is not a finding-shaped comment: replies,
    acknowledgements, walkthrough chatter -- only bodies carrying the
    category|severity header count, so the corpus stays label-clean.
    """
    body = str(raw.get("body") or "")
    header = _INLINE_HEADER_RE.search(body)
    if header is None:
        return None
    pr = _pr_number(str(raw.get("pull_request_url") or ""))
    if pr is None:
        return None
    # Title = the FIRST non-blank line after the header, bold-stripped when
    # present. Anchoring after the header stops preamble bold text from
    # masquerading as the title, and taking the first line (rather than
    # searching the whole remainder for a bold span) stops a bold string deep
    # in the detail text from being grabbed either (review findings on #608).
    # header.end() is mid-line (the severity match); skip past the header
    # LINE's newline so trailing chips ("| Quick win") are never the title.
    nl = body.find("\n", header.end())
    after = body[nl + 1:] if nl != -1 else ""
    first_line = next((ln.strip() for ln in after.splitlines() if ln.strip()), "")
    bold_m = _INLINE_TITLE_RE.match(first_line)
    finding = (bold_m.group("title") if bold_m else first_line).strip()
    line = raw.get("line")
    original_line = raw.get("original_line")
    return BotFinding(
        source="src-a",
        repo=repo,
        pr=pr,
        path=str(raw.get("path") or ""),
        line=int(line) if line is not None else (
            int(original_line) if original_line is not None else None
        ),
        severity=_INLINE_SEVERITY_MAP[header.group("severity").lower()],
        category=_kebab(header.group("category")),
        finding=redact_finding_text(finding, _kebab(header.group("category")))[:300],
        # `line` null with `original_line` set == GitHub says the position is
        # outdated (the code moved after the comment) -- a weak "addressed"
        # signal the eval can weigh.
        outdated=line is None and original_line is not None,
        ts=str(raw.get("created_at") or ""),
        url=str(raw.get("html_url") or ""),
    )


def parse_summary_block_comment(
    repo: str, pr: int, body: str, ts: str, url: str,
) -> list[BotFinding]:
    """Extract finding blocks from one summary-block-format issue comment.

    Findings arrive inside `<details>/<summary>` HTML with star-graded
    relevance and `[path[Rn-m]](link)` anchors. Best-effort: a block without a
    file link is kept with path=''/line=None rather than dropped, so counting
    stays honest even when anchoring fails.
    """
    if "Code Review by" not in body:
        return []
    out: list[BotFinding] = []
    # Split per finding block on <summary> boundaries; pair each with the
    # text until the next block for stars + file link.
    blocks = list(_SUMMARY_BLOCK_RE.finditer(body))
    for i, m in enumerate(blocks):
        title = re.sub(r"\s+", " ", m.group("title")).strip()
        if title.lower().startswith(("high-level", "pr summary")):
            continue
        seg = body[m.start(): blocks[i + 1].start() if i + 1 < len(blocks) else len(body)]
        stars_m = _SUMMARY_STARS_RE.search(seg)
        severity = _SUMMARY_STARS_MAP.get(len(stars_m.group("stars")) if stars_m else 0, "medium")
        file_m = _SUMMARY_FILELINK_RE.search(seg)
        out.append(
            BotFinding(
                source="src-b",
                repo=repo,
                pr=pr,
                path=file_m.group("path") if file_m else "",
                line=int(file_m.group("line")) if file_m else None,
                severity=severity,
                category="review-bug",
                finding=redact_finding_text(title, "review-bug")[:300],
                outdated=False,
                ts=ts,
                url=url,
            )
        )
    return out


# Elder's inline header: `**HIGH · `caller-not-updated`** · heavy lift`
_GRUG_HEADER_RE = re.compile(
    r"^\*\*(?P<severity>LOW|MEDIUM|HIGH|CRITICAL)\s*·\s*`(?P<category>[^`]+)`\*\*",
    re.MULTILINE,
)


def parse_grug_comment(repo: str, raw: dict[str, Any]) -> BotFinding | None:
    """One repo-wide `/pulls/comments` row from grug-tribe[bot] -> a finding.

    Only Elder's finding-shaped bodies (the bold SEVERITY-dot-class header)
    count; walkthrough/ack chatter is skipped, same posture as the inline-
    header parser.
    """
    body = str(raw.get("body") or "")
    header = _GRUG_HEADER_RE.search(body)
    if header is None:
        return None
    pr = _pr_number(str(raw.get("pull_request_url") or ""))
    if pr is None:
        return None
    # First non-blank line AFTER the header LINE = the descriptive text; the
    # header line itself would just duplicate severity/category.
    nl = body.find("\n", header.end())
    after = body[nl + 1:] if nl != -1 else ""
    first_line = next((ln.strip() for ln in after.splitlines() if ln.strip()), "")
    line = raw.get("line")
    original_line = raw.get("original_line")
    return BotFinding(
        source="grug",
        repo=repo,
        pr=pr,
        path=str(raw.get("path") or ""),
        line=int(line) if line is not None else (
            int(original_line) if original_line is not None else None
        ),
        severity=header.group("severity").lower(),
        category=_kebab(header.group("category")),
        finding=redact_finding_text(first_line, header.group("category"))[:300],
        outdated=line is None and original_line is not None,
        ts=str(raw.get("created_at") or ""),
        url=str(raw.get("html_url") or ""),
    )


# --------------------------------------------------------------------------- #
# Recall: corpus findings vs Grug's own inline findings (same surface)
# --------------------------------------------------------------------------- #

_LINE_SLACK = 3  # a Grug finding within +/-3 lines of the bot's anchor counts


def recall_report(
    bot_findings: Iterable[dict[str, Any]],
    grug_findings: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Of the corpus's anchored findings, how many did Grug also flag?
    Join key: same (repo, pr, path) and |line delta| <= 3; a bot finding
    with no line anchor matches on (repo, pr, path) alone.

    Pure + deterministic: dict rows in, dict report out (per-source,
    per-severity, overall). Un-anchored bot rows (no path) are counted
    separately and EXCLUDED from the denominator -- an unanchorable finding
    can't be fairly scored as a miss.
    """
    elder_index: dict[tuple[str, int, str], list[int | None]] = {}
    for row in grug_findings:
        try:
            key = (str(row["repo"]), int(row["pr"]), str(row.get("path", "")))
        except (KeyError, TypeError, ValueError):
            continue
        line = row.get("line")
        elder_index.setdefault(key, []).append(int(line) if line is not None else None)

    def elder_has(repo: str, pr: int, path: str, line: "int | str | None") -> bool:
        lines = elder_index.get((repo, pr, path))
        if lines is None:
            return False
        try:
            line = int(line) if line is not None else None
        except (TypeError, ValueError):
            line = None  # malformed anchor -> match on path alone
        if line is None:
            return True
        return any(el is None or abs(el - line) <= _LINE_SLACK for el in lines)

    total = matched = unanchored = 0
    by_source: dict[str, dict[str, int]] = {}
    by_severity: dict[str, dict[str, int]] = {}
    for f in bot_findings:
        path = str(f.get("path") or "")
        if not path:
            unanchored += 1
            continue
        # Same defensiveness as the grug_findings loop above: one malformed
        # row must not abort the whole report.
        try:
            repo, pr = str(f["repo"]), int(f["pr"])
            source, severity = str(f["source"]), str(f["severity"])
        except (KeyError, TypeError, ValueError):
            continue
        total += 1
        hit = elder_has(repo, pr, path, f.get("line"))
        matched += hit
        for bucket, key in ((by_source, source), (by_severity, severity)):
            b = bucket.setdefault(key, {"total": 0, "matched": 0})
            b["total"] += 1
            b["matched"] += hit

    def rate(m: int, t: int) -> float:
        return round(m / t, 4) if t else 0.0

    return {
        "denominator": total,
        "matched": matched,
        "recall": rate(matched, total),
        "unanchored_excluded": unanchored,
        "by_source": {
            k: {**v, "recall": rate(v["matched"], v["total"])} for k, v in sorted(by_source.items())
        },
        "by_severity": {
            k: {**v, "recall": rate(v["matched"], v["total"])} for k, v in sorted(by_severity.items())
        },
    }


# --------------------------------------------------------------------------- #
# Network main (no tests import below this line; mirrors sast_benchmark)
# --------------------------------------------------------------------------- #

_DEFAULT_REPOS = (
    "grug", "digital-ledger", "claude-stuff", "infra", "infra-public",
    "macchina", "grugthink", "brother-claudius", "conducted", "holdfast",
    "meow-now", "vroom-vroom", "gemini-plugin-cc", "aws-solutions-architect-study",
)
_PAGE_CAP = 30  # 3000 comments/repo/endpoint - far past observed volume


def _paged(client: Any, url: str, *, params: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for page in range(1, _PAGE_CAP + 1):
        resp = client.get(url, params={**params, "per_page": 100, "page": page})
        resp.raise_for_status()
        batch = resp.json() or []
        yield from batch
        if len(batch) < 100:
            return
    log.warning("harvest_pagination_cap", extra={"url": url})


def harvest(owner: str, repos: Iterable[str], token: str, since: str) -> list[BotFinding]:
    """Pull the configured reviewers' comments repo-wide (inline review
    comments for src-a, issue comments for src-b) since `since` (ISO date)."""
    import httpx

    client = httpx.Client(
        base_url="https://api.github.com",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30.0,
    )
    out: list[BotFinding] = []
    # Exact-duplicate guard (review finding on #608): reviewers repost
    # identical findings across re-reviews of the same head; 8% of the first
    # harvest was exact dupes, inflating the recall denominator.
    seen: set[tuple[str, str, int, str, int | None, str]] = set()

    def _add(f: BotFinding) -> None:
        key = (f.source, f.repo, f.pr, f.path, f.line, f.finding)
        if key in seen:
            return
        seen.add(key)
        out.append(f)

    try:
        _harvest_into(client, owner, repos, since, _add)
    finally:
        client.close()
    return out


def _harvest_into(
    client: Any, owner: str, repos: Iterable[str], since: str, add: Any,
) -> None:
    for repo_name in repos:
        repo = f"{owner}/{repo_name}"
        for raw in _paged(
            client, f"/repos/{repo}/pulls/comments",
            params={"since": since, "sort": "created", "direction": "asc"},
        ):
            login = (raw.get("user") or {}).get("login")
            f = None
            if _SRC_A_LOGIN and login == _SRC_A_LOGIN:
                f = parse_inline_header_comment(repo, raw)
            elif login == _BOT_GRUG:
                f = parse_grug_comment(repo, raw)
            if f is not None:
                add(f)
        for raw in _paged(
            client, f"/repos/{repo}/issues/comments",
            params={"since": since, "sort": "created", "direction": "asc"},
        ):
            if not _SRC_B_LOGIN or (raw.get("user") or {}).get("login") != _SRC_B_LOGIN:
                continue
            pr = _pr_number(str(raw.get("issue_url") or "").replace("/issues/", "/pulls/"))
            if pr is None:
                continue
            for f in parse_summary_block_comment(
                repo, pr, str(raw.get("body") or ""),
                str(raw.get("created_at") or ""), str(raw.get("html_url") or ""),
            ):
                add(f)
        log.info("harvest_repo_done", extra={"repo": repo})


def main() -> None:
    import argparse
    import os
    import sys

    ap = argparse.ArgumentParser(description="Build the review-findings eval corpus (#595)")
    ap.add_argument("--owner", default="githumps")
    ap.add_argument("--repos", default=",".join(_DEFAULT_REPOS))
    ap.add_argument("--since", required=True, help="ISO date, e.g. 2026-06-01")
    ap.add_argument("--out", default="logs/review-findings.jsonl")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("GITHUB_TOKEN required", file=sys.stderr)
        raise SystemExit(2)
    if not (_SRC_A_LOGIN and _SRC_B_LOGIN):
        print("GRUG_HARVEST_SRC_A_LOGIN + GRUG_HARVEST_SRC_B_LOGIN required "
              "(reviewer accounts are runtime config; see module docstring)",
              file=sys.stderr)
        raise SystemExit(2)

    logging.basicConfig(level=logging.INFO)
    findings = harvest(args.owner, args.repos.split(","), token, args.since)
    with open(args.out, "w", encoding="utf-8") as fh:
        for f in findings:
            fh.write(json.dumps(asdict(f), ensure_ascii=False) + "\n")
    rows = [asdict(f) for f in findings]
    corpus = [r for r in rows if r["source"] in ("src-a", "src-b")]
    grug = [r for r in rows if r["source"] == "grug"]
    report = recall_report(corpus, grug)
    # Self-verifying corpus (#608 review): a manifest of the counts/baseline
    # is committed beside the JSONL so a test can assert the checked-in data
    # reproduces the published metrics.
    manifest = {
        "total_rows": len(rows),
        "by_source": {
            k: sum(1 for r in rows if r["source"] == k)
            for k in sorted({r["source"] for r in rows})
        },
        "recall_report": report,
    }
    with open(args.out.replace(".jsonl", ".manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
    print(f"wrote {len(rows)} findings -> {args.out} "
          f"(corpus={len(corpus)}, grug={len(grug)})")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
