"""Blocking comment budget and the drip-feed rule (grug#1570)."""
from __future__ import annotations

from llm_client import Backend, FindingOrigin
from ledger import format_metrics, parse_row, tier_rates
from personas.code_reviewer import dispatch as cr_dispatch
from personas.code_reviewer.comment_budget import (
    budget_enabled,
    drip_fed,
    held_lines,
    split_for_budget,
)
from personas.code_reviewer.dedup import finding_key
from personas.code_reviewer.persona import (
    CodeReviewEvaluation,
    Finding,
)
from personas.code_reviewer.repo_config import RepoConfig, parse_repo_config

ORIGIN = FindingOrigin(backend=Backend.POOLSIDE, model="poolside/laguna-s")


def _finding(line=1, severity="high", rule="r", origins=(ORIGIN,)):
    return Finding(
        file="a.py", line=line, severity=severity, rule_name=rule, message="m",
        suggestion=None, origins=origins,
    )


def _blocking(count):
    return tuple(_finding(line=i + 1) for i in range(count))


def test_blocking_markings_stop_at_the_cap():
    split = split_for_budget(_blocking(7), cap=5, open_blocking=0, confidence={})

    assert len(split.inline) == 5
    assert len(split.held) == 2


def test_open_markings_from_earlier_rounds_use_the_budget():
    split = split_for_budget(_blocking(3), cap=5, open_blocking=4, confidence={})

    assert len(split.inline) == 1


def test_the_cap_keeps_the_top_severity_then_the_most_confident():
    criticals = (_finding(line=9, severity="critical"),)
    highs = _blocking(2)
    confidence = {finding_key("a.py", 2, "r"): (True, 0.9)}

    split = split_for_budget(highs + criticals, cap=2, open_blocking=0, confidence=confidence)

    assert sorted(f.line for f in split.inline) == [2, 9]
    assert [f.line for f in split.held] == [1]


def test_deterministic_findings_never_count_and_never_collapse():
    scanner = tuple(_finding(line=i + 1, origins=()) for i in range(8))

    split = split_for_budget(scanner, cap=1, open_blocking=0, confidence={})

    assert len(split.inline) == 8
    assert split.held == ()


def test_non_blocking_findings_stay_inline_unless_the_judge_doubts_them():
    kept = _finding(line=1, severity="medium")
    doubted = _finding(line=2, severity="low")
    confidence = {finding_key("a.py", 2, "r"): (False, 0.6)}

    split = split_for_budget((kept, doubted), cap=5, open_blocking=0, confidence=confidence)

    assert split.inline == (kept,)
    assert split.held == (doubted,)


def test_a_cap_of_zero_means_no_cap():
    split = split_for_budget(_blocking(9), cap=0, open_blocking=0, confidence={})

    assert len(split.inline) == 9


def test_a_low_confidence_non_blocking_finding_is_held():
    doubted = _finding(line=2, severity="medium")
    confidence = {finding_key("a.py", 2, "r"): (True, 0.4)}

    split = split_for_budget((doubted,), cap=5, open_blocking=0, confidence=confidence)

    assert split.held == (doubted,)


def test_a_finding_the_judge_rejects_ranks_below_one_it_confirms():
    rejected = _finding(line=1)
    confirmed = _finding(line=2)
    confidence = {
        finding_key("a.py", 1, "r"): (False, 0.8),
        finding_key("a.py", 2, "r"): (True, 0.6),
    }

    split = split_for_budget(
        (rejected, confirmed), cap=1, open_blocking=0, confidence=confidence,
    )

    assert split.inline == (confirmed,)


def test_held_lines_rank_highest_first_in_one_block():
    text = held_lines((_finding(line=1), _finding(line=2, severity="critical")), {})

    assert text.count("Held back by the comment budget") == 1
    assert text.index("a.py:2") < text.index("a.py:1")


def test_round_two_drops_new_findings_on_unchanged_lines_below_critical():
    posted = _finding(line=1)
    unchanged = _finding(line=2, rule="new")
    changed = _finding(line=3, rule="new")
    top = _finding(line=4, severity="critical", rule="new")

    kept, dripped = drip_fed(
        (posted, unchanged, changed, top),
        prior_keys=frozenset({finding_key("a.py", 1, "r")}),
        changed_lines=frozenset({("a.py", 3)}),
    )

    assert kept == (posted, changed, top)
    assert dripped == (unchanged,)


