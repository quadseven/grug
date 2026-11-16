"""ADR-0027: Elder's blocking relaxes to advisory during a sustained backend
outage, and comes back by itself.

The decision is pure (clock and backend health are arguments); the publish
path is driven through `dispatch_code_review` with a controlled clock."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from llm_client import Backend, LlmReviewResponse
from personas.code_reviewer import dispatch as cr_dispatch
from personas.code_reviewer import outage_advisory as oa
from personas.code_reviewer.persona import CodeReviewEvaluation
from personas.review_publish import plan_publish
from review_pipeline import ReviewCoverage

MIN = 60.0
ON = oa.OutageSettings(enabled=True, window_s=20 * MIN, gap_s=60 * MIN)
OFF = oa.OutageSettings(enabled=False, window_s=20 * MIN, gap_s=60 * MIN)


def _cov(failed=(), unattempted=(), models=()):
    return ReviewCoverage(
        total_cohorts=3, completed_cohorts=3 - len(failed),
        failed_cohorts=tuple(failed), cohort_labels=("a", "b", "c"),
        unattempted_cohorts=tuple(unattempted), failed_models=tuple(models),
    )


# -- settings ----------------------------------------------------------------

def test_defaults_are_on_20_and_60_minutes():
    cfg = oa.settings_from_env({})
    assert (cfg.enabled, cfg.window_s, cfg.gap_s) == (True, 1200.0, 3600.0)


@pytest.mark.parametrize("raw", ["0", "false", "no", "OFF"])
def test_switch_off(raw):
    assert oa.settings_from_env({"GRUG_ELDER_OUTAGE_ADVISORY": raw}).enabled is False


def test_minutes_override_and_bad_value_falls_back():
    cfg = oa.settings_from_env(
        {"GRUG_ELDER_OUTAGE_MINUTES": "5", "GRUG_ELDER_OUTAGE_GAP_MINUTES": "x"},
    )
    assert (cfg.window_s, cfg.gap_s) == (300.0, 3600.0)


# -- classify ------------------------------------------------------------------

@pytest.mark.parametrize("reason", ["all_failed", "parse_failed", "low_confidence_review"])
def test_backend_reasons_are_outage(reason):
    assert oa.classify(reason) == "outage"


def test_partial_with_a_cohort_that_ran_and_failed_is_outage():
    assert oa.classify("partial_review", _cov(failed=(2,))) == "outage"


def test_partial_from_budget_only_is_not_outage():
    assert oa.classify("partial_review", _cov(failed=(3,), unattempted=(3,))) == "other"


def test_clean_is_healthy_and_no_diff_is_other():
    assert oa.classify(None) == "healthy"
    assert oa.classify("no_diff") == "other"


# -- streak ---------------------------------------------------------------------

def test_streak_starts_extends_survives_other_and_healthy_ends_it():
    s = oa.advance(None, "outage", 100.0, 3600.0)
    assert s == oa.Streak(100.0, 100.0)
    s = oa.advance(s, "outage", 500.0, 3600.0)
    assert s == oa.Streak(100.0, 500.0)
    assert oa.advance(s, "other", 600.0, 3600.0) == s
    assert oa.advance(s, "healthy", 600.0, 3600.0) is None


def test_gap_restarts_the_streak():
    s = oa.Streak(0.0, 10.0)
    assert oa.advance(s, "outage", 10.0 + 3601.0, 3600.0) == oa.Streak(3611.0, 3611.0)


# -- decide ---------------------------------------------------------------------

def _decide(*, settings=ON, blocking=True, kind="outage", since=0.0,
            now=20 * MIN, failed=("poolside",)):
    return oa.decide(
        settings=settings, blocking=blocking, kind=kind,
        streak=oa.Streak(since, now), now=now, failed=failed,
    )


def test_before_the_window_fails_closed():
    assert _decide(now=20 * MIN - 1).advisory is False


def test_at_the_window_it_is_advisory_and_names_failures():
    v = _decide()
    assert v.advisory is True and v.since == 0.0 and v.failed == ("poolside",)


def test_switch_off_restores_adr_0023():
    assert _decide(settings=OFF, now=999 * MIN).advisory is False


def test_warn_mode_and_unknown_mode_never_advisory():
    assert _decide(blocking=False).advisory is False
    assert _decide(blocking=None).advisory is False


def test_non_outage_degradation_keeps_failing_closed_however_old_the_streak():
    assert _decide(kind="other", now=999 * MIN).advisory is False
    assert _decide(kind="healthy", now=999 * MIN).advisory is False


def test_no_streak_is_not_advisory():
    v = oa.decide(settings=ON, blocking=True, kind="outage", streak=None, now=1.0)
    assert v.advisory is False


# -- observe: the whole sequence, injected clock -----------------------------------

def _obs(reason, now, coverage=None, blocking=True):
    return oa.observe(
        reason, coverage, blocking=blocking, settings=ON, now=now,
        failed=("poolside",),
    )


def test_sustained_outage_goes_advisory_then_one_healthy_review_restores_blocking():
    assert _obs("all_failed", 0.0).advisory is False
    assert _obs("all_failed", 10 * MIN).advisory is False
    assert _obs("all_failed", 21 * MIN).advisory is True
    # Backends back: a real review ends the streak ...
    assert _obs(None, 22 * MIN).advisory is False
    # ... so the very next degraded review blocks again.
    assert _obs("all_failed", 23 * MIN).advisory is False


def test_huge_pr_partial_with_healthy_backends_never_goes_advisory():
    cov = _cov(failed=(3,), unattempted=(3,))
    for t in (0, 30, 60, 90):
        assert _obs("partial_review", t * MIN, cov).advisory is False


def test_healthy_reviews_in_between_prevent_the_streak():
    _obs("all_failed", 0.0)
    _obs(None, 5 * MIN)
    assert _obs("all_failed", 25 * MIN).advisory is False


def test_a_non_outage_degraded_result_neither_extends_nor_breaks_the_streak():
    _obs("all_failed", 0.0)
    _obs("partial_review", 5 * MIN, _cov(failed=(3,), unattempted=(3,)))
    assert _obs("all_failed", 21 * MIN).advisory is True


def test_peek_does_not_move_the_streak():
    oa.observe(
        "all_failed", None, blocking=True, settings=ON, now=0.0, record=False,
        failed=(),
    )
    assert _obs("all_failed", 25 * MIN).advisory is False


def test_a_broken_store_fails_closed_and_says_so(monkeypatch, caplog):
    def boom(*_a):
        raise RuntimeError("down")

    monkeypatch.setattr(oa, "_read", boom)
    with caplog.at_level("WARNING", logger="grug.code_reviewer.outage_advisory"):
        assert _obs("all_failed", 0.0).advisory is False
    assert "elder_outage_streak_failed" in caplog.text


def test_backend_naming_failure_is_logged_not_silent(monkeypatch, caplog):
    import llm_client as lc

    monkeypatch.delattr(lc, "_BACKEND_CONFIGS")
    with caplog.at_level("WARNING", logger="grug.code_reviewer.outage_advisory"):
        names = oa.failed_backends(None)
    assert names == ()
    assert "elder_outage_backend_naming_failed" in caplog.text


def test_advisory_summary_is_distinguishable_and_names_since_and_backends():
    v = oa.OutageVerdict(True, 0.0, ("poolside (timeout)",))
    text = oa.advisory_summary(v, "body")
    assert oa.ADVISORY_TITLE == "Elder advisory: review backends down"
    assert "1970-01-01 00:00 UTC" in text and "poolside (timeout)" in text
    assert "/grug improve" in text and text.endswith("body")
    assert not oa.ADVISORY_TITLE.startswith("Elder clear")


# -- shared store ---------------------------------------------------------------

def test_streak_round_trips_through_the_shared_store(monkeypatch):
    import shared_benches as sb

    table: dict[str, float] = {}

    def key(k, a, b=""):
        return f"{k}#{a}#{b}"

    monkeypatch.setattr(sb, "enabled", lambda: True)
    monkeypatch.setattr(sb, "blocked_until", lambda k, a, b="": table.get(key(k, a, b), 0.0))
    monkeypatch.setattr(
        sb, "publish",
        lambda k, a, b, u: table.__setitem__(key(k, a, b), max(table.get(key(k, a, b), 0.0), u)),
    )
    monkeypatch.setattr(sb, "clear", lambda k, a, b="": table.pop(key(k, a, b), None))
    base = 1_700_000_000.0
    assert _obs("all_failed", base).advisory is False
    assert _obs("all_failed", base + 21 * MIN).advisory is True
    assert oa._read(ON.gap_s, base) == oa.Streak(base, base + 21 * MIN)
    _obs(None, base + 22 * MIN)
    assert table == {}


# -- plan_publish ----------------------------------------------------------------

def _ev(reason):
    return CodeReviewEvaluation(findings=(), conclusion="neutral", degraded_reason=reason)


def test_plan_publish_outage_advisory_is_neutral_only_when_asked():
    closed = plan_publish(_ev("all_failed"), mode="blocking", degraded_fails_closed=True)
    assert closed.conclusion == "failure"
    out = plan_publish(
        _ev("all_failed"), mode="blocking", degraded_fails_closed=True,
        outage_advisory=True,
    )
    assert (out.conclusion, out.event) == ("neutral", "COMMENT")


def test_plan_publish_advisory_flag_does_not_soften_a_real_finding():
    ev = CodeReviewEvaluation(findings=(), conclusion="failure", degraded_reason=None)
    out = plan_publish(
        ev, mode="blocking", degraded_fails_closed=True, outage_advisory=True,
    )
    assert out.conclusion == "failure"


# -- the publish path ---------------------------------------------------------------

_DIFF = """diff --git a/src/x.py b/src/x.py
--- a/src/x.py
+++ b/src/x.py
@@ -1,3 +1,4 @@
 context
