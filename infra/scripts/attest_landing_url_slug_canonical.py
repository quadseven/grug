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

  4. `web/public/_redirects` MUST NOT contain a `200`-status rewrite
     whose destination is `*.html` AND whose source is `/`. CF Pages
     Pretty URLs canonicalizes `.html` away — the 200 rewrite is
     silently degraded to a 301 redirect that surfaces the
     destination filename in the address bar.

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
# We flag any line where source is "/" AND destination ends in ".html".
_ROOT_REWRITE_RE = re.compile(r"^\s*/\s+(\S+\.html)\s+(\d{3})\s*$")


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
        match = _ROOT_REWRITE_RE.match(line)
        if match:
            dest, status = match.group(1), match.group(2)
            failures.append(
                f"web/public/_redirects:{lineno}: `/ {dest} {status}` — "
                f"CF Pages Pretty URLs strips `.html` from rewrite targets that "
                f"resolve to real files, degrading the 200 to a 301 → /{dest[:-5]}"
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
