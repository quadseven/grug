"""Measure whether a repo guideline changes Elder's borderline finding.

Run with ``python -m elder_eval.guideline_effect`` against configured bench
backends. ``--record`` stores live outcomes; ``--check`` requires the two
arms to differ and match the recorded baseline.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

import httpx
from llm_client import Hunk, _build_messages, _parse_review
from review_evidence import assemble_evidence
from sast_benchmark.backends import BenchBackend, configured_backends
from sast_benchmark.runner import _post

BASELINE_PATH = Path(__file__).parent / "guideline_effect_baseline.json"
PATH = "sample.py"
GUIDELINE = "TRIBE'S OWN CARVINGS: Never use a bare except clause."
DIFF = """@@ -1,2 +1,4 @@
+def load_value(loader):
+    try:
+        return loader()
+    except:
+        return None
"""


def _review(backend: BenchBackend, *, guided: bool,
            post: Callable = _post) -> tuple[bool, bool]:
    evidence = assemble_evidence([Hunk(path=PATH, body=DIFF)])
    messages = _build_messages(
        evidence, "v2", guidelines=GUIDELINE if guided else "",
    )
    try:
        response = post(backend, messages)
    except (httpx.RequestError, httpx.TimeoutException):
        return False, True
    parsed = _parse_review(response)
    if parsed.error and not parsed.findings:
        return False, True
    found = any(
        finding.path == PATH and finding.line in (4, 5)
        and ("except" in finding.message.lower() or "exception" in finding.message.lower())
        for finding in parsed.findings
    )
    return found, False


def _load_baseline() -> dict:
    return json.loads(BASELINE_PATH.read_text())


def _check(name: str, result: dict[str, bool], baseline: dict) -> bool:
    if result["without_guideline"] == result["with_guideline"]:
        return False
    return baseline.get("measurements", {}).get(name) == result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="elder_eval.guideline_effect")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--record", action="store_true")
    group.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    backends = configured_backends()
    if not backends:
        print("No backends configured.", file=sys.stderr)
        return 2
    baseline = _load_baseline()
    status = 0
    for backend in backends:
        without, error_a = _review(backend, guided=False)
        with_guideline, error_b = _review(backend, guided=True)
        if error_a or error_b:
            print(f"{backend.name}: replay errored", file=sys.stderr)
            status = 2
            continue
        result = {"without_guideline": without, "with_guideline": with_guideline}
        print(f"{backend.name}: {result}")
        if args.record:
            baseline.setdefault("measurements", {})[backend.name] = result
            baseline["models"] = baseline.get("models", {}) | {backend.name: backend.model}
            BASELINE_PATH.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n")
        elif args.check and not _check(backend.name, result, baseline):
            print(f"{backend.name}: guideline effect differs from baseline", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(main())
