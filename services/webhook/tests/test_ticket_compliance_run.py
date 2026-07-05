"""#529: the impure ticket-compliance runner - upsert semantics + best-effort."""

from __future__ import annotations

import personas.tpm.ticket_compliance_run as run


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
        ("get", "/issues/1/comments"): _Resp(200, [{"id": 77, "body": f"{run._MARKER} old"}]),
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
        ("get", "/issues/1/comments"): _Resp(200, [{"id": 77, "body": f"{run._MARKER} old"}]),
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
