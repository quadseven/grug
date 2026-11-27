import json

import pytest
from escape_sweep import main


def _snapshot():
    return {
        "repo": "example/project",
        "as_of": "2026-11-15T00:00:00Z",
        "pulls": [
            {
                "number": 10,
                "merged_at": "2026-10-01T00:00:00Z",
                "files": ["src/task.py"],
            },
            {
                "number": 20,
                "merged_at": "2026-11-01T00:00:00+00:00",
                "files": ["src/task.py"],
                "body": "Fixes regression from #10",
            },
        ],
        "review_evidence": {"10": {"reviews": [], "findings": []}},
    }


def test_preview_emits_only_new_rows_for_this_repository(tmp_path, capsys):
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps(_snapshot()), encoding="utf-8")
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        json.dumps({
            "repo": "example/other",
            "reviewer": "escape-loop",
            "class": "escape",
            "evidence": "correcting_pr=20",
        }) + "\n",
        encoding="utf-8",
    )
    argv = [str(snapshot), "--ledger", str(ledger)]

    assert main(argv) == 0
    output = capsys.readouterr().out
    row = json.loads(output)
    assert (row["pr"], row["finding"], row["ts"]) == (
        10, "not-reviewed", "2026-11-01T00:00:00+00:00",
    )
    assert row["evidence"].startswith("correcting_pr=20 ")

    ledger.write_text(ledger.read_text(encoding="utf-8") + output, encoding="utf-8")
    assert main(argv) == 0
    assert capsys.readouterr().out == ""


def test_missing_coverage_and_future_corrections_do_not_emit_rows(tmp_path, capsys):
    data = _snapshot()
    future = {**data["pulls"][1], "number": 21, "merged_at": "2026-11-16T00:00:00Z"}
    data["pulls"].append(future)
    data["review_evidence"] = {}
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps(data), encoding="utf-8")
    assert main([str(snapshot)]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Corrections without review evidence: 20" in captured.err

    data["review_evidence"] = _snapshot()["review_evidence"]
    snapshot.write_text(json.dumps(data), encoding="utf-8")
    assert main([str(snapshot)]) == 0
    assert json.loads(capsys.readouterr().out)["evidence"].startswith("correcting_pr=20 ")


def test_seen_paths_use_captured_review_scope(tmp_path, capsys):
    data = _snapshot()
    data["review_evidence"]["10"]["reviews"] = [
        {"source": "elder", "seen_paths": ["src/task.py"]},
    ]
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps(data), encoding="utf-8")
    assert main([str(snapshot)]) == 0
    assert json.loads(capsys.readouterr().out)["finding"] == "seen-not-flagged"


def test_invalid_path_list_fails_before_emitting_rows(tmp_path, capsys):
    data = _snapshot()
    data["pulls"][0]["files"] = "src/task.py"
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main([str(snapshot)])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "files must be an array of strings" in captured.err
