"""Trial mutation worker — runs INSIDE the locked-down Job (#469, ADR-0013).

WEBHOOK-ONLY (not mirrored): this is a Job entrypoint, not shared app code.
It executes PR-author code (the repo's own test suite), so it runs only ever
inside the sandbox Job — never in the webhook or api pod.

`run_trial(...)` is the injectable core (a `run_tests` callable is passed so the
loop is unit-testable without a real pytest subprocess). `main()` reads the
Job's env, calls `run_trial`, and writes the JSON summary to the pod
termination message (`/dev/termination-log`) — the only channel the launcher
can read back (BYON kubelet logs are unreachable, ADR-0013).

Soundness: a survived mutant only means something if the PRISTINE checkout's
tests PASS. So `run_trial` runs the baseline first; a red baseline degrades the
whole run (we cannot tell a mutation kill from a pre-existing failure). A
per-mutant timeout counts as a KILL (a hang is behavior change), never a
survivor — the conservative direction (we never cry wolf about coverage).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from personas.smasher.mutate import generate_mutants

log = logging.getLogger("grug.smasher.trial_worker")

# (workspace_dir, timeout_seconds) -> exit code. Raises TimeoutError on timeout.
RunTests = Callable[[str, int], int]

_TERMINATION_LOG = "/dev/termination-log"


def run_trial(
    *,
    workspace: str,
    targets: dict[str, list[int]],
    mutant_cap: int,
    per_mutant_timeout: int,
    run_tests: RunTests,
) -> dict[str, Any]:
    """Mutate the target lines, run the suite per mutant, classify. Returns the
    summary dict written to the termination message. Never raises."""
    ws = Path(workspace)

    # Baseline: the pristine checkout must pass, else no mutant verdict is
    # trustworthy. A timeout or nonzero baseline degrades the whole run.
    try:
        if run_tests(workspace, per_mutant_timeout) != 0:
            return _summary("degraded", reason="baseline_failed")
    except TimeoutError:
        return _summary("degraded", reason="baseline_timeout")
    except Exception as e:  # noqa: BLE001 — a broken runner degrades, never raises
        log.warning("trial_baseline_error", extra={"kind": type(e).__name__})
        return _summary("degraded", reason="baseline_error")

    survived: list[dict[str, Any]] = []
    total = killed = timed_out = errored = skipped_files = 0

    for rel_path in sorted(targets):
        if total >= mutant_cap:
            break
        if not _is_safe_relpath(rel_path):
            # An absolute or `..`-bearing path (attacker-controlled diff) would
            # escape the workspace. Skip it — never open it.
            log.warning("trial_unsafe_target_path", extra={"path": rel_path})
            skipped_files += 1
            continue
        file_abs = ws / rel_path
        try:
            original = file_abs.read_text()
        except OSError:
            # A target file that isn't in the checkout — visible skip (a silent
            # one would let a broken tarball layout render as a clean pass).
            log.warning("trial_target_file_absent", extra={"path": rel_path})
            skipped_files += 1
            continue

        remaining = mutant_cap - total
        mutants = generate_mutants(
            original,
            file=rel_path,
            target_lines=frozenset(targets[rel_path]),
            cap=remaining,
        )
        for mutant in mutants:
            total += 1
            restored = True
            try:
                file_abs.write_text(mutant.source)
                try:
                    code = run_tests(workspace, per_mutant_timeout)
                except TimeoutError:
                    timed_out += 1  # a hang is a kill, never a survivor
                    continue
                if code == 0:
                    survived.append({
                        "file": mutant.file,
                        "line": mutant.line,
                        "operator": mutant.operator,
                        "original": mutant.original,
                        "mutated": mutant.mutated,
                    })
                else:
                    killed += 1
            except Exception as e:  # noqa: BLE001 — one bad mutant doesn't sink the run
                errored += 1
                log.warning("trial_mutant_error", extra={"kind": type(e).__name__})
            finally:
                # ALWAYS restore the pristine file before the next mutant.
                try:
                    file_abs.write_text(original)
                except OSError:
                    restored = False
                    log.error("trial_restore_failed", extra={"path": rel_path})
            if not restored:
                # A left-mutated file would poison every SUBSEQUENT file's tests
                # (their runs execute against a corrupt tree), so the counts are
                # no longer trustworthy — degrade rather than report them as
                # complete (ADR-0003 "no lies").
                return _summary("degraded", reason="restore_failed")

    if skipped_files and total == 0:
        # Targets were provided but no target file was readable/mutable — the
        # checkout or targets broke; that is a degrade, never a clean pass.
        return _summary("degraded", reason="targets_absent_from_checkout")

    return _summary(
        "completed", total=total, killed=killed,
        survived=survived, timed_out=timed_out, errored=errored,
    )


def _is_safe_relpath(path: str) -> bool:
    """Reject absolute paths and `..` traversal in an attacker-controlled target
    path before it is joined onto the workspace root."""
    if not path or path.startswith("/") or path.startswith("\\"):
        return False
    return ".." not in path.replace("\\", "/").split("/")


def _summary(
    status: str, *, total: int = 0, killed: int = 0,
    survived: list[dict[str, Any]] | None = None,
    timed_out: int = 0, errored: int = 0, reason: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": status,
        "total": total,
        "killed": killed,
        "survived": survived or [],
        "timed_out": timed_out,
        "errored": errored,
    }
    if reason:
        out["reason"] = reason
    return out


def _default_run_tests(workspace: str, timeout: int) -> int:
    """Run the repo's pytest suite once. `-x` stops at the first failure so a
    kill is detected fast; `-q` keeps output small. Deps vendored by the `deps`
    init container are on PYTHONPATH. Raises TimeoutError on the per-mutant
    budget."""
    env = dict(os.environ)
    deps = str(Path(workspace) / ".grug-deps")
    env["PYTHONPATH"] = deps + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider"],
            cwd=workspace,
            env=env,
            timeout=timeout,
            capture_output=True,
        )
    except subprocess.TimeoutExpired as e:
        raise TimeoutError from e
    return proc.returncode


def main() -> int:
    """Job entrypoint: read env, run the trial, write the termination message."""
    logging.basicConfig(level=logging.INFO)
    workspace = os.getenv("GRUG_TRIAL_WORKSPACE", "/workspace")
    repo_dir = str(Path(workspace) / "repo")
    cap = _int_env("GRUG_TRIAL_MUTANT_CAP", 10)
    timeout = _int_env("GRUG_TRIAL_PER_MUTANT_TIMEOUT", 30)

    try:
        targets = json.loads(os.getenv("GRUG_TRIAL_TARGETS", "{}"))
    except json.JSONDecodeError:
        targets = None
    if not isinstance(targets, dict):
        # A corrupt/truncated targets env must degrade, NOT silently become an
        # empty run that renders as a clean pass (ADR-0003 "no lies").
        log.error("trial_targets_unparseable")
        _write_termination(_summary("degraded", reason="targets_unparseable"))
        return 0

    summary = run_trial(
        workspace=repo_dir,
        targets=targets,
        mutant_cap=cap,
        per_mutant_timeout=timeout,
        run_tests=_default_run_tests,
    )
    _write_termination(summary)
    # Exit 0 regardless of survivors: the Job SUCCEEDED at measuring; survivors
    # are a finding, not a Job failure (which would trip backoff semantics).
    return 0


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _write_termination(summary: dict[str, Any]) -> None:
    """Write the summary to the termination message (4 KiB cap). If the survivor
    list would blow the cap, drop rows (keep the counts) so the channel never
    truncates mid-JSON into an unparseable blob the launcher would degrade on."""
    payload = json.dumps(summary)
    while len(payload.encode()) > 4000 and summary["survived"]:
        summary["survived"].pop()
        summary["truncated"] = True
        payload = json.dumps(summary)
    try:
        Path(_TERMINATION_LOG).write_text(payload)
    except OSError as e:
        # Either a local run (no /dev/termination-log) OR an in-pod write
        # failure of the ONLY channel the launcher can read. Log so the latter
        # is distinguishable from a benign local run, then fall back to stdout.
        log.warning("trial_termination_write_failed", extra={"kind": type(e).__name__})
        sys.stdout.write(payload + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
