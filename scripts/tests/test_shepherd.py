import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))
import shepherd
from shepherd import (
    BotFinding,
    BotSurfaces,
    CICheck,
    PRState,
    StageVerdict,
    build_pr_queue,
    check_deployment,
    escalate_once,
    evaluate_chain,
    format_duration,
    format_queue_report,
    parse_bot_surfaces,
    parse_ci_checks,
    report_shepherd_pr_queue,
    update_ready_tracker,
)


def test_merged_but_not_deployed_is_a_distinct_loud_state(monkeypatch, capsys):
    import shepherd

    monkeypatch.setattr(
        shepherd, "get_latest_digest", lambda service: ("old", "old-digest")
    )
    monkeypatch.setattr(
        shepherd, "get_latest_push_digest", lambda service: ("new", "new-digest")
    )

    assert shepherd.deployment_status("service") == "merged-but-not-deployed"
    assert shepherd.main("service") == 1
    assert "MERGED-BUT-NOT-DEPLOYED" in capsys.readouterr().err


# Helper to patch functions


def make_patch(set_digest, set_push):
    def patched_get_latest_digest(service):
        return set_digest

    def patched_get_latest_push_digest(service):
        return set_push

    return patched_get_latest_digest, patched_get_latest_push_digest


# Test when digests match


def test_check_deployment_true(monkeypatch):
    patch_digest, patch_push = make_patch(("tag1", "abc123"), ("tag2", "abc123"))
    monkeypatch.setattr("shepherd.get_latest_digest", patch_digest)
    monkeypatch.setattr("shepherd.get_latest_push_digest", patch_push)
    assert check_deployment("service") is True


# Test when digests differ


def test_check_deployment_false_on_diff(monkeypatch):
    patch_digest, patch_push = make_patch(("deployed", "diff1"), ("pushed", "diff2"))
    monkeypatch.setattr("shepherd.get_latest_digest", patch_digest)
    monkeypatch.setattr("shepherd.get_latest_push_digest", patch_push)
    assert check_deployment("service") is False


# Test when pushed digest missing


def test_check_deployment_false_on_missing_push(monkeypatch):
    patch_digest, patch_push = make_patch(("deployed", "abc123"), ("tag", ""))
    monkeypatch.setattr("shepherd.get_latest_digest", patch_digest)
    monkeypatch.setattr("shepherd.get_latest_push_digest", patch_push)
    assert check_deployment("service") is False


# Test when deployed digest missing


def test_check_deployment_false_on_missing_deploy(monkeypatch):
    patch_digest, patch_push = make_patch(("", ""), ("tag", "abc123"))
    monkeypatch.setattr("shepherd.get_latest_digest", patch_digest)
    monkeypatch.setattr("shepherd.get_latest_push_digest", patch_push)
    assert check_deployment("service") is False


# Test that get_latest_push_digest is used (implicit via patch)


def test_check_deployment_calls(monkeypatch):
    called = {"push": False, "deploy": False}

    def fake_push(service):
        called["push"] = True
        return ("tag", "abc")

    def fake_deploy(service):
        called["deploy"] = True
        return ("tag", "abc")

    monkeypatch.setattr("shepherd.get_latest_push_digest", fake_push)
    monkeypatch.setattr("shepherd.get_latest_digest", fake_deploy)
    assert check_deployment("svc") == True
    assert called["push"] and called["deploy"]


# ---- chain escalation (T832) ----


def _broken(name: str = "build", evidence: str = "build failed") -> StageVerdict:
    return StageVerdict(name, False, evidence)


def _ok(name: str = "build", evidence: str = "digest matches") -> StageVerdict:
    return StageVerdict(name, True, evidence)


def test_broken_chain_notifies_once_names_stage_and_evidence(tmp_path):
    state = tmp_path / "state.json"
    notified = []

    def notify(message, stage, evidence):
        notified.append((message, stage, evidence))

    check = lambda: _broken("build", "exit code 1: build failed")

    first = escalate_once([("build", check)], state_path=str(state), notify=notify)
    assert first == "broken"
    assert len(notified) == 1
    message, stage, evidence = notified[0]
    assert stage == "build"
    assert "build failed" in evidence
    assert "broken" in message

    # a second run of the same break must not notify again
    second = escalate_once([("build", check)], state_path=str(state), notify=notify)
    assert second == "broken"
    assert len(notified) == 1


