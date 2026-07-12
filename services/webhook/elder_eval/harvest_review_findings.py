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
    # Search from the END of the header so a bold string in any preamble
    # (quoted text, prior-section chatter) can never masquerade as the title
    # (review finding on #608 - corrupted `finding` text in the corpus).
    title_m = _INLINE_TITLE_RE.search(body, header.end())
    if title_m:
        finding = title_m.group("title")
    else:
        after = body[header.end():].strip()
        finding = after.splitlines()[0] if after else body.splitlines()[0]
    finding = finding.strip()
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
        finding=finding[:300],
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
                finding=title[:300],
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
    first_line = next((ln for ln in body.splitlines() if ln.strip()), "")
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
        finding=first_line[:300],
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

    def elder_has(repo: str, pr: int, path: str, line: int | None) -> bool:
        lines = elder_index.get((repo, pr, path))
        if lines is None:
            return False
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
    print(f"wrote {len(rows)} findings -> {args.out} "
          f"(corpus={len(corpus)}, grug={len(grug)})")
    print(json.dumps(recall_report(corpus, grug), indent=2))


if __name__ == "__main__":
    main()
