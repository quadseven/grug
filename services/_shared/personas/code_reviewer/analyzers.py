"""Elder's inline OSS analyzers, matched by language (#1254, was #681).

lint.py proved the idea for one engine: a real analyzer over the changed
files finds the mechanical class the model skims past, and its findings
cannot be hallucinations because they are measured. This module turns that
into a bounded SET of engines, each matched to the languages it reads:

    engine     languages                    rules
    ruff       Python                       lint.py's selection (MIT)
    opengrep   JavaScript, TypeScript, JSX  grug's own elder_analyzer_rules/

THE SET IS CONFIGURED, NOT GROWN. `GRUG_ELDER_ANALYZERS` names the engines to
run, comma separated. Only names in `ANALYZERS` count, at most
`_MAX_ANALYZERS` of them, and an unknown name is logged and ignored rather
than run. Unset means none: the #707 lesson is that a new finding source must
be switched on per environment once its acceptance rate is watched, never
fleet-wide by default. The older `GRUG_LINT_EVIDENCE=ruff` switch still turns
ruff on.

EVERY ENGINE IS OPTIONAL. Each engine runs under its own timeout, and all of
them share one wall-clock budget per review. A missing binary, a timeout, a
non-zero exit, unreadable output or a spent budget is recorded as a SKIP with
its reason and the review goes on. An analyzer never blocks or fails a review.

THE REPO'S OWN CONFIG WINS. Ruff findings pass through the repo's ruff policy
(repo_ruff_policy.py). Opengrep runs with the repo's `.semgrepignore`, read
at the PR base, placed at the scan root, so paths the team ignored stay
ignored; a `// nosemgrep` comment in the file is honored by the engine.

READ-ONLY. Engines scan a temp copy of the changed files in a read-only tree,
with a scrubbed environment and grug's own rules - see analyzer_sandbox.py.

ATTRIBUTION AND DEDUP. Every analyzer marking names its engine and rule id.
`merge_analyzer_findings` drops an analyzer finding that lands on the same
file and line as one of the model's own findings, and notes the engine and
rule on the model's marking instead, so one issue is never posted twice.

WHY NOT sast.py. Same reason as lint.py: these are mechanical findings, not
security claims, so they skip the exploitability judge and publish as
advisory markings. Opengrep plumbing (byte budget, file staging, the HOME
cache) is reused from sast.py rather than copied.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from personas.code_reviewer.analyzer_sandbox import (
    AnalyzerSkip, read_only_tree, sandbox_env,
)
from personas.code_reviewer.complexity import _changed_by_file
from personas.code_reviewer.diff_parser import DiffHunk
from personas.code_reviewer.lint import scan_ruff
from personas.code_reviewer.persona import Finding
from personas.code_reviewer.repo_ruff_policy import RepoRuffPolicy
from personas.code_reviewer.sast import (
    _budget_files, _opengrep_home_env, _write_scan_files,
)
from review_types import Severity

log = logging.getLogger("grug.code_reviewer.analyzers")

_MAX_ANALYZERS = 4
# One review's total wall clock for all engines together. Each engine also has
# its own cap below; whichever is smaller wins.
_TOTAL_BUDGET_S = 90.0
# Below this much budget an engine is not started at all: a run that is sure
# to time out costs the review its latency and returns nothing.
_MIN_RUN_S = 5.0
_MAX_FINDINGS_PER_ENGINE = 20

_OPENGREP_TIMEOUT_S = 60.0
_OPENGREP_EXTENSIONS = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")
_RUFF_EXTENSIONS = (".py",)
# Webhook-owned like sast_rules/, resolved from the service working directory.
_ELDER_RULES_DIR = os.getenv(
    "GRUG_ELDER_RULES_DIR", os.path.join(os.getcwd(), "elder_analyzer_rules"),
)

# Same charset `code_review_prompt.ReviewRule` and dedup's marker accept, so an
# engine rule id round-trips through the hidden `grug-rule:` marker.
_RULE_CHARS = re.compile(r"[^A-Za-z0-9_-]+")

# Engine levels map to ADVISORY severities only: a mechanical marking never
# fails the check on its own (the same rule lint.py and complexity.py follow).
_LEVEL_SEVERITY: dict[str, Severity] = {
    "ERROR": "medium", "WARNING": "medium", "INFO": "low",
}


@dataclass(frozen=True)
class RepoAnalyzerConfig:
    """The repo's own analyzer settings, read at the PR base revision."""

    ruff_policy: RepoRuffPolicy | None = None
    semgrepignore: str | None = None


