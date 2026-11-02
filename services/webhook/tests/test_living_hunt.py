"""Living Hunt (#557): last-reviewed head + delta scope."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import httpx

from personas import review_publish
from personas.code_reviewer import dispatch as cr_dispatch
from personas.code_reviewer.persona import CodeReviewEvaluation, Finding
from personas.code_reviewer.diff_parser import DiffHunk
from personas.code_reviewer.persona import _changed_line_text


def test_summary_markdown_includes_living_range():
    ev = CodeReviewEvaluation(
        findings=(
            Finding(
                file="x.py", line=1, severity="high", rule_name="null-deref",
                message="m", suggestion=None,
            ),
        ),
        conclusion="failure",
    )
    title, summary = cr_dispatch._summary_markdown(
        ev, living_range="abc12345..def67890",
    )
    assert "Living Hunt abc12345..def67890" in title
    assert "Living Hunt: reviewing `abc12345..def67890`" in summary


def test_summary_markdown_omits_living_range_when_empty():
    ev = CodeReviewEvaluation(findings=(), conclusion="success")
    title, summary = cr_dispatch._summary_markdown(ev)
    assert "Living Hunt" not in title
    assert "Living Hunt" not in summary


def test_clean_living_hunt_names_delta_in_title_and_scope():
    ev = CodeReviewEvaluation(findings=(), conclusion="success")
    title, summary = cr_dispatch._summary_markdown(
        ev, living_range="abc12345..def67890",
    )
    assert title.startswith("Living Hunt abc12345..def67890 - ")
    assert "walked the delta diff" in summary
    assert "walked the whole diff" not in summary


def test_degraded_living_hunt_names_delta_in_title():
    ev = CodeReviewEvaluation(
        findings=(), conclusion="neutral", degraded_reason="timeout",
    )
    title, _ = cr_dispatch._summary_markdown(
        ev, living_range="abc12345..def67890",
    )
    assert title.startswith("Living Hunt abc12345..def67890 - ")


def test_fetch_pr_diff_scope_reports_full_fallback(monkeypatch):
    responses = [
        httpx.Response(422, request=httpx.Request("GET", "https://compare")),
        httpx.Response(
            200, text="full diff",
            request=httpx.Request("GET", "https://pull"),
        ),
    ]
    requested_urls: list[str] = []

    def fake_get(url: str, **_kwargs):
        requested_urls.append(url)
        return responses.pop(0)

    monkeypatch.setattr(
        cr_dispatch.httpx, "get", fake_get,
    )

    diff, used_compare, _rejected = cr_dispatch.fetch_pr_diff_with_scope(
        "token", "owner", "repo", 7, base_sha="abc", head_sha="def",
    )

    assert diff == "full diff"
    assert used_compare is False
    assert requested_urls == [
        "https://api.github.com/repos/owner/repo/compare/abc...def",
        "https://api.github.com/repos/owner/repo/pulls/7",
    ]


def test_fetch_pr_diff_scope_zippie_127_diverged_baseline_falls_back(monkeypatch):
    """grug#845, reproduced live on quadseven/zippie PR #127 and verified
    independently against the GitHub compare API: a diff-media-type
    compare between a non-ancestor base and the PR head returns 200 with a
    REAL (but wrong) diff - nothing about that response says the history
    diverged. Using the recorded compare response
    (`GET .../compare/4dbd2011...2a913a31` -> `status: diverged, ahead_by:
    18, behind_by: 2`) rather than a live call, per acceptance criterion 2."""
    responses = [
        # 1. the diff-media-type compare "succeeds" with a real diff of
        #    unrelated history - exactly what fooled the caller before this
        #    fix (it names files nowhere in the actual PR).
        httpx.Response(
            200,
            text="diff --git a/companion-android/x.kt b/companion-android/x.kt\n...",
            request=httpx.Request("GET", "https://compare-diff"),
        ),
        # 2. the JSON-media-type re-check on the SAME range - the recorded
        #    zippie #127 response.
        httpx.Response(
            200,
            json={"status": "diverged", "ahead_by": 18, "behind_by": 2},
            request=httpx.Request("GET", "https://compare-json"),
        ),
        # 3. fallback: the ordinary mutable PR diff.
        httpx.Response(
            200, text="the real PR #127 diff",
            request=httpx.Request("GET", "https://pull"),
        ),
    ]
    requested_urls: list[str] = []

    def fake_get(url: str, **_kwargs):
        requested_urls.append(url)
        return responses.pop(0)

    monkeypatch.setattr(cr_dispatch.httpx, "get", fake_get)

    diff, used_compare, _rejected = cr_dispatch.fetch_pr_diff_with_scope(
        "token", "quadseven", "zippie", 127,
        base_sha="4dbd2011", head_sha="2a913a31",
    )

    assert diff == "the real PR #127 diff"
    assert used_compare is False   # rejected baseline - never silently trusted
    assert requested_urls == [
        "https://api.github.com/repos/quadseven/zippie/compare/4dbd2011...2a913a31",
        "https://api.github.com/repos/quadseven/zippie/compare/4dbd2011...2a913a31",
        "https://api.github.com/repos/quadseven/zippie/pulls/127",
    ]


def test_fetch_pr_diff_scope_behind_baseline_falls_back(monkeypatch):
    """A base AHEAD of head (status: behind) is just as unusable as
    diverged - head moved backward relative to the stored pointer, which
    should never happen for an honest git history but must still degrade
    safely rather than being trusted."""
    responses = [
        httpx.Response(200, text="wrong diff", request=httpx.Request("GET", "https://compare-diff")),
        httpx.Response(
            200, json={"status": "behind", "ahead_by": 0, "behind_by": 3},
            request=httpx.Request("GET", "https://compare-json"),
        ),
        httpx.Response(200, text="full diff", request=httpx.Request("GET", "https://pull")),
    ]
    monkeypatch.setattr(cr_dispatch.httpx, "get", lambda url, **kw: responses.pop(0))

    diff, used_compare, _rejected = cr_dispatch.fetch_pr_diff_with_scope(
        "token", "owner", "repo", 7, base_sha="abc", head_sha="def",
    )
    assert diff == "full diff"
    assert used_compare is False


def test_fetch_pr_diff_scope_clean_ahead_baseline_uses_compare(monkeypatch):
    """The happy path is unchanged: a genuine ancestor base (status: ahead,
    behind_by: 0) still returns the scoped delta diff, not the full PR."""
    responses = [
        httpx.Response(200, text="the real delta diff", request=httpx.Request("GET", "https://compare-diff")),
        httpx.Response(
            200,
            json={
                "status": "ahead", "ahead_by": 3, "behind_by": 0,
                "commits": [_commit("c1", "p0"), _commit("c2", "c1"),
                            _commit("c3", "c2")],
            },
            request=httpx.Request("GET", "https://compare-json"),
        ),
    ]
    monkeypatch.setattr(cr_dispatch.httpx, "get", lambda url, **kw: responses.pop(0))

    diff, used_compare, _rejected = cr_dispatch.fetch_pr_diff_with_scope(
        "token", "owner", "repo", 7, base_sha="abc", head_sha="def",
    )
    assert diff == "the real delta diff"
    assert used_compare is True


def _commit(sha: str, *parents: str) -> dict:
    return {"sha": sha, "parents": [{"sha": p} for p in parents]}


def test_fetch_pr_diff_scope_merge_from_main_reviews_pr_own_diff(monkeypatch):
    """grug#1092: reviewed SHA -> merge of main -> head. The compare is
    `ahead` with behind_by 0, but its diff carries main's changes. The range
    holds a merge commit, so the PR's own diff must be reviewed instead."""
    main_only = "diff --git a/walkthrough/related.py b/walkthrough/related.py\n"
    pr_only = "diff --git a/feature.py b/feature.py\n"
    responses = [
        httpx.Response(
            200, text=pr_only + main_only,
            request=httpx.Request("GET", "https://compare-diff"),
        ),
        httpx.Response(
            200,
            json={
                "status": "ahead", "ahead_by": 3, "behind_by": 0,
                "commits": [
                    _commit("m1", "reviewed", "main-tip"),
                    _commit("m0", "main-tip", "main-prev"),
                    _commit("h1", "m1"),
                ],
            },
            request=httpx.Request("GET", "https://compare-json"),
        ),
        httpx.Response(
            200, text=pr_only, request=httpx.Request("GET", "https://pull"),
        ),
    ]
    urls: list[str] = []

    def fake_get(url: str, **_kw):
        urls.append(url)
        return responses.pop(0)

    monkeypatch.setattr(cr_dispatch.httpx, "get", fake_get)

    diff, used_compare, rejected = cr_dispatch.fetch_pr_diff_with_scope(
        "token", "owner", "repo", 7, base_sha="reviewed", head_sha="h1",
    )
    assert "walkthrough/related.py" not in diff
    assert "feature.py" in diff
    assert used_compare is False
    assert rejected is True
    assert urls[-1] == "https://api.github.com/repos/owner/repo/pulls/7"


