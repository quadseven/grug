"""Failing-first tests for #675: Teller walkthrough 'Possibly related hunts'."""

from __future__ import annotations

from personas.walkthrough.related import RelatedPR, find_related_prs
from personas.walkthrough.render import FileStat, walkthrough_body

# --- render: the section -----------------------------------------------


def _body(related):
    return walkthrough_body(
        summary="Does a thing.",
        files=[FileStat(path="a.py", additions=1, deletions=0)],
        diagram=None,
        effort="quick",
        head_sha="b" * 40,
        degraded=False,
        related=related,
    )


def test_walkthrough_body_renders_related_hunts_section():
    related = [
        RelatedPR(number=101, title="Fix the thing", reason="touched a.py"),
        RelatedPR(number=99, title="Earlier thing", reason="similar intent"),
    ]
    body = _body(related)
    assert "Possibly related hunts" in body
    assert "#101" in body
    assert "Fix the thing" in body
    assert "touched a.py" in body
    assert "#99" in body


def test_walkthrough_body_omits_related_hunts_when_empty():
    body = _body([])
    assert "Possibly related hunts" not in body


def test_walkthrough_body_omits_related_hunts_when_none():
    body = _body(None)
    assert "Possibly related hunts" not in body


def test_walkthrough_body_neutralizes_mentions_in_related_titles():
    related = [RelatedPR(number=7, title="ping @evil-user", reason="x")]
    body = _body(related)
    assert "@evil-user" not in body


# --- retrieval: scoring + bounds ----------------------------------------


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def _fake_get_factory(pr_list, files_by_pr):
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(url)
        if url.endswith("/pulls") or "/pulls?" in url:
            return _FakeResp(pr_list)
        for n, files in files_by_pr.items():
            if url.endswith(f"/pulls/{n}/files"):
                return _FakeResp([{"filename": f} for f in files])
        return _FakeResp([])

    fake_get.calls = calls  # type: ignore[attr-defined]
    return fake_get


def test_find_related_prs_scores_file_overlap_first():
    pr_list = [
        {
            "number": 10,
            "title": "Unrelated docs tweak",
            "merged_at": "2026-01-01T00:00:00Z",
        },
        {"number": 11, "title": "Fix the widget", "merged_at": "2026-01-02T00:00:00Z"},
    ]
    fake_get = _fake_get_factory(
        pr_list,
        {10: ["docs/README.md"], 11: ["src/widget.py"]},
    )
    out = find_related_prs(
        "tok",
        "o",
        "r",
        12,
        ["src/widget.py"],
        "widget fix",
        http_get=fake_get,
    )
    assert [r.number for r in out] == [11]
    assert "src/widget.py" in out[0].reason


def test_find_related_prs_skips_current_and_unmerged():
    pr_list = [
        {"number": 12, "title": "widget fix", "merged_at": "2026-01-03T00:00:00Z"},
        {"number": 13, "title": "widget fix", "merged_at": None},
    ]
    fake_get = _fake_get_factory(pr_list, {})
    out = find_related_prs(
        "tok",
        "o",
        "r",
        12,
        ["src/widget.py"],
        "widget fix",
        http_get=fake_get,
    )
    assert out == []


def test_find_related_prs_caps_at_three_and_bounds_file_fetches():
    pr_list = [
        {
            "number": n,
            "title": "widget fix iteration",
            "merged_at": "2026-01-01T00:00:00Z",
        }
        for n in range(1, 11)
    ]
    fake_get = _fake_get_factory(pr_list, {n: ["src/widget.py"] for n in range(1, 11)})
    out = find_related_prs(
        "tok",
        "o",
        "r",
        99,
        ["src/widget.py"],
        "widget fix",
        http_get=fake_get,
    )
    assert len(out) <= 3
    file_calls = [u for u in fake_get.calls if u.endswith("/files")]
    assert len(file_calls) <= 5


# --- failures are best-effort but never silent ---------------------------


def test_list_failure_returns_empty_and_logs_the_kind(caplog):
    """A failed list call still yields [] (the section is optional), but the
    failure must leave a trace: an auth outage and an honest empty looked
    the same before, and the section vanished with no record."""

    def boom(url, headers=None, params=None, timeout=None):
        raise RuntimeError("401 bad credentials")

    with caplog.at_level("INFO"):
        out = find_related_prs("tok", "o", "r", 12, ["a.py"], "t", http_get=boom)

    assert out == []
    rec = [r for r in caplog.records if r.getMessage() == "walkthrough_related_list_failed"]
    assert rec and rec[0].kind == "RuntimeError"


def test_candidate_file_failure_is_logged_and_skipped(caplog):
    """One candidate's /files fetch failing must not kill the section, and
    must not vanish either."""
    pr_list = [{"number": 11, "title": "widget fix", "merged_at": "2026-01-02T00:00:00Z"}]

    def get(url, headers=None, params=None, timeout=None):
        if url.endswith("/files"):
            raise ValueError("bad json")
        return _FakeResp(pr_list)

    with caplog.at_level("INFO"):
        out = find_related_prs("tok", "o", "r", 12, ["a.py"], "widget fix", http_get=get)

    assert isinstance(out, list)
    rec = [r for r in caplog.records if r.getMessage() == "walkthrough_related_files_failed"]
    assert rec and rec[0].kind == "ValueError" and rec[0].candidate == 11
