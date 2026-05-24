#!/usr/bin/env python3
"""Grounding attester for spec 0012.Landing.

Proves NECESSARY conditions for the bools:

  - `canonical_landing_owns_root_index_html_per_cf_pretty_urls`
  - `cross_doc_paths_use_absolute_root_routes_per_design_intent`

Rules:

  1. `web/public/index.html` MUST exist (it's the static landing).
     `web/public/Grug.html` MUST NOT exist — pre-rename name.

  2. `web/index.html` MUST NOT exist (Vite SPA entry moved to
     `web/app.html`). Existence of `web/index.html` indicates the
     pre-rename SPA-vs-landing collision is back; Vite would
     compete for `/index.html` output and CF Pages' Pretty URLs
     would 301-strip `/` → `/Grug` again.

  3. `web/app.html` MUST exist (renamed SPA entry).

  4. `web/public/_redirects` MUST NOT contain ANY `200`-status
     rewrite whose destination ends in `.html`. CF Pages Pretty URLs
     canonicalizes `*.html` → `*` via 308 even on rewrite
     destinations — silently converting our `200 rewrite`
     (URL-preserving) into a `308 redirect` (URL-changing) that
     surfaces the destination filename in the address bar.

     Originally written as "src=`/` AND dest=`*.html`". Broadened to
     "any source AND dest=`*.html`" after the first PR #157 deploy:
     a `/* /app.html 200` catchall hijacked `/` (matching before
     static-asset precedence) and the `.html`-strip on the dest
     produced a 308 → `/app`. The blast radius is the whole rewrite
     table, not just the root rule.

     Additionally rule out any rewrite whose SOURCE is `/*` (catchall)
     — even with an extension-less dest, `/*` matches `/` and steals
     it from `/index.html`'s static-asset precedence.

  5. Every `<a href>` in `web/public/{Privacy,Terms}.html` that
     navigates back to the landing MUST use an absolute root path
     (`/`, `/privacy`, `/terms`) — never relative `Grug.html`,
     `Privacy.html`, `Terms.html`. Relative links break after the
     `Grug.html` → `index.html` rename and surface as 404s.

Stdlib only (html.parser). No third-party deps.
"""
from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = REPO_ROOT / "web"
PUBLIC_DIR = WEB_DIR / "public"

# Filenames whose presence as a relative href indicates pre-rename drift.
LEGACY_RELATIVE_HREFS = {"Grug.html", "Privacy.html", "Terms.html"}

# _redirects line format: `<source> <destination> <status>` (whitespace-separated).
# Source and destination are non-whitespace tokens; status is 3 digits.
_REDIRECT_LINE_RE = re.compile(r"^\s*(\S+)\s+(\S+)\s+(\d{3})\s*$")


class _HrefCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href" and value is not None:
                self.hrefs.append(value)


def _collect_hrefs(html_path: Path) -> list[str]:
    parser = _HrefCollector()
    parser.feed(html_path.read_text(encoding="utf-8"))
    return parser.hrefs


def _check_filesystem(failures: list[str]) -> None:
    # Rule 1
    if not (PUBLIC_DIR / "index.html").is_file():
        failures.append("web/public/index.html missing — landing not at canonical root")
    if (PUBLIC_DIR / "Grug.html").is_file():
        failures.append("web/public/Grug.html exists — pre-rename file should be removed")

    # Rule 2 + 3
    if (WEB_DIR / "index.html").is_file():
        failures.append(
            "web/index.html exists — Vite SPA entry should be web/app.html "
            "(the collision triggers CF Pages Pretty URLs to 301 `/` → `/Grug`)"
        )
    if not (WEB_DIR / "app.html").is_file():
        failures.append("web/app.html missing — Vite SPA entry expected at this path")


def _check_redirects(failures: list[str]) -> None:
    # Rule 4
    redirects = PUBLIC_DIR / "_redirects"
    if not redirects.is_file():
        failures.append("web/public/_redirects missing")
        return
    for lineno, line in enumerate(redirects.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _REDIRECT_LINE_RE.match(line)
        if not match:
            continue
        src, dest, status = match.group(1), match.group(2), match.group(3)

        if status == "200" and dest.endswith(".html"):
            failures.append(
                f"web/public/_redirects:{lineno}: `{src} {dest} {status}` — "
                f"CF Pages Pretty URLs strips `.html` from 200-rewrite "
                f"destinations, converting the rewrite to a 308 → {dest[:-5]}. "
                f"Use `{dest[:-5]}` (no extension) instead."
            )

        if src == "/*":
            failures.append(
                f"web/public/_redirects:{lineno}: `/* {dest} {status}` "
                f"catchall matches `/` and hijacks the static `/index.html` "
                f"landing (matches before static-asset precedence). Drop the "
                f"catchall and enumerate SPA routes explicitly."
            )


def _check_cross_doc_links(failures: list[str]) -> None:
    # Rule 5
    for name in ("Privacy.html", "Terms.html"):
        html = PUBLIC_DIR / name
        if not html.is_file():
            failures.append(f"web/public/{name} missing")
            continue
        for href in _collect_hrefs(html):
            # Strip query/fragment for comparison.
            base = href.split("#", 1)[0].split("?", 1)[0]
            # Allow absolute URLs, anchor-only, root paths, mailto.
            if (
                base.startswith(("/", "http://", "https://", "mailto:"))
                or base == ""
                or base.startswith("#")
            ):
                continue
            if base in LEGACY_RELATIVE_HREFS or any(base.startswith(f) for f in LEGACY_RELATIVE_HREFS):
                failures.append(
                    f"web/public/{name}: <a href={href!r}> uses relative legacy path — "
                    f"use absolute `/`, `/privacy`, `/terms` instead"
                )


def main() -> int:
    failures: list[str] = []
    _check_filesystem(failures)
    _check_redirects(failures)
    _check_cross_doc_links(failures)

    if failures:
        print(f"FAIL: URL-slug canonical drift ({len(failures)} issues):")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("OK: landing owns /index.html, SPA at /app.html, _redirects clean of CF Pretty-URLs trap, cross-doc links use absolute paths")
    return 0


if __name__ == "__main__":
    sys.exit(main())