def test_compare_is_clean_ancestor_rejects_range_with_merge_commit(monkeypatch):
    body = {
        "status": "ahead", "ahead_by": 2, "behind_by": 0,
        "commits": [_commit("m1", "a", "b"), _commit("h1", "m1")],
    }
    monkeypatch.setattr(
        cr_dispatch.httpx, "get",
        lambda url, **kw: httpx.Response(
            200, json=body, request=httpx.Request("GET", url)),
    )
    assert review_publish._compare_is_clean_ancestor(
        "token", "owner", "repo", "a", "h1") is False


def test_compare_is_clean_ancestor_fails_closed_on_truncated_commit_list(
    monkeypatch,
):
    """GitHub caps `commits` per page; a list shorter than ahead_by cannot
    prove the absence of a merge commit."""
    body = {
        "status": "ahead", "ahead_by": 400, "behind_by": 0,
        "commits": [_commit("c1", "c0")],
    }
    monkeypatch.setattr(
        cr_dispatch.httpx, "get",
        lambda url, **kw: httpx.Response(
            200, json=body, request=httpx.Request("GET", url)),
    )
    assert review_publish._compare_is_clean_ancestor(
        "token", "owner", "repo", "a", "h1") is False


def test_compare_is_clean_ancestor_accepts_linear_fast_forward(monkeypatch):
    body = {
        "status": "ahead", "ahead_by": 2, "behind_by": 0,
        "commits": [_commit("c1", "a"), _commit("c2", "c1")],
    }
    monkeypatch.setattr(
        cr_dispatch.httpx, "get",
        lambda url, **kw: httpx.Response(
            200, json=body, request=httpx.Request("GET", url)),
    )
    assert review_publish._compare_is_clean_ancestor(
        "token", "owner", "repo", "a", "c2") is True


