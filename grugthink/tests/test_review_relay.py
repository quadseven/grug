"""Tests for the Grug -> real Elder verdict review-relay.

Most of what's here is the pure, deterministic pieces (extract_pr_number,
format_verdict, _get_token) - relay_review itself is live Discord I/O and
can only be meaningfully verified against the real bots (see grug PR that
introduced this module). fetch_elder_verdict's error path IS covered
here though, via a mocked transport: it must never leak the bearer token
into a log call on failure (grug#720/#721 Elder review,
`secret-in-log-or-trace` critical finding).
"""

import logging

import httpx
import pytest

from src.grugthink.bot import review_relay
from src.grugthink.bot.review_relay import ElderVerdict


@pytest.mark.parametrize(
    "statement,expected",
    [
        ("review PR #123 for macchina", 123),
        ("what did elder say about #4567", 4567),
        ("look at #1", 1),
        ("no pr number here", None),
        ("", None),
    ],
)
def test_extract_pr_number(statement, expected):
    assert review_relay.extract_pr_number(statement) == expected


@pytest.mark.parametrize(
    "statement",
    [
        "review PR #123 for macchina",
        "what did elder say about #4567",
        "review the pr for infra",
        "look at this pr",
        "can you do a code review",
        "elder verdict on #99",
        "check the check-run for #12",
    ],
)
def test_looks_like_review_request_true(statement):
    assert review_relay.looks_like_review_request(statement) is True


@pytest.mark.parametrize(
    "statement",
    [
        # CodeRabbit finding on grug#742: this matches task_relay's generic
        # "fix" keyword and has both a PR-shaped number and a resolvable
        # repo, but means "implement a fix for issue #123", not "read
        # Elder's verdict on PR #123" - must NOT be treated as a review
        # request.
        "fix infra #123",
        "implement the change from #45 in macchina",
        "build a dashboard",
        "what is the capital of France",
        "",
    ],
)
def test_looks_like_review_request_false(statement):
    assert review_relay.looks_like_review_request(statement) is False


def test_get_token_unset_returns_none(monkeypatch):
    monkeypatch.delenv("GRUGTHINK_GITHUB_CHECKS_TOKEN", raising=False)
    assert review_relay._get_token() is None


def test_get_token_returns_configured_value(monkeypatch):
    monkeypatch.setenv("GRUGTHINK_GITHUB_CHECKS_TOKEN", "ghp_fake_token")
    assert review_relay._get_token() == "ghp_fake_token"


def test_format_verdict_none_means_not_found_or_not_answerable():
    message = review_relay.format_verdict(None, "Grug", "macchina", 42)
    assert "Grug" in message
    assert "macchina" in message
    assert "42" in message


def test_format_verdict_still_running():
    verdict = ElderVerdict(conclusion=None, title=None, summary=None, html_url=None)
    message = review_relay.format_verdict(verdict, "Grug", "grug", 100)
    assert "still look" in message


def test_format_verdict_success():
    verdict = ElderVerdict(
        conclusion="success",
        title="Elder: 0 findings",
        summary="No issues found.",
        html_url="https://grug.lol/some/check",
    )
    message = review_relay.format_verdict(verdict, "Grug", "infra", 1851)
    assert "good hunt" in message
    assert "Elder: 0 findings" in message
    assert "No issues found." in message
    assert "https://grug.lol/some/check" in message


def test_format_verdict_failure():
    verdict = ElderVerdict(conclusion="failure", title=None, summary=None, html_url=None)
    message = review_relay.format_verdict(verdict, "Grug", "infra", 1851)
    assert "bad omen" in message


def test_format_verdict_unknown_conclusion_falls_back_to_raw_word():
    verdict = ElderVerdict(conclusion="cancelled", title=None, summary=None, html_url=None)
    message = review_relay.format_verdict(verdict, "Grug", "infra", 1851)
    assert "cancelled" in message


def test_format_verdict_trims_long_summary():
    long_summary = "x" * 1000
    verdict = ElderVerdict(conclusion="success", title=None, summary=long_summary, html_url=None)
    message = review_relay.format_verdict(verdict, "Grug", "infra", 1)
    assert len(message) < len(long_summary) + 200
    assert message.count("x") <= review_relay._SUMMARY_MAX_CHARS


