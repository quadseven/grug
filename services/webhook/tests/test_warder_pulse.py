"""Warder (#471) + Pulse (#472) tracer tests - the roster-completing
personas. Warder: changelog grouping + semver hint (pure) and the
merged-PR dispatch gate. Pulse: staleness filter, DoR gate, claim
idempotency, per-install cap.
"""
from __future__ import annotations

from unittest.mock import patch

import httpx

from personas.warder import dispatch as warder
from personas.warder import slo_gate
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


def _warder_ctx(payload, *, blocking=False):
    from personas.registry import PullRequestContext

    return PullRequestContext(
        installation_id=1, owner="o", repo_name="r", head_sha="headsha",
        pr_number=5, pr_body="", payload=payload, delivery_id="d", blocking=blocking,
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


# ── Warder: deploy gate (grug#533) ──────────────────────────────────────


def test_slo_map_parser_rejects_junk():
    assert slo_gate._slo_map_from_json("not json") == {}
    assert slo_gate._slo_map_from_json('["list"]') == {}
    assert slo_gate._slo_map_from_json(
        '{"o/r": 123, "bad": "not-an-int", "also_bad": null}',
    ) == {"o/r": 123}


def test_query_monitor_state_returns_overall_state(monkeypatch):
    def fake_get(url, **kw):
        assert url == "https://api.datadoghq.com/api/v1/monitor/123"
        assert kw["headers"]["DD-API-KEY"] == "api"
        assert kw["headers"]["DD-APPLICATION-KEY"] == "app"
        return httpx.Response(
            200, json={"overall_state": "Alert"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(slo_gate.httpx, "get", fake_get)
    assert slo_gate.query_monitor_state(123, "api", "app") == "Alert"


def test_query_monitor_state_degrades_on_http_error(monkeypatch):
    def fake_get(url, **kw):
        return httpx.Response(404, request=httpx.Request("GET", url))

    monkeypatch.setattr(slo_gate.httpx, "get", fake_get)
    assert slo_gate.query_monitor_state(123, "api", "app") is None


def test_query_monitor_state_degrades_on_malformed_body(monkeypatch):
    def fake_get(url, **kw):
        return httpx.Response(200, json={"no_state_field": True}, request=httpx.Request("GET", url))

    monkeypatch.setattr(slo_gate.httpx, "get", fake_get)
    assert slo_gate.query_monitor_state(123, "api", "app") is None


def test_is_healthy():
    assert slo_gate.is_healthy("OK") is True
    assert slo_gate.is_healthy("Alert") is False
    assert slo_gate.is_healthy("Warn") is False
    assert slo_gate.is_healthy("No Data") is False


def _wire_release_happy(monkeypatch, posted):
    """No-monitor-configured baseline: release-tracer path succeeds,
    checked-in helpers stubbed so only `_gate_verdict` varies per test."""
    monkeypatch.setattr(
        warder, "with_install_token_retry", lambda iid, fn: fn("tok"),
    )
    monkeypatch.setattr(
        warder, "_fetch_commits_since_last_tag",
        lambda token, owner, repo, sha: (("feat: thing",), "v1.0.0"),
    )
    monkeypatch.setattr(warder, "post_check_run", lambda *a, **kw: posted.append(kw) or {})
    monkeypatch.setattr(warder, "record_check_verdict", lambda **kw: posted.append(kw))


def test_gate_no_monitor_configured_is_untouched():
    warder_gate = warder._gate_verdict("o", "r", blocking=False)
    section, override, breached = warder_gate
    assert (section, override, breached) == ("", None, False)


def test_gate_creds_absent_is_untouched(monkeypatch):
    monkeypatch.setattr(warder, "get_warder_slo_map", lambda: {"o/r": 123})
    monkeypatch.setattr(warder, "get_dd_api_key", lambda: "")
    monkeypatch.setattr(warder, "get_dd_app_key", lambda: "")
    assert warder._gate_verdict("o", "r", blocking=True) == ("", None, False)


def test_gate_query_failure_degrades_silently(monkeypatch):
    monkeypatch.setattr(warder, "get_warder_slo_map", lambda: {"o/r": 123})
    monkeypatch.setattr(warder, "get_dd_api_key", lambda: "api")
    monkeypatch.setattr(warder, "get_dd_app_key", lambda: "app")
    monkeypatch.setattr(warder, "query_monitor_state", lambda *a: None)
    section, override, breached = warder._gate_verdict("o", "r", blocking=True)
    assert override is None and breached is False
    assert "could not read" in section


def test_gate_healthy_reads_clear(monkeypatch):
    monkeypatch.setattr(warder, "get_warder_slo_map", lambda: {"o/r": 123})
    monkeypatch.setattr(warder, "get_dd_api_key", lambda: "api")
    monkeypatch.setattr(warder, "get_dd_app_key", lambda: "app")
    monkeypatch.setattr(warder, "query_monitor_state", lambda *a: "OK")
    section, override, breached = warder._gate_verdict("o", "r", blocking=True)
    assert override is None and breached is False
    assert "release clear" in section


def test_gate_breach_advisory_by_default(monkeypatch):
    monkeypatch.setattr(warder, "get_warder_slo_map", lambda: {"o/r": 123})
    monkeypatch.setattr(warder, "get_dd_api_key", lambda: "api")
    monkeypatch.setattr(warder, "get_dd_app_key", lambda: "app")
    monkeypatch.setattr(warder, "query_monitor_state", lambda *a: "Alert")
    section, override, breached = warder._gate_verdict("o", "r", blocking=False)
    assert override is None  # advisory: never overrides the tracer's own conclusion
    assert breached is True  # but IS recorded as a breach
    assert "advisory-blocks" in section


def test_gate_breach_blocking_overrides_to_failure(monkeypatch):
    monkeypatch.setattr(warder, "get_warder_slo_map", lambda: {"o/r": 123})
    monkeypatch.setattr(warder, "get_dd_api_key", lambda: "api")
    monkeypatch.setattr(warder, "get_dd_app_key", lambda: "app")
    monkeypatch.setattr(warder, "query_monitor_state", lambda *a: "Alert")
    section, override, breached = warder._gate_verdict("o", "r", blocking=True)
    assert override == "failure" and breached is True
    assert "BLOCKS" in section


def test_release_gate_healthy_keeps_neutral_pass(monkeypatch):
    posted: list = []
    _wire_release_happy(monkeypatch, posted)
    monkeypatch.setattr(warder, "_gate_verdict", lambda o, r, *, blocking: (
        "\n\n---\nDeploy gate: DD monitor `1` is **OK** - release clear.", None, False,
    ))
    out = warder.dispatch_warder_release(
        installation_id=1, owner="o", repo_name="r", head_sha="s", pr_number=2,
        gate_blocking=False,
    )
    assert out == {"persona": "warder", "result": "pass"}
    verdict = next(p for p in posted if p.get("persona_key") == "warder")
    assert verdict["conclusion"] == "neutral" and verdict["findings_count"] == 0


def test_release_gate_breach_advisory_marks_warn_not_pass(monkeypatch):
    """The tracer must not read as a clean 'pass' when its own gate found
    a breach, even though it isn't configured to block (ADR-0003)."""
    posted: list = []
    _wire_release_happy(monkeypatch, posted)
    monkeypatch.setattr(warder, "_gate_verdict", lambda o, r, *, blocking: (
        "\n\n---\nDeploy gate: DD monitor `1` is **Alert** (in breach) - "
        "Warder advisory-blocks (warn only) the release.", None, True,
    ))
    out = warder.dispatch_warder_release(
        installation_id=1, owner="o", repo_name="r", head_sha="s", pr_number=2,
        gate_blocking=False,
    )
    assert out == {"persona": "warder", "result": "pass"}  # never blocks - advisory
    verdict = next(p for p in posted if p.get("persona_key") == "warder")
    assert verdict["conclusion"] == "neutral"  # tracer's own conclusion stands
    assert verdict["findings_count"] == 1  # but NOT a silent clean pass
    assert verdict["blocking"] is False


def test_release_gate_breach_blocking_fails_the_check_run(monkeypatch):
    posted: list = []
    _wire_release_happy(monkeypatch, posted)
    monkeypatch.setattr(warder, "_gate_verdict", lambda o, r, *, blocking: (
        "\n\n---\nDeploy gate: DD monitor `1` is **Alert** (in breach) - "
        "Warder BLOCKS the release.", "failure", True,
    ))
    out = warder.dispatch_warder_release(
        installation_id=1, owner="o", repo_name="r", head_sha="s", pr_number=2,
        gate_blocking=True,
    )
    assert out == {"persona": "warder", "result": "fail"}
    verdict = next(p for p in posted if p.get("persona_key") == "warder")
    assert verdict["conclusion"] == "failure"
    assert verdict["blocking"] is True
    assert verdict["findings_count"] == 1
    assert "deploy gate FAILED" in verdict["summary"]  # summary=title for the Activity row


def test_webhook_dispatch_passes_ctx_blocking_as_gate_blocking(monkeypatch):
    from personas.warder import webhook_dispatch as wd

    seen = {}
    monkeypatch.setattr(
        "personas.warder.dispatch.dispatch_warder_release",
        lambda **kw: seen.update(kw) or {"persona": "warder", "result": "pass"},
    )
    ctx = _warder_ctx({
        "pull_request": {"merged": True, "base": {"ref": "main"}, "merge_commit_sha": "m"},
        "repository": {"default_branch": "main"},
    }, blocking=True)
    wd.dispatch_pull_request(ctx)
    assert seen["gate_blocking"] is True


# ── Pulse ─────────────────────────────────────────────────────────────


def _pr(number, updated_at, sha="s"):
    return {"number": number, "updated_at": updated_at, "head": {"sha": sha}}


def test_pulse_nudges_stale_green_pr_once(monkeypatch):
    stale = _pr(1, "2020-01-01T00:00:00Z")
    monkeypatch.setattr(pulse, "_recent_nudge_exists", lambda t, o, r, p: False)
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
    monkeypatch.setattr(pulse, "_recent_nudge_exists", lambda t, o, r, p: False)
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


def _wire_pulse_happy(monkeypatch, released):
    monkeypatch.setattr(pulse, "get_repo_config", lambda i, r: {"pulse_enabled": True})
    monkeypatch.setattr(pulse, "_stale_prs", lambda t, o, r: [_pr(1, "2020-01-01T00:00:00Z")])
    monkeypatch.setattr(pulse, "_dor_green", lambda t, o, r, s: True)
    monkeypatch.setattr(pulse, "_recent_nudge_exists", lambda t, o, r, p: False)
    monkeypatch.setattr(pulse, "claim_pulse_nudge", lambda i, repo, pr: True)
    monkeypatch.setattr(
        pulse, "release_pulse_nudge",
        lambda i, repo, pr: released.append((repo, pr)),
    )


def test_pulse_definite_post_failure_releases_claim(monkeypatch):
    """Codex PR #489 r2: a DEFINITE no-write (4xx) releases the claim so
    the next cron tick retries."""
    released: list = []
    _wire_pulse_happy(monkeypatch, released)
    resp404 = httpx.Response(404, request=httpx.Request("POST", "https://x"))
    monkeypatch.setattr(
        pulse.httpx, "post",
        lambda url, **kw: (_ for _ in ()).throw(
            httpx.HTTPStatusError("404", request=resp404.request, response=resp404)
        ),
    )
    assert pulse.run_pulse_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}]) == 0
    assert released == [("o/r", 1)]