def test_fetch_pr_diff_scope_diverged_reports_baseline_rejected(monkeypatch):
    """grug#845's last criterion: a rejected baseline must be REPORTED, not
    just logged. The fetch layer distinguishes \"baseline unusable because
    diverged\" (rejected=True) from \"baseline unavailable\" (404/422,
    rejected=False) so the summary can say which happened."""
    responses = [
        httpx.Response(
            200,
            text="diff --git a/unrelated/x.kt b/unrelated/x.kt\n...",
            request=httpx.Request("GET", "https://compare-diff"),
        ),
        httpx.Response(
            200,
            json={"status": "diverged", "ahead_by": 18, "behind_by": 2},
            request=httpx.Request("GET", "https://compare-json"),
        ),
        httpx.Response(200, text="full diff", request=httpx.Request("GET", "https://pull")),
    ]
    monkeypatch.setattr(cr_dispatch.httpx, "get", lambda url, **kw: responses.pop(0))

    diff, used_compare, baseline_rejected = cr_dispatch.fetch_pr_diff_with_scope(
        "token",
        "owner",
        "repo",
        7,
        base_sha="abc",
        head_sha="def",
    )
    assert diff == "full diff"
    assert used_compare is False
    assert baseline_rejected is True


def test_fetch_pr_diff_scope_unavailable_baseline_is_not_rejected(monkeypatch):
    """A 404/422 means the baseline is simply unavailable (never stored,
    GC'd) - not a rejected one. The summary must not claim a rewrite."""
    responses = [
        httpx.Response(422, request=httpx.Request("GET", "https://compare")),
        httpx.Response(200, text="full diff", request=httpx.Request("GET", "https://pull")),
    ]
    monkeypatch.setattr(cr_dispatch.httpx, "get", lambda url, **kw: responses.pop(0))

    diff, used_compare, baseline_rejected = cr_dispatch.fetch_pr_diff_with_scope(
        "token",
        "owner",
        "repo",
        7,
        base_sha="abc",
        head_sha="def",
    )
    assert diff == "full diff"
    assert used_compare is False
    assert baseline_rejected is False