def test_check_elder_names_matches_canonical_and_legacy():
    # Mirrors services/_shared/personas/tribe.py's CHECK_ELDER +
    # aliases - the canonical name must always be present.
    assert "Grug - Elder" in review_relay.CHECK_ELDER_NAMES


_FAKE_TOKEN = "ghp_supersecrettoken1234567890"


@pytest.mark.asyncio
async def test_fetch_elder_verdict_never_logs_the_token_on_http_error(monkeypatch, caplog):
    monkeypatch.setenv("GRUGTHINK_GITHUB_CHECKS_TOKEN", _FAKE_TOKEN)

    def handler(request: httpx.Request) -> httpx.Response:
        # Fail the very first call (the PR lookup) with a 500 - a real
        # httpx.HTTPStatusError, carrying the original request (and its
        # Authorization header) in .request.
        return httpx.Response(500, request=request)

    transport = httpx.MockTransport(handler)
    real_client_cls = httpx.AsyncClient

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client_cls(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_client)

    with caplog.at_level(logging.DEBUG):
        result = await review_relay.fetch_elder_verdict("infra", 1851)

    assert result is None
    all_log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert _FAKE_TOKEN not in all_log_text
    # exc_info=True (log.exception) would attach the exception - and with
    # it, the httpx.Request carrying the Authorization header - to the
    # record; assert no record in this test carried exception info at all.
    assert not any(r.exc_info for r in caplog.records)


@pytest.mark.asyncio
async def test_fetch_elder_verdict_never_logs_the_token_on_network_error(monkeypatch, caplog):
    monkeypatch.setenv("GRUGTHINK_GITHUB_CHECKS_TOKEN", _FAKE_TOKEN)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(handler)
    real_client_cls = httpx.AsyncClient

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client_cls(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_client)

    with caplog.at_level(logging.DEBUG):
        result = await review_relay.fetch_elder_verdict("infra", 1851)

    assert result is None
    all_log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert _FAKE_TOKEN not in all_log_text
    assert not any(r.exc_info for r in caplog.records)


@pytest.mark.asyncio
async def test_fetch_elder_verdict_finds_elder_on_a_later_page(monkeypatch):
    """The check-runs API's own default is 30 per page, but the loop's
    termination math (page * 100 >= total) assumes 100 - so the request
    MUST explicitly ask for per_page=100, or a commit with more than 30
    check-runs would silently stop after page 1 and never find Elder if
    it landed on page 2. Regression for that exact bug."""
    monkeypatch.setenv("GRUGTHINK_GITHUB_CHECKS_TOKEN", _FAKE_TOKEN)

    # 100 unrelated check-runs on page 1 (filling exactly one full page at
    # the per_page=100 this code is expected to request), Elder itself
    # arrives only on page 2 - total_count reflects both pages.
    page_1_runs = [{"name": f"Some Other Check {i}"} for i in range(100)]
    page_2_runs = [
        {
            "name": "Grug - Elder",
            "conclusion": "success",
            "html_url": "https://grug.lol/some/check",
            "output": {"title": "Elder: 0 findings", "summary": "No issues found."},
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/1851"):
            return httpx.Response(200, json={"head": {"sha": "deadbeef"}})

        assert request.url.params.get("per_page") == "100", (
            "must request per_page=100 to match the page*100>=total termination check"
        )
        page = int(request.url.params.get("page", "1"))
        if page == 1:
            return httpx.Response(200, json={"check_runs": page_1_runs, "total_count": 101})
        return httpx.Response(200, json={"check_runs": page_2_runs, "total_count": 101})

    transport = httpx.MockTransport(handler)
    real_client_cls = httpx.AsyncClient

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client_cls(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_client)

    result = await review_relay.fetch_elder_verdict("infra", 1851)

    assert result is not None
    assert result.conclusion == "success"
    assert result.title == "Elder: 0 findings"


@pytest.mark.asyncio
async def test_fetch_elder_verdict_handles_malformed_pr_payload(monkeypatch, caplog):
    """A valid-JSON-but-wrong-shape PR payload (missing head.sha) must
    degrade to None, not raise out of a detached asyncio.create_task
    where nothing would ever see the exception."""
    monkeypatch.setenv("GRUGTHINK_GITHUB_CHECKS_TOKEN", _FAKE_TOKEN)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"head": {}})

    transport = httpx.MockTransport(handler)
    real_client_cls = httpx.AsyncClient

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client_cls(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_client)

    with caplog.at_level(logging.DEBUG):
        result = await review_relay.fetch_elder_verdict("infra", 1851)

    assert result is None
    all_log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert _FAKE_TOKEN not in all_log_text


