"""Deterministic lint evidence for Elder reviews (#681, epic #707).

#707 measured Elder's RECALL at 1.8% against the corpus, and 0 of 24
critical-severity findings. Precision work (#708/#779) removes false
markings but adds no coverage. This module adds coverage the model cannot
reliably reproduce: a real linter over the changed files, whose findings
never hallucinate because they are not generated - they are measured.

Ruff, because it is already the fleet's Python linter (provisioned to every
repo via #995's standard `.pre-commit-config.yaml`) and grug already depends
on the binary - so this adds coverage with no new dependency and nothing
vendored.

WHAT IS SELECTED, AND WHY THAT CHANGED. This module originally ran ruff with
no `--select`, i.e. ruff's defaults. Measured against a file with five
planted vulnerabilities, those defaults caught 1 of 5 - missing SQL
injection, unsafe pickle, non-cryptographic randomness and an insecure hash.
It now selects flake8-bandit (`S`), flake8-bugbear (`B`) and the async checks
(`ASYNC`): 132 rules aimed squarely at the recall gap above, and 5 of 5 on
that same file. See `_SELECT`/`_IGNORE` for the licence position and the
measured basis for every exclusion.

An honest caveat that used to be stated the other way round: the fleet's
pre-commit adopts ruff's DEFAULTS, not `S`/`B`/`ASYNC`. So a finding from
this module is NOT "a rule the repo already adopted" - it is grug asserting
a rule the repo has not opted into. That is defensible because the finding
is MEASURED rather than generated (a `noqa` settles it in one line), but it
is a stronger claim than before and the finding text says so plainly instead
of borrowing authority it does not have.

WHY THIS IS NOT sast.py. `scan_semgrep` produces `Candidate`s that go
through the exploitability judge, which is correct for security findings.
Lint findings are not security claims and must not be judged for
exploitability - they are deterministic and precise by construction. So this
mirrors `scan_complexity` instead: it returns `Finding`s directly, and
dispatch merges them via `with_extra_findings` AFTER the verification pass
and refute gate, both of which exist to police MODEL judgment.

THE RUFF EXIT-CODE TRAP. Ruff exits **1 when it finds violations** - that is
success, not failure. Copying `scan_semgrep`'s `returncode != 0 -> return ()`
would discard every real result and degrade to a permanent silent zero, which
is the exact shape of the semgrep HOME-dir bug found live on 2026-07-13. Only
exit >= 2 is a genuine ruff error. Cache is disabled outright (`--no-cache`)
rather than repointing HOME, since the pods run readOnlyRootFilesystem as uid
10001 with --no-create-home and a cache write is the thing that crashed
semgrep there.

TEST FILES. Bandit-style rules about hardcoded secrets (S105/S106/S107),
`assert` (S101) and non-cryptographic randomness (S311) fire on fixtures,
fake tokens and seeded randomness, which is what test code is made of. A fake
`TOKEN = "abc123"` in a test file once raised a high-severity marking that
alone failed a PR's review check. Those rules are therefore dropped for test
files (see `_is_test_file`). Real committed secrets in tests are still caught
by the separate `secret_scan` source, which matches key SHAPES, not names.
Every other rule, and every rule in non-test files, is unchanged.

Diff-anchored like every other source: only violations on lines THIS PR
ADDED are reported, so Elder flags what the PR introduces rather than
pre-existing debt the author did not touch.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile

from personas.code_reviewer.diff_parser import DiffHunk
from personas.code_reviewer.persona import Finding
from personas.code_reviewer.test_paths import is_test_file
from review_types import Severity  # single source (#250)
# Reuse the canonical added-lines walk rather than a third copy of it
# (standard-pattern-reuse-or-elect): complexity._changed_by_file already
# walks the unified diff body and returns new-side ADDED line numbers.
from personas.code_reviewer.complexity import _changed_by_file

log = logging.getLogger("grug.code_reviewer.lint")

# OFF by default. A new finding source fires on EVERY PR fleet-wide, and the
# #707 lesson is that a speculative source is worse than none. Enable per
# environment once the acceptance rate is observed:
#   GRUG_LINT_EVIDENCE=ruff
# Mirrors sast.py's GRUG_SAST_ENGINE gate rather than inventing a new
# mechanism (standard-pattern-reuse-or-elect).
#
# TWO REASONS IT IS STILL OFF, both measured 2026-07-31 - recorded here so the
# next person does not re-run the same experiment:
#
# 1. `ruff` IS NOT IN THE RUNTIME IMAGE. Verified against the live consumer:
#    `exec: "ruff": executable file not found in $PATH`. So setting the env var
#    alone changes nothing - every call would take the FileNotFoundError path
#    below and return (). Shipping the binary is a prerequisite, not a detail.
#
# 2. ON THIS CODEBASE THE SELECTION FOUND NOTHING REAL. Run against
#    `services/` with the exact _SELECT/_IGNORE below: 127 findings, 34 outside
#    tests, and not ONE confirmable true positive on inspection.
#      S105/S106 (72) - fired on NAMES: `SECRET_HEADER` (a header name),
#                       `EXPOSED_SECRET` (one of grug's own SAST rule names).
#                       67 of 72 were in test files.
#      S608 (13)      - "SQL injection" on f-strings interpolating the module
#                       constant `TTL_LIVE`, with every user value bound via
#                       `%s`. Correct, safe code.
#      B023 (2)       - real pattern, but both closures are invoked
#                       SYNCHRONOUSLY inside the same loop iteration.
#      ASYNC (0)      - clean.
#
# The `B` family is the only one worth revisiting (5 source findings, all
# style-grade); `S` measured 0/29 and should be dropped from _SELECT before
# anyone turns this on. Left as-is rather than re-tuned blind: changing the
# selection without a fresh measurement is how the last speculative source got
# to 71% of all output (#767).
_ENABLED = os.getenv("GRUG_LINT_EVIDENCE", "").strip().lower() == "ruff"

_RULE = "lint-violation"

# Advisory, same as complexity: a lint nit must never be the thing that
# blocks a merge on its own.
_SEVERITY: Severity = "medium"

# --- rule selection: the RECALL fix -------------------------------------
#
# Ruff was previously invoked with NO `--select`, so it ran its DEFAULTS.
# MEASURED, not assumed (ruff 0.16, `--isolated`, against a file containing
# five planted vulnerabilities): the defaults caught 1 of the 5, and missed
# SQL injection (S608), unsafe pickle (S301), non-crypto randomness (S311)
# and an insecure hash (S324). Explicit `--select S` caught 5 of 5.
#
# (An earlier draft of this comment claimed the defaults were `E4,E7,E9,F`
# with zero security coverage. That is wrong for ruff 0.16 - the defaults
# are broader and do include some `S` rules, S110 among them. Recorded
# because a file whose premise is "measured, not generated" has no business
# carrying an unverified claim about its own tool.)
#
# Context for why this matters: #707 measured Elder's recall at 1.8% with 0
# of 24 critical findings caught, and grug's entire SAST layer is 10
# hand-written semgrep rules in ONE language.
#
# Ruff already vendors ports of flake8-bandit (73 security rules),
# flake8-bugbear (43 real-bug rules) and its async checks (16) - all under
# ruff's MIT licence, in a binary grug ALREADY depends on. Selecting them
# costs nothing and needs no vendoring.
#
# ATTRIBUTION: rule content originates in ruff (https://github.com/astral-sh/ruff,
# MIT), which ports flake8-bandit (Apache-2.0) and flake8-bugbear (MIT). We
# invoke the tool and map its output; no rule text is copied into this repo.
#
# NOT USED, deliberately: the semgrep community registry. The "Semgrep Rules
# License v1.0" is PROPRIETARY - it grants use for "internal business
# purposes" only, forbids distribution, and forbids making the rules
# "available to others as a service". grug is a public AGPL repo (vendoring
# = distribution) AND a GitHub App reviewing other people's repos (= as a
# service), so that registry is doubly off-limits. grug's own `grug-*` rules
# in services/webhook/sast_rules/ stay hand-written for exactly this reason.
_SELECT = "S,B,ASYNC"

# Measured against grug's own tree before choosing (4425 raw violations ->
# 125 after these exclusions, a 98% cut) - every entry here earned its place
# with a number, not a hunch:
#   S101 - `assert` used. 4284 of the 4425, and 4269 of those in tests,
#          where assert IS the idiom. On its own it is 97% of the noise.
#   B008 - function call in an argument default. FastAPI's `Depends()` is
#          exactly that and is the FRAMEWORK'S REQUIRED form, so this fires
#          on correct code across every route in services/api.
#   S603/S607 - subprocess call / partial executable path. Whether input is
#          trusted is not decidable from a diff, and grug shells out to
#          ruff/semgrep/gh on purpose. A known recurring false-positive
#          class; re-enable only with a trust model behind it.
_IGNORE = "S101,B008,S603,S607"

# Severity per rule family. A string-concatenated SQL query and a missing
# `strict=` on zip() are not the same event, and flattening both to "medium"
# is what makes a finding source easy to ignore wholesale.
_HIGH_PREFIXES = (
    "S1",   # hardcoded secrets/passwords (S105-S107), try-except-pass (S110)
    "S3",   # weak crypto / insecure hash / unsafe URL scheme (S31x, S32x)
    "S5",   # unsafe deserialisation + weak SSL/TLS defaults
    "S6",   # injection: SQL (S608), shell, Jinja autoescape off
    "S7",   # XML/XXE
)


# Rules that are idiomatic noise in test code: assert (S101), hardcoded
# password/token constants and arguments (S105-S107), seeded non-crypto
# random (S311). Dropped for test files only.
_TEST_NOISE_RULES = frozenset({"S101", "S105", "S106", "S107", "S311"})
_is_test_file = is_test_file  # kept: lint tests and callers use this name


def _severity_for(code: str) -> Severity:
    """`high` for the security families, `medium` for the rest.

    Deliberately prefix-based rather than an exhaustive per-code table: a
    ruff upgrade that adds S6xx rules should inherit the right severity
    automatically instead of silently defaulting to medium, which is the
    direction that loses a real finding.
    """
    if code.startswith("S") and any(code.startswith(p) for p in _HIGH_PREFIXES):
        return "high"
    return _SEVERITY


def _rule_name_for(code: str) -> str:
    """Machine-readable finding class, so the #595 scoreboard can measure
    precision PER CLASS. Everything used to be `lint-violation`, which made
    'security findings are accurate, style nits are noisy' impossible to
    see in the data - and therefore impossible to act on."""
    if code.startswith("ASYNC"):
        return "lint-async"
    if code.startswith("S"):
        return "lint-security"
    if code.startswith("B"):
        return "lint-bug"
    return _RULE


_TIMEOUT_S = 30
_MAX_SCAN_BYTES = 1_000_000
_MAX_FINDINGS = 20

# Ruff exit codes: 0 = clean, 1 = violations found (SUCCESS for us),
# >= 2 = ruff itself failed. See the module docstring.
_RUFF_ERROR_EXIT = 2


def _budget(file_contents: dict[str, str]) -> dict[str, str]:
    """Python files only, under a total byte budget, no path escapes."""
    kept: dict[str, str] = {}
    total = 0
    for path, text in sorted(file_contents.items()):
        if not path.endswith(".py"):
            continue
        if os.path.isabs(path) or ".." in path.split("/"):
            log.warning("lint_path_escape_skipped", extra={"path": path})
            continue
        size = len(text.encode("utf-8", "replace"))
        if total + size > _MAX_SCAN_BYTES:
            continue
        kept[path] = text
        total += size
    return kept


def _write(tmp: str, files: dict[str, str]) -> None:
    for path, text in files.items():
        dest = os.path.join(tmp, path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(text)


def scan_ruff(
    hunks: tuple[DiffHunk, ...], file_contents: dict[str, str]
) -> tuple[Finding, ...]:
    """Ruff over the changed Python files, diff-anchored to added lines.

    Best-effort by contract: a missing binary, a real ruff error, a timeout or
    unparseable output all return () and log. Lint evidence is additive and
    must never break a review.
    """
    if not _ENABLED:
        return ()
    if not file_contents:
        return ()
    kept = _budget(file_contents)
    if not kept:
        return ()
    added = _changed_by_file(hunks)
    if not added:
        return ()

    try:
        with tempfile.TemporaryDirectory(prefix="grug-lint-") as tmp:
            _write(tmp, kept)
            proc = subprocess.run(
                ["ruff", "check", "--select", _SELECT, "--ignore", _IGNORE,
                 "--output-format", "json", "--no-cache", tmp],
                capture_output=True, text=True, timeout=_TIMEOUT_S,
                check=False,
            )
            # ONLY >= 2 is an error. Exit 1 means violations were found and
            # stdout carries them - see the module docstring.
            if proc.returncode >= _RUFF_ERROR_EXIT:
                log.warning(
                    "lint_ruff_run_failed",
                    extra={
                        "returncode": proc.returncode,
                        "stderr": (proc.stderr or "")[-2000:],
                    },
                )
                return ()
            results = json.loads(proc.stdout or "[]")
            prefix = tmp.rstrip("/") + "/"
    except FileNotFoundError:
        # WARNING, not info: reaching here means someone set
        # GRUG_LINT_EVIDENCE=ruff and is getting silent nothing, because the
        # binary is not in the image (measured 2026-07-31). An operator who
        # deliberately enabled a finding source deserves to see why it produced
        # none, rather than reading an info line nobody greps for.
        log.warning("lint_ruff_binary_missing_but_enabled")
        return ()
    except subprocess.TimeoutExpired:
        log.warning("lint_ruff_timeout", extra={"timeout_s": _TIMEOUT_S})
        return ()
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("lint_ruff_unparseable", extra={"kind": type(e).__name__})
        return ()

    return _map_results(results, prefix, added)


def _is_reportable(
    path: str, row: int, code: str, added: dict[str, set[int]]
) -> bool:
    """False for test-file noise rules and for pre-existing violations on
    lines this PR did not add."""
    if code in _TEST_NOISE_RULES and _is_test_file(path):
        return False
    return row in added.get(path, set())


def _map_results(
    results: object, prefix: str, added: dict[str, set[int]]
) -> tuple[Finding, ...]:
    """Map ruff JSON to diff-anchored Findings. Tolerant of shape drift: a
    result missing a field is skipped, never fatal."""
    if not isinstance(results, list):
        return ()
    findings: list[Finding] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        path = str(r.get("filename") or "")
        if path.startswith(prefix):
            path = path[len(prefix):]
        loc = r.get("location") or {}
        row = loc.get("row") if isinstance(loc, dict) else None
        code = r.get("code")
        msg = r.get("message")
        if not path or not isinstance(row, int) or not code or not msg:
            continue
        if not _is_reportable(path, row, code, added):
            continue
        findings.append(
            Finding(
                file=path,
                line=row,
                severity=_severity_for(code),
                rule_name=_rule_name_for(code),
                message=(
                    f"ruff `{code}`: {msg}. Deterministic finding - MEASURED by "
                    f"ruff (MIT), not generated by a model, so it cannot be a "
                    f"hallucination. Disagree with the rule rather than with "
                    f"whether the code matches it. Silence it with "
                    f"`# noqa: {code}` and a reason if it is wrong here."
                ),
                suggestion=None,
                effort=None,
            )
        )
        if len(findings) >= _MAX_FINDINGS:
            log.info("lint_ruff_findings_capped", extra={"cap": _MAX_FINDINGS})
            break
    return tuple(findings)
