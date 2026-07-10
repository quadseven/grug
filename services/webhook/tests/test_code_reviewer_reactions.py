"""Tests for personas/code_reviewer/reactions.py.

The reaction engine reads 👍/👎 reactions on Grug inline comments and
submits a `human_verdict` DD annotation (ground-truth for calibrating
the judge). Best-effort + deduped: only submits when the classification
changes from the last-recorded verdict. Mocks GH API + DD seam + the
persistence store; no real network or DDB."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from personas.code_reviewer import reactions as cr_reactions


def _record(comment_id=1, last_verdict=None, span={"trace_id": "t", "span_id": "s"}):
    return {
        "comment_id": comment_id,
        "repo": "o/r",
        "pr_number": 7,
        "review_span_context": span,
        "finding_tags": {"rule_name": "null-deref", "file": "x.py", "line": "2"},
        "last_verdict": last_verdict,
    }


def _ensemble_record(comment_id=1, last_verdict=None):
    record = _record(comment_id=comment_id, last_verdict=last_verdict, span=None)
    record["finding_origins"] = [
        {
            "backend": "poolside",
            "model": "poolside/laguna-m.1",
            "review_span_context": {"span_id": "poolside-span"},
        },
        {
            "backend": "openrouter",
            "model": "anthropic/claude-opus-4.7",
            "review_span_context": {"span_id": "openrouter-span"},
        },
    ]
    return record


def _learning_record(comment_id=1):
    record = _record(comment_id=comment_id)
    record.update({
        "finding_text": "Optional value is dereferenced without a guard.",
        "head_sha": "abc123",
        "author_login": "evan",
        "trust_reactors": True,
    })
    record["finding_tags"]["severity"] = "high"
    return record


def _reactions_response(contents):
    r = MagicMock(spec=httpx.Response)
    r.status_code = 200
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value=[{"content": c} for c in contents])
    return r


def test_annotation_targets_do_not_misattribute_untraced_origin():
    record = _record(span={"span_id": "different-first-backend"})
    record["finding_origins"] = [{
        "backend": "poolside",
        "model": "poolside/laguna-m.1",
        "review_span_context": None,
    }]

    assert cr_reactions._annotation_targets(record) == []


# --- classification ---

def test_classify_thumbs_down_is_false_positive():
    assert cr_reactions._classify_reactions([{"content": "-1"}]) == "false_positive"


def test_classify_thumbs_up_is_confirmed():
    assert cr_reactions._classify_reactions([{"content": "+1"}]) == "confirmed"


def test_classify_thumbs_down_wins_when_both_present():
    """A developer flagging a false positive is the higher-value
    correction — 👎 takes precedence over 👍 when both are present."""
    out = cr_reactions._classify_reactions([{"content": "+1"}, {"content": "-1"}])
    assert out == "false_positive"


def test_classify_no_thumbs_returns_none():
    """Other reactions (heart, rocket, eyes) carry no verdict signal."""
    assert cr_reactions._classify_reactions([{"content": "heart"}, {"content": "rocket"}]) is None


def test_classify_empty_returns_none():
    assert cr_reactions._classify_reactions([]) is None


# --- poll_and_annotate orchestration ---

@pytest.fixture
def _patch_store(monkeypatch):
    updates: list[dict] = []
    monkeypatch.setattr(
        cr_reactions, "update_comment_record_reaction",
        lambda **kw: updates.append(kw),
    )
    return updates


def _patch_annotate(monkeypatch):
    submitted: list[dict] = []
    monkeypatch.setattr(
        cr_reactions, "submit_reaction_annotation",
        lambda **kw: submitted.append(kw),
    )
    return submitted


def test_poll_and_annotate_submits_on_new_thumbs_down(monkeypatch, _patch_store):
    submitted = _patch_annotate(monkeypatch)
    with patch("httpx.get", return_value=_reactions_response(["-1"])):
        n = cr_reactions.poll_and_annotate(
            [_record(comment_id=5)], install_id=1, fetch_token=lambda: "tok",
        )
    assert n == 1
    assert submitted[0]["verdict"] == "false_positive"
    assert submitted[0]["review_span_context"] == {"trace_id": "t", "span_id": "s"}
    assert submitted[0]["tags"]["rule_name"] == "null-deref"
    # dedup baseline updated.
    assert _patch_store[0] == {"install_id": 1, "comment_id": 5, "verdict": "false_positive"}


def test_poll_uses_write_collaborator_reaction_and_ignores_outsider(
    monkeypatch, _patch_store,
):
    submitted = _patch_annotate(monkeypatch)
    learned = MagicMock(return_value=True)
    monkeypatch.setattr(cr_reactions, "_record_reaction_learning", learned)
    monkeypatch.setattr(
        cr_reactions,
        "_has_write_permission",
        lambda token, owner, repo, login: login == "evan",
    )
    response = MagicMock(spec=httpx.Response)
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value=[
        {"content": "-1", "user": {"login": "outsider"}},
        {"content": "+1", "user": {"login": "evan"}},
    ])

    with patch("httpx.get", return_value=response):
        n = cr_reactions.poll_and_annotate(
            [_learning_record(comment_id=5)],
            install_id=1,
            fetch_token=lambda: "tok",
        )

    assert n == 1
    assert submitted[0]["verdict"] == "confirmed"
    learned.assert_called_once_with(_learning_record(comment_id=5), "confirmed")


def test_trusted_reaction_refreshes_ledger_practices_and_examples(monkeypatch):
    from adapters import install_store

    rows: list[dict] = []
    practices: list[list[dict]] = []
    exemplars: list[list[dict]] = []
    monkeypatch.setattr(install_store, "put_ledger_row", lambda row: rows.__setitem__(slice(None), [row]))
    monkeypatch.setattr(install_store, "list_ledger_rows", lambda repo: list(rows))
    monkeypatch.setattr(
        install_store,
        "put_repo_practices",
        lambda repo, data: practices.append(data),
    )
    monkeypatch.setattr(
        install_store,
        "put_repo_exemplars",
        lambda repo, data: exemplars.append(data),
    )

    assert cr_reactions._record_reaction_learning(
        _learning_record(comment_id=5), "confirmed",
    ) is True

    assert rows[0]["verdict"] == "declined"
    assert rows[0]["class"] == "null-deref"
    assert rows[0]["evidence"] == "github-review-comment:5"
    assert practices[0][0]["finding_class"] == "null-deref"
    assert exemplars[0][0]["class"] == "null-deref"


def test_false_positive_reaction_refreshes_negative_prompt_guidance(monkeypatch):
    """A trusted thumbs-down must change later behavior, not only precision
    telemetry. It creates an AVOID practice while remaining excluded from the
    positive few-shot examples."""
    from adapters import install_store

    rows: list[dict] = []
    practices: list[list[dict]] = []
    exemplars: list[list[dict]] = []
    monkeypatch.setattr(
        install_store,
        "put_ledger_row",
        lambda row: rows.__setitem__(slice(None), [row]),
    )
    monkeypatch.setattr(install_store, "list_ledger_rows", lambda repo: list(rows))
    monkeypatch.setattr(
        install_store,
        "put_repo_practices",
        lambda repo, data: practices.append(data),
    )
    monkeypatch.setattr(
        install_store,
        "put_repo_exemplars",
        lambda repo, data: exemplars.append(data),
    )

    assert cr_reactions._record_reaction_learning(
        _learning_record(comment_id=6), "false_positive",
    ) is True

    assert rows[0]["verdict"] == "false-positive"
    assert practices[0][0]["disposition"] == "avoid"
    assert practices[0][0]["rule"] == (
        "Optional value is dereferenced without a guard."
    )
    assert exemplars == [[]]


def test_untrusted_record_cannot_update_learning_store():
    record = _learning_record(comment_id=6)
    record["trust_reactors"] = False

    assert cr_reactions._record_reaction_learning(
        record, "false_positive",
    ) is False


def test_poll_and_annotate_fans_out_to_all_persisted_origins(
    monkeypatch, _patch_store,
):
    """A reaction on a merged inline finding trains every model span that
    produced it, then advances the per-comment dedup baseline once."""
    submitted = _patch_annotate(monkeypatch)

    with patch("httpx.get", return_value=_reactions_response(["+1"])):
        n = cr_reactions.poll_and_annotate(
            [_ensemble_record(comment_id=5)],
            install_id=1,
            fetch_token=lambda: "tok",
        )

    assert n == 1
    assert [s["review_span_context"] for s in submitted] == [
        {"span_id": "poolside-span"},
        {"span_id": "openrouter-span"},
    ]
    assert [s["tags"]["source_backend"] for s in submitted] == [
        "poolside",
        "openrouter",
    ]
    assert _patch_store == [
        {"install_id": 1, "comment_id": 5, "verdict": "confirmed"},
    ]


def test_origin_submit_failure_does_not_skip_sibling_or_advance_baseline(
    monkeypatch, _patch_store,
):
    """A failed first producer remains retryable without depriving the
    second producer of the current human signal."""
    submit = MagicMock(side_effect=[RuntimeError("DD intake 500"), None])
    monkeypatch.setattr(cr_reactions, "submit_reaction_annotation", submit)

    with patch("httpx.get", return_value=_reactions_response(["-1"])):
        n = cr_reactions.poll_and_annotate(
            [_ensemble_record(comment_id=5)],
            install_id=1,
            fetch_token=lambda: "tok",
        )

    assert submit.call_count == 2
    assert n == 0
    assert _patch_store == []


def test_poll_and_annotate_dedups_unchanged_verdict(monkeypatch, _patch_store):
    """A comment already recorded as false_positive whose reaction is
    still 👎 must NOT re-submit — the dedup baseline matches."""
    submitted = _patch_annotate(monkeypatch)
    with patch("httpx.get", return_value=_reactions_response(["-1"])):
        n = cr_reactions.poll_and_annotate(
            [_record(comment_id=5, last_verdict="false_positive")],
            install_id=1, fetch_token=lambda: "tok",
        )
    assert n == 0
    assert submitted == []
    assert _patch_store == []  # no baseline update either


def test_poll_and_annotate_resubmits_on_changed_verdict(monkeypatch, _patch_store):
    """Developer flips 👎 → 👍 (changed their mind): the new verdict
    differs from the baseline, so re-submit."""
    submitted = _patch_annotate(monkeypatch)
    with patch("httpx.get", return_value=_reactions_response(["+1"])):
        n = cr_reactions.poll_and_annotate(
            [_record(comment_id=5, last_verdict="false_positive")],
            install_id=1, fetch_token=lambda: "tok",
        )
    assert n == 1
    assert submitted[0]["verdict"] == "confirmed"


def test_poll_and_annotate_skips_no_reaction(monkeypatch, _patch_store):
    """A comment with no 👍/👎 yet → no classification → no submit, no
    baseline update."""
    submitted = _patch_annotate(monkeypatch)
    with patch("httpx.get", return_value=_reactions_response(["heart"])):
        n = cr_reactions.poll_and_annotate(
            [_record()], install_id=1, fetch_token=lambda: "tok",
        )
    assert n == 0
    assert submitted == []


def test_poll_and_annotate_continues_batch_on_one_failure(monkeypatch, _patch_store):
    """One comment's reaction poll failing (GH 5xx) must not abort the
    whole batch — best-effort per-record."""
    submitted = _patch_annotate(monkeypatch)

    def staged(url, *a, **kw):
        if "/comments/1/" in url:
            raise httpx.ReadTimeout("hung")
        return _reactions_response(["-1"])

    with patch("httpx.get", side_effect=staged):
        n = cr_reactions.poll_and_annotate(
            [_record(comment_id=1), _record(comment_id=2)],
            install_id=1, fetch_token=lambda: "tok",
        )
    # comment 1 failed; comment 2 still submitted.
    assert n == 1
    assert submitted[0]["tags"]["rule_name"] == "null-deref"


def test_poll_and_annotate_skips_record_without_span_context(monkeypatch, _patch_store):
    """A persisted record missing its review span (degraded review at
    publish time) can't attach an annotation — skip, don't crash."""
    submitted = _patch_annotate(monkeypatch)
    with patch("httpx.get", return_value=_reactions_response(["-1"])):
        n = cr_reactions.poll_and_annotate(
            [_record(span=None)], install_id=1, fetch_token=lambda: "tok",
        )
    assert n == 0
    assert submitted == []


def test_poll_learns_without_span_when_trusted_fields_exist(monkeypatch, _patch_store):
    record = _learning_record(comment_id=5)
    record["review_span_context"] = None
    learned = MagicMock(return_value=True)
    monkeypatch.setattr(cr_reactions, "_record_reaction_learning", learned)
    monkeypatch.setattr(
        cr_reactions, "_has_write_permission", lambda *args: True,
    )
    submitted = _patch_annotate(monkeypatch)
    response = MagicMock(spec=httpx.Response)
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value=[
        {"content": "+1", "user": {"login": "evan"}},
    ])

    with patch("httpx.get", return_value=response):
        n = cr_reactions.poll_and_annotate(
            [record], install_id=1, fetch_token=lambda: "tok",
        )

    assert n == 1
    assert submitted == []
    learned.assert_called_once_with(record, "confirmed")
    assert _patch_store == [
        {"install_id": 1, "comment_id": 5, "verdict": "confirmed"},
    ]


def test_write_permission_check_uses_collaborator_endpoint(monkeypatch):
    response = MagicMock(spec=httpx.Response)
    response.status_code = 200
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value={"permission": "write"})
    captured: dict = {}

    def get(url, **kwargs):
        captured["url"] = url
        return response

    monkeypatch.setattr(httpx, "get", get)

    assert cr_reactions._has_write_permission("tok", "o", "r", "evan") is True
    assert captured["url"].endswith("/repos/o/r/collaborators/evan/permission")


def test_reaction_poll_quotes_repository_coordinates(monkeypatch):
    response = _reactions_response(["+1"])
    captured: dict = {}

    def get(url, **kwargs):
        captured["url"] = url
        return response

    monkeypatch.setattr(httpx, "get", get)

    cr_reactions.poll_comment_reactions("tok", "org name", "repo#one", 7)

    assert "/repos/org%20name/repo%23one/pulls/comments/7/reactions" in (
        captured["url"]
    )


def test_poll_and_annotate_skips_malformed_repo_without_aborting(monkeypatch, _patch_store):
    """A persisted record whose `repo` lacks a '/' must skip (logged),
    not raise a TypeError that aborts the batch. The next record still
    processes."""
    submitted = _patch_annotate(monkeypatch)
    bad = _record(comment_id=1)
    bad["repo"] = "no-slash-here"
    good = _record(comment_id=2)
    with patch("httpx.get", return_value=_reactions_response(["-1"])):
        n = cr_reactions.poll_and_annotate(
            [bad, good], install_id=1, fetch_token=lambda: "tok",
        )
    assert n == 1  # only the well-formed record submitted
    assert submitted[0]["verdict"] == "false_positive"


def test_poll_and_annotate_submit_failure_does_not_abort_batch(monkeypatch, _patch_store):
    """A DD-submit or DDB-baseline raise on one record must be caught +
    logged + skipped — the next record still processes (best-effort).
    The baseline is NOT advanced on failure, so the signal re-submits
    next cycle rather than being lost."""
    monkeypatch.setattr(
        cr_reactions, "submit_reaction_annotation",
        MagicMock(side_effect=[RuntimeError("DD intake 500"), None]),
    )
    updates: list[dict] = []
    monkeypatch.setattr(
        cr_reactions, "update_comment_record_reaction",
        lambda **kw: updates.append(kw),
    )
    with patch("httpx.get", return_value=_reactions_response(["-1"])):
        n = cr_reactions.poll_and_annotate(
            [_record(comment_id=1), _record(comment_id=2)],
            install_id=1, fetch_token=lambda: "tok",
        )
    # record 1 submit raised → skipped, no baseline update; record 2 ok.
    assert n == 1
    assert [u["comment_id"] for u in updates] == [2]


def test_poll_and_annotate_baseline_write_failure_skips_count(monkeypatch):
    """submit succeeds but the baseline DDB write fails → not counted as
    submitted-and-recorded; re-submits next cycle (at-least-once)."""
    monkeypatch.setattr(cr_reactions, "submit_reaction_annotation", lambda **kw: None)
    monkeypatch.setattr(
        cr_reactions, "update_comment_record_reaction",
        MagicMock(side_effect=RuntimeError("DDB throttle")),
    )
    with patch("httpx.get", return_value=_reactions_response(["-1"])):
        n = cr_reactions.poll_and_annotate(
            [_record(comment_id=1)], install_id=1, fetch_token=lambda: "tok",
        )
    assert n == 0  # baseline never advanced → will retry next cycle


def test_poll_and_annotate_logs_mixed_signal(monkeypatch, _patch_store, caplog):
    """A comment with both 👍 and 👎 (developer disagreement) logs
    reaction_mixed_signal so contested verdicts are filterable in DD."""
    _patch_annotate(monkeypatch)
    with caplog.at_level("INFO"):
        with patch("httpx.get", return_value=_reactions_response(["+1", "-1"])):
            cr_reactions.poll_and_annotate(
                [_record(comment_id=5)], install_id=1, fetch_token=lambda: "tok",
            )
    assert any("reaction_mixed_signal" in r.message for r in caplog.records)


def test_poll_comment_reactions_non_list_body_returns_empty(monkeypatch):
    """GitHub returning a dict error body (not a list) → []  — never
    hand a dict to _classify_reactions, which would iterate its keys
    and silently misclassify."""
    r = MagicMock(spec=httpx.Response)
    r.status_code = 200
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value={"message": "Not Found"})
    with patch("httpx.get", return_value=r):
        out = cr_reactions.poll_comment_reactions("tok", "o", "r", 1)
    assert out == []


def test_poll_and_annotate_first_poll_with_no_last_verdict_submits(monkeypatch, _patch_store):
    """First poll of a freshly-persisted record (no `last_verdict` key
    at all — NotRequired absent) must submit: None != verdict."""
    submitted = _patch_annotate(monkeypatch)
    rec = _record(comment_id=5)
    del rec["last_verdict"]  # NotRequired — absent on a fresh row
    with patch("httpx.get", return_value=_reactions_response(["+1"])):
        n = cr_reactions.poll_and_annotate(
            [rec], install_id=1, fetch_token=lambda: "tok",
        )
    assert n == 1
    assert submitted[0]["verdict"] == "confirmed"


def test_poll_and_annotate_flip_confirmed_to_false_positive(monkeypatch, _patch_store):
    """The 👍→👎 flip direction (developer reconsiders, decides it WAS a
    real issue's inverse — a false positive after all). Mirror of the
    👎→👍 test so both flip directions exercise the resubmit branch."""
    submitted = _patch_annotate(monkeypatch)
    with patch("httpx.get", return_value=_reactions_response(["-1"])):
        n = cr_reactions.poll_and_annotate(
            [_record(comment_id=5, last_verdict="confirmed")],
            install_id=1, fetch_token=lambda: "tok",
        )
    assert n == 1
    assert submitted[0]["verdict"] == "false_positive"


def test_poll_comment_reactions_uses_reactions_endpoint(monkeypatch):
    """Confirm the GH reactions REST path + preview Accept header."""
    captured = {}

    def capture(url, *, params, headers, timeout):
        captured["url"] = url
        captured["headers"] = headers
        return _reactions_response(["+1"])

    with patch("httpx.get", side_effect=capture):
        out = cr_reactions.poll_comment_reactions("tok", "o", "r", 99)
    assert captured["url"] == (
        "https://api.github.com/repos/o/r/pulls/comments/99/reactions"
    )
    assert captured["headers"]["Authorization"] == "Bearer tok"
    assert out == [{"content": "+1"}]


def test_poll_comment_reactions_requests_max_page_size(monkeypatch):
    """per_page=100 so a 👎 past the default 30-per-page boundary on a
    heavily-reacted comment isn't silently dropped (would misclassify
    as confirmed and poison the calibration set)."""
    captured = {}

    def capture(url, *, params, headers, timeout):
        captured["params"] = params
        return _reactions_response(["+1"])

    with patch("httpx.get", side_effect=capture):
        cr_reactions.poll_comment_reactions("tok", "o", "r", 99)
    assert captured["params"]["per_page"] == 100


def test_classify_finds_thumbs_down_among_many_reactions():
    """A 👎 mixed into a large reaction list (e.g. 40 hearts + 1 👎)
    must still classify false_positive — the set-membership check
    doesn't depend on position, but lock it so a future refactor to
    'first N' slicing can't reintroduce the miss."""
    reactions = [{"content": "heart"}] * 40 + [{"content": "-1"}]
    assert cr_reactions._classify_reactions(reactions) == "false_positive"
