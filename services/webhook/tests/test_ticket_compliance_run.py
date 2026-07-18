"""#529: the impure ticket-compliance runner - upsert semantics + best-effort."""

from __future__ import annotations

import pytest

import personas.tpm.ticket_compliance_run as run

_OWN_APP_ID = "1"  # matches performed_via_github_app.id in the fixtures below


@pytest.fixture(autouse=True)
def _stub_own_app_id(monkeypatch):
    """Every test exercises _find_marker_comment's own-app-identity check
    (#560, same class as Teller's #554 round-3 fix) - stub it once rather
    than per-test, since the real get_app_id() reads GITHUB_APP_ID_SSM
    (unset in this env)."""
    monkeypatch.setattr(run, "get_app_id", lambda: _OWN_APP_ID)


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400 and self.status_code != 404:
            raise run.httpx.HTTPStatusError("boom", request=None, response=None)


class _FakeHttp:
    """Records calls and replays canned responses keyed by (method, url-substr)."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def _match(self, method, url):
        for (m, frag), resp in self.routes.items():
            if m == method and frag in url:
                return resp
        return _Resp(200, [] if method == "get" else {})

    def get(self, url, **kw):
        self.calls.append(("get", url))
        return self._match("get", url)

    def post(self, url, **kw):
        self.calls.append(("post", url, kw.get("json")))
        return self._match("post", url)

    def patch(self, url, **kw):
        self.calls.append(("patch", url, kw.get("json")))
        return self._match("patch", url)

    # exception types the runner references
    HTTPStatusError = run.httpx.HTTPStatusError
    RequestError = run.httpx.RequestError


_ISSUE = "## Acceptance criteria\n- [ ] add the nist ghsa merged feed\n- [ ] emit dogstatsd gauge\n"


def _install(monkeypatch, routes):
    fake = _FakeHttp(routes)
    monkeypatch.setattr(run, "httpx", fake)
    return fake


def test_no_closing_refs_makes_no_calls(monkeypatch):
    fake = _install(monkeypatch, {})
    res = run.run_ticket_compliance("t", owner="o", repo="r", pr_number=1, pr_body="refs #9 only")
    assert res["checked"] == 0
    assert fake.calls == []


def test_unaddressed_posts_new_comment(monkeypatch):
    routes = {
        ("get", "/pulls/1/files"): _Resp(200, [{"filename": "services/webhook/consumer.py"}]),
        ("get", "/issues/5"): _Resp(200, {"body": _ISSUE}),
        ("get", "/issues/1/comments"): _Resp(200, []),  # no existing marker
    }
    fake = _install(monkeypatch, routes)
    res = run.run_ticket_compliance("t", owner="o", repo="r", pr_number=1, pr_body="closes #5")
    posts = [c for c in fake.calls if c[0] == "post"]
    assert len(posts) == 1
    assert run._MARKER in posts[0][2]["body"]
    assert res["flagged"] == {5: 2}


def test_existing_marker_patches_not_posts(monkeypatch):
    routes = {
        ("get", "/pulls/1/files"): _Resp(200, [{"filename": "README.md"}]),
        ("get", "/issues/5"): _Resp(200, {"body": _ISSUE}),
        ("get", "/issues/1/comments"): _Resp(200, [{
            "id": 77, "body": f"{run._MARKER} old",
            "performed_via_github_app": {"id": 1, "slug": "grug"},
        }]),
    }
    fake = _install(monkeypatch, routes)
    run.run_ticket_compliance("t", owner="o", repo="r", pr_number=1, pr_body="closes #5")
    assert any(c[0] == "patch" and "/comments/77" in c[1] for c in fake.calls)
    assert not any(c[0] == "post" for c in fake.calls)


def test_all_addressed_clears_stale_advisory(monkeypatch):
    # diff touches files whose tokens cover both criteria -> nothing unaddressed
    routes = {
        ("get", "/pulls/1/files"): _Resp(200, [{"filename": "nist_ghsa_merged_feed.py"},
                                               {"filename": "dogstatsd_gauge.py"}]),
        ("get", "/issues/5"): _Resp(200, {"body": _ISSUE}),
        ("get", "/issues/1/comments"): _Resp(200, [{
            "id": 77, "body": f"{run._MARKER} old",
            "performed_via_github_app": {"id": 1, "slug": "grug"},
        }]),
    }
    fake = _install(monkeypatch, routes)
    res = run.run_ticket_compliance("t", owner="o", repo="r", pr_number=1, pr_body="closes #5")
    assert res.get("cleared") is True
    patch = [c for c in fake.calls if c[0] == "patch"][0]
    assert "looks like it addresses" in patch[2]["body"]


def test_addressed_with_no_prior_comment_is_noop(monkeypatch):
    routes = {
        ("get", "/pulls/1/files"): _Resp(200, [{"filename": "nist_ghsa_merged_feed.py"},
                                               {"filename": "dogstatsd_gauge.py"}]),
        ("get", "/issues/5"): _Resp(200, {"body": _ISSUE}),
        ("get", "/issues/1/comments"): _Resp(200, []),
    }
    fake = _install(monkeypatch, routes)
    res = run.run_ticket_compliance("t", owner="o", repo="r", pr_number=1, pr_body="closes #5")
    assert res["flagged"] == {}
    assert not any(c[0] in ("post", "patch") for c in fake.calls)


def test_files_fetch_failure_is_graceful(monkeypatch):
    routes = {("get", "/pulls/1/files"): _Resp(500)}
    _install(monkeypatch, routes)
    res = run.run_ticket_compliance("t", owner="o", repo="r", pr_number=1, pr_body="closes #5")
    assert res["checked"] == 0 and "failed" in res["reason"]


def test_global_kill_switch(monkeypatch):
    monkeypatch.setenv("GRUG_TICKET_COMPLIANCE_DISABLED", "1")
    fake = _install(monkeypatch, {})
    res = run.run_ticket_compliance("t", owner="o", repo="r", pr_number=1, pr_body="closes #5")
    assert res["reason"] == "disabled"
    assert fake.calls == []


# --- own-app-identity check on the marker comment (#560) ------------------

def test_find_marker_comment_ignores_user_authored_decoy(monkeypatch):
    """#560 (same class as Teller's #554 round-3 fix): a PR commenter who
    posts the literal marker string before Chief's first run must not be
    mistaken for Chief's own comment - only performed_via_github_app (set
    server-side, unforgeable by a human commenter) counts."""
    routes = {
        ("get", "/issues/1/comments"): _Resp(200, [
            {"id": 111, "body": run._MARKER, "performed_via_github_app": None},
            {"id": 222, "body": run._MARKER, "performed_via_github_app": {"id": 1, "slug": "grug"}},
        ]),
    }
    _install(monkeypatch, routes)
    result = run._find_marker_comment("t", "o", "r", 1)
    assert result == 222


def test_decoy_marker_causes_post_not_patch(monkeypatch):
    """End-to-end: a human-authored decoy marker comment exists, but no
    genuine app-authored one - the runner must POST a fresh comment, not
    attempt (and fail) a PATCH against a comment it can't edit."""
    routes = {
        ("get", "/pulls/1/files"): _Resp(200, [{"filename": "services/webhook/consumer.py"}]),
        ("get", "/issues/5"): _Resp(200, {"body": _ISSUE}),
        ("get", "/issues/1/comments"): _Resp(200, [
            {"id": 999, "body": run._MARKER, "performed_via_github_app": None},
        ]),
    }
    fake = _install(monkeypatch, routes)
    run.run_ticket_compliance("t", owner="o", repo="r", pr_number=1, pr_body="closes #5")
    assert any(c[0] == "post" for c in fake.calls)
    assert not any(c[0] == "patch" for c in fake.calls)