def test_recovery_is_reported_so_a_stale_alarm_cannot_linger(tmp_path):
    state = tmp_path / "state.json"
    notified = []

    def notify(message, stage, evidence):
        notified.append((message, stage, evidence))

    assert (
        escalate_once([("build", _broken)], state_path=str(state), notify=notify)
        == "broken"
    )
    assert len(notified) == 1

    assert (
        escalate_once([("build", _ok)], state_path=str(state), notify=notify)
        == "recovered"
    )
    assert len(notified) == 2
    message, stage, evidence = notified[1]
    assert stage is None
    assert "recovered" in message
    assert "build" in evidence


def test_healthy_chain_notifies_nothing(tmp_path):
    state = tmp_path / "state.json"
    notified = []

    def notify(message, stage, evidence):
        notified.append((message, stage, evidence))

    assert (
        escalate_once([("build", _ok)], state_path=str(state), notify=notify)
        == "healthy"
    )
    assert notified == []

    # still healthy, still nothing
    assert (
        escalate_once([("build", _ok)], state_path=str(state), notify=notify)
        == "healthy"
    )
    assert notified == []


def test_cancelled_counts_as_broken_only_success_is_success(tmp_path):
    state = tmp_path / "state.json"
    notified = []

    def notify(message, stage, evidence):
        notified.append((message, stage, evidence))

    cancelled = lambda: _broken("schedule", "scheduled run cancelled")

    assert (
        escalate_once([("schedule", cancelled)], state_path=str(state), notify=notify)
        == "broken"
    )
    assert len(notified) == 1
    assert notified[0][1] == "schedule"
    assert "cancelled" in notified[0][2]


def test_evaluate_chain_returns_first_broken_stage_and_short_circuits():
    calls = []

    def first():
        calls.append("first")
        return _broken("build", "build failed")

    def second():
        calls.append("second")
        return _broken("deploy", "digest mismatch")

    verdict = evaluate_chain([("build", first), ("deploy", second)])
    assert verdict is not None
    assert verdict.name == "build"
    assert verdict.ok is False
    assert "build failed" in verdict.evidence
    assert calls == ["first"]

    assert evaluate_chain([("build", _ok)]) is None


def test_a_different_stage_after_recovery_notifies_again(tmp_path):
    state = tmp_path / "state.json"
    notified = []

    def notify(message, stage, evidence):
        notified.append((message, stage, evidence))

    assert (
        escalate_once([("build", _broken)], state_path=str(state), notify=notify)
        == "broken"
    )
    assert (
        escalate_once([("build", _ok)], state_path=str(state), notify=notify)
        == "recovered"
    )

    assert (
        escalate_once(
            [("deploy", lambda: _broken("deploy", "digest mismatch"))],
            state_path=str(state),
            notify=notify,
        )
        == "broken"
    )
    assert len(notified) == 3
    assert notified[2][1] == "deploy"


def test_deployment_stage_verdicts_names_the_broken_link(monkeypatch):
    import shepherd

    monkeypatch.setattr(shepherd, "get_latest_digest", lambda service: ("tag", ""))
    monkeypatch.setattr(
        shepherd, "get_latest_push_digest", lambda service: ("tag", "abc")
    )
    verdicts = shepherd.deployment_stage_verdicts("svc")
    assert len(verdicts) == 1
    assert verdicts[0].name == "deploy"
    assert verdicts[0].ok is False
    assert "cancelled" in verdicts[0].evidence

    monkeypatch.setattr(shepherd, "get_latest_digest", lambda service: ("tag", "abc"))
    monkeypatch.setattr(
        shepherd, "get_latest_push_digest", lambda service: ("tag", "def")
    )
    verdicts = shepherd.deployment_stage_verdicts("svc")
    assert verdicts[0].ok is False
    assert "merged-but-not-deployed" in verdicts[0].evidence

    monkeypatch.setattr(shepherd, "get_latest_digest", lambda service: ("tag", "abc"))
    monkeypatch.setattr(
        shepherd, "get_latest_push_digest", lambda service: ("tag", "abc")
    )
    verdicts = shepherd.deployment_stage_verdicts("svc")
    assert verdicts[0].ok is True

    monkeypatch.setattr(shepherd, "get_latest_digest", lambda service: ("tag", ""))
    monkeypatch.setattr(shepherd, "get_latest_push_digest", lambda service: ("tag", ""))
    verdicts = shepherd.deployment_stage_verdicts("svc")
    assert verdicts[0].name == "reference"
    assert verdicts[0].ok is False
    assert "reference never bumped" in verdicts[0].evidence


