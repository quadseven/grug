"""Failing-first tests for #656: Pulse comment-nudge on stale threads."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from personas.pulse.comment_nudge import (
    _MARKER_PREFIX,
    _STALE_HOURS,
    find_stale_threads,
    run_comment_nudge_for_install,
)

NOW = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
OLD = (NOW - timedelta(hours=_STALE_HOURS + 5)).isoformat()
RECENT = (NOW - timedelta(hours=1)).isoformat()


def _thread(root_id, root_body, root_created, replies=(), author="author"):
    return {
        "root_id": root_id,
        "root_body": root_body,
        "root_created": root_created,
        "root_author": "grug-tribe[bot]",
        "pr_author": author,
        "replies": list(replies),
    }


def test_find_stale_threads_flags_unreplied_old_thread():
    threads = [_thread(1, "Fix the null deref", OLD)]
    out = find_stale_threads(threads, now=NOW)
    assert len(out) == 1
    assert out[0]["root_id"] == 1


def test_find_stale_threads_skips_recent_thread():
    threads = [_thread(1, "Fix the null deref", RECENT)]
    assert find_stale_threads(threads, now=NOW) == []


def test_find_stale_threads_skips_thread_with_author_reply():
    threads = [
        _thread(
            1,
            "Fix the null deref",
            OLD,
            replies=[{"author": "author", "created": RECENT, "body": "will fix"}],
        )
    ]
    assert find_stale_threads(threads, now=NOW) == []


def test_find_stale_threads_skips_thread_with_nudge_marker():
    marker = _MARKER_PREFIX.format(comment_id=1)
    threads = [
        _thread(
            1,
            "Fix the null deref",
            OLD,
            replies=[
                {
                    "author": "grug-tribe[bot]",
                    "created": RECENT,
                    "body": f"nudge {marker}",
                }
            ],
        )
    ]
    assert find_stale_threads(threads, now=NOW) == []


def test_find_stale_threads_keeps_thread_with_only_bot_replies():
    threads = [
        _thread(
            1,
            "Fix the null deref",
            OLD,
            replies=[
                {
                    "author": "grug-tribe[bot]",
                    "created": RECENT,
                    "body": "still waiting",
                }
            ],
        )
    ]
    out = find_stale_threads(threads, now=NOW)
    assert len(out) == 1


class _FakeGH:
    """Minimal fake for the three comment surfaces + reply POST."""

    def __init__(self):
        self.posts = []
        self.gets = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.gets.append(url)
        if url.endswith("/pulls"):
            return _Resp([{"number": 5, "title": "t", "user": {"login": "author"}}])
        if "/pulls/5/comments" in url:
            return _Resp(
                [
                    {
                        "id": 101,
                        "body": "**Fix**\n\n```suggestion\nx = 1\n```",
                        "created_at": OLD,
                        "user": {"login": "grug-tribe[bot]"},
                        "in_reply_to_id": None,
                    }
                ]
            )
        if "/pulls/5/reviews" in url:
            return _Resp([])
        if "/issues/5/comments" in url:
            return _Resp([])
        return _Resp([])

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append((url, json))
        return _Resp({"id": 999})


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def test_run_posts_one_nudge_with_marker_on_stale_thread():
    gh = _FakeGH()
    n = run_comment_nudge_for_install(
        "tok",
        1,
        [{"id": 1, "full_name": "o/r"}],
        http_client=gh,
        now=NOW,
    )
    assert n == 1
    assert len(gh.posts) == 1
    url, payload = gh.posts[0]
    assert "/comments" in url
    assert "<!-- grug-pulse-comment-nudge:101 -->" in payload["body"]
    # The suggested fix from the original marking is carried into the nudge.
    assert "x = 1" in payload["body"]


def test_run_never_merges_or_pushes():
    gh = _FakeGH()
    run_comment_nudge_for_install(
        "tok",
        1,
        [{"id": 1, "full_name": "o/r"}],
        http_client=gh,
        now=NOW,
    )
    for url, _ in gh.posts:
        assert "merges" not in url
        assert "/git/" not in url
