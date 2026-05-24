#!/usr/bin/env python3
"""Grounding attester for spec 0012.Landing.

Proves NECESSARY conditions for the bools:

  - `primary_ctas_never_use_mailto_protocol_per_design_intent`
  - `signin_button_points_to_react_signin_route_per_design_intent`

Rules:

  1. No anchor whose class contains `btn primary` or `btn pick` may
     have `href^=mailto:`. Primary CTAs are conversion points; a
     mailto: opens the OS mail client and abandons the funnel.

  2. The Sign in button (`<a>` with class `btn` followed by text
     containing 'Sign in', NOT `btn primary` / `btn pick`) MUST
     point at `/signin` (the React route). The legacy design
     handoff wired this to `mailto:grug@grug.lol?subject=Sign in`.

Allowed mailto exceptions (rule 1):
  - Org tier sales contact: `<a class="btn ghost pick" href="mailto:...">Talk to Grug</a>`
    Sales CTAs ARE allowed mailto: at this stage.
  - Footer "Questions?" / Contact links: `<a href="mailto:...">grug@grug.lol</a>`
    No `btn` class. Bare anchor mailto is fine.
  - Privacy DSAR contact: inline `<a href="mailto:...">grug@grug.lol</a>` in
    privacy-policy prose. Required by GDPR/CCPA for data subject requests.

Stdlib only (html.parser). No third-party deps.
"""
from __future__ import annotations

import sys
from html.parser import HTMLParser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PUBLIC_DIR = REPO_ROOT / "web" / "public"

# Whitelisted mailto on `btn`-class anchors. Each entry is a (file, text) pair.
ALLOWED_MAILTO_BUTTONS: set[tuple[str, str]] = {
    # Org pricing tier — sales contact is a legitimate first-touch flow.
    ("Grug.html", "Talk to Grug"),
    ("index.html", "Talk to Grug"),
}


class _AnchorCollector(HTMLParser):
    """Collect every `<a>` element with (href, class, inner_text)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[tuple[str, str, str]] = []
        self._current: list[tuple[str, str]] | None = None  # (href, class)
        self._text_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = ""
        cls = ""
        for name, value in attrs:
            if name == "href":
                href = value or ""
            elif name == "class":
                cls = value or ""
        self._current = [(href, cls)]
        self._text_buf = []

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._text_buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._current is None:
            return
        href, cls = self._current[0]
        text = " ".join("".join(self._text_buf).split())
        self.anchors.append((href, cls, text))
        self._current = None
        self._text_buf = []


def _collect(html_path: Path) -> list[tuple[str, str, str]]:
    parser = _AnchorCollector()
    parser.feed(html_path.read_text(encoding="utf-8"))
    return parser.anchors


def _has_class(cls: str, needle: str) -> bool:
    """True iff every word in `needle` is present in `cls` (order-free)."""
    cls_words = set(cls.split())
    return all(word in cls_words for word in needle.split())


def _normalize_button_text(text: str) -> str:
    """Strip whitespace + trailing arrow glyphs so whitelist matches are
    resilient to design tweaks like `Talk to Grug` vs `Talk to Grug →`."""
    cleaned = text.strip()
    while cleaned and cleaned[-1] in "→←➔➜>":
        cleaned = cleaned[:-1].rstrip()
    return cleaned


def main() -> int:
    if not PUBLIC_DIR.is_dir():
        print(f"FAIL: {PUBLIC_DIR} missing")
        return 1

    html_files = sorted(PUBLIC_DIR.glob("*.html"))
    if not html_files:
        print(f"FAIL: no *.html files under {PUBLIC_DIR}")
        return 1

    failures: list[str] = []
    signin_seen = False
    total_btn_primary = 0

    for html in html_files:
        rel = html.relative_to(PUBLIC_DIR).as_posix()
        for href, cls, text in _collect(html):
            # Rule 1: no mailto: on btn primary / btn pick.
            is_primary = _has_class(cls, "btn primary") or _has_class(cls, "btn pick")
            if is_primary:
                total_btn_primary += 1
                if href.startswith("mailto:"):
                    if (rel, _normalize_button_text(text)) in ALLOWED_MAILTO_BUTTONS:
                        continue
                    failures.append(
                        f"{rel}: primary CTA `<a class={cls!r}>{text}</a>` "
                        f"uses mailto: href — spec 0012 primary_ctas_never_use_mailto_protocol_per_design_intent"
                    )

            # Rule 2: Sign in button MUST be /signin.
            if text.strip().lower() == "sign in" and _has_class(cls, "btn"):
                signin_seen = True
                if href != "/signin":
                    failures.append(
                        f"{rel}: Sign in button href={href!r}, expected '/signin' — "
                        f"spec 0012 signin_button_points_to_react_signin_route_per_design_intent"
                    )

    if total_btn_primary == 0:
        print("FAIL: zero `btn primary` / `btn pick` anchors found — landing CTAs removed?")
        return 1

    if not signin_seen:
        # Sign in button is present in the nav of the landing page (index.html
        # post-rename). Absence here means the design regressed; flag it.
        failures.append(
            "no `<a class=\"btn ...\">Sign in</a>` found across landing HTML — "
            "spec 0012 expects nav-bar Sign in button"
        )

    if failures:
        print(f"FAIL: CTA routing drift ({len(failures)} issues):")
        for f in failures:
            print(f"  - {f}")
        return 1

    print(
        f"OK: {total_btn_primary} primary/pick CTAs scanned across "
        f"{len(html_files)} HTML file(s); Sign in button routes to /signin; "
        f"{len(ALLOWED_MAILTO_BUTTONS)} mailto exception(s) honored"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