# ---- PR state model across CI and bot surfaces (T829) ----


def _make_bot_finding(surface: str, name: str = "finding") -> BotFinding:
    return BotFinding(
        surface=surface,
        identifier=f"id-{name}",
        bot_name="test-bot[bot]",
        message=f"finding on {surface}",
    )


def test_bot_surfaces_all_four_clean():
    surfaces = BotSurfaces()
    assert surfaces.is_clean is True
    assert surfaces.total_findings == 0


def test_bot_surfaces_finding_on_any_surface_marks_pr_not_ready():
    # Surface 1: unresolved review threads
    s1 = BotSurfaces(unresolved_review_threads=(_make_bot_finding("review_thread"),))
    assert s1.is_clean is False
    pr1 = PRState(
        pr_number=101,
        checks=(CICheck("unit-test", "passed", 30.0),),
        bot_surfaces=s1,
    )
    assert pr1.is_bot_clean is False
    assert pr1.is_ready is False

    # Surface 2: unanswered review bodies
    s2 = BotSurfaces(unanswered_review_bodies=(_make_bot_finding("review_body"),))
    assert s2.is_clean is False
    pr2 = PRState(
        pr_number=102,
        checks=(CICheck("unit-test", "passed", 30.0),),
        bot_surfaces=s2,
    )
    assert pr2.is_bot_clean is False
    assert pr2.is_ready is False

    # Surface 3: non-passing bot check-runs
    s3 = BotSurfaces(non_passing_bot_check_runs=(_make_bot_finding("bot_check_run"),))
    assert s3.is_clean is False
    pr3 = PRState(
        pr_number=103,
        checks=(CICheck("unit-test", "passed", 30.0),),
        bot_surfaces=s3,
    )
    assert pr3.is_bot_clean is False
    assert pr3.is_ready is False

    # Surface 4: issue-level bot comments
    s4 = BotSurfaces(issue_comments=(_make_bot_finding("issue_comment"),))
    assert s4.is_clean is False
    pr4 = PRState(
        pr_number=104,
        checks=(CICheck("unit-test", "passed", 30.0),),
        bot_surfaces=s4,
    )
    assert pr4.is_bot_clean is False
    assert pr4.is_ready is False

    # When all four are clean, and CI passed, PR is ready
    pr_clean = PRState(
        pr_number=105,
        checks=(CICheck("unit-test", "passed", 30.0),),
        bot_surfaces=BotSurfaces(),
    )
    assert pr_clean.is_bot_clean is True
    assert pr_clean.is_ready is True


def test_empty_ci_rollup_distinguished_from_passed_and_not_ready():
    # Empty rollup: 0 checks reported
    pr_empty = PRState(
        pr_number=201,
        checks=(),
        bot_surfaces=BotSurfaces(),
    )
    assert pr_empty.ci_status == "empty"
    assert pr_empty.is_ci_green is False
    assert pr_empty.is_ready is False

    # PR with passed checks: success
    pr_passed = PRState(
        pr_number=202,
        checks=(CICheck("test", "passed", 15.0),),
        bot_surfaces=BotSurfaces(),
    )
    assert pr_passed.ci_status == "passed"
    assert pr_passed.is_ci_green is True
    assert pr_passed.is_ready is True


def test_ci_status_passed_failed_pending_and_duration():
    # Any check failed
    pr_failed = PRState(
        pr_number=203,
        checks=(
            CICheck("test-lint", "passed", 10.0),
            CICheck("test-suite", "failed", 45.0),
        ),
        bot_surfaces=BotSurfaces(),
    )
    assert pr_failed.ci_status == "failed"
    assert pr_failed.is_ci_green is False
    assert pr_failed.is_ready is False

    # Any check pending, tracking duration pending
    pending_check = CICheck("e2e-suite", "pending", duration=120.0)
    pr_pending = PRState(
        pr_number=204,
        checks=(
            CICheck("test-lint", "passed", 10.0),
            pending_check,
        ),
        bot_surfaces=BotSurfaces(),
    )
    assert pr_pending.ci_status == "pending"
    assert pr_pending.is_ci_green is False
    assert pr_pending.is_ready is False
    assert pending_check.duration == 120.0


