"""Trial worker tests (#469) — the in-Job mutate-and-run loop.

`run_trial` is the injectable core: given a checked-out workspace, the target
lines, and a `run_tests` callable, it applies each mutant, classifies
survived/killed/timed_out, restores the file, and returns the summary dict the
worker writes to the termination message. Tested with a fake `run_tests` so no
real pytest subprocess runs.
"""

from __future__ import annotations

from pathlib import Path

import personas.smasher.trial_worker as tw
from personas.smasher.trial_worker import run_trial


def test_reap_noop_when_not_pid1(monkeypatch):
    # A local run (worker is not PID 1) must NEVER kill anything.
    monkeypatch.setattr(tw.os, "getpid", lambda: 4242)
    killed = []
    monkeypatch.setattr(tw.os, "kill", lambda pid, sig: killed.append(pid))
    tw._reap_other_processes()
    assert killed == []


def test_reap_kills_others_but_never_pid1_when_worker_is_pid1(monkeypatch):
    # Inside the sandbox pod the worker is PID 1 and reaps every OTHER process
    # (an author-spawned daemon) before writing the authoritative result.
    monkeypatch.setattr(tw.os, "getpid", lambda: 1)
    monkeypatch.setattr(tw.os, "listdir", lambda p: ["1", "37", "88", "notapid"])
    killed = []
    monkeypatch.setattr(tw.os, "kill", lambda pid, sig: killed.append(pid))
    tw._reap_other_processes()
    assert killed == [37, 88]  # PID 1 (self) never signalled


def _write(ws: Path, rel: str, body: str) -> None:
    p = ws / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


def test_baseline_red_degrades(tmp_path):
    _write(tmp_path, "m.py", "def f(x):\n    return x > 0\n")
    # Baseline itself fails -> we cannot trust any mutant verdict.
    summary = run_trial(
        workspace=str(tmp_path),
        targets={"m.py": [2]},
        mutant_cap=10,
        per_mutant_timeout=5,
        run_tests=lambda ws, timeout: 1,  # always fail, incl. baseline
    )
    assert summary["status"] == "degraded"
    assert summary["survived"] == []


def test_survived_mutant_detected(tmp_path):
    _write(tmp_path, "m.py", "def f(x):\n    return x > 0\n")
    calls = {"n": 0}

    def run_tests(ws, timeout):
        calls["n"] += 1
        return 0  # baseline passes AND every mutant passes -> all survive

    summary = run_trial(
        workspace=str(tmp_path), targets={"m.py": [2]},
        mutant_cap=10, per_mutant_timeout=5, run_tests=run_tests,
    )
    assert summary["status"] == "completed"
    assert summary["total"] >= 1
    assert summary["survived"], "a mutant the tests never fail on must survive"
    assert calls["n"] >= 2  # baseline + >=1 mutant


def test_killed_mutant_not_reported(tmp_path):
    original = "def f(x):\n    return x > 0\n"
    _write(tmp_path, "m.py", original)

    def run_tests(ws, timeout):
        src = (Path(ws) / "m.py").read_text()
        # Baseline (pristine) passes; EVERY mutated source fails (tests catch it).
        return 0 if src == original else 1

    summary = run_trial(
        workspace=str(tmp_path), targets={"m.py": [2]},
        mutant_cap=10, per_mutant_timeout=5, run_tests=run_tests,
    )
    assert summary["status"] == "completed"
    assert summary["killed"] >= 1
    assert summary["survived"] == []


def test_pristine_checkout_never_mutated(tmp_path):
    # Every run happens in a COPY; the pristine checkout is never touched.
    original = "def f(x):\n    return x > 0\n"
    _write(tmp_path, "m.py", original)
    run_trial(
        workspace=str(tmp_path), targets={"m.py": [2]},
        mutant_cap=10, per_mutant_timeout=5, run_tests=lambda ws, t: 0,
    )
    assert (tmp_path / "m.py").read_text() == original


def test_stateful_test_cannot_poison_later_mutants(tmp_path):
    # A test that writes to a SIBLING file must not affect later mutants - each
    # runs in a fresh copy of the pristine tree (codex peer-review isolation).
    _write(tmp_path, "m.py", "def f(a, b):\n    return a == 1 or b == 2\n")

    def run_tests(ws, timeout):
        poison = Path(ws) / "poison.py"
        # If a prior run's pollution leaked into this copy, fail (mislabel).
        if poison.exists():
            return 1
        poison.write_text("x = 1\n")  # try to pollute for the next run
        return 0

    summary = run_trial(
        workspace=str(tmp_path), targets={"m.py": [2]},
        mutant_cap=10, per_mutant_timeout=5, run_tests=run_tests,
    )
    # Every mutant saw a pristine tree (no poison) -> all survive; none mislabeled.
    assert summary["status"] == "completed"
    assert summary["killed"] == 0 and summary["total"] >= 2
    # The pollution never reached the pristine checkout either.
    assert not (tmp_path / "poison.py").exists()


