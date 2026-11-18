import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shepherd import report_shepherd_pr_queue

REPO = "example-org/example-repo"
HEAD = "a" * 40


def test_queue_does_not_treat_failed_bot_lookup_as_clean(tmp_path):
    def run(argv):
        if argv[:3] == ["gh", "pr", "list"]:
            return json.dumps([{"number": 123, "headRefOid": HEAD}])
        assert argv[:2] == ["gh", "api"]
        if any("/pulls?" in arg for arg in argv):
            return json.dumps([[{"number": 123, "head": {"sha": HEAD}}]])
        if any("/check-runs" in arg for arg in argv):
            checks = [{"name": "tests", "status": "completed", "conclusion": "success"}]
            return json.dumps([{"check_runs": checks}] if "--slurp" in argv else checks)
        if any("/reviews" in arg for arg in argv):
            raise RuntimeError("denied")
        if "graphql" in argv:
            return json.dumps([{"data": {"repository": {"pullRequest": {
                "reviewThreads": {"nodes": [], "pageInfo": {"hasNextPage": False}},
            }}}}]) if "--slurp" in argv else "[]"
        return "[[]]" if "--slurp" in argv else "[]"

    report = report_shepherd_pr_queue(REPO, run, 1000, str(tmp_path / "state.json"))

    assert "PR #123: NOT READY" in report
    assert "review bodies unavailable" in report
    assert f"https://github.com/{REPO}/pull/123" in report
