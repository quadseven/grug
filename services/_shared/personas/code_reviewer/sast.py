"""SAST detection tracer for Elder — clear-text-secret-log (#400, ADR-0006).

The vendor-neutral RECALL layer: `scan_candidates` finds candidate vuln sites
in the diff deterministically (a builtin detector for ONE class here — the
heavy Semgrep OSS engine slots in behind this same boundary at #401). The
PRECISION layer (the LLM exploitability judge that keeps/suppresses each
candidate with a reason) lives in `sast_judge.py`; kept candidates become
ordinary `Finding`s that flow through the EXISTING anti-hallucination filter
(`evaluate_diff`) + advisory/blocking publish path — no parallel posting.

Design split (ADR-0006 "SAST recall + LLM precision"): the detector is
deliberately LIBERAL — it flags BOTH a real secret reaching a log sink AND the
#391 public-config-path log (logging an SSM PARAM NAME). Discriminating those
is the judge's job, not the detector's. A detector miss is unrecoverable
(no candidate -> no finding); a detector over-flag is recovered by the judge.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Optional

from llm_client import _cave_judge_config, FindingJudgement, Hunk, JudgeFindingRepr, PrContext, judge_findings
from review_evidence import assemble_evidence

from .diff_parser import DiffHunk
from .persona import Finding

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.code_reviewer.sast")

# The one class this tracer covers. Kept as a constant so the judge + tests +
# the (future) engine share one spelling.
CLEARTEXT_SECRET_LOG = "cleartext-secret-log"

# Committed-secret class (#436). Defined here (not in secret_scan) so the judge
# can special-case it without importing secret_scan, which would cycle
# (secret_scan imports Candidate from this module). secret_scan re-imports it.
EXPOSED_SECRET = "exposed-secret"

# IaC misconfiguration class (#447). Defined here (not in iac_scan) for the same
# no-cycle reason as EXPOSED_SECRET; iac_scan re-imports it.
IAC_MISCONFIG = "iac-misconfig"

# A logging / print SINK on an added line. Covers the stdlib + common logger
# idioms (`logging.info`, `log.warning`, `logger.debug`, `self.log.error`) and
# bare `print(`. Deliberately broad — the judge filters non-exploitable hits.
_SINK_RE = re.compile(
    r"(?:\b(?:logging|log|logger|_log|self\.log|LOG)\s*\.\s*"
    r"(?:debug|info|warning|warn|error|exception|critical)\s*\()"
    r"|(?:\bprint\s*\()"
)

# A SECRET-ish token: a name/keyword that suggests a credential is in scope on
# the line. Matches identifiers AND substrings inside an f-string/format. The
# judge decides whether the secret VALUE (vs a public name/path) actually
# reaches the sink.
_SECRET_TOKEN_RE = re.compile(
    r"(?i)(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|"
    r"secret[_-]?key|private[_-]?key|credential|client[_-]?secret|auth[_-]?token)"
)


@dataclass(frozen=True, slots=True)
class Candidate:
    """One detected candidate vuln site, pre-judgement. `(file, line)` map to a
    real added diff line (so a kept candidate passes the anti-hallucination
    filter). `snippet` is the offending added line (for the judge + the finding
    message). `vuln_class` groups it (benchmark + future multi-class)."""

    vuln_class: str
    file: str
    line: int
    snippet: str


def _added_lines(hunk: DiffHunk) -> list[tuple[int, str]]:
    """Walk a hunk body -> [(new_side_line_number, added_text)] for each ADDED
    line. New-side number starts at `new_start` and advances on added/context
    lines, not on removed lines (unified-diff semantics). The first body line
    is the `@@` header (skipped)."""
    out: list[tuple[int, str]] = []
    lineno = hunk.new_start
    for raw in hunk.body.splitlines():
        if raw.startswith("@@"):
            continue
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("+"):
            out.append((lineno, raw[1:]))
            lineno += 1
        elif raw.startswith("-"):
            continue  # removed line: no new-side advance
        else:
            lineno += 1  # context line
    return out


def _is_cleartext_secret_log(text: str) -> bool:
    """True when an added line both calls a log/print SINK and references a
    secret-ish token — the recall heuristic for this class."""
    return bool(_SINK_RE.search(text) and _SECRET_TOKEN_RE.search(text))


def scan_builtin(hunks: tuple[DiffHunk, ...]) -> tuple[Candidate, ...]:
    """Pure builtin detector for clear-text-secret-log over the diff hunks
    (#400). The zero-dependency engine + the fallback when Semgrep is absent.

    Returns one Candidate per added line that looks like a secret reaching a
    log sink. LIBERAL by design — the judge does the exploitability
    discrimination. Line numbers are real new-side numbers in the hunk's
    `new_lines`, so a kept candidate survives `evaluate_diff`'s
    anti-hallucination filter. No IO, no logging — deterministic + unit-tested.
    """
    candidates: list[Candidate] = []
    for hunk in hunks:
        for lineno, text in _added_lines(hunk):
            if _is_cleartext_secret_log(text):
                candidates.append(
                    Candidate(
                        vuln_class=CLEARTEXT_SECRET_LOG,
                        file=hunk.file_path,
                        line=lineno,
                        snippet=text.strip(),
                    )
                )
    return tuple(candidates)


# Engine selection (ADR-0006 vendor-neutral boundary). `opengrep` = the OSS
# engine over the vendored offline rules (#401, full class coverage); `builtin`
# = the #400 zero-dep detector. Config value, never an import — the vendor stays
# swappable. Default `builtin` so a misconfig/absent-engine degrades to the
# safe zero-dep path rather than failing the review.
_ENGINE = os.getenv("GRUG_SAST_ENGINE", "builtin").strip().lower()
# Rules are WEBHOOK-OWNED (vendored under services/webhook/sast_rules/; the
# api never runs the opengrep engine). Post-extraction (#77) this shared module
# can't reach them via __file__, so resolve from the service's working dir
# (pods run WORKDIR /app; tests run from the service dir), with an env escape
# hatch. A missing dir degrades exactly like a missing opengrep binary: () + log.
_RULES_DIR = os.getenv(
    "GRUG_SAST_RULES_DIR", os.path.join(os.getcwd(), "sast_rules")
)
# AC5 cost bound: cap the bytes opengrep scans per review so a huge PR can't blow
# review latency. Files beyond the budget are skipped + logged (never silently).
_MAX_SCAN_BYTES = 1_000_000
_OPENGREP_TIMEOUT_S = 60


def _added_lines_by_file(hunks: tuple[DiffHunk, ...]) -> dict[str, set[int]]:
    """file_path -> the new-side line numbers the PR added/changed (the hunks'
    `new_lines`). Used to keep only Semgrep findings on lines THIS PR touched —
    a PR review flags what the PR introduces, not pre-existing code (and GitHub
    inline comments only attach to diff lines anyway)."""
    out: dict[str, set[int]] = {}
    for h in hunks:
        out.setdefault(h.file_path, set()).update(h.new_lines)
    return out


def _budget_files(file_contents: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Select files within the byte budget (AC5). Returns (kept, skipped). Sorted
    by size so a few huge files don't starve the rest. Skipped files are returned
    so the caller logs them — a silent truncation reads as 'scanned everything'."""
    kept: dict[str, str] = {}
    skipped: list[str] = []
    total = 0
    for path, content in sorted(file_contents.items(), key=lambda kv: len(kv[1])):
        size = len(content.encode("utf-8", "ignore"))
        if total + size > _MAX_SCAN_BYTES:
            skipped.append(path)
            continue
        kept[path] = content
        total += size
    return kept, skipped


def _write_scan_files(tmp: str, kept_files: dict[str, str]) -> None:
    """Materialize the budgeted head-SHA file contents under `tmp` for the
    opengrep subprocess to scan."""
    tmp_real = os.path.realpath(tmp)
    for path, content in kept_files.items():
        dest = os.path.join(tmp, path)
        # Containment guard: `path` is PR-controlled (from the diff's
        # `+++ b/<path>`). A crafted `../../etc/foo` would escape the
        # temp dir into an arbitrary write — refuse anything that does
        # not resolve to UNDER tmp (defensive even though the pod is
        # readOnlyRootFilesystem + non-root).
        dest_real = os.path.realpath(dest)
        if dest_real != tmp_real and not dest_real.startswith(tmp_real + os.sep):
            log.warning("sast_opengrep_path_escape_skipped", extra={"path": path})
            continue
        os.makedirs(os.path.dirname(dest) or tmp, exist_ok=True)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(content)


def _opengrep_home_env(scan_tmp: str) -> dict[str, str]:
    """HOME/XDG_CACHE_HOME for the opengrep subprocess (#865).

    Prefers `GRUG_SAST_CACHE_DIR` - a stable, writable, per-pod path on its
    own volume - so opengrep's 242M runtime extraction happens ONCE per pod
    instead of once per scan, outside both the scanned tree and the
    size-capped `tmp` volume.

    Returns the scan dir instead when that path is unset or not writable.
    That is the pre-#865 behaviour: correct but wasteful, and it keeps a
    self-hosted deploy that has not added the volume working rather than
    crashing on an unwritable HOME (the 2026-07-13 zero-findings regression).
    """
    cache = os.getenv("GRUG_SAST_CACHE_DIR", "").strip()
    if cache:
        try:
            os.makedirs(cache, exist_ok=True)
            if os.access(cache, os.W_OK):
                return {"HOME": cache,
                        "XDG_CACHE_HOME": os.path.join(cache, ".cache")}
            log.warning("sast_cache_dir_not_writable", extra={"dir": cache})
        except OSError:
            log.warning("sast_cache_dir_unusable", extra={"dir": cache},
                        exc_info=True)
    return {"HOME": scan_tmp,
            "XDG_CACHE_HOME": os.path.join(scan_tmp, ".cache")}


def scan_opengrep(
    hunks: tuple[DiffHunk, ...], file_contents: dict[str, str]
) -> tuple[Candidate, ...]:
    """Semgrep OSS engine over the vendored offline rules (#401). Writes the
    changed files to a temp dir, runs `opengrep scan --config <rules>` (NO
    network — registry refs are refused), maps each result to a Candidate via
    the rule's `metadata.vuln_class`, and keeps only findings on lines THIS PR
    added (so we flag what the PR introduces, not pre-existing code).

    Best-effort: a missing opengrep binary, a non-zero exit, a timeout, or
    unparseable output returns () + logs — SAST is additive and must never
    break the review (the dispatch caller also guards). file_contents is the
    head-SHA content of changed files (#336); without it Semgrep has nothing
    to scan -> ().
    """
    if not file_contents:
        return ()
    kept_files, skipped = _budget_files(file_contents)
    if skipped:
        log.info("sast_opengrep_files_skipped_over_budget", extra={"skipped": len(skipped)})
    if not kept_files:
        return ()
    # The rules dir is resolved from the service cwd post-#77 (ADR-0014) -
    # a wrong working directory is a REAL misconfiguration class now, and
    # opengrep without rules = the security scanner silently finding
    # nothing. Fail loudly-and-degrade with a dedicated, monitorable line
    # (the missing-BINARY sibling below already has one).
    if not os.path.isdir(_RULES_DIR):
        log.error("sast_opengrep_rules_dir_missing", extra={"rules_dir": _RULES_DIR})
        return ()
    added = _added_lines_by_file(hunks)
    try:
        with tempfile.TemporaryDirectory(prefix="grug-sast-") as tmp:
            _write_scan_files(tmp, kept_files)
            # Opengrep initializes settings/cache under $HOME at
            # startup. The pods run as uid 10001 created with --no-create-home
            # on a readOnlyRootFilesystem, so that mkdir crashed semgrep with
            # exit 1 before it scanned anything - every production scan
            # silently degraded to zero findings (found live 2026-07-13
            # during an infrastructure sweep). HOME must therefore point
            # somewhere WRITABLE.
            #
            # It must also point somewhere STABLE and OUTSIDE `tmp` (#865).
            # Pointing it at the scan dir cost three things at once:
            #   1. opengrep scanned its own cache (the original #865 report);
            #   2. a cold HOME makes opengrep extract 242M of runtime EVERY
            #      scan, because a fresh temp dir is never a warm cache;
            #   3. that 242M landed in the `tmp` emptyDir, whose sizeLimit is
            #      64Mi - measured evicting a live webhook pod on 2026-08-15
            #      ("Usage of EmptyDir volume tmp exceeds the limit 64Mi").
            # A dedicated cache volume fixes all three: extract once per pod,
            # never inside the scanned tree, never against the tmp cap.
            #
            # Falls back to the old scan-dir behaviour when the cache dir is
            # unset or not writable, so a self-hosted deploy without the extra
            # volume still scans (degraded to re-extracting, never broken).
            scan_env = {**os.environ, **_opengrep_home_env(tmp)}
            proc = subprocess.run(
                ["opengrep", "scan", "--config", _RULES_DIR, "--json", "--quiet",
                 "--disable-version-check", "--no-rewrite-rule-ids", tmp],
                capture_output=True, text=True, timeout=_OPENGREP_TIMEOUT_S,
                env=scan_env,
                # We inspect returncode ourselves below (a non-zero exit with
                # parseable JSON is a real degrade path, #77), so never raise.
                check=False,
            )
            if proc.returncode != 0:
                # Version-dependent, opengrep can exit non-zero AND emit
                # parseable-but-empty JSON - without this check that
                # degrades to a silent zero-findings scan. Keep enough stderr
                # to actually diagnose: the old 200-char cap hid the failing
                # path of the exact home-dir crash this env fix addresses.
                log.warning(
                    "sast_opengrep_run_failed",
                    extra={
                        "kind": "NonZeroExit",
                        "returncode": proc.returncode,
                        "rules_dir": _RULES_DIR,
                        "stderr": (proc.stderr or "")[-2000:],
                    },
                )
                return ()
            data = json.loads(proc.stdout)
            tmp_prefix = tmp.rstrip("/") + "/"
            # A per-file parse failure lands in `errors` while the process
            # still exits 0 with a well-formed `results` array. Reading only
            # `results` turns "could not scan this file" into "scanned it and
            # it was clean" - opposite facts that look identical, and the
            # failure lands on exactly the files the YAML rules target
            # (Helm-templated manifests, Jinja-templated Ansible: `{{ }}` is
            # not valid YAML). Report it rather than fail the review - those
            # files are legitimately unparseable and blocking on them would
            # make the scanner unusable - but never stay silent about reduced
            # coverage.
            _log_unscanned_files(data, tmp_prefix, set(kept_files))
    except FileNotFoundError:
        log.warning("sast_opengrep_binary_missing")
        return ()
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
        log.warning("sast_opengrep_run_failed", extra={"kind": type(e).__name__})
        return ()

    return _map_opengrep_results(data, added, tmp_prefix)



def _log_unscanned_files(data: dict, tmp_prefix: str, staged: set[str]) -> None:
    """Report coverage the engine did not deliver. Two different silences.

    1. `errors` - rule/IO failures the engine does name. Cheap to surface and
       previously dropped on the floor.

    2. Files the engine NEVER SCANNED. This is the serious one, and it is
       invisible without comparing against what we staged. Measured against the
       real engine on 2026-08-15: four files staged, `paths.scanned` came back
       with ONE, `errors` came back EMPTY. A Swift file, an unknown extension
       and an oversized file were all dropped without a word.

       That silence hits whole LANGUAGES, not stray files - a Swift repo gets a
       green review having been scanned not at all - which is the same shape as
       the ruleset that matched nothing, one level further down. "Scanned and
       clean" and "never looked at" are opposite facts and the JSON renders
       them identically unless you do this comparison.

    Neither case fails the review: files legitimately fall outside an engine's
    languages, and blocking on that would make the scanner unusable. But a
    review that covered less than it claims must say so.
    """
    errors = data.get("errors") or []
    if errors:
        detail = []
        for e in errors:
            path = (e.get("path") or "").replace(tmp_prefix, "", 1)
            detail.append(
                f"{path or '<no path>'}: {e.get('message') or e.get('type') or 'unknown'}"
            )
        log.warning(
            "sast_opengrep_files_unscanned",
            extra={"count": len(detail), "files": detail[:20]},
        )

    scanned_raw = (data.get("paths") or {}).get("scanned") or []
    scanned = {str(p).replace(tmp_prefix, "", 1) for p in scanned_raw}
    missed = sorted(staged - scanned)
    if missed:
        log.warning(
            "sast_opengrep_files_not_scanned",
            extra={
                "count": len(missed),
                "staged": len(staged),
                "files": missed[:20],
            },
        )


def _map_opengrep_results(
    data: dict, added: dict[str, set[int]], tmp_prefix: str
) -> tuple[Candidate, ...]:
    """Map raw semgrep JSON results to Candidates: strip the temp-dir prefix
    back to repo-relative paths, require the rule's metadata.vuln_class, and
    keep only findings on lines THIS PR added/changed."""
    candidates: list[Candidate] = []
    for r in data.get("results", []):
        rel = r.get("path", "")
        if rel.startswith(tmp_prefix):
            rel = rel[len(tmp_prefix):]
        line = (r.get("start") or {}).get("line")
        vuln_class = (r.get("extra", {}).get("metadata") or {}).get("vuln_class")
        if not (rel and line and vuln_class):
            continue
        if line not in added.get(rel, set()):
            continue
        snippet = (r.get("extra", {}).get("lines") or "").strip()
        candidates.append(Candidate(vuln_class=vuln_class, file=rel, line=line, snippet=snippet))
    return tuple(candidates)


def scan_candidates(
    hunks: tuple[DiffHunk, ...],
    file_contents: dict[str, str] | None = None,
    engine: str | None = None,
) -> tuple[Candidate, ...]:
    """The vendor-neutral detection boundary (ADR-0006). Dispatches to the
    configured engine: Semgrep OSS over the vendored rules (`engine="semgrep"`,
    the #401 full-class engine — needs `file_contents`) or the #400 zero-dep
    builtin (default). `engine` defaults to `GRUG_SAST_ENGINE`. The judge +
    merge + publish downstream are engine-agnostic, so swapping the engine here
    is the only change #401 makes."""
    eng = (engine or _ENGINE).strip().lower()
    # "semgrep" kept as an accepted alias (#858): the engine was renamed, and a
    # manifest that still says semgrep must keep scanning rather than silently
    # degrade to the builtin engine - a quiet downgrade of a security control is
    # exactly the failure mode this file already warns about elsewhere.
    if eng in ("opengrep", "semgrep") and file_contents:
        return scan_opengrep(hunks, file_contents)
    return scan_builtin(hunks)


# Secret-leak severity. "high" lives in the blocking partition, so an operator
# who flips code_reviewer_blocking gets it as a merge gate; advisory by default.
_SAST_SEVERITY = "high"


# Human-readable label per vuln class for the judge prompt + the published
# finding message. Generic fallback so a new class (or a Semgrep rule whose
# metadata.vuln_class we haven't enumerated) still reads sensibly instead of
# being mislabeled. (Pre-#401/#434 these messages were hardcoded to
# clear-text-secret-log, mislabeling every other class - fixed here.)
_CLASS_LABELS: dict[str, str] = {
    CLEARTEXT_SECRET_LOG: "Clear-text logging of a secret",
    "sql-injection": "SQL injection",
    "command-injection": "Command injection",
    "template-injection": "Template injection",
    "ssrf": "Server-side request forgery (SSRF)",
    "path-traversal": "Path traversal",
    "unsafe-deserialization": "Unsafe deserialization",
    "weak-crypto": "Weak or misused cryptography",
    "xxe": "XML external entity (XXE)",
    "hardcoded-credential": "Hardcoded credential",
    "vulnerable-dependency": "Vulnerable dependency",
    "exposed-secret": "Exposed secret or credential",
    "iac-misconfig": "Infrastructure-as-code misconfiguration",
}


def _label(vuln_class: str) -> str:
    return _CLASS_LABELS.get(vuln_class, vuln_class.replace("-", " ").capitalize())


def _candidate_to_repr(c: Candidate) -> JudgeFindingRepr:
    """Provisional finding handed to the exploitability judge. The message
    states the candidate hypothesis; the judge decides if the secret VALUE
    (vs a public name/path, the #391 case) actually reaches the sink."""
    return {
        "rule_name": c.vuln_class,
        "file": c.file,
        "line": c.line,
        "severity": _SAST_SEVERITY,
        "message": f"Possible {_label(c.vuln_class)}: {c.snippet}",
    }


def judge_candidates(
    candidates: tuple[Candidate, ...],
    hunks: tuple[DiffHunk, ...],
    installation_id: int,
    *,
    pr_context: Optional[PrContext] = None,
    file_contents: dict[str, str] | None = None,
) -> tuple[Finding, ...]:
    """Precision layer: LLM-judge each candidate for real exploitability and
    return Findings for the KEPT (exploitable) ones, suppressing the rest.

    Reuses the existing `llm_client.judge_findings` (is_real_bug) as the
    exploitability gate — `reasoning` becomes the finding's source->sink
    rationale. A judged-FALSE candidate (e.g. the #391 public-config-path log)
    is suppressed (dropped), so the FP never reaches the PR.

    FAIL-CLOSED on a judge outage: if the judge can't run (LLM down, parse
    error, count mismatch), candidates are SUPPRESSED, not posted raw — an
    un-triaged candidate flood is exactly the noisy-SAST failure precision
    exists to prevent (a real vuln re-surfaces on the next push's re-review).
    """
    if not candidates:
        return ()

    # #439 (ADR-0009): route the exposed-secret class to the in-cluster
    # Cave judge (raw value never leaves the boundary) and the remaining
    # classes to the SaaS judge with REDACTED input (2d). When the Cave is
    # unconfigured/unreachable, secrets fall back to today's SaaS path
    # (unredacted - the class needs the raw value; detection beats privacy
    # regression until the gateway route is live), logged either way.
    cave = _cave_judge_config()
    secrets = tuple(c for c in candidates if c.vuln_class == EXPOSED_SECRET)
    others = tuple(c for c in candidates if c.vuln_class != EXPOSED_SECRET)

    if cave is None or not secrets:
        # Single-call path. Redact iff no secret candidate rides along
        # (a secret candidate on SaaS still needs its raw value).
        kept, _ok = _judge_batch(
            candidates, hunks, installation_id,
            pr_context=pr_context, file_contents=file_contents,
            config=None, redact=not secrets,
        )
        return kept

    kept, cave_ok = _judge_batch(
        secrets, hunks, installation_id,
        pr_context=pr_context, file_contents=file_contents,
        config=cave, redact=False,
    )
    if not cave_ok:
        # The Cave judge FAILED (transport/config/parse - NOT an
        # all-suppressed verdict, which returns judged_ok=True with an
        # empty kept). FAIL-CLOSED (codex PR #486 round 2): once the
        # in-cluster boundary is CONFIGURED, a raw secret batch never
        # falls back to SaaS - the outage moment is exactly when the
        # privacy control matters most. The secret candidates are
        # suppressed for THIS pass (same fail-closed shape as any judge
        # outage), re-triggered on the next push/rerun; the monitored
        # log line below is the alerting channel.
        log.warning(
            "cave_judge_failed_secrets_suppressed",
            extra={"installation_id": installation_id, "secrets": len(secrets)},
        )
        kept = ()
    else:
        log.info(
            "cave_judge_used",
            extra={"installation_id": installation_id, "secrets": len(secrets),
                   "kept": len(kept)},
        )
    others_kept, _ok = _judge_batch(
        others, hunks, installation_id,
        pr_context=pr_context, file_contents=file_contents,
        config=None, redact=True,
    )
    return kept + others_kept


def _judge_batch(
    candidates: tuple[Candidate, ...],
    hunks: tuple[DiffHunk, ...],
    installation_id: int,
    *,
    pr_context: Optional[PrContext],
    file_contents: dict[str, str] | None,
    config,
    redact: bool,
) -> tuple[tuple[Finding, ...], bool]:
    """One judge call over one candidate batch - the pre-#439 body of
    judge_candidates, parameterized by backend config + redaction.

    Returns (kept_findings, judged_ok). `judged_ok` distinguishes "the
    judge RAN and returned verdicts" (kept may legitimately be empty -
    every candidate suppressed) from "the judge FAILED" (error or the
    ()-on-failure shape) - codex PR #486 HIGH: conflating the two made a
    legitimately all-suppressed Cave batch retry on SaaS unredacted,
    leaking exactly the benign/example credentials the in-cluster judge
    had correctly suppressed."""
    if not candidates:
        return (), True
    reprs = [_candidate_to_repr(c) for c in candidates]
    evidence = assemble_evidence(
        (Hunk(path=h.file_path, body=h.body) for h in hunks),
        pr_context=pr_context, file_contents=file_contents,
    )
    try:
        judgements = judge_findings(
            reprs, evidence, installation_id, config=config, redact=redact,
        )
    except Exception as e:  # noqa: BLE001 — judge failure must not crash the review
        log.warning(
            "sast_judge_unavailable_candidates_suppressed",
            extra={"installation_id": installation_id, "candidates": len(candidates),
                   "kind": type(e).__name__},
        )
        return (), False
    if not judgements:
        # judge_findings' ()-on-failure shape for a non-empty batch.
        return (), False

    by_index = {j.finding_index: j for j in judgements}
    kept: list[Finding] = []
    for i, c in enumerate(candidates):
        j = by_index.get(i)
        if j is None:
            # The judge returned no verdict for this candidate -> can't confirm
            # exploitability -> suppress (fail-closed, same as an outage).
            log.info(
                "sast_candidate_unjudged_suppressed",
                extra={"installation_id": installation_id, "file": c.file, "line": c.line},
            )
            continue
        if not j.is_real_bug:
            log.info(
                "sast_candidate_suppressed_not_exploitable",
                extra={"file": c.file, "line": c.line, "reason": _safe_reason(c, j)},
            )
            continue
        kept.append(
            Finding(
                file=c.file,
                line=c.line,
                severity=_SAST_SEVERITY,
                rule_name=c.vuln_class,
                message=_finding_message(c, j),
                suggestion=None,
            )
        )
    return tuple(kept), True


# The judge sees the full raw file content (#336), so its free-text `reasoning`
# can quote the very secret an exposed-secret candidate is about. That reasoning
# is published in the finding message and logged, which would defeat the
# scanner's no-echo invariant. For exposed-secret findings we therefore NEVER
# publish or log the judge's free text - the judge's keep/suppress DECISION is
# still honored; only its prose is withheld and replaced with a fixed rationale.
_SECRET_FINDING_RATIONALE = (
    "A committed credential was detected on this line. Treat it as compromised: "
    "rotate it, and remove it from the diff (and from history if already pushed)."
)


def _finding_message(c: Candidate, j: FindingJudgement) -> str:
    if c.vuln_class == EXPOSED_SECRET:
        return f"{_label(c.vuln_class)}. {_SECRET_FINDING_RATIONALE} (line: `{c.snippet}`)"
    return f"{_label(c.vuln_class)}. {j.reasoning} (line: `{c.snippet}`)"


def _safe_reason(c: Candidate, j: FindingJudgement) -> str:
    """Judge reasoning is safe to log EXCEPT for exposed-secret, where it could
    quote the raw value the judge read from full-file context."""
    return "<redacted: exposed-secret>" if c.vuln_class == EXPOSED_SECRET else j.reasoning
