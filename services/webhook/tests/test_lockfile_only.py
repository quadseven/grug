"""grug#1649: a lock-file-only pull request gets a real Elder review.

Lock files are dropped from Elder's plate (#609), so a diff made only of
them reached the model with nothing and published a neutral "eyes clouded
(no_diff)" that never clears a blocking gate. No verdict may come from a
skip: when every changed file is a lock file, Elder sends the full
base..head lock diff to the model and concludes from findings like any
other diff, with the deterministic lock-source check added. A failed lock
review does not advance the Living Hunt baseline.
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import httpx
import pytest
from github_checks_client import CheckRunResult
from llm_client import Backend, LlmReviewResponse
from personas import review_publish
from personas.code_reviewer import dispatch as cr_dispatch
from personas.code_reviewer.lock_sources import RULE

_UV_BASE = """version = 1

[[package]]
name = "app"
version = "0.1.0"
source = { virtual = "." }
"""
_GOOD = [
    "[[package]]",
    'name = "idna"',
    'version = "3.7"',
    'source = { registry = "https://pypi.org/simple" }',
]
_EVIL = [
    "[[package]]",
    'name = "idna"',
    'version = "3.7"',
    'source = { registry = "https://evil.example.net/simple" }',
]


def _added(path: str, lines: list[str]) -> str:
    body = "".join(f"+{x}\n" for x in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        "index 39dc28b90..1a2b3c4d5 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -6,0 +7,{len(lines)} @@\n" + body
    )


def _head(lines: list[str]) -> str:
    return _UV_BASE + "\n" + "\n".join(lines) + "\n"


def _response(*, text: str = "", json: object = None, status: int = 200):
    r = MagicMock(spec=httpx.Response)
    r.status_code = status
    r.text = text
    r.content = text.encode()
    r.json = MagicMock(return_value=json)
    if status >= 400:
        r.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError(
            "missing", request=MagicMock(), response=r,
        ))
    else:
        r.raise_for_status = MagicMock()
    return r


def _review_pr(
    monkeypatch, *, full: str, files: dict[tuple[str, str], str],
    delta: str | None = None,
) -> dict:
    """Run one Elder review. `files` maps (path, ref) to whole-file text;
    `delta` set means a stored last Elder head and a synchronize push."""
    seen: dict = {"reviewed": [], "full": [], "checks": [], "baseline": []}

    def _fake_get(url, *args, **kwargs):
        if "/contents/" in url:
            path = url.split("/contents/", 1)[1]
            ref = (kwargs.get("params") or {}).get("ref", "")
            text = files.get((path, ref))
            return _response(text=text) if text is not None else _response(status=404)
        if "/git/trees/" in url:
            return _response(json={"tree": []})
        if "/search/code" in url:
            return _response(json={"total_count": 0, "items": []})
        return _response(text=full)

    def _review(evidence, *_a, **_k):
        if not evidence.hunks:
            return LlmReviewResponse(kind="no_diff")
        seen["reviewed"].append({h.path for h in evidence.hunks})
        return LlmReviewResponse(
            kind="reviewed", findings=(), backend_used=Backend.CAVE, model_name="coder",
        )

    def _check(*a, **k):
        result = k.get("result") or next(x for x in a if isinstance(x, CheckRunResult))
        seen["checks"].append(result)
        return {"id": 1}

    def _full(*_a, **k):
        seen["full"].append((k.get("base_sha"), k.get("head_sha")))
        return full

    monkeypatch.setattr(cr_dispatch, "with_install_token_retry", lambda _i, fn: fn("t"))
    monkeypatch.setattr(review_publish, "with_install_token_retry", lambda _i, fn: fn("t"))
    monkeypatch.setattr(cr_dispatch, "review_diff", _review)
    monkeypatch.setattr(cr_dispatch, "post_check_run", _check)
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **k: {"id": 2})
    monkeypatch.setattr(cr_dispatch, "fetch_pr_diff", _full)
    monkeypatch.setattr(
        cr_dispatch, "fetch_pr_diff_with_scope",
        lambda *_a, **_k: (delta or "", True, False),
    )
    monkeypatch.setattr(
        cr_dispatch, "_fetch_current_review_snapshot",
        lambda *a, **k: ("base5678ijkl", "abcd1234efgh", "t", "b"),
    )
    with patch("httpx.get", side_effect=_fake_get), patch(
        "adapters.install_store.get_elder_last_reviewed",
        return_value="prior0001head" if delta is not None else None,
    ), patch(
        "adapters.install_store.put_elder_last_reviewed",
        lambda **k: seen["baseline"].append(k["head_sha"]),
    ):
        cr_dispatch.dispatch_code_review({
            "action": "synchronize" if delta is not None else "opened",
            "installation": {"id": 11},
            "repository": {"id": 22, "name": "myrepo", "owner": {"login": "myorg"}},
            "pull_request": {
                "number": 7,
                "head": {"sha": "abcd1234efgh"},
                "base": {"sha": "base5678ijkl"},
                "title": "Bump idna in the lock",
                "body": "Refreshes the lock file.",
                "user": {"login": "alice"},
            },
        }, blocking=True)
    return seen


def _lock_files(lines: list[str], path: str = "uv.lock") -> dict:
    return {(path, "abcd1234efgh"): _head(lines), (path, "base5678ijkl"): _UV_BASE}


def test_a_lock_file_only_diff_is_sent_to_the_model(monkeypatch):
    seen = _review_pr(monkeypatch, full=_added("uv.lock", _GOOD), files=_lock_files(_GOOD))
    assert seen["reviewed"] == [{"uv.lock"}]
    final = seen["checks"][-1]
    assert final.conclusion == "success", (final.conclusion, final.title)
    assert "no_diff" not in final.title
    assert "lock files only" not in final.title


def test_a_lock_file_only_diff_with_a_bad_source_fails(monkeypatch):
    seen = _review_pr(monkeypatch, full=_added("uv.lock", _EVIL), files=_lock_files(_EVIL))
    final = seen["checks"][-1]
    assert final.conclusion == "failure", (final.conclusion, final.title)
    assert RULE in final.summary
    assert re.search(r"host `evil\.example\.net` not on", final.summary)


def test_a_mixed_diff_reviews_the_source_and_still_checks_the_lock(monkeypatch):
    full = _added("uv.lock", _EVIL) + _added("src/app.py", ["a = 1"])
    seen = _review_pr(monkeypatch, full=full, files=_lock_files(_EVIL))
    assert seen["reviewed"] == [{"src/app.py"}]
    final = seen["checks"][-1]
    assert final.conclusion == "failure", (final.conclusion, final.title)
    assert RULE in final.summary


def test_a_mixed_clean_diff_still_skips_the_lock_for_the_model(monkeypatch):
    full = _added("uv.lock", _GOOD) + _added("src/app.py", ["a = 1"])
    seen = _review_pr(monkeypatch, full=full, files=_lock_files(_GOOD))
    assert seen["reviewed"] == [{"src/app.py"}]
    assert seen["checks"][-1].conclusion == "success"


def test_an_unlisted_lock_name_is_still_skipped(monkeypatch):
    seen = _review_pr(monkeypatch, full=_added("tools/flake.lock", ["x"]), files={})
    assert seen["reviewed"] == []
    assert seen["checks"][-1].conclusion != "success"


def test_a_lock_only_living_delta_reviews_the_full_pr_diff(monkeypatch):
    delta = _added("uv.lock", _GOOD)
    full = delta + _added("src/app.py", ["a = 1"])
    seen = _review_pr(monkeypatch, full=full, files=_lock_files(_GOOD), delta=delta)
    assert ("base5678ijkl", "abcd1234efgh") in seen["full"]
    assert seen["reviewed"] == [{"src/app.py"}]
    assert not seen["checks"][-1].title.startswith("Living Hunt")


def test_a_lock_only_full_diff_after_a_lock_only_delta_is_reviewed(monkeypatch):
    delta = _added("uv.lock", _GOOD)
    seen = _review_pr(monkeypatch, full=delta, files=_lock_files(_GOOD), delta=delta)
    assert ("base5678ijkl", "abcd1234efgh") in seen["full"]
    assert seen["reviewed"] == [{"uv.lock"}]


def test_a_passing_lock_review_advances_the_baseline(monkeypatch):
    seen = _review_pr(monkeypatch, full=_added("uv.lock", _GOOD), files=_lock_files(_GOOD))
    assert seen["baseline"] == ["abcd1234efgh"]


@pytest.mark.parametrize("extra", ["", "src"])
def test_a_failed_lock_review_keeps_the_baseline(monkeypatch, extra):
    full = _added("uv.lock", _EVIL) + (_added("src/app.py", ["a = 1"]) if extra else "")
    seen = _review_pr(monkeypatch, full=full, files=_lock_files(_EVIL))
    assert seen["checks"][-1].conclusion == "failure"
    assert seen["baseline"] == []


def test_an_unchecked_lock_format_is_reviewed_and_noted(monkeypatch):
    full = _added("web/yarn.lock", ['  resolved "https://registry.yarnpkg.com/x.tgz"'])
    seen = _review_pr(monkeypatch, full=full, files={})
    assert seen["reviewed"] == [{"web/yarn.lock"}]
    final = seen["checks"][-1]
    assert final.conclusion == "success", (final.conclusion, final.title)
    assert "registry sources not machine-checked for this format" in final.summary


def test_a_mixed_pr_sends_an_unchecked_lock_to_the_model(monkeypatch):
    full = _added("web/yarn.lock", ['  resolved "https://registry.yarnpkg.com/x.tgz"']) + _added(
        "src/app.py", ["a = 1"])
    seen = _review_pr(monkeypatch, full=full, files={})
    sent = set().union(*seen["reviewed"])
    assert {"web/yarn.lock", "src/app.py"} <= sent
    final = seen["checks"][-1]
    assert final.conclusion == "success", (final.conclusion, final.title)
    assert "`yarn.lock`: registry sources not machine-checked" in final.summary
    assert "web/yarn.lock`: registry" not in final.summary


def test_an_unchecked_lock_dropped_for_size_concludes_neutral(monkeypatch):
    monkeypatch.setattr(cr_dispatch, "review_cohort_char_budget", lambda: 80)
    big = '  resolved "https://registry.yarnpkg.com/' + "x" * 200 + '.tgz"'
    full = _added("web/yarn.lock", [big]) + _added("src/app.py", ["a = 1"])
    seen = _review_pr(monkeypatch, full=full, files={})
    sent = set().union(*seen["reviewed"]) if seen["reviewed"] else set()
    assert "web/yarn.lock" not in sent
    final = seen["checks"][-1]
    assert final.conclusion == "neutral", (final.conclusion, final.title)
    assert "`yarn.lock`" in final.summary
    assert "machine-checked for this format; Elder's review covers it" not in final.summary
    assert seen["baseline"] == []