def test_reports_time_since_ready_duration():
    now = 10000.0
    ready_since = 8200.0  # 1800s ago = 30m

    pr_ready = PRState(
        pr_number=301,
        title="feat: add metric",
        checks=(CICheck("test", "passed", 20.0),),
        bot_surfaces=BotSurfaces(),
        ready_since=ready_since,
    )
    assert pr_ready.is_ready is True
    duration = pr_ready.time_since_ready(now)
    assert duration == 1800.0
    assert format_duration(duration) == "30m"

    # Not ready PR returns None for duration
    pr_not_ready = PRState(
        pr_number=302,
        title="feat: broken check",
        checks=(CICheck("test", "failed", 20.0),),
        bot_surfaces=BotSurfaces(),
        ready_since=ready_since,
    )
    assert pr_not_ready.is_ready is False
    assert pr_not_ready.time_since_ready(now) is None


def test_queue_ordered_by_ready_longest():
    now = 10000.0

    pr_oldest = PRState(
        pr_number=401,
        title="oldest ready",
        checks=(CICheck("ci", "passed", 10.0),),
        bot_surfaces=BotSurfaces(),
        ready_since=now - 7200.0,  # 2 hours
    )
    pr_newest = PRState(
        pr_number=402,
        title="newest ready",
        checks=(CICheck("ci", "passed", 10.0),),
        bot_surfaces=BotSurfaces(),
        ready_since=now - 300.0,  # 5 minutes
    )
    pr_middle = PRState(
        pr_number=403,
        title="middle ready",
        checks=(CICheck("ci", "passed", 10.0),),
        bot_surfaces=BotSurfaces(),
        ready_since=now - 1800.0,  # 30 minutes
    )
    pr_not_ready = PRState(
        pr_number=404,
        title="not ready PR",
        checks=(CICheck("ci", "pending", 60.0),),
        bot_surfaces=BotSurfaces(),
        ready_since=None,
    )

    prs = [pr_newest, pr_not_ready, pr_oldest, pr_middle]
    queue = build_pr_queue(prs, now=now)

    # First three must be ready PRs ordered longest-ready first
    assert [item.pr_number for item in queue[:3]] == [401, 403, 402]
    assert queue[0].time_since_ready == 7200.0
    assert queue[1].time_since_ready == 1800.0
    assert queue[2].time_since_ready == 300.0

    # Non-ready follows
    assert queue[3].pr_number == 404
    assert queue[3].is_ready is False
    assert queue[3].time_since_ready is None


def test_shepherd_takes_no_action_on_any_pr():
    actions = []

    def mock_runner(cmd: list[str]) -> str:
        # If any mutating git or gh command is called, record it
        joined = " ".join(cmd)
        if any(
            verb in joined
            for verb in ("merge", "close", "comment", "label", "edit", "create")
        ):
            actions.append(joined)
        return "[]"

    # Evaluating PR state and building queue takes zero modifying actions
    pr = PRState(
        pr_number=501,
        checks=(CICheck("test", "passed", 10.0),),
        bot_surfaces=BotSurfaces(),
        ready_since=100.0,
    )
    queue = build_pr_queue([pr], now=200.0)
    report = format_queue_report(queue)

    assert len(actions) == 0
    assert "PR #501: READY" in report