def test_timeout_is_not_a_survivor(tmp_path):
    original = "def f(x):\n    return x > 0\n"
    _write(tmp_path, "m.py", original)

    def run_tests(ws, timeout):
        src = (Path(ws) / "m.py").read_text()
        if src != original:
            raise TimeoutError  # every mutant hangs
        return 0  # baseline passes

    summary = run_trial(
        workspace=str(tmp_path), targets={"m.py": [2]},
        mutant_cap=10, per_mutant_timeout=5, run_tests=run_tests,
    )
    assert summary["timed_out"] >= 1
    assert summary["survived"] == []  # a hang is a kill, never a survivor


def test_mutant_cap_bounds_work(tmp_path):
    _write(tmp_path, "m.py", "def f(a, b, c):\n    return a == 1 or b == 2 or c == 3\n")
    calls = {"n": 0}

    def run_tests(ws, timeout):
        calls["n"] += 1
        return 0

    summary = run_trial(
        workspace=str(tmp_path), targets={"m.py": [2]},
        mutant_cap=2, per_mutant_timeout=5, run_tests=run_tests,
    )
    assert summary["total"] == 2
    # baseline + exactly 2 mutants
    assert calls["n"] == 3


def test_missing_target_file_skipped(tmp_path):
    _write(tmp_path, "present.py", "def f(x):\n    return x > 0\n")
    summary = run_trial(
        workspace=str(tmp_path),
        targets={"present.py": [2], "gone.py": [1]},
        mutant_cap=10, per_mutant_timeout=5, run_tests=lambda ws, t: 0,
    )
    # gone.py silently skipped; present.py still mutated.
    assert summary["status"] == "completed"
    assert summary["total"] >= 1


def test_survivor_rows_carry_reproducer(tmp_path):
    _write(tmp_path, "m.py", "def f(x):\n    return x > 0\n")
    summary = run_trial(
        workspace=str(tmp_path), targets={"m.py": [2]},
        mutant_cap=1, per_mutant_timeout=5, run_tests=lambda ws, t: 0,
    )
    row = summary["survived"][0]
    assert row["file"] == "m.py" and row["line"] == 2
    assert row["operator"] and row["original"] and row["mutated"]


def test_errored_mutant_classified_not_survived(tmp_path):
    original = "def f(x):\n    return x > 0\n"
    _write(tmp_path, "m.py", original)

    def run_tests(ws, timeout):
        if (Path(ws) / "m.py").read_text() != original:
            raise ValueError("runner blew up on the mutant")
        return 0  # baseline passes

    summary = run_trial(
        workspace=str(tmp_path), targets={"m.py": [2]},
        mutant_cap=10, per_mutant_timeout=5, run_tests=run_tests,
    )
    assert summary["errored"] >= 1
    assert summary["survived"] == []


def test_baseline_timeout_degrades_with_reason(tmp_path):
    _write(tmp_path, "m.py", "def f(x):\n    return x > 0\n")

    def run_tests(ws, timeout):
        raise TimeoutError

    summary = run_trial(
        workspace=str(tmp_path), targets={"m.py": [2]},
        mutant_cap=10, per_mutant_timeout=5, run_tests=run_tests,
    )
    assert summary["status"] == "degraded" and summary["reason"] == "baseline_timeout"


def test_baseline_error_degrades_with_reason(tmp_path):
    _write(tmp_path, "m.py", "def f(x):\n    return x > 0\n")

    def run_tests(ws, timeout):
        raise RuntimeError("runner broke at baseline")

    summary = run_trial(
        workspace=str(tmp_path), targets={"m.py": [2]},
        mutant_cap=10, per_mutant_timeout=5, run_tests=run_tests,
    )
    assert summary["status"] == "degraded" and summary["reason"] == "baseline_error"


def test_unsafe_target_path_skipped(tmp_path):
    _write(tmp_path, "ok.py", "def f(x):\n    return x > 0\n")
    summary = run_trial(
        workspace=str(tmp_path),
        targets={"ok.py": [2], "../escape.py": [1], "/etc/passwd": [1]},
        mutant_cap=10, per_mutant_timeout=5, run_tests=lambda ws, t: 0,
    )
    # The traversal/absolute paths are never opened; ok.py still mutated.
    assert summary["status"] == "completed"
    assert all(row["file"] == "ok.py" for row in summary["survived"])


def test_all_targets_absent_degrades(tmp_path):
    # Targets provided but no file present -> degrade, NEVER a clean pass.
    summary = run_trial(
        workspace=str(tmp_path), targets={"gone.py": [1], "also_gone.py": [2]},
        mutant_cap=10, per_mutant_timeout=5, run_tests=lambda ws, t: 0,
    )
    assert summary["status"] == "degraded"
    assert summary["reason"] == "targets_absent_from_checkout"