def test_unknown_changed_lines_drop_nothing():
    finding = _finding(rule="new")

    assert drip_fed((finding,), prior_keys=frozenset(), changed_lines=None) == ((finding,), ())


def test_drip_feed_spares_deterministic_findings():
    scanner = _finding(rule="new", origins=())

    kept, _ = drip_fed((scanner,), prior_keys=frozenset(), changed_lines=frozenset())

    assert kept == (scanner,)


def test_build_review_result_posts_only_the_inline_share():
    evaluation = CodeReviewEvaluation(findings=_blocking(3), conclusion="failure")
    held = frozenset({finding_key("a.py", 3, "r")})

    result, posted = cr_dispatch.build_review_result(
        evaluation, head_sha="h", event="COMMENT", held_back=held,
    )

    assert [f.line for f in posted] == [1, 2]
    assert len(result.comments) == 2


def _review_with_eight_blocking(monkeypatch, *, action="opened", prior=frozenset()):
    from unittest.mock import patch

    import httpx

    from llm_client import Finding as LlmFinding, LlmReviewResponse
    from personas import review_publish

    diff = "diff --git a/a.py b/a.py\n--- /dev/null\n+++ b/a.py\n@@ -0,0 +1,8 @@\n"
    diff += "".join(f"+line{i}\n" for i in range(8))
    findings = tuple(
        LlmFinding(path="a.py", line=i + 1, rule=f"bug-{i}", severity="high",  # type: ignore[arg-type]
                   message="a defect", origins=(ORIGIN,))
        for i in range(8)
    )
    response = LlmReviewResponse(
        kind="reviewed", findings=findings, backend_used=Backend.POOLSIDE,
    )
    posted: list = []
    checks: list = []
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: response)
    monkeypatch.setattr(cr_dispatch, "grade_findings", lambda *a, **kw: ())
    monkeypatch.setattr(
        cr_dispatch, "refute_with_outcome",
        lambda f, *a, **kw: cr_dispatch_outcome(),
    )
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda token, owner, repo, result, external_id=None: checks.append(result) or {},
    )
    monkeypatch.setattr(
        cr_dispatch, "post_review", lambda *a, **kw: posted.append(kw["result"]) or {},
    )
    monkeypatch.setattr(
        cr_dispatch, "fetch_file_contents", lambda *a, **kw: {"a.py": "x\n"},
    )
    monkeypatch.setattr(
        cr_dispatch, "prior_finding_keys", lambda *a, **kw: (prior, False),
    )
    for module in (cr_dispatch, review_publish):
        monkeypatch.setattr(
            module, "with_install_token_retry", lambda inst, fn: fn("t"),
        )
    payload = {
        "action": action, "installation": {"id": 1},
        "repository": {"id": 2, "name": "r", "owner": {"login": "o"}},
        "pull_request": {
            "number": 3, "head": {"sha": "abcd1234"}, "base": {"sha": "base1234"},
            "title": "t", "body": "b", "user": {"login": "a"},
        },
    }
    response_diff = httpx.Response(
        200, text=diff, request=httpx.Request("GET", "https://example.test"),
    )
    with patch("httpx.get", return_value=response_diff):
        cr_dispatch.dispatch_code_review(payload, blocking=True)
    return posted, checks


def cr_dispatch_outcome():
    from personas.code_reviewer.judge import RefuteOutcome

    return RefuteOutcome((), frozenset())


def test_a_failed_prior_fetch_holds_nothing_back_as_late(monkeypatch):
    monkeypatch.setenv("GRUG_COMMENT_BUDGET", "1")
    changed = cr_dispatch._round_changed_lines(
        (), prior_review_head="abc", head_sha="abc", delta_reviewed=False,
        keys_known=False,
    )

    assert changed is None


def test_open_threads_spend_the_budget_on_a_delta_round():
    from personas.code_reviewer.carry_forward import open_blocking_count

    def _thread(body, resolved=False):
        return {"isResolved": resolved, "comments": {"nodes": [{"body": body}]}}

    threads = [
        _thread("\U0001F534 critical | x <!-- grug-rule:a -->"),
        _thread("\U0001F7E1 medium | x <!-- grug-rule:b -->"),
        _thread("\U0001F534 critical | x <!-- grug-rule:c -->", resolved=True),
    ]

    assert open_blocking_count(threads) == 1


