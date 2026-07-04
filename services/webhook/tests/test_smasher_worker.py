"""Trial worker tests (#469) — the in-Job mutate-and-run loop.

`run_trial` is the injectable core: given a checked-out workspace, the target
lines, and a `run_tests` callable, it applies each mutant, classifies
survived/killed/timed_out, restores the file, and returns the summary dict the
worker writes to the termination message. Tested with a fake `run_tests` so no
real pytest subprocess runs.
"""

from __future__ import annotations

from pathlib import Path

from personas.smasher.trial_worker import run_trial


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


def test_file_restored_after_each_mutant(tmp_path):
    original = "def f(x):\n    return x > 0\n"
    _write(tmp_path, "m.py", original)
    run_trial(
        workspace=str(tmp_path), targets={"m.py": [2]},
        mutant_cap=10, per_mutant_timeout=5, run_tests=lambda ws, t: 0,
    )
    # The workspace file is left pristine (each mutant is reverted).
    assert (tmp_path / "m.py").read_text() == original


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
