#!/usr/bin/env python3
"""Grounding attester for spec 0012.Landing.

Proves a NECESSARY condition for the bool:

  - `install_url_uses_canonical_grug_tribe_slug_per_app_settings`

Asserts that every `<a href*='installations/new'>` anchor in
`web/public/*.html` points at the canonical GitHub App slug
`grug-tribe` (see reference_grug_tribe_app_canonical_urls memory).

Bare `grug` slug 404s — the slug comes from the App registration's
`Public link` field, NOT the repo name. Live audit on 2026-05-24
caught 5 occurrences across Grug.html + Terms.html using the broken
slug.

Stdlib only (html.parser). No third-party deps.
"""
from __future__ import annotations

import sys
from html.parser import HTMLParser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PUBLIC_DIR = REPO_ROOT / "web" / "public"
CANONICAL_SLUG = "grug-tribe"


class _InstallAnchorCollector(HTMLParser):
    """Collect every `<a href>` whose href contains 'installations/new'."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href" and value and "installations/new" in value:
                self.hrefs.append(value)


def _install_hrefs(html_path: Path) -> list[str]:
    parser = _InstallAnchorCollector()
    parser.feed(html_path.read_text(encoding="utf-8"))
    return parser.hrefs


def main() -> int:
    if not PUBLIC_DIR.is_dir():
        print(f"FAIL: {PUBLIC_DIR} missing — landing source moved?")
        return 1

    html_files = sorted(PUBLIC_DIR.glob("*.html"))
    if not html_files:
        print(f"FAIL: no *.html files under {PUBLIC_DIR}")
        return 1

    failures: list[str] = []
    total_anchors = 0

    for html in html_files:
        for href in _install_hrefs(html):
            total_anchors += 1
            if f"/apps/{CANONICAL_SLUG}/" not in href:
                failures.append(
                    f"{html.relative_to(REPO_ROOT)}: href={href!r} "
                    f"does NOT contain `/apps/{CANONICAL_SLUG}/` — "
                    f"spec 0012 install_url_uses_canonical_grug_tribe_slug_per_app_settings"
                )

    if total_anchors == 0:
        print("FAIL: zero install anchors found across web/public/*.html — landing CTAs removed?")
        return 1

    if failures:
        print(f"FAIL: install URL slug drift ({len(failures)}/{total_anchors} anchors):")
        for f in failures:
            print(f"  - {f}")
        return 1

    print(f"OK: {total_anchors} install anchor(s) across {len(html_files)} HTML file(s) — all point at /apps/{CANONICAL_SLUG}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
