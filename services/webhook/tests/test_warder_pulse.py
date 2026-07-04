"""Warder (#471) + Pulse (#472) tracer tests - the roster-completing
personas. Warder: changelog grouping + semver hint (pure) and the
merged-PR dispatch gate. Pulse: staleness filter, DoR gate, claim
idempotency, per-install cap.
"""
from __future__ import annotations

from unittest.mock import patch

import httpx

from personas.warder import dispatch as warder
from personas.pulse import nudge as pulse


# ── Warder: pure grouping/semver ──────────────────────────────────────


def test_group_commits_conventional():
    groups = warder.group_commits((
        "feat(api): add tenant scoping",
        "fix: null deref in poller",
        "feat!: drop v1 endpoints",
        "docs: update readme",
        "random commit without prefix",
    ))
    assert groups["feat"] == ["add tenant scoping"]
    assert groups["fix"] == ["null deref in poller"]
    assert groups["breaking"] == ["drop v1 endpoints"]
    assert groups["other"] == ["random commit without prefix"]


def test_group_commits_freeform_repo_never_crashes():
    groups = warder.group_commits(("wip", "more stuff", "", "final?"))
    assert set(groups) == {"other"}
    assert len(groups["other"]) == 3  # empty subject dropped


def test_semver_hint_precedence():
    assert warder.semver_hint({"breaking": ["x"], "feat": ["y"]}) == "major"
    assert warder.semver_hint({"feat": ["y"], "fix": ["z"]}) == "minor"
    assert warder.semver_hint({"fix": ["z"]}) == "patch"
    assert warder.semver_hint({}) == "patch"


def test_changelog_markdown_groups_in_order():
    md = warder.changelog_markdown(
        {"fix": ["b"], "breaking": ["a"], "other": ["c"]}, since="v1.2.3",
    )
    assert md.index("BREAKING") < md.index("Fixes") < md.index("Other")
    assert "v1.2.3" in md


# ── Warder: dispatch gate ─────────────────────────────────────────────


def _warder_ctx(payload):
    from personas.registry import PullRequestContext

    return PullRequestContext(
        installation_id=1, owner="o", repo_name="r", head_sha="headsha",
        pr_number=5, pr_body="", payload=payload, delivery_id="d", blocking=False,
    )


def test_warder_skips_unmerged_close(monkeypatch):
    from personas.warder import webhook_dispatch as wd

    called = []
    monkeypatch.setattr(
        warder, "dispatch_warder_release", lambda **kw: called.append(kw) or {},
    )
    out = wd.dispatch_pull_request(_warder_ctx({
        "pull_request": {"merged": False}, "repository": {},
    }))
    assert out == {"persona": "warder", "result": "skipped"}
    assert called == []


def test_warder_skips_side_branch_merge(monkeypatch):
    from personas.warder import webhook_dispatch as wd

    out = wd.dispatch_pull_request(_warder_ctx({
        "pull_request": {"merged": True, "base": {"ref": "develop"}},
        "repository": {"default_branch": "main"},
    }))
    assert out == {"persona": "warder", "result": "skipped"}


def test_warder_dispatches_default_branch_merge_on_merge_sha(monkeypatch):
    from personas.warder import webhook_dispatch as wd

    seen = {}
    monkeypatch.setattr(
        wd, "dispatch_pull_request", wd.dispatch_pull_request,  # no-op anchor
    )

    def fake_release(**kw):
        seen.update(kw)
        return {"persona": "warder", "result": "pass"}

    monkeypatch.setattr(
        "personas.warder.dispatch.dispatch_warder_release", fake_release,
    )
    out = wd.dispatch_pull_request(_warder_ctx({
        "pull_request": {"merged": True, "base": {"ref": "main"},
                         "merge_commit_sha": "mergesha"},
        "repository": {"default_branch": "main"},
    }))
    assert out["result"] == "pass"
    assert seen["head_sha"] == "mergesha"  # anchored on the merge commit


def test_warder_release_degrades_on_fetch_failure(monkeypatch):
    posted = []
    monkeypatch.setattr(
        warder, "with_install_token_retry",
        lambda iid, fn: (_ for _ in ()).throw(httpx.ConnectTimeout("gh down", request=None)),
    )
    # publish + verdict paths also use with_install_token_retry - the
    # patched version raises there too, exercising publish_failed... use
    # a two-phase patch instead: first call raises, later calls succeed.
    calls = {"n": 0}

    def fake_retry(iid, fn):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("gh down", request=None)
        return fn("tok")

    monkeypatch.setattr(warder, "with_install_token_retry", fake_retry)
    monkeypatch.setattr(warder, "post_check_run", lambda *a, **kw: posted.append(kw) or {})
    monkeypatch.setattr(warder, "record_check_verdict", lambda **kw: posted.append(kw))
    out = warder.dispatch_warder_release(
        installation_id=1, owner="o", repo_name="r", head_sha="s", pr_number=2,
    )
    assert out == {"persona": "warder", "result": "skipped"}  # degraded, honest
    # verdict row recorded with the degraded reason
    verdicts = [p for p in posted if p.get("persona_key") == "warder"]
    assert verdicts and verdicts[0]["degraded_reason"] == "fetch_failed"


# ── Pulse ─────────────────────────────────────────────────────────────


def _pr(number, updated_at, sha="s"):
    return {"number": number, "updated_at": updated_at, "head": {"sha": sha}}