def test_summary_marks_rejected_living_hunt_baseline():
    """grug#845: when the stored Living Hunt baseline was rejected as a
    non-ancestor, the check-run summary says so - the fallback to the full
    PR diff is visible, not silent."""
    from personas.code_reviewer.persona import CodeReviewEvaluation
    ev = CodeReviewEvaluation(findings=(), conclusion="success")
    title, summary = cr_dispatch._summary_markdown(
        ev,
        living_baseline_rejected="4dbd2011",
    )
    assert "4dbd2011" in summary
    assert "not an ancestor" in summary
    assert "full PR diff" in summary
    # The title stays clean: no range to name, nothing to prefix.
    assert "Living Hunt" not in title


def test_summary_omits_rejected_note_without_rejection():
    """No rejection, no note - ordinary reviews and clean Living Hunt
    deltas render exactly as before."""
    from personas.code_reviewer.persona import CodeReviewEvaluation
    ev = CodeReviewEvaluation(findings=(), conclusion="success")
    _, summary = cr_dispatch._summary_markdown(ev)
    assert "not an ancestor" not in summary
    _, summary = cr_dispatch._summary_markdown(ev, living_range="abc123..def456")
    assert "not an ancestor" not in summary
    assert "Living Hunt: reviewing `abc123..def456`" in summary


def test_compare_is_clean_ancestor_fails_closed_on_transport_error(monkeypatch):
    """An unreadable status is not proof of a clean history - a compare
    that cannot even be checked must never be trusted."""
    def _raise(url, **kw):
        raise httpx.ConnectTimeout("gh down", request=httpx.Request("GET", url))

    monkeypatch.setattr(cr_dispatch.httpx, "get", _raise)
    assert review_publish._compare_is_clean_ancestor(
        "token", "owner", "repo", "abc", "def",
    ) is False


def test_compare_is_clean_ancestor_fails_closed_on_malformed_body(monkeypatch):
    monkeypatch.setattr(
        cr_dispatch.httpx, "get",
        lambda url, **kw: httpx.Response(200, text="not json", request=httpx.Request("GET", url)),
    )
    assert review_publish._compare_is_clean_ancestor(
        "token", "owner", "repo", "abc", "def",
    ) is False


def test_elder_last_sk_is_stable():
    from adapters.pg_install_store import _elder_last_sk

    assert _elder_last_sk("o/r", 7) == "ELDER#LAST#o/r#7"


def test_changed_line_text_empty_body_returns_none():
    """No AttributeError when body is empty/missing (high null-deref path)."""
    header_only = DiffHunk(
        file_path="a.yml",
        new_start=1,
        new_lines=frozenset({1}),
        body="@@ -0,0 +1 @@\n",
    )
    # Valid header-only body: no new-side content at line 1 after header.
    assert _changed_line_text(header_only, 1) is None
    # Synthetic object with body=None must not raise.
    synthetic = MagicMock(spec=["body", "new_start"])
    synthetic.body = None
    synthetic.new_start = 1
    assert _changed_line_text(synthetic, 1) is None