def test_a_held_blocking_finding_keeps_the_review_baseline(monkeypatch):
    """The next push must review this ground again, or a held finding with no
    thread would vanish from the check."""
    import adapters.install_store as store

    monkeypatch.setenv("GRUG_COMMENT_BUDGET", "1")
    stored: list = []
    monkeypatch.setattr(
        store, "put_elder_last_reviewed", lambda **kw: stored.append(kw),
    )
    monkeypatch.setattr(
        cr_dispatch, "_fetch_current_review_snapshot",
        lambda *a, **kw: ("s", "abcd1234", "b", "t"),
    )

    _review_with_eight_blocking(monkeypatch)

    assert stored == []


def test_no_held_finding_moves_the_baseline_as_before(monkeypatch):
    import adapters.install_store as store

    monkeypatch.delenv("GRUG_COMMENT_BUDGET", raising=False)
    stored: list = []
    monkeypatch.setattr(
        store, "put_elder_last_reviewed", lambda **kw: stored.append(kw),
    )
    monkeypatch.setattr(
        cr_dispatch, "_fetch_current_review_snapshot",
        lambda *a, **kw: ("s", "abcd1234", "b", "t"),
    )

    _review_with_eight_blocking(monkeypatch)

    assert len(stored) == 1


def test_a_late_finding_stays_in_the_check(monkeypatch):
    monkeypatch.setenv("GRUG_COMMENT_BUDGET", "1")
    monkeypatch.setattr(
        cr_dispatch, "_round_changed_lines", lambda *a, **kw: frozenset(),
    )

    posted, checks = _review_with_eight_blocking(monkeypatch, action="synchronize")

    assert posted == []
    assert checks[-1].conclusion == "failure"


def test_eight_blocking_findings_post_five_and_still_fail_the_check(monkeypatch):
    monkeypatch.setenv("GRUG_COMMENT_BUDGET", "1")

    posted, checks = _review_with_eight_blocking(monkeypatch)

    assert len(posted[0].comments) == 5
    assert checks[-1].conclusion == "failure"
    assert "Held back by the comment budget** (3" in checks[-1].summary


def test_switch_off_posts_every_finding(monkeypatch):
    monkeypatch.delenv("GRUG_COMMENT_BUDGET", raising=False)

    posted, _ = _review_with_eight_blocking(monkeypatch)

    assert len(posted[0].comments) == 8


def test_repo_config_carries_the_cap_and_drip_setting():
    config = parse_repo_config("elder_blocking_cap: 2\nelder_drip_feed: false\n")

    assert config.problems == ()
    assert config.elder_blocking_cap == 2
    assert config.elder_drip_feed is False
    assert RepoConfig().elder_blocking_cap == 5
    assert RepoConfig().elder_drip_feed is True


def test_bad_repo_values_keep_the_defaults_and_warn():
    config = parse_repo_config("elder_blocking_cap: many\nelder_drip_feed: 3\n")

    assert len(config.problems) == 2
    assert config.elder_blocking_cap == 5
    assert config.elder_drip_feed is True


def test_switch_defaults_off(monkeypatch):
    monkeypatch.delenv("GRUG_COMMENT_BUDGET", raising=False)
    assert not budget_enabled()
    monkeypatch.setenv("GRUG_COMMENT_BUDGET", "1")
    assert budget_enabled()


def _row(severity, verdict):
    return parse_row({
        "repo": "o/r", "pr": 1, "reviewer": "elder", "severity": severity,
        "class": "c", "finding": "f", "verdict": verdict, "evidence": "e", "ts": "",
    })


def test_addressed_rate_is_reported_per_tier():
    rows = [
        _row("critical", "fixed"), _row("critical", "fixed"),
        _row("high", "fixed"), _row("high", "false-positive"),
    ]

    rates = tier_rates(rows)

    assert rates["CRITICAL"] == (2, 2)
    assert rates["HIGH"] == (1, 2)
    assert "HIGH: 1/2 addressed" in format_metrics(rows)