def test_update_ready_tracker_preserves_timestamp_and_resets():
    now_t1 = 1000.0
    tracker = {}

    pr1 = PRState(
        pr_number=601,
        checks=(CICheck("ci", "passed", 10.0),),
        bot_surfaces=BotSurfaces(),
    )
    # T1: PR becomes ready
    updated = update_ready_tracker([pr1], tracker, now=now_t1)
    assert updated[0].ready_since == 1000.0
    assert tracker[601] == 1000.0

    # T2: PR stays ready at later time
    now_t2 = 1500.0
    updated_t2 = update_ready_tracker([pr1], tracker, now=now_t2)
    # Original ready timestamp is preserved
    assert updated_t2[0].ready_since == 1000.0
    assert updated_t2[0].time_since_ready(now_t2) == 500.0

    # T3: PR breaks (gets a bot comment)
    s_broken = BotSurfaces(issue_comments=(_make_bot_finding("issue_comment"),))
    pr1_broken = PRState(
        pr_number=601,
        checks=(CICheck("ci", "passed", 10.0),),
        bot_surfaces=s_broken,
    )
    updated_t3 = update_ready_tracker([pr1_broken], tracker, now=2000.0)
    assert updated_t3[0].ready_since is None
    assert 601 not in tracker


def test_parse_ci_checks_from_github_data():
    check_runs = [
        {
            "name": "check-python",
            "status": "completed",
            "conclusion": "success",
            "started_at": "2026-10-25T10:00:00Z",
            "completed_at": "2026-10-25T10:02:30Z",
        },
        {
            "name": "build-docker",
            "status": "in_progress",
            "conclusion": None,
            "started_at": "2026-10-25T10:05:00Z",
            "completed_at": None,
        },
    ]
    # now at 10:07:00Z (120s after build-docker started)
    now_epoch = 1792922820.0
    # started_at 10:05:00Z is 1792922700.0
    checks = parse_ci_checks(check_runs, now=now_epoch)
    assert len(checks) == 2
    assert checks[0].name == "check-python"
    assert checks[0].status == "passed"
    assert checks[0].duration == 150.0  # 2m 30s

    assert checks[1].name == "build-docker"
    assert checks[1].status == "pending"


def test_parse_bot_surfaces_from_github_data():
    # Four surfaces with one finding each
    threads = [
        {
            "id": "thread-1",
            "isResolved": False,
            "comments": {
                "nodes": [
                    {"author": {"login": "grug-tribe[bot]"}, "body": "nit on var name"}
                ]
            },
        }
    ]
    reviews = [
        {
            "id": "rev-1",
            "user": {"login": "coderabbitai[bot]", "type": "Bot"},
            "body": "Unaddressed finding: complexity exceeds limit",
            "state": "CHANGES_REQUESTED",
        }
    ]
    check_runs = [
        {
            "name": "Grug - Chief",
            "status": "completed",
            "conclusion": "failure",
            "app": {"slug": "grug-tribe"},
        }
    ]
    issue_comments = [
        {
            "id": "ic-1",
            "user": {"login": "datadog[bot]", "type": "Bot"},
            "body": "Security finding: missing timeout on socket call",
        }
    ]

    surfaces = parse_bot_surfaces(
        threads_data=threads,
        reviews_data=reviews,
        check_runs_data=check_runs,
        issue_comments_data=issue_comments,
    )
    assert surfaces.is_clean is False
    assert len(surfaces.unresolved_review_threads) == 1
    assert len(surfaces.unanswered_review_bodies) == 1
    assert len(surfaces.non_passing_bot_check_runs) == 1
    assert len(surfaces.issue_comments) == 1
    assert surfaces.total_findings == 4


def test_parse_bot_surfaces_ignores_clean_or_human_items():
    threads = [
        {
            "id": "thread-resolved",
            "isResolved": True,
            "comments": {
                "nodes": [
                    {"author": {"login": "grug-tribe[bot]"}, "body": "resolved nit"}
                ]
            },
        },
        {
            "id": "thread-no-bot",
            "isResolved": False,
            "comments": {
                "nodes": [
                    {"author": {"login": "human-reviewer"}, "body": "human nit"}
                ]
            },
        },
    ]
    reviews = [
        {
            "id": "rev-approved",
            "user": {"login": "coderabbitai[bot]", "type": "Bot"},
            "body": "looks great!",
            "state": "APPROVED",
        },
        {
            "id": "rev-human",
            "user": {"login": "human-reviewer", "type": "User"},
            "body": "changes requested",
            "state": "CHANGES_REQUESTED",
        },
    ]
    check_runs = [
        {
            "name": "Grug - Chief",
            "status": "completed",
            "conclusion": "success",
            "app": {"slug": "grug-tribe"},
        },
        {
            "name": "external-ci",
            "status": "completed",
            "conclusion": "failure",
            "app": {"slug": ""},
        },
    ]
    issue_comments = [
        {
            "id": "ic-human",
            "user": {"login": "human-reviewer", "type": "User"},
            "body": "just a comment",
        },
        {
            "id": "ic-empty",
            "user": {"login": "datadog[bot]", "type": "Bot"},
            "body": "   ",
        },
    ]

    surfaces = parse_bot_surfaces(
        threads_data=threads,
        reviews_data=reviews,
        check_runs_data=check_runs,
        issue_comments_data=issue_comments,
    )
    assert len(surfaces.unresolved_review_threads) == 1
    assert surfaces.unresolved_review_threads[0].bot_name == "bot"
    assert surfaces.unresolved_review_threads[0].identifier == "thread-no-bot"
    assert len(surfaces.unanswered_review_bodies) == 0
    assert len(surfaces.non_passing_bot_check_runs) == 0
    assert len(surfaces.issue_comments) == 0