def test_put_elder_last_reviewed_upserts_with_90d_ttl(monkeypatch):
    """put_elder_last_reviewed writes ON CONFLICT upsert and ~90-day TTL."""
    import adapters.pg_install_store as store

    executed: list[tuple] = []

    class _Conn:
        def execute(self, sql, params):
            executed.append((sql, params))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Pool:
        def connection(self):
            return _Conn()

    monkeypatch.setattr(store, "get_pool", lambda: _Pool())
    before = int(datetime.now(timezone.utc).timestamp())
    store.put_elder_last_reviewed(
        install_id=9, repo="o/r", pr_number=7, head_sha="deadbeef",
    )
    after = int(datetime.now(timezone.utc).timestamp())

    assert len(executed) == 1
    sql, params = executed[0]
    assert "ON CONFLICT (pk, sk) DO UPDATE" in sql
    assert params["sk"] == "ELDER#LAST#o/r#7"
    assert params["data"]  # encoded attrs present
    # TTL = now + 90 days (±2s clock slack for the test run).
    expected_lo = before + store._ELDER_LAST_TTL_DAYS * 86400 - 2
    expected_hi = after + store._ELDER_LAST_TTL_DAYS * 86400 + 2
    assert expected_lo <= params["ttl"] <= expected_hi
    assert store._ELDER_LAST_TTL_DAYS == 90


def test_living_hunt_delta_done_log_on_successful_persist(monkeypatch, caplog):
    """Successful delta review with stable head emits elder_living_hunt_delta_done."""
    import logging

    from llm_client import Backend, LlmReviewResponse

    llm = LlmReviewResponse(
        kind="reviewed", findings=(), backend_used=Backend.CAVE, model_name="coder",
    )
    put_calls: list[dict] = []

    monkeypatch.setattr(
        cr_dispatch, "with_install_token_retry",
        lambda inst_id, fn: fn("fake-token"),
    )
    monkeypatch.setattr(
        review_publish, "with_install_token_retry",
        lambda inst_id, fn: fn("fake-token"),
    )
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **k: llm)
    monkeypatch.setattr(
        cr_dispatch, "post_check_run",
        lambda *a, **k: {"id": 1},
    )
    monkeypatch.setattr(
        cr_dispatch, "post_review",
        lambda *a, **k: {"id": 2},
    )
    monkeypatch.setattr(cr_dispatch, "grade_findings", lambda *a, **kw: ())
    monkeypatch.setattr(
        cr_dispatch, "fetch_pr_diff_with_scope",
        lambda *a, **k: ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n", True, False),
    )
    monkeypatch.setattr(
        cr_dispatch, "_fetch_current_review_snapshot",
        lambda *a, **k: ("base5678ijkl", "abcd1234efgh", "t", "b"),
    )

    def _fake_put(**kwargs):
        put_calls.append(kwargs)

    # Prior head makes living_prior_sha set and living_range non-empty.
    with patch(
        "adapters.install_store.get_elder_last_reviewed",
        return_value="priorhead0001",
    ), patch(
        "adapters.install_store.put_elder_last_reviewed",
        side_effect=_fake_put,
    ):
        with caplog.at_level(logging.INFO):
            out = cr_dispatch.dispatch_code_review(
                {
                    "action": "synchronize",
                    "installation": {"id": 11},
                    "repository": {
                        "id": 22,
                        "name": "myrepo",
                        "owner": {"login": "myorg"},
                    },
                    "pull_request": {
                        "number": 7,
                        "head": {"sha": "abcd1234efgh"},
                        "base": {"sha": "base5678ijkl"},
                        "title": "t",
                        "body": "b",
                        "user": {"login": "alice"},
                    },
                },
                blocking=False,
            )

    assert out["result"] in {"pass", "fail", "skipped"}
    assert put_calls, "expected put_elder_last_reviewed after stable head"
    assert any(
        getattr(r, "message", "") == "elder_living_hunt_delta_done"
        or "elder_living_hunt_delta_done" in str(r)
        for r in caplog.records
    ) or any(
        "elder_living_hunt_delta_done" in r.getMessage() for r in caplog.records
    )