def test_pulse_ambiguous_failure_keeps_claim_no_spam(monkeypatch):
    """Codex PR #489 r2: a transport failure is AMBIGUOUS (the write may
    have landed) - keep the claim; a missed nudge beats duplicate spam."""
    released: list = []
    _wire_pulse_happy(monkeypatch, released)
    monkeypatch.setattr(
        pulse.httpx, "post",
        lambda url, **kw: (_ for _ in ()).throw(
            httpx.ConnectTimeout("gh down", request=None)
        ),
    )
    assert pulse.run_pulse_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}]) == 0
    assert released == []


def test_pulse_marker_precheck_skips_existing_nudge(monkeypatch):
    """Codex PR #489 r2: write-verification - a marker comment already
    inside the window (a prior ambiguous failure that actually landed)
    means SKIP, never double-post."""
    released: list = []
    _wire_pulse_happy(monkeypatch, released)
    monkeypatch.setattr(pulse, "_recent_nudge_exists", lambda t, o, r, p: True)
    monkeypatch.setattr(
        pulse.httpx, "post",
        lambda url, **kw: (_ for _ in ()).throw(AssertionError("no POST expected")),
    )
    assert pulse.run_pulse_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}]) == 0


def test_stale_prs_paginates_past_ineligible_prefix(monkeypatch):
    """Codex PR #489 r4: a full first page of stale PRs must not end the
    scan - page 2's stale PRs are still collected; the first NON-stale
    PR ends it (updated-asc makes staleness a prefix property)."""
    pages = {
        1: [_pr(i, "2020-01-01T00:00:00Z") for i in range(30)],
        2: [_pr(30, "2020-06-01T00:00:00Z"), _pr(31, "2099-01-01T00:00:00Z")],
    }

    def fake_get(url, params=None, **kw):
        body = pages.get((params or {}).get("page", 1), [])
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(pulse.httpx, "get", fake_get)
    out = pulse._stale_prs("tok", "o", "r")
    assert len(out) == 31           # 30 from page 1 + 1 stale from page 2
    assert all(p_["number"] != 31 for p_ in out)  # fresh PR excluded, scan ended
