# MIRRORED — sibling at services/api/personas/code_reviewer/sca.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
"""SCA (dependency-CVE) detection for Elder (#434, ADR-0007 Track 1).

Flags dependencies a PR INTRODUCES or bumps-to that have known CVEs by querying
the free public OSV advisory database (the same data the `pip-audit` OSS tool
uses). Diff-scoped (only deps on added manifest lines, like the SAST engine
flags only added code) and bounded. Produces the SAME `Candidate` shape the
SAST pipeline uses, so the exploitability judge (`sast.judge_candidates`) and
the publish path are reused unchanged - SCA is just a new candidate SOURCE.

Engine note: we query the OSV batch API directly over httpx rather than shelling
out to the pip-audit CLI - pip-audit's requirements-audit mode spins up a venv +
ensurepip, which fails on the pod's readOnlyRootFilesystem (proven: SIGABRT). A
direct OSV query is the SAME advisory data, pod-safe, one HTTPS call over the
already-allowed 443 egress - free, no key, no SaaS spend.

Python-first (this slice): parses pinned `name==version` deps from
requirements-style manifests + pyproject. Multi-ecosystem is a follow-up behind
the same boundary. Best-effort: an OSV-unreachable / parse failure returns () +
logs (additive; never breaks the review).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass

import httpx

from .diff_parser import DiffHunk
from .sast import Candidate

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.code_reviewer.sca")

VULNERABLE_DEPENDENCY = "vulnerable-dependency"

# Manifest files whose added `name==version` lines we audit. Python-first.
_MANIFEST_RE = re.compile(r"(^|/)(requirements[^/]*\.txt|pyproject\.toml|setup\.cfg|constraints[^/]*\.txt)$")
# A PINNED dependency spec on an added line: `name==1.2.3` (extras/markers
# tolerated). Only `==` is deterministically auditable; unpinned bumps are a
# follow-up (which version is live is ambiguous).
_PINNED_DEP_RE = re.compile(
    r'^[\'"]?(?P<name>[A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_,.-]+\])?\s*==\s*(?P<version>[A-Za-z0-9_.!+-]+)'
)
# AC5 cost bound: cap how many changed deps we audit per review.
_MAX_DEPS = 100
_OSV_BATCH_URL = "https://api.osv.dev/v1/querybatch"
_OSV_TIMEOUT_S = 20


@dataclass(frozen=True, slots=True)
class ChangedDep:
    """A pinned dependency on an added manifest line. `(file, line)` anchor the
    finding to the diff; `name`/`version` feed the audit."""

    file: str
    line: int
    name: str
    version: str


def _added_lines(hunk: DiffHunk) -> list[tuple[int, str]]:
    """[(new_side_line_number, added_text)] for added lines in a hunk."""
    out: list[tuple[int, str]] = []
    lineno = hunk.new_start
    for raw in hunk.body.splitlines():
        if raw.startswith("@@") or raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("+"):
            out.append((lineno, raw[1:]))
            lineno += 1
        elif raw.startswith("-"):
            continue
        else:
            lineno += 1
    return out


def extract_changed_deps(hunks: tuple[DiffHunk, ...]) -> tuple[ChangedDep, ...]:
    """Pure: pinned `name==version` deps on added lines of dependency manifests.
    Diff-scoped (only what the PR introduces/bumps) + deduped by (file, name,
    version). Capped at `_MAX_DEPS` (AC5)."""
    out: list[ChangedDep] = []
    seen: set[tuple[str, str, str]] = set()
    for hunk in hunks:
        if not _MANIFEST_RE.search(hunk.file_path):
            continue
        for lineno, text in _added_lines(hunk):
            m = _PINNED_DEP_RE.match(text.strip())
            if not m:
                continue
            name = m.group("name")
            version = m.group("version")
            key = (hunk.file_path, name.lower(), version)
            if key in seen:
                continue
            seen.add(key)
            out.append(ChangedDep(file=hunk.file_path, line=lineno, name=name, version=version))
    return tuple(out[:_MAX_DEPS])


def _audit(deps: tuple[ChangedDep, ...]) -> dict[tuple[str, str], list[str]]:
    """Query the OSV batch API for the changed deps; return {(name_lower,
    version): [advisory-id, ...]} for those with known vulns. ONE HTTPS call;
    OSV returns results in query order, so we zip back to deps. Best-effort: an
    OSV-unreachable / unparseable response -> {} + log (additive)."""
    if not deps:
        return {}
    queries = [
        {"package": {"name": d.name, "ecosystem": "PyPI"}, "version": d.version}
        for d in deps
    ]
    try:
        resp = httpx.post(_OSV_BATCH_URL, json={"queries": queries}, timeout=_OSV_TIMEOUT_S)
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except (httpx.HTTPError, json.JSONDecodeError, ValueError) as e:
        log.warning("sca_osv_query_failed", extra={"kind": type(e).__name__})
        return {}

    vulns_by_dep: dict[tuple[str, str], list[str]] = {}
    for dep, res in zip(deps, results):
        ids = [v.get("id", "") for v in ((res or {}).get("vulns") or []) if v.get("id")]
        if ids:
            vulns_by_dep[(dep.name.lower(), dep.version)] = ids
    return vulns_by_dep


def scan_dependencies(hunks: tuple[DiffHunk, ...]) -> tuple[Candidate, ...]:
    """SCA candidate source: a Candidate per changed dependency with a known
    CVE. Same `Candidate` shape the SAST pipeline judges + publishes, so the
    exploitability judge decides whether the vuln is actually a concern here.
    Best-effort + diff-anchored (line is the changed manifest line)."""
    deps = extract_changed_deps(hunks)
    if not deps:
        return ()
    vulns = _audit(deps)
    if not vulns:
        return ()
    candidates: list[Candidate] = []
    for d in deps:
        ids = vulns.get((d.name.lower(), d.version))
        if not ids:
            continue
        candidates.append(
            Candidate(
                vuln_class=VULNERABLE_DEPENDENCY,
                file=d.file,
                line=d.line,
                snippet=f"{d.name}=={d.version} (known advisories: {', '.join(ids)})",
            )
        )
    return tuple(candidates)
