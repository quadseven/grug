"""pytest config for services/webhook/ tests.

Adds the parent directory to sys.path so tests can `from hmac_verify
import ...` without a package install (handler files live alongside the
tests folder, not under a package).

Also exposes `mock_transport_client` + `raise_status_error` helpers
that build a real `httpx.Client` whose request handler is driven by
`httpx.MockTransport`. Closes the mock-vs-real gap from
async-blocker-hunter F-01 (issue #105): direct construction of
`httpx.HTTPStatusError(...)` keeps tests green even if the production
`except` clause silently narrows to a sub-class. With MockTransport,
the exception comes from real httpx machinery.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def _build_handler(
    *,
    status_codes: list[int] | None = None,
    raise_exc: BaseException | None = None,
    json_bodies: list[Any] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Build a MockTransport handler that returns / raises in sequence."""

    seq_status = list(status_codes or [])
    seq_json = list(json_bodies or [])
    idx = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if raise_exc is not None:
            raise raise_exc
        i = idx["n"]
        idx["n"] += 1
        status = seq_status[i] if i < len(seq_status) else (seq_status[-1] if seq_status else 200)
        body = seq_json[i] if i < len(seq_json) else (seq_json[-1] if seq_json else {})
        return httpx.Response(status, json=body)

    return handler


@pytest.fixture
def mock_transport_client() -> Iterator[Callable[..., httpx.Client]]:
    """Factory fixture: build a real httpx.Client backed by MockTransport.

    Usage:

        client = mock_transport_client(status_codes=[401, 200],
                                       json_bodies=[{}, {"ok": True}])
        resp = client.get("https://api.github.com/foo")
        # First call returns 401, second returns 200.

        client = mock_transport_client(raise_exc=httpx.ConnectError("boom"))
        # Every call raises ConnectError from real httpx machinery.

    Pass the returned client into a `monkeypatch.setattr(httpx, "get",
    client.get)` (or post) to redirect a production module-level call.
    """
    created: list[httpx.Client] = []

    def factory(
        *,
        status_codes: list[int] | None = None,
        raise_exc: BaseException | None = None,
        json_bodies: list[Any] | None = None,
    ) -> httpx.Client:
        transport = httpx.MockTransport(
            _build_handler(
                status_codes=status_codes,
                raise_exc=raise_exc,
                json_bodies=json_bodies,
            ),
        )
        client = httpx.Client(transport=transport)
        created.append(client)
        return client

    yield factory
    for c in created:
        c.close()


@pytest.fixture(autouse=True)
def _reset_llm_provider_switches(monkeypatch: pytest.MonkeyPatch) -> None:
    """grug#1225: the LLM provider breaker is module state, and a shell's
    GRUG_DISABLED_BACKENDS must not reach a test. Without the reset, one test
    whose fake backend answers 401 would block that provider for every later
    test in the session."""
    monkeypatch.delenv("GRUG_DISABLED_BACKENDS", raising=False)
    monkeypatch.delenv("GRUG_SHARED_BENCHES", raising=False)
    lc = sys.modules.get("llm_client")
    if lc is not None:
        monkeypatch.setattr(lc, "_provider_blocked_until", {}, raising=False)


@pytest.fixture(autouse=True)
def _mock_pr_enforcement_ensure(monkeypatch):
    """Neutralize the PR-path enforcement ensure by default.

    `_handle_pull_request` re-runs `ensure_enforcement` on every PR (the
    empty-repo bootstrap). Without this, tests that exercise the PR handler
    would reach `get_repo_config` (real DB in CI) and `with_install_token_retry`
    (real GitHub App token) - side effects the pre-existing tests never
    expected. Tests that specifically verify the ensure behavior patch
    `dispatcher._ensure_enforcement_on_pr` themselves (innermost patch wins).
    """
    from unittest.mock import MagicMock

    import dispatcher

    monkeypatch.setattr(dispatcher, "_ensure_enforcement_on_pr", MagicMock())


@pytest.fixture(autouse=True)
def _mock_chief_ticket_recheck(monkeypatch):
    """Neutralize the ticket-change Chief re-run by default.

    `sub_issues` and `issues.edited` (body) also re-run Chief on the open PRs
    naming the ticket, which reads the store (`is_persona_enabled`) and lists
    open PRs on GitHub - side effects the issue-advisory tests never
    expected. Tests that verify the re-run patch the real
    `dispatcher._recheck_chief_for_ticket` back in (innermost patch wins).
    """
    from unittest.mock import MagicMock

    import dispatcher

    monkeypatch.setattr(dispatcher, "_recheck_chief_for_ticket", MagicMock(return_value={}))


@pytest.fixture(autouse=True)
def _mock_learn_living_review_sync(monkeypatch):
    """Neutralize the learn lane's living-review sync (#1187) by default.

    `rerun._run_learn` ends by syncing the PR's Elder findings, which lists
    review threads on GitHub and reads CommentRecords from the store - side
    effects the learn tests never expected. Tests that verify the sync patch
    `rerun._sync_living_review` themselves (innermost patch wins).
    """
    from unittest.mock import MagicMock

    import rerun

    monkeypatch.setattr(rerun, "_sync_living_review", MagicMock(return_value=0))


class _LabelHttpResponse:
    """Minimal httpx.Response stand-in for the label calls."""

    def __init__(self, status_code: int, body: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        return None


class _LabelHttp:
    """Records the `grug:reviewing` label calls instead of making them."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    def get(self, url: str, **kw: object) -> _LabelHttpResponse:
        self.calls.append(("get", url, None))
        return _LabelHttpResponse(200)

    def post(self, url: str, **kw: object) -> _LabelHttpResponse:
        self.calls.append(("post", url, kw.get("json")))
        return _LabelHttpResponse(200)

    def delete(self, url: str, **kw: object) -> _LabelHttpResponse:
        self.calls.append(("delete", url, None))
        return _LabelHttpResponse(204)


@pytest.fixture(autouse=True)
def _mock_review_label_http(monkeypatch):
    """Keep the `grug:reviewing` label writes off the network by default.

    The label is best-effort by design (a missing sign is never a failed
    command), so its HTTP calls would otherwise be real api.github.com
    requests from every test that touches a re-run - slow, flaky, and
    meaningless. The fake is yielded so a test can assert on the calls it
    recorded; tests that need different answers patch `review_label.httpx`
    themselves (innermost patch wins).
    """
    import review_label

    fake = _LabelHttp()
    monkeypatch.setattr(review_label, "httpx", fake)
    return fake
