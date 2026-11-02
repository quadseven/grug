"""Structured-logging guard (2026-07-24 audit): a bare `logging.basicConfig(`
call in a deployed service's entrypoint emits Python's default unparseable
text format ("LEVEL:logger:message"), which DD's log pipeline cannot parse
a severity out of - it defaults EVERY line, including plain INFO, to
status:error. Found live in `consumer.py` (39097/39097 of grug-consumer's
7-day log volume misclassified) and the identical bug in
`personas/smasher/trial_janitor.py` - both fixed to call
`observability.configure_logging()` instead, the same JSON formatter
webhook/api/poller already use.

Same scan-source-tree shape as test_log_pii_guard.py.
"""

from __future__ import annotations

import re
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent  # services/webhook/

_BASIC_CONFIG_RE = re.compile(r"\blogging\.basicConfig\(")

# Files allowed to call basicConfig() directly, with the reason each is safe:
# - one-off developer CLI tools (sast_benchmark, elder_eval, review_latency
#   __main__.py entrypoints, and elder_eval/missed_classes.py) are run locally/in CI for ad hoc analysis, never
#   deployed as a long-running k8s workload DD scrapes for status-based
#   alerting.
# - smasher's trial_worker/trial_deps/trial_fetch run INSIDE the locked-down,
#   ephemeral sandbox Job (#469, ADR-0013) on the BYON node class, where DD
#   stdout-scraping is a known-unreliable channel (kubelet:10250 i/o
#   timeout) - the real output channel is the pod termination message, not
#   stdout, so fixing their log format wouldn't fix anything DD-side.
_ALLOWLIST_RELATIVE = (
    "sast_benchmark/__main__.py",
    "elder_eval/__main__.py",
    "elder_eval/missed_classes.py",
    "elder_eval/column_type_mismatch.py",
    "review_latency/__main__.py",
    "personas/smasher/trial_worker.py",
    "personas/smasher/trial_deps.py",
    "personas/smasher/trial_fetch.py",
    "tests/",
    "__pycache__",
    ".pyc",
)


def _calls_basic_config(text: str) -> bool:
    """True if some non-comment line actually CALLS basicConfig - a comment
    mentioning the pattern in prose (as this guard's own file, and the
    fixed files' audit notes, both do) is not a call."""
    return any(
        _BASIC_CONFIG_RE.search(line)
        for line in text.splitlines()
        if not line.lstrip().startswith("#")
    )


def _candidate_files() -> list[Path]:
    shared_root = SERVICE_DIR.parent / "_shared"
    out: list[Path] = []
    for root in (SERVICE_DIR, shared_root):
        for path in root.rglob("*.py"):
            rel = path.relative_to(root).as_posix()
            if any(skip in rel for skip in _ALLOWLIST_RELATIVE):
                continue
            out.append(path)
    return out


def test_no_bare_basicconfig_in_deployed_entrypoints():
    findings: list[str] = []
    for path in _candidate_files():
        if _calls_basic_config(path.read_text()):
            findings.append(str(path.relative_to(SERVICE_DIR.parent.parent)))
    assert not findings, (
        "Bare logging.basicConfig() call found in a deployed entrypoint - "
        "DD's log pipeline can't parse a severity out of its default text "
        "format and defaults every line to status:error. Use "
        "observability.configure_logging() instead (same JSON formatter "
        "webhook/api/poller/consumer already use), or add the file to "
        "_ALLOWLIST_RELATIVE above with a reason if it genuinely never "
        "reaches DD's log pipeline:\n  " + "\n  ".join(findings)
    )


def test_consumer_and_trial_janitor_use_configure_logging():
    """Direct regression pin for the two files this audit actually fixed -
    the scan test above is the general guard, this is a named check that
    survives even if the scan's allowlist logic is ever refactored."""
    for rel in ("consumer.py", "../_shared/personas/smasher/trial_janitor.py"):
        text = (SERVICE_DIR / rel).read_text()
        assert "configure_logging()" in text, rel
        assert not _calls_basic_config(text), rel