# --- defensive type validation (CodeRabbit finding on grug#742: a
# valid-JSON, wrong-shape payload must degrade to None, not crash with an
# AttributeError three .get() calls deep in a detached asyncio task) ---


@pytest.mark.parametrize(
    "pr_json,expected",
    [
        ({"head": {"sha": "abc123"}}, "abc123"),
        ({"head": []}, None),  # head is a list, not a dict
        ({"head": {"sha": 12345}}, None),  # sha is not a string
        ({"head": {}}, None),  # sha missing entirely
        ({}, None),
        ("not even a dict", None),
        (None, None),
    ],
)
def test_extract_head_sha_handles_malformed_shapes(pr_json, expected):
    assert review_relay._extract_head_sha(pr_json) == expected


@pytest.mark.parametrize(
    "checks_json,expected_found",
    [
        ({"check_runs": [{"name": "Grug - Elder", "conclusion": "success"}]}, True),
        ({"check_runs": {}}, False),  # check_runs is a dict, not a list
        ({"check_runs": ["not a dict", 42, None]}, False),  # entries aren't dicts
        ({"check_runs": []}, False),
        ({}, False),
        ("not even a dict", False),
    ],
)
def test_extract_elder_run_handles_malformed_shapes(checks_json, expected_found):
    result = review_relay._extract_elder_run(checks_json)
    assert (result is not None) == expected_found


@pytest.mark.parametrize(
    "checks_json,expected",
    [
        ({"check_runs": [{"name": "x"}], "total_count": 5}, ([{"name": "x"}], 5)),
        ({"check_runs": {}, "total_count": 5}, ([], 5)),  # check_runs wrong type
        ({"check_runs": [], "total_count": "not a number"}, ([], 0)),  # total_count wrong type
        ({}, ([], 0)),
    ],
)
def test_page_count_hint_handles_malformed_shapes(checks_json, expected):
    assert review_relay._page_count_hint(checks_json) == expected


@pytest.mark.asyncio
async def test_fetch_elder_verdict_handles_malformed_check_runs_shape(monkeypatch, caplog):
    """check_runs present but shaped as a dict, not a list - must degrade
    to None instead of crashing when iterating."""
    monkeypatch.setenv("GRUGTHINK_GITHUB_CHECKS_TOKEN", _FAKE_TOKEN)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/1851"):
            return httpx.Response(200, json={"head": {"sha": "deadbeef"}})
        return httpx.Response(200, json={"check_runs": {}, "total_count": 0})

    transport = httpx.MockTransport(handler)
    real_client_cls = httpx.AsyncClient

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client_cls(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_client)

    with caplog.at_level(logging.DEBUG):
        result = await review_relay.fetch_elder_verdict("infra", 1851)

    assert result is None
    all_log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert _FAKE_TOKEN not in all_log_text


@pytest.mark.asyncio
async def test_fetch_elder_verdict_handles_non_dict_output(monkeypatch):
    """Elder's own check-run has a non-dict `output` field - must still
    return a verdict with title/summary as None, not crash."""
    monkeypatch.setenv("GRUGTHINK_GITHUB_CHECKS_TOKEN", _FAKE_TOKEN)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/1851"):
            return httpx.Response(200, json={"head": {"sha": "deadbeef"}})
        return httpx.Response(
            200,
            json={
                "check_runs": [{"name": "Grug - Elder", "conclusion": "success", "output": ["not", "a", "dict"]}],
                "total_count": 1,
            },
        )

    transport = httpx.MockTransport(handler)
    real_client_cls = httpx.AsyncClient

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client_cls(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_client)

    result = await review_relay.fetch_elder_verdict("infra", 1851)

    assert result is not None
    assert result.conclusion == "success"
    assert result.title is None
    assert result.summary is None