def test_pulse_nudges_stale_green_pr_once(monkeypatch):
    stale = _pr(1, "2020-01-01T00:00:00Z")
    monkeypatch.setattr(pulse, "get_repo_config", lambda i, r: {"pulse_enabled": True})
    monkeypatch.setattr(pulse, "_stale_prs", lambda t, o, r: [stale])
    monkeypatch.setattr(pulse, "_dor_green", lambda t, o, r, s: True)
    claims = []
    monkeypatch.setattr(
        pulse, "claim_pulse_nudge",
        lambda i, repo, pr: claims.append((repo, pr)) or True,
    )
    comments = []
    monkeypatch.setattr(
        pulse.httpx, "post",
        lambda url, **kw: comments.append(url) or httpx.Response(
            201, request=httpx.Request("POST", url), json={},
        ),
    )
    verdicts = []
    monkeypatch.setattr(pulse, "record_check_verdict", lambda **kw: verdicts.append(kw))

    n = pulse.run_pulse_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}])
    assert n == 1 and len(comments) == 1
    assert claims == [("o/r", 1)]
    assert verdicts[0]["persona_key"] == "pulse"


def test_pulse_lost_claim_posts_nothing(monkeypatch):
    monkeypatch.setattr(pulse, "get_repo_config", lambda i, r: {"pulse_enabled": True})
    monkeypatch.setattr(pulse, "_stale_prs", lambda t, o, r: [_pr(1, "2020-01-01T00:00:00Z")])
    monkeypatch.setattr(pulse, "_dor_green", lambda t, o, r, s: True)
    monkeypatch.setattr(pulse, "claim_pulse_nudge", lambda i, repo, pr: False)
    monkeypatch.setattr(
        pulse.httpx, "post",
        lambda url, **kw: (_ for _ in ()).throw(AssertionError("no comment expected")),
    )
    assert pulse.run_pulse_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}]) == 0


def test_pulse_disabled_repo_costs_no_github_calls(monkeypatch):
    monkeypatch.setattr(pulse, "get_repo_config", lambda i, r: {"pulse_enabled": False})
    monkeypatch.setattr(
        pulse, "_stale_prs",
        lambda t, o, r: (_ for _ in ()).throw(AssertionError("no PR list expected")),
    )
    assert pulse.run_pulse_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}]) == 0


def test_pulse_respects_per_install_cap(monkeypatch):
    monkeypatch.setattr(pulse, "get_repo_config", lambda i, r: {"pulse_enabled": True})
    many = [_pr(i, "2020-01-01T00:00:00Z") for i in range(10)]
    monkeypatch.setattr(pulse, "_stale_prs", lambda t, o, r: many)
    monkeypatch.setattr(pulse, "_dor_green", lambda t, o, r, s: True)
    monkeypatch.setattr(pulse, "claim_pulse_nudge", lambda i, repo, pr: True)
    posts = []
    monkeypatch.setattr(
        pulse.httpx, "post",
        lambda url, **kw: posts.append(url) or httpx.Response(
            201, request=httpx.Request("POST", url), json={},
        ),
    )
    monkeypatch.setattr(pulse, "record_check_verdict", lambda **kw: None)
    n = pulse.run_pulse_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}])
    assert n == pulse._MAX_NUDGES_PER_INSTALL_RUN
    assert len(posts) == pulse._MAX_NUDGES_PER_INSTALL_RUN


def test_pulse_dor_not_green_skips(monkeypatch):
    monkeypatch.setattr(pulse, "get_repo_config", lambda i, r: {"pulse_enabled": True})
    monkeypatch.setattr(pulse, "_stale_prs", lambda t, o, r: [_pr(1, "2020-01-01T00:00:00Z")])
    monkeypatch.setattr(pulse, "_dor_green", lambda t, o, r, s: False)
    monkeypatch.setattr(
        pulse, "claim_pulse_nudge",
        lambda i, repo, pr: (_ for _ in ()).throw(AssertionError("no claim expected")),
    )
    assert pulse.run_pulse_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}]) == 0


# ── Registry: warder action seam ──────────────────────────────────────


def test_warder_only_wakes_on_closed_action():
    """The dispatcher tests already pin that 'opened' dispatches exactly
    tpm/code_reviewer/guard - this pins the inverse: warder registers
    for 'closed' only, and pulse for no webhook action at all."""
    from personas import registry

    assert registry.by_key("warder").actions == ("closed",)
    assert registry.by_key("pulse").actions == ()
    assert registry.by_key("pulse").events == ()
    assert registry.by_key("tpm").actions == registry.PR_UPDATE_ACTIONS


def test_pulse_post_failure_releases_claim_for_retry(monkeypatch):
    """Codex PR #489: a claim must represent a COMPLETED nudge. If the
    comment POST fails after the claim, the claim is released so the
    next cron tick retries - a transient GitHub blip must not burn the
    weekly slot."""
    monkeypatch.setattr(pulse, "get_repo_config", lambda i, r: {"pulse_enabled": True})
    monkeypatch.setattr(pulse, "_stale_prs", lambda t, o, r: [_pr(1, "2020-01-01T00:00:00Z")])
    monkeypatch.setattr(pulse, "_dor_green", lambda t, o, r, s: True)
    monkeypatch.setattr(pulse, "claim_pulse_nudge", lambda i, repo, pr: True)
    released = []
    monkeypatch.setattr(
        pulse, "release_pulse_nudge",
        lambda i, repo, pr: released.append((repo, pr)),
    )
    monkeypatch.setattr(
        pulse.httpx, "post",
        lambda url, **kw: (_ for _ in ()).throw(
            httpx.ConnectTimeout("gh down", request=None)
        ),
    )
    n = pulse.run_pulse_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}])
    assert n == 0
    assert released == [("o/r", 1)]
