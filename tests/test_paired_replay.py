import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bench import paired_replay  # noqa: E402
from bench.paired_replay import CORPUS_SHA256, ReplayError, _arm_result, _export_checkout, _scored_metrics, measure  # noqa: E402
from bench.replay_inputs import Snapshot  # noqa: E402


class Pacer:
    def __init__(self):
        self.calls = []

    def __call__(self, label):
        self.calls.append(label)

    def check(self):
        self.calls.append("done")


def _snapshot(case_id):
    return Snapshot(case_id, "example/repo", 1, Path("."), "a" * 40, "b" * 40, "diff", "a" * 64)


def _result(**extra):
    return {"status": "scored", "findings": [], "verdict_seconds": 2.0,
            "model": "model-v1", "backend": "cave", **extra}


@pytest.mark.parametrize("extra", [
    {"status": "partial"}, {"model": "fallback"}, {"backend": "fallback"}, {"model": None},
    {"verdict_seconds": None}, {"verdict_seconds": True}, {"verdict_seconds": float("inf")},
    {"findings": [{"path": "wrong.py", "line": True, "severity": "high", "rule": "rule", "text": "text"}]},
])
def test_unattributed_incomplete_or_malformed_arms_do_not_score(extra):
    with pytest.raises(ReplayError):
        _arm_result(_result(**extra), model="model-v1", backend="cave", label="case")


def _report(revision, rows):
    metrics = {
        "defects": 1, "caught": 1, "catch_rate": 1.0, "false_positives_per_clean_pr": 0.0,
        "median_time_to_verdict_s": 2.0, "not_scored": {},
    }
    return {"cases": rows, "mode": "replay", "grug_sha": revision,
            "corpus_sha256": CORPUS_SHA256, "summary": {"public": {"all": metrics}}}


@pytest.mark.parametrize("rows", [
    [{"id": "bug", "status": "scored"}],
    [{"id": "bug", "status": "scored"}, {"id": "clean", "status": "not-replayed"}],
    [{"id": "bug", "status": "scored"}, {"id": "bug", "status": "scored"}],
])
def test_successful_scorer_exit_alone_does_not_prove_completeness(rows):
    with pytest.raises(ReplayError, match="every case"):
        _scored_metrics(_report("old", rows), {"bug", "clean"}, "old")


@pytest.mark.parametrize(("configured", "expected_timeout"), [(None, 150), ("240.5", 240.5), ("300", 300)])
def test_pair_alternates_order_with_one_recorded_timeout(tmp_path, monkeypatch, configured, expected_timeout):
    snapshots = [_snapshot("bug"), _snapshot("clean")]
    pace = Pacer()
    checkouts = {"baseline": Path("old"), "candidate": Path("new")}
    revisions = {"baseline": "pre", "candidate": "post"}
    calls = []

    if configured is None:
        monkeypatch.delenv("GRUG_BENCH_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("GRUG_BENCH_TIMEOUT_SECONDS", configured)

    def arm(_args, *, cwd, label, timeout, env, input):
        assert timeout == 1200
        data = json.loads(input)
        calls.append((cwd.name, label, data["diff_sha256"], data["backend"], data["model"]))
        assert float(env["GRUG_BENCH_TIMEOUT_SECONDS"]) == expected_timeout
        # Each worker still gets the original timeout if the ambient value changes mid-pair.
        monkeypatch.setenv("GRUG_BENCH_TIMEOUT_SECONDS", "100")
        return json.dumps(_result())

    monkeypatch.setattr(paired_replay, "_command", arm)

    def score(suite, path, revision, expected_ids):
        assert path.is_file()
        return _scored_metrics(_report(revision, [
            {"id": case_id, "status": "scored"} for case_id in sorted(expected_ids)
        ]), expected_ids, revision)

    output = tmp_path / "out"
    outcome = measure(snapshots, suite=tmp_path, output=output, checkouts=checkouts,
                      revisions=revisions, model="model-v1", pace=pace, score=score)
    assert calls == [
        ("old", "bug", "a" * 64, "cave", "model-v1"),
        ("new", "bug", "a" * 64, "cave", "model-v1"),
        ("new", "clean", "a" * 64, "cave", "model-v1"),
        ("old", "clean", "a" * 64, "cave", "model-v1"),
    ]
    assert pace.calls == ["bug (baseline)", "bug (candidate)", "clean (candidate)", "clean (baseline)", "done"]
    assert outcome["revisions"] == revisions
    assert outcome["delta"] == {key: 0 for key in outcome["scores"]["baseline"]}
    assert json.loads((output / "comparison.json").read_text())["request_timeout_seconds"] == expected_timeout


@pytest.mark.parametrize("configured", ["", "bad", "-1", "0", "nan", "inf", "300.01"])
def test_pair_refuses_invalid_timeout_before_starting_a_worker(tmp_path, monkeypatch, configured):
    monkeypatch.setenv("GRUG_BENCH_TIMEOUT_SECONDS", configured)
    pace = Pacer()
    output = tmp_path / "out"
    with pytest.raises(ReplayError, match="GRUG_BENCH_TIMEOUT_SECONDS"):
        measure([_snapshot("bug")], suite=tmp_path, output=output,
                checkouts={"baseline": tmp_path, "candidate": tmp_path},
                revisions={"baseline": "old", "candidate": "new"}, model="model-v1", pace=pace,
                run_arm=lambda *args: pytest.fail("an invalid timeout cannot start a worker"))
    assert not pace.calls
    assert not output.exists()


def test_incomplete_arm_does_not_publish_any_comparison(tmp_path):
    def fail(*args):
        pytest.fail("a partial run cannot call the scorer")

    with pytest.raises(ReplayError, match="complete verdict"):
        measure([_snapshot("bug")], suite=tmp_path, output=tmp_path / "out",
                checkouts={"baseline": tmp_path, "candidate": tmp_path},
                revisions={"baseline": "old", "candidate": "new"}, model="model-v1",
                pace=Pacer(), run_arm=lambda *args: _result(status="partial"), score=fail)
    assert not list((tmp_path / "out").iterdir())


def test_measurement_exports_the_named_commit_instead_of_live_worktree(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()

    def git(*args):
        return subprocess.check_output(
            ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
             "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args], cwd=root,
        ).strip()

    git("init", "-q")
    source = root / "review.py"
    source.write_text("review = 'original'\n", encoding="ascii")
    git("add", "review.py")
    git("commit", "-qm", "Record replay code")
    commit = git("rev-parse", "HEAD").decode("ascii")
    source.write_text("review = 'worktree edit'\n", encoding="ascii")
    frozen = tmp_path / "frozen"
    _export_checkout(root, commit, frozen, "review.py")
    assert (frozen / "review.py").read_text(encoding="ascii") == "review = 'original'\n"
