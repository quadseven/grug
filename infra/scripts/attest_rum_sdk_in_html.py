#!/usr/bin/env python3
"""Grounding attester for spec 0013.RumInstrumentation.

Proves NECESSARY conditions for the bools:

  - `rum_sdk_loaded_on_react_spa_entry_per_observability_intent`
  - `rum_sdk_loaded_on_static_landing_entries_per_observability_intent`
  - `rum_service_tag_is_grug_web_canonical_per_dd_naming_canon`

Two checks:

  1. SPA path (web/app.html → web/src/main.tsx → web/src/rum.ts):
     - main.tsx imports `initRum` from `./rum` AND calls `initRum()`
       BEFORE `ReactDOM.createRoot().render()`.
     - rum.ts imports `datadogRum` from `@datadog/browser-rum`.
     - rum.ts calls `datadogRum.init({...service: 'grug-web', ...})`.

  2. Static path (web/public/{index,Privacy,Terms}.html):
     - Each file contains the DD RUM CDN snippet (matched via the
       canonical CDN URL substring `datadoghq-browser-agent.com`).
     - Each contains `service: 'grug-web'` (single-quoted in the
       snippet — strict match catches drift).
     - Each contains all four placeholders so build-time substitution
       has a target to replace (`__DD_RUM_APPLICATION_ID__`,
       `__DD_RUM_CLIENT_TOKEN__`, `__DD_RUM_ENV__`, `__DD_RUM_VERSION__`).
     - `package.json` lists `@datadog/browser-rum` as a dependency.

Stdlib only (re + html.parser). No third-party deps.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = REPO_ROOT / "web"
SRC_DIR = WEB_DIR / "src"
PUBLIC_DIR = WEB_DIR / "public"
PKG_JSON = WEB_DIR / "package.json"

CANONICAL_SERVICE = "grug-web"
CDN_URL_SUBSTRING = "datadoghq-browser-agent.com"
REQUIRED_PLACEHOLDERS = [
    "__DD_RUM_APPLICATION_ID__",
    "__DD_RUM_CLIENT_TOKEN__",
    "__DD_RUM_ENV__",
    "__DD_RUM_VERSION__",
]
STATIC_HTMLS = ("index.html", "Privacy.html", "Terms.html")


def _attest_spa_chain(failures: list[str]) -> None:
    main_tsx = SRC_DIR / "main.tsx"
    rum_ts = SRC_DIR / "rum.ts"

    if not main_tsx.exists():
        failures.append("web/src/main.tsx missing")
        return
    if not rum_ts.exists():
        failures.append("web/src/rum.ts missing — SPA RUM init module absent")
        return

    main_src = main_tsx.read_text()
    rum_src = rum_ts.read_text()

    # main.tsx imports initRum from ./rum
    if not re.search(r"import\s+\{\s*initRum\s*\}\s+from\s+['\"]\./rum['\"]", main_src):
        failures.append(
            "web/src/main.tsx: missing `import { initRum } from './rum'` — RUM module not wired in"
        )

    # main.tsx calls initRum() — before the render. We can't easily AST
    # this for TypeScript without a TS parser, so verify both ordering
    # hints: initRum() appears AND it appears before createRoot.
    init_match = re.search(r"\binitRum\s*\(\s*\)", main_src)
    render_match = re.search(r"\bcreateRoot\b", main_src)
    if not init_match:
        failures.append(
            "web/src/main.tsx: `initRum()` is never called — module imported but init skipped"
        )
    elif render_match and init_match.start() > render_match.start():
        failures.append(
            "web/src/main.tsx: `initRum()` called AFTER createRoot — must run "
            "BEFORE so RUM captures initial view + cold-start errors"
        )

    # rum.ts imports @datadog/browser-rum
    if not re.search(
        r"import\s+\{[^}]*datadogRum[^}]*\}\s+from\s+['\"]@datadog/browser-rum['\"]",
        rum_src,
    ):
        failures.append(
            "web/src/rum.ts: missing `import { datadogRum } from '@datadog/browser-rum'`"
        )

    # rum.ts calls datadogRum.init({...})
    if not re.search(r"datadogRum\.init\s*\(", rum_src):
        failures.append("web/src/rum.ts: no `datadogRum.init(...)` call found")

    # service: "grug-web" (double OR single quotes)
    if not re.search(rf"""service:\s*["']{re.escape(CANONICAL_SERVICE)}["']""", rum_src):
        failures.append(
            f"web/src/rum.ts: missing `service: \"{CANONICAL_SERVICE}\"` in datadogRum.init(...) — "
            f"spec 0013 rum_service_tag_is_grug_web_canonical_per_dd_naming_canon"
        )

    # package.json lists the dep
    if not PKG_JSON.exists():
        failures.append("web/package.json missing")
        return
    pkg = json.loads(PKG_JSON.read_text())
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    if "@datadog/browser-rum" not in deps:
        failures.append(
            "web/package.json: `@datadog/browser-rum` not listed as dependency — "
            "rum.ts import won't resolve at build"
        )


def _attest_static_snippets(failures: list[str]) -> None:
    for name in STATIC_HTMLS:
        path = PUBLIC_DIR / name
        if not path.exists():
            failures.append(f"web/public/{name} missing")
            continue
        body = path.read_text()

        if CDN_URL_SUBSTRING not in body:
            failures.append(
                f"web/public/{name}: missing DD RUM CDN script "
                f"(no reference to `{CDN_URL_SUBSTRING}`)"
            )
            # If the CDN snippet itself is missing, the placeholder/service
            # checks below would be redundant. Continue to next file.
            continue

        # Strict service-tag check (single-quoted in the CDN snippet)
        if not re.search(rf"""service:\s*['"]{re.escape(CANONICAL_SERVICE)}['"]""", body):
            failures.append(
                f"web/public/{name}: missing `service: '{CANONICAL_SERVICE}'` in CDN snippet"
            )

        # All four placeholders must be present so the build-time sed
        # has a target. If any is missing the substitution silently
        # leaves the others — partial init is worse than no init.
        for ph in REQUIRED_PLACEHOLDERS:
            if ph not in body:
                failures.append(
                    f"web/public/{name}: missing placeholder `{ph}` — "
                    f"build-time substitution would leave the field unfilled"
                )


def main() -> int:
    failures: list[str] = []
    _attest_spa_chain(failures)
    _attest_static_snippets(failures)

    if failures:
        print(f"FAIL: RUM SDK loading drift ({len(failures)} issues):")
        for f in failures:
            print(f"  - {f}")
        return 1

    print(
        f"OK: SPA chain (main.tsx → rum.ts → @datadog/browser-rum) intact + "
        f"3 static HTML files all carry the CDN snippet with "
        f"service:'{CANONICAL_SERVICE}' and all 4 placeholders"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
