import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lane_review_metrics as metrics


def comment(comment_id, *, body=None, user_type="Bot", reply_to=None):
    return {
        "id": comment_id,
        "user": {
            "login": "grug-tribe[bot]" if user_type == "Bot" else "reviewer",
            "type": user_type,
        },
        "body": body
        if body is not None
        else (
            "high | _maintainability_ | `high-complexity` | small\n\n"
            "<!-- grug-rule:high-complexity -->"
        ),
        "path": "module.py",
        "side": "RIGHT",
        "original_line": 7,
        "original_commit_id": "original",
        "created_at": "2026-10-12T10:03:00Z",
        "updated_at": "2026-10-12T10:04:00Z",
        "in_reply_to_id": reply_to,
    }


def test_counts_each_finding_once_and_keeps_category_and_rule_breakdowns():
    original = comment(3)["body"]
    dismissed = "<!-- grug-state:dismissed -->\n\n<!-- grug-original -->\n" + original
    comments = [
        comment(10, user_type="User", reply_to=1),
        comment(1),
        comment(2),
        comment(11, user_type="User", reply_to=2),
        comment(3, body=dismissed),
        comment(
            4,
            body="<!-- grug-state:resolved -->\n\n<!-- grug-original -->\n" + original,
        ),
        comment(12, reply_to=4),
        comment(5, body="A bot's inline acknowledgment"),
        comment(6, user_type="User"),
    ]
    findings = metrics.classify_findings(
        comments,
        lambda root: "2026-10-12T10:05:00Z" if root["id"] in (1, 3) else None,
    )
    report = metrics.summarize([findings, []], [])

    counts = {
        "findings_total": 4,
        "fixed": 1,
        "answered": 1,
        "dismissed": 1,
        "unanswered": 1,
        "fixed_share": 0.25,
    }
    assert {key: report[key] for key in counts} == counts
    assert report["unanswered_per_pr"] == 0.5
    assert report["by_severity"] == {"high": counts}
    assert report["by_category"] == {"maintainability": counts}
    assert report["by_rule"] == {"high-complexity": counts}


def test_counts_non_bot_reply_from_an_imported_account():
    report = metrics.summarize(
        [
            metrics.classify_findings(
                [comment(1), comment(2, user_type="Mannequin", reply_to=1)],
                lambda _: None,
            )
        ],
        [],
    )

    assert report["answered"] == 1


@pytest.mark.parametrize(
    ("durations", "median", "p90"),
    [
        ([50, 10, 30, 20], 25, 44),
        ([12], 12, 12),
        ([], None, None),
    ],
)
def test_ready_percentiles(durations, median, p90):
    report = metrics.summarize([], durations)

    assert report["median_minutes_to_ready"] == median
    assert report["p90_minutes_to_ready"] == p90
    assert report["ready_prs"] == len(durations)
    assert report["fixed_share"] is None
    assert report["unanswered_per_pr"] is None


def comparison(patch, *, status="ahead", filename="module.py", previous=None):
    return {
        "status": status,
        "files": [
            {
                "filename": filename,
                "previous_filename": previous,
                "status": "renamed" if previous else "modified",
                "deletions": sum(line.startswith("-") for line in patch.splitlines()),
                "patch": patch,
            }
        ],
    }


def test_later_commit_changes_anchor_even_if_head_restores_it():
    root = comment(1)
    root["original_start_line"] = 6
    shifted = comparison("@@ -5,3 +5,4 @@\n context\n+inserted\n start\n end")
    diverged = comparison("@@ -7 +7 @@\n-end\n+changed", status="diverged")
    edited = comparison(
        "@@ -6,2 +6,2 @@\n-start\n+changed\n end",
        filename="renamed.py",
        previous="module.py",
    )
    comparisons = {
        "shifted": shifted,
        "diverged": diverged,
        "edited": edited,
        "head": shifted,
    }
    commits = [
        (sha, f"2026-10-12T10:0{minute}:00Z")
        for minute, sha in enumerate(comparisons, start=4)
    ]

    def compare(original, later):
        assert original == "original"
        return comparisons[later]

    assert metrics.find_line_change(root, commits[:2], compare) is None
    assert metrics.find_line_change(root, commits, compare) == "2026-10-12T10:06:00Z"
    assert metrics.find_line_change({**root, "side": "LEFT"}, commits, compare) is None