-old
+new1
+new2
"""


def _payload():
    return {
        "action": "opened", "installation": {"id": 11},
        "repository": {"id": 22, "name": "myrepo", "owner": {"login": "myorg"}},
        "pull_request": {
            "number": 7, "head": {"sha": "abcd1234efgh"},
            "base": {"sha": "base5678ijkl"},
            "title": "t", "body": "b", "user": {"login": "alice"},
        },
    }


@pytest.fixture
def run(monkeypatch):
    monkeypatch.setattr(cr_dispatch, "with_install_token_retry", lambda iid, fn: fn("tok"))
    monkeypatch.setenv("GRUG_DEEP_ASYNC", "0")
    posted: list = []
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda token, owner, repo, result, external_id=None: (
            posted.append(result) or {"id": 1}
        ),
    )
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **kw: {"id": 2})
    monkeypatch.setattr(cr_dispatch, "grade_findings", lambda *a, **kw: ())
    clock = {"now": 1_700_000_000.0}
    monkeypatch.setattr(oa.time, "time", lambda: clock["now"])
    monkeypatch.setattr(oa, "failed_backends", lambda cov: ("poolside (timeout)",))

    def go(llm, *, blocking=True, advance_min=0.0):
        clock["now"] += advance_min * MIN
        monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **kw: llm)
        r = MagicMock(spec=httpx.Response)
        r.status_code, r.text, r.raise_for_status = 200, _DIFF, MagicMock()
        with patch("httpx.get", return_value=r):
            cr_dispatch.dispatch_code_review(_payload(), blocking=blocking)
        return posted[-1]

    return go


_DOWN = LlmReviewResponse(kind="all_failed", error="poolside: timeout")
_UP = LlmReviewResponse(kind="reviewed", backend_used=Backend.POOLSIDE)


def test_publish_fails_closed_then_advisory_then_blocks_again(run):
    first = run(_DOWN)
    assert first.conclusion == "failure"
    assert first.title.startswith("WARN")
    later = run(_DOWN, advance_min=25)
    assert later.conclusion == "neutral"
    assert later.title == oa.ADVISORY_TITLE
    assert later.summary.split("\n")[0] == f"## {oa.ADVISORY_TITLE}"
    assert "Degraded review (all_failed): not a verdict." in later.summary
    assert "poolside (timeout)" in later.summary and "since" in later.summary
    assert "this check fails until" not in later.summary
    clear = run(_UP, advance_min=1)
    assert clear.title.startswith("Elder clear") and clear.title != oa.ADVISORY_TITLE
    again = run(_DOWN, advance_min=1)
    assert again.conclusion == "failure"


def test_publish_switch_off_keeps_adr_0023(run, monkeypatch):
    monkeypatch.setenv("GRUG_ELDER_OUTAGE_ADVISORY", "0")
    run(_DOWN)
    assert run(_DOWN, advance_min=90).conclusion == "failure"


def test_publish_warn_mode_is_untouched(run):
    run(_DOWN, blocking=False)
    later = run(_DOWN, blocking=False, advance_min=90)
    assert later.conclusion == "neutral" and later.title != oa.ADVISORY_TITLE