@dataclass(frozen=True)
class AnalyzerRun:
    """What one review's analyzers did. `skipped` holds (engine, reason) for
    every engine that was configured and had files to read but produced
    nothing usable; an engine with no matching files is in neither list."""

    findings: tuple[Finding, ...] = ()
    ran: tuple[str, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()
    policy_dropped: int = 0


@dataclass
class _Context:
    hunks: tuple[DiffHunk, ...]
    files: dict[str, str]
    repo: RepoAnalyzerConfig
    timeout_s: float
    policy_dropped: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class AnalyzerSpec:
    engine: str
    extensions: tuple[str, ...]
    binary: str
    run: Callable[[_Context], tuple[Finding, ...]]
    timeout_s: float


def _run_ruff(ctx: _Context) -> tuple[Finding, ...]:
    return scan_ruff(
        ctx.hunks, ctx.files, ctx.repo.ruff_policy, ctx.policy_dropped,
        enabled=True, timeout_s=ctx.timeout_s, strict=True,
    )


def _run_opengrep(ctx: _Context) -> tuple[Finding, ...]:
    if not os.path.isdir(_ELDER_RULES_DIR):
        raise AnalyzerSkip("rules-missing")
    kept, over = _budget_files(ctx.files)
    if over:
        log.info("elder_analyzer_files_over_budget",
                 extra={"engine": "opengrep", "skipped": len(over)})
    if not kept:
        raise AnalyzerSkip("over-budget")
    with tempfile.TemporaryDirectory(prefix="grug-analyzer-") as tmp:
        scan_root = os.path.join(tmp, "scan")
        os.makedirs(scan_root)
        _write_scan_files(scan_root, kept)
        if ctx.repo.semgrepignore is not None:
            with open(os.path.join(scan_root, ".semgrepignore"), "w",
                      encoding="utf-8") as fh:
                fh.write(ctx.repo.semgrepignore)
        home = _opengrep_home_env(os.path.join(tmp, "home"))
        os.makedirs(home["HOME"], exist_ok=True)
        with read_only_tree(scan_root):
            proc = subprocess.run(
                ["opengrep", "scan", "--config", _ELDER_RULES_DIR, "--json",
                 "--quiet", "--disable-version-check", "--no-rewrite-rule-ids",
                 "."],
                cwd=scan_root, env=sandbox_env(home["HOME"], home),
                capture_output=True, text=True, timeout=ctx.timeout_s,
                check=False,
            )
    if proc.returncode != 0:
        log.warning("elder_analyzer_engine_failed", extra={
            "engine": "opengrep", "returncode": proc.returncode,
            "stderr": (proc.stderr or "")[-2000:],
        })
        raise AnalyzerSkip("error")
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise AnalyzerSkip("unparseable") from exc
    return map_opengrep_results(data, _changed_by_file(ctx.hunks))


ANALYZERS: dict[str, AnalyzerSpec] = {
    "ruff": AnalyzerSpec("ruff", _RUFF_EXTENSIONS, "ruff", _run_ruff, 30.0),
    "opengrep": AnalyzerSpec(
        "opengrep", _OPENGREP_EXTENSIONS, "opengrep", _run_opengrep,
        _OPENGREP_TIMEOUT_S,
    ),
}


def configured_engines(raw: str | None = None, legacy_lint: str | None = None) -> tuple[str, ...]:
    """The engines to run, in the order named, bounded to the registry.

    Defaults read `GRUG_ELDER_ANALYZERS` and the older `GRUG_LINT_EVIDENCE`."""
    raw = os.getenv("GRUG_ELDER_ANALYZERS", "") if raw is None else raw
    legacy = os.getenv("GRUG_LINT_EVIDENCE", "") if legacy_lint is None else legacy_lint
    names = [part.strip().lower() for part in raw.split(",") if part.strip()]
    if legacy.strip().lower() == "ruff":
        names.append("ruff")
    engines: list[str] = []
    for name in names:
        if name not in ANALYZERS:
            log.warning("elder_analyzer_unknown", extra={"engine": name})
            continue
        if name not in engines:
            engines.append(name)
    if len(engines) > _MAX_ANALYZERS:
        log.warning("elder_analyzer_set_capped",
                    extra={"configured": len(engines), "cap": _MAX_ANALYZERS})
    return tuple(engines[:_MAX_ANALYZERS])


def run_analyzers(
    hunks: tuple[DiffHunk, ...],
    file_contents: dict[str, str],
    engines: tuple[str, ...],
    repo: RepoAnalyzerConfig | None = None,
    *,
    budget_s: float = _TOTAL_BUDGET_S,
    clock: Callable[[], float] = time.monotonic,
) -> AnalyzerRun:
    """Run each configured engine over the changed files in its languages.

    Never raises: every failure becomes a skip with a reason."""
    repo = repo or RepoAnalyzerConfig()
    deadline = clock() + budget_s
    findings: list[Finding] = []
    ran: list[str] = []
    skipped: list[tuple[str, str]] = []
    dropped: list[int] = []
    for name in engines:
        spec = ANALYZERS.get(name)
        if spec is None:
            continue
        files = {path: text for path, text in file_contents.items()
                 if path.endswith(spec.extensions)}
        if not files:
            continue
        reason, found = _run_one(spec, hunks, files, repo, deadline - clock(), dropped)
        if reason is not None:
            skipped.append((name, reason))
            log.info("elder_analyzer_skipped", extra={"engine": name, "reason": reason})
            continue
        ran.append(name)
        findings.extend(found[:_MAX_FINDINGS_PER_ENGINE])
    return AnalyzerRun(tuple(findings), tuple(ran), tuple(skipped), sum(dropped))


def _run_one(
    spec: AnalyzerSpec, hunks: tuple[DiffHunk, ...], files: dict[str, str],
    repo: RepoAnalyzerConfig, remaining_s: float, dropped: list[int],
) -> tuple[str | None, tuple[Finding, ...]]:
    """(skip reason or None, findings) for one engine."""
    if remaining_s < _MIN_RUN_S:
        return "budget", ()
    if shutil.which(spec.binary) is None:
        return "missing", ()
    ctx = _Context(hunks, files, repo, min(spec.timeout_s, remaining_s), dropped)
    try:
        return None, spec.run(ctx)
    except AnalyzerSkip as skip:
        return str(skip), ()
    except subprocess.TimeoutExpired:
        return "timeout", ()
    except Exception:  # noqa: BLE001 - an analyzer must never break a review
        log.warning("elder_analyzer_crashed", extra={"engine": spec.engine},
                    exc_info=True)
        return "error", ()


def map_opengrep_results(
    data: object, added: dict[str, set[int]],
) -> tuple[Finding, ...]:
    """Opengrep JSON to diff-anchored, attributed Findings. A result missing
    a field is skipped, never fatal."""
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return ()
    findings: list[Finding] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        path = str(r.get("path") or "").removeprefix("./")
        start = r.get("start") or {}
        line = start.get("line") if isinstance(start, dict) else None
        rule_id = str(r.get("check_id") or "")
        extra = r.get("extra") or {}
        if not isinstance(extra, dict):
            continue
        message = " ".join(str(extra.get("message") or "").split())
        if not path or not isinstance(line, int) or not rule_id or not message:
            continue
        if line not in added.get(path, set()):
            continue
        findings.append(_opengrep_finding(path, line, rule_id, message,
                                          str(extra.get("severity") or "")))
    return tuple(findings)


def _opengrep_finding(path: str, line: int, rule_id: str, message: str,
                      level: str) -> Finding:
    rule = _RULE_CHARS.sub("-", rule_id).strip("-") or "analyzer"
    return Finding(
        file=path,
        line=line,
        severity=_LEVEL_SEVERITY.get(level.upper(), "medium"),
        rule_name=rule,
        message=(
            f"opengrep `{rule}`: {message} Deterministic finding - MEASURED "
            f"by opengrep (LGPL-2.1) against grug's own rule pack, not "
            f"generated by a model. Silence it with `// nosemgrep: {rule}` "
            f"and a reason if it is wrong here."
        ),
        suggestion=None,
    )


def _engine_label(finding: Finding) -> str:
    """'ruff `S608`' or 'opengrep `elder-js-dynamic-eval`' from the message
    prefix every analyzer finding carries."""
    head = finding.message.split(":", 1)[0].strip()
    return head or finding.rule_name


def merge_analyzer_findings(
    existing: tuple[Finding, ...], analyzer: tuple[Finding, ...],
) -> tuple[tuple[Finding, ...], tuple[Finding, ...]]:
    """(existing with corroboration notes, analyzer findings to add).

    An analyzer finding on the same file and line as one of the MODEL's own
    findings (one that carries model origins) is the same marking twice: it
    is dropped, and the model's marking gains a line naming the engine and
    rule that measured it. Findings from other deterministic sources are not
    model findings and do not absorb analyzer findings. Analyzer findings
    are also deduped among themselves on (file, line, rule)."""
    model_at: dict[tuple[str, int], int] = {
        (f.file, f.line): i for i, f in enumerate(existing) if f.origins
    }
    notes: dict[int, list[str]] = {}
    seen: set[tuple[str, int, str]] = set()
    added: list[Finding] = []
    for f in analyzer:
        key = (f.file, f.line, f.rule_name)
        if key in seen:
            continue
        seen.add(key)
        index = model_at.get((f.file, f.line))
        if index is None:
            added.append(f)
            continue
        label = _engine_label(f)
        if label not in notes.setdefault(index, []):
            notes[index].append(label)
    merged = tuple(
        replace(f, message=f"{f.message}\n\nAlso measured by {', '.join(notes[i])}.")
        if i in notes else f
        for i, f in enumerate(existing)
    )
    return merged, tuple(added)