def test_missing_patch_is_not_counted_as_an_unchanged_line():
    incomplete = comparison("")
    incomplete["files"][0]["deletions"] = 1

    with pytest.raises(ValueError, match="omitted part"):
        metrics.find_line_change(
            comment(1),
            [("head", "2026-10-12T10:05:00Z")],
            lambda *_: incomplete,
        )


@pytest.fixture
def github():
    def pr(number, *, body="Loom ticket #42, lane example", date="2026-10-12"):
        return {"number": number, "body": body, "created_at": f"{date}T10:00:00Z"}

    def check(name, conclusion, minute):
        return {
            "name": name,
            "status": "completed",
            "conclusion": conclusion,
            "completed_at": f"2026-10-12T10:{minute}:00Z",
        }

    commits = [
        {"commit": {"oid": "original", "committedDate": "2026-10-12T10:02:00Z"}},
        {"commit": {"oid": "head", "committedDate": "2026-10-12T10:07:00Z"}},
    ]
    first_page = [pr(4, date="2026-10-13"), pr(1)] + [pr(5, body="Other work")] * 98
    responses = {
        "pulls?state=all&sort=created&direction=desc&per_page=100&page=1": first_page,
        "pulls?state=all&sort=created&direction=desc&per_page=100&page=2": [
            pr(2),
            pr(3),
        ],
        "pulls/1": {
            "head": {"sha": "head"},
            "state": "closed",
            "draft": False,
            "merged_at": "2026-10-12T10:30:00Z",
        },
        "pulls/2": {
            "head": {"sha": "other"},
            "state": "open",
            "draft": False,
            "mergeable_state": "clean",
            "merged_at": None,
        },
        "pulls/3": {
            "head": {"sha": "blocked"},
            "state": "open",
            "draft": False,
            "mergeable_state": "clean",
            "merged_at": None,
        },
        "pulls/1/comments?per_page=100": [[], [comment(1)]],
        "pulls/2/comments?per_page=100": [[]],
        "pulls/3/comments?per_page=100": [[]],
        "graphql": [
            {"data": {"repository": {"pullRequest": {"commits": {"nodes": [node]}}}}}
            for node in commits
        ],
        "compare/original...head?per_page=1": comparison("@@ -7 +7 @@\n-old\n+new"),
        "commits/head/check-runs?per_page=100&filter=latest": [
            {
                "check_runs": [
                    check("Grug - Chief", "neutral", "01"),
                    check("Grug - Elder", "success", "10"),
                    check("grug python tests", "success", "16"),
                ]
            }
        ],
        "commits/other/check-runs?per_page=100&filter=latest": [
            {
                "check_runs": [
                    check("Grug - Chief", "success", "01"),
                    check("Grug - Elder", "success", "05"),
                    check("grug python tests", "success", "05"),
                ]
            }
        ],
        "commits/blocked/check-runs?per_page=100&filter=latest": [
            {
                "check_runs": [
                    check("Grug - Chief", "success", "01"),
                    check("Grug - Elder", "neutral", "05"),
                    check("grug python tests", "success", "05"),
                ]
            }
        ],
        "commits/head/statuses?per_page=100": [
            [
                {
                    "context": "legacy-ci",
                    "state": "success",
                    "created_at": "2026-10-12T10:18:00Z",
                },
                {
                    "context": "legacy-ci",
                    "state": "failure",
                    "created_at": "2026-10-12T10:15:00Z",
                },
            ]
        ],
        "commits/other/statuses?per_page=100": [[]],
        "issues/1/timeline?per_page=100": [
            [
                {"event": "ready_for_review", "created_at": "2026-10-12T10:20:00Z"},
            ]
        ],
        "issues/2/timeline?per_page=100": [[]],
    }
    return responses


def test_cli_reports_one_object_for_lane_prs_with_open_dates_in_window(github, capsys):
    def run(argv):
        assert argv[:2] == ["gh", "api"]
        if "graphql" in argv:
            assert "--paginate" in argv and "pr=1" in argv
            return json.dumps(github["graphql"])
        endpoint = argv[-1].removeprefix("repos/example-org/example-repo/")
        return json.dumps(github[endpoint])

    assert (
        metrics.main(
            [
                "--repo",
                "example-org/example-repo",
                "--since",
                "2026-10-12",
                "--until",
                "2026-10-13",
            ],
            run=run,
        )
        == 0
    )

    report = json.loads(capsys.readouterr().out)
    assert report["lane_prs"] == 3
    assert report["findings_total"] == report["fixed"] == 1
    assert report["ready_prs"] == 2
    assert report["median_minutes_to_ready"] == 12.5
    assert report["p90_minutes_to_ready"] == 18.5