def test_push_after_first_review_is_a_scoped_delta_review(monkeypatch):
    """grug#1188 AC: a push after the first review gives the model the diff
    from the last reviewed head, and the check names the commit range it
    reviewed. The full PR diff is fetched only for the deterministic
    lane-work checks (#1343), never handed to the model."""
    from github_checks_client import CheckRunResult
    from llm_client import Backend, LlmReviewResponse

    llm = LlmReviewResponse(
        kind="reviewed", findings=(), backend_used=Backend.CAVE, model_name="coder",
    )
    scoped_calls: list[dict] = []
    titles: list[str] = []

    def _scoped(*_a, **k):
        scoped_calls.append(k)
        return ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n", True, False)

    full_calls: list[dict] = []
    reviewed_paths: list[set[str]] = []

    def _full(*_a, **k):
        full_calls.append(k)
        return "diff --git a/earlier.py b/earlier.py\n--- a/earlier.py\n+++ b/earlier.py\n@@ -1 +1 @@\n-a\n+b\n"

    contexts: list[dict] = []
    steering_refs: list[tuple[str, str]] = []

    def _review(evidence, *_a, **_k):
        reviewed_paths.append({h.path for h in evidence.hunks})
        contexts.append(dict(evidence.pr_context))
        return llm

    def _check(*a, **k):
        result = k.get("result") or next(x for x in a if isinstance(x, CheckRunResult))
        titles.append(result.title)
        return {"id": 1}

    def _docs(_token, _owner, _repo, ref, _hunks):
        steering_refs.append(("docs", ref))

    def _ruff(_install, _owner, _repo, ref):
        steering_refs.append(("ruff", ref))
        return cr_dispatch.parse_repo_ruff_policy(None)

    monkeypatch.setenv("GRUG_ELDER_ANALYZERS", "ruff")
    monkeypatch.setattr(cr_dispatch, "_load_repo_docs", _docs)
    monkeypatch.setattr(cr_dispatch, "_load_repo_ruff_policy", _ruff)
    monkeypatch.setattr(cr_dispatch, "with_install_token_retry", lambda _i, fn: fn("t"))
    monkeypatch.setattr(review_publish, "with_install_token_retry", lambda _i, fn: fn("t"))
    monkeypatch.setattr(cr_dispatch, "review_diff", _review)
    monkeypatch.setattr(cr_dispatch, "post_check_run", _check)
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **k: {"id": 2})
    monkeypatch.setattr(cr_dispatch, "grade_findings", lambda *a, **kw: ())
    monkeypatch.setattr(cr_dispatch, "fetch_pr_diff_with_scope", _scoped)
    monkeypatch.setattr(cr_dispatch, "fetch_pr_diff", _full)
    monkeypatch.setattr(
        cr_dispatch, "_fetch_current_review_snapshot",
        lambda *a, **k: ("base5678ijkl", "abcd1234efgh", "t", "b"),
    )

    with patch(
        "adapters.install_store.get_elder_last_reviewed",
        return_value="prior0001head",
    ), patch("adapters.install_store.put_elder_last_reviewed", lambda **k: None):
        cr_dispatch.dispatch_code_review(
            {
                "action": "synchronize",
                "installation": {"id": 11},
                "repository": {"id": 22, "name": "myrepo", "owner": {"login": "myorg"}},
                "pull_request": {
                    "number": 7,
                    "head": {"sha": "abcd1234efgh"},
                    "base": {"sha": "base5678ijkl"},
                    "title": "t",
                    "body": "b",
                    "user": {"login": "alice"},
                },
            },
            blocking=False,
        )

    assert len(scoped_calls) == 1
    assert scoped_calls[0]["base_sha"] == "prior0001head"
    assert scoped_calls[0]["head_sha"] == "abcd1234efgh"
    assert titles and titles[-1].startswith("Living Hunt prior000..abcd1234")
    assert reviewed_paths == [{"x.py"}]
    assert [(c["base_sha"], c["head_sha"]) for c in full_calls] == [
        ("base5678ijkl", "abcd1234efgh"),
    ]
    # grug#1574: the diff starts at a PR commit, the steering files do not.
    assert contexts[0]["base_sha"] == "prior0001head"
    assert contexts[0]["pr_base_sha"] == "base5678ijkl"
    assert ("docs", "base5678ijkl") in steering_refs
    assert ("ruff", "base5678ijkl") in steering_refs
    assert {ref for _, ref in steering_refs} == {"base5678ijkl"}


# --- #1343: the lane-work checks judge the whole PR on every pass ------------

_LANE_BODY = (
    "## Why\nRetry the nightly upload.\n\n---\n"
    "Loom ticket example.ops-83-r1, lane codex, model coder.\nRefs #83\n"
)
_LINKED = "#83 criterion 1 [open]: The nightly sync retries a failed upload three times\n"
_SCRIPT = ["#!/bin/sh", "set -eu", "for i in 1 2 3; do upload && break; done"]
_WORKFLOW = ["on: schedule", "jobs:", "  sync:", "    steps:", "      - run: ops/retry-upload.sh"]


