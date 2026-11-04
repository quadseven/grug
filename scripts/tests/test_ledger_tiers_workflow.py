"""The weekly ledger tier report runs on a schedule and writes only to the
job summary (#1570)."""

from __future__ import annotations

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _ROOT / ".github" / "workflows" / "report.ledger-tiers.yml"


def _load() -> dict:
    data = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML reads the bare key `on` as the boolean True.
    data["on"] = data.pop(True, data.get("on"))
    return data


def test_runs_weekly_and_on_demand():
    triggers = _load()["on"]
    assert "workflow_dispatch" in triggers
    crons = [entry["cron"] for entry in triggers["schedule"]]
    assert len(crons) == 1
    _minute, _hour, day, month, weekday = crons[0].split()
    assert day == "*" and month == "*" and weekday.isdigit(), crons[0]


def test_reports_tiers_into_the_job_summary_with_read_only_access():
    data = _load()
    assert data["permissions"] == {"contents": "read"}
    steps = data["jobs"]["tiers"]["steps"]
    run = "\n".join(step.get("run", "") for step in steps)
    assert "python3 -m ledger --tiers ../../logs/review-ledger.jsonl" in run
    assert '>> "$GITHUB_STEP_SUMMARY"' in run
    assert "set -euo pipefail" in run
    for forbidden in ("gh issue", "gh pr", "curl", "secrets."):
        assert forbidden not in _WORKFLOW.read_text(encoding="utf-8"), forbidden