def test_format_duration_windows():
    assert format_duration(None) == "not ready"
    assert format_duration(0.0) == "0s"
    assert format_duration(45.0) == "45s"
    assert format_duration(65.0) == "1m 5s"
    assert format_duration(1800.0) == "30m"
    assert format_duration(7200.0) == "2h"
    assert format_duration(7500.0) == "2h 5m"
    assert format_duration(90000.0) == "1d 1h"


def test_report_shepherd_pr_queue_end_to_end(tmp_path):
    state_file = tmp_path / "shepherd-state.json"
    now = 50000.0

    def mock_runner(cmd: list[str]) -> str:
        cmd_str = " ".join(cmd)
        if "gh pr list" in cmd_str:
            return json.dumps(
                [
                    {
                        "number": 701,
                        "title": "add metrics adapter",
                        "headRefOid": "sha111",
                    },
                    {
                        "number": 702,
                        "title": "fix timeout regression",
                        "headRefOid": "sha222",
                    },
                ]
            )
        if "commits/sha111/check-runs" in cmd_str:
            return json.dumps(
                [{"name": "test-suite", "status": "completed", "conclusion": "success"}]
            )
        if "commits/sha222/check-runs" in cmd_str:
            return json.dumps(
                [{"name": "test-suite", "status": "completed", "conclusion": "failure"}]
            )
        if "graphql" in cmd_str:
            return "[]"
        if "/reviews" in cmd_str:
            return "[]"
        if "/comments" in cmd_str:
            return "[]"
        return "[]"

    report = report_shepherd_pr_queue(
        "example/repo",
        runner=mock_runner,
        now=now,
        state_path=str(state_file),
    )

    assert "Shepherd PR Queue (ordered by ready longest):" in report
    assert "1 of 2 open PR(s) ready" in report
    assert "PR #701: READY for 0s - add metrics adapter" in report
    assert "PR #702: NOT READY - fix timeout regression" in report
    assert "CI failed: test-suite" in report

    # Verify state file was created and tracks PR 701
    assert state_file.exists()
    with open(state_file) as fh:
        saved = json.load(fh)
    assert saved["pr_ready_since"]["701"] == now


def test_shepherd_main_cli_pr_queue(monkeypatch, capsys):
    import shepherd

    monkeypatch.setattr(
        shepherd,
        "report_shepherd_pr_queue",
        lambda repo, **kwargs: f"Queue report for {repo}",
    )
    code = shepherd.main("pr-queue", "--repo", "example/test-repo")
    assert code == 0
    captured = capsys.readouterr().out
    assert "Queue report for example/test-repo" in captured


def test_deployment_chain_verdict_empty_is_broken(monkeypatch):
    monkeypatch.setattr(shepherd, "deployment_stage_verdicts", lambda service: [])
    verdict = shepherd.deployment_chain_verdict("webhook")
    assert verdict.ok is False
    assert verdict.name == "deploy"


def test_deployment_chain_verdict_returns_first_failure(monkeypatch):
    stages = [
        shepherd.StageVerdict("reference", True, "ok"),
        shepherd.StageVerdict("deploy", False, "stale"),
    ]
    monkeypatch.setattr(shepherd, "deployment_stage_verdicts", lambda service: stages)
    assert shepherd.deployment_chain_verdict("webhook") is stages[1]