def _added(path: str, lines: list[str]) -> str:
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\nindex 0000000..1111111\n"
        f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n"
        + "".join(f"+{ln}\n" for ln in lines)
    )


def _lane_work_on_delta(monkeypatch, *, delta: str, full: str) -> list[tuple]:
    """Run a delta review (a prior head is stored) of a lane PR and return
    the (diff, findings) of every lane-work call."""
    from llm_client import Backend, LlmReviewResponse

    llm = LlmReviewResponse(
        kind="reviewed", findings=(), backend_used=Backend.CAVE, model_name="coder",
    )
    calls: list[tuple] = []
    real = cr_dispatch.lane_work_findings

    def _spy(**kw):
        found = real(**kw)
        calls.append((kw["diff_text"], tuple((f.rule_name, f.severity) for f in found)))
        return found

    monkeypatch.setattr(cr_dispatch, "lane_work_findings", _spy)
    monkeypatch.setattr(cr_dispatch, "with_install_token_retry", lambda _i, fn: fn("t"))
    monkeypatch.setattr(review_publish, "with_install_token_retry", lambda _i, fn: fn("t"))
    monkeypatch.setattr(cr_dispatch, "review_diff", lambda *a, **k: llm)
    monkeypatch.setattr(cr_dispatch, "post_check_run", lambda *a, **k: {"id": 1})
    monkeypatch.setattr(cr_dispatch, "post_review", lambda *a, **k: {"id": 2})
    monkeypatch.setattr(cr_dispatch, "grade_findings", lambda *a, **kw: ())
    monkeypatch.setattr(cr_dispatch, "fetch_file_contents", lambda *a, **k: {})
    monkeypatch.setattr(cr_dispatch, "build_linked_issue_context", lambda *a, **k: _LINKED)
    monkeypatch.setattr(
        cr_dispatch, "fetch_pr_diff_with_scope", lambda *a, **k: (delta, True, False),
    )
    monkeypatch.setattr(cr_dispatch, "fetch_pr_diff", lambda *a, **k: full)
    monkeypatch.setattr(
        cr_dispatch, "_fetch_current_review_snapshot",
        lambda *a, **k: ("base5678ijkl", "abcd1234efgh", "t", _LANE_BODY),
    )
    with patch(
        "adapters.install_store.get_elder_last_reviewed", return_value="prior0001head",
    ), patch("adapters.install_store.put_elder_last_reviewed", lambda **k: None):
        cr_dispatch.dispatch_code_review(
            {
                "action": "synchronize",
                "installation": {"id": 11},
                "repository": {"id": 22, "name": "myrepo", "owner": {"login": "myorg"}},
                "pull_request": {
                    "number": 7,
                    "head": {"sha": "abcd1234efgh"},
                    "base": {"sha": "base5678ijkl"},
                    "title": "t",
                    "body": _LANE_BODY,
                    "user": {"login": "alice"},
                },
            },
            blocking=False,
        )
    return calls


def test_a_delta_review_judges_criteria_on_the_whole_pr(monkeypatch):
    """The earlier commit wired the script into the nightly workflow; the
    delta only adds the script. Judged alone the delta's script is inert
    against the runtime criterion; judged as the PR it meets it."""
    delta = _added("ops/retry-upload.sh", _SCRIPT)
    full = delta + _added(".github/workflows/nightly.yml", _WORKFLOW)
    calls = _lane_work_on_delta(monkeypatch, delta=delta, full=full)
    assert [diff for diff, _ in calls] == [full]
    assert calls[0][1] == ()


def test_a_delta_review_still_reports_a_criterion_no_commit_meets(monkeypatch):
    delta = _added("ops/retry-upload.sh", _SCRIPT)
    full = delta + _added("docs/notes.md", ["Upload notes."])
    calls = _lane_work_on_delta(monkeypatch, delta=delta, full=full)
    assert len(calls) == 1
    assert ("intent-mismatch", "high") in calls[0][1]
