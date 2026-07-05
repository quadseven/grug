"""Tests for poller_handler.handler — the scheduled reaction-poll Lambda
entry point (#247b). Mocks install_store / auth / reactions; no DDB or
network. Webhook-only (the poller ships in the webhook image)."""
from __future__ import annotations

import poller_handler
import pytest


@pytest.fixture(autouse=True)
def _no_ambient_ra_config(monkeypatch):
    """Hermeticity (audit #388 stage-7, VERIFIED failing): a developer
    shell with AWS_CONFIG_FILE + ambient AWS_* creds sent every handler()
    test into the identity proof's tripwire. CI never caught it (hosted
    runners lack the env). The proof tests setenv explicitly, so they are
    unaffected by this delenv."""
    monkeypatch.delenv("AWS_CONFIG_FILE", raising=False)



def _wire(monkeypatch, *, installs, records_for, retry, poll):
    monkeypatch.setattr(poller_handler, "list_allowlisted_installs", lambda: installs)
    monkeypatch.setattr(poller_handler, "list_comment_records", records_for)
    monkeypatch.setattr(poller_handler, "with_install_token_retry", retry)
    monkeypatch.setattr(poller_handler, "poll_and_annotate", poll)
    # #407: stub the auto-replay to a no-op so reaction-poll tests don't hit
    # GitHub and their exact-result assertions stay about the reaction poll.
    monkeypatch.setattr(poller_handler, "_replay_missed_deliveries", lambda: {})
    # #472: default the Pulse pass to idle (no enabled repos) so the
    # reaction-poll assertions stay about the reaction poll.
    monkeypatch.setattr(
        "adapters.install_store.list_pulse_enabled_repos", lambda iid: [],
    )
    monkeypatch.setattr(
        "adapters.install_store.list_dep_watch_repos", lambda iid: [],
    )
    # #460: default the enforcement re-emission pass to idle (GitHub
    # reports no repos) so the reaction-poll assertions stay about the
    # reaction poll. NOTE the pass still acquires one token per install
    # (the repo listing itself is a GitHub call).
    monkeypatch.setattr(
        "github_rulesets_client.list_installation_repos", lambda token: [],
    )


def test_poller_polls_each_allowlisted_install(monkeypatch):
    """One poll_and_annotate per allowlisted install with records; the
    token thunk resolves to the retry-supplied token; summary sums verdicts."""
    polled = []

    def _poll(records, *, install_id, fetch_token):
        polled.append(install_id)
        assert fetch_token() == "tok"   # thunk yields the retry's token
        return 2
    _wire(
        monkeypatch,
        installs=[11, 22],
        records_for=lambda iid: [{"comment_id": iid}],
        retry=lambda iid, fn: fn("tok"),
        poll=_poll,
    )
    out = poller_handler.handler({}, None)
    assert polled == [11, 22]
    assert out == {"installs": 2, "records": 2, "submitted": 4, "failed_installs": 0, "pulse_nudges": 0, "pulse_failed_installs": 0, "dep_watch_reports": 0, "dep_watch_failed_installs": 0, "enforcement_emitted": 0, "enforcement_failed_installs": 0}


def test_poller_one_install_failure_does_not_abort_cycle(monkeypatch, caplog):
    """A single install's token/GH failure is logged + counted, and the
    cycle continues to the next install (best-effort per install). A PARTIAL
    failure must NOT escalate to error (else a status:error monitor false-
    fires every time one of many installs hiccups)."""
    import logging as _logging

    def _retry(iid, fn):
        if iid == 1:
            raise RuntimeError("install 1 token fetch failed")
        return fn("tok")
    _wire(
        monkeypatch,
        installs=[1, 2],
        records_for=lambda iid: [{"comment_id": iid}],
        retry=_retry,
        poll=lambda records, *, install_id, fetch_token: 3,
    )
    with caplog.at_level(_logging.INFO):
        out = poller_handler.handler({}, None)
    assert out["installs"] == 2
    assert out["failed_installs"] == 1
    assert out["submitted"] == 3   # install 2 still polled despite install 1 failing
    assert out["records"] == 2     # both installs' records counted as attempted
    # partial failure → cycle-complete at INFO, NOT the all-failed error.
    cycle = [r for r in caplog.records if r.msg == "reaction_poll_cycle_complete"]
    assert cycle and cycle[0].levelno == _logging.INFO
    assert not any(r.levelno >= _logging.ERROR for r in caplog.records)


def test_poller_records_listing_failure_is_best_effort(monkeypatch):
    """A CommentRecord LISTING failure (DDB error) for one install must be
    caught too — it's inside the per-install try — so the cron counts it
    failed and continues to the next install (codex BLOCK regression)."""
    def _records(iid):
        if iid == 1:
            raise RuntimeError("DDB list failure")
        return [{"comment_id": iid}]
    _wire(
        monkeypatch,
        installs=[1, 2],
        records_for=_records,
        retry=lambda iid, fn: fn("tok"),
        poll=lambda records, *, install_id, fetch_token: 5,
    )
    out = poller_handler.handler({}, None)
    assert out["installs"] == 2
    assert out["failed_installs"] == 1   # install 1 listing failed
    assert out["submitted"] == 5         # install 2 still polled


def test_poller_skips_installs_with_no_records(monkeypatch):
    """An install with no CommentRecords skips the REACTIONS poll, and
    with no pulse-enabled repos (store-driven targeting, #472/PR #489)
    the Pulse pass costs no token either. The #460 enforcement pass DOES
    acquire one token per install by design - its GitHub repo listing is
    the denominator - so exactly one retry call remains."""
    touched = []
    monkeypatch.setattr(
        "adapters.install_store.list_pulse_enabled_repos", lambda iid: [],
    )
    monkeypatch.setattr(
        "adapters.install_store.list_dep_watch_repos", lambda iid: [],
    )
    _wire(
        monkeypatch,
        installs=[7],
        records_for=lambda iid: [],
        retry=lambda iid, fn: touched.append(iid),
        poll=lambda *a, **k: 0,
    )
    out = poller_handler.handler({}, None)
    assert touched == [7]   # the enforcement pass's single token acquisition
    assert out == {"installs": 1, "records": 0, "submitted": 0, "failed_installs": 0, "pulse_nudges": 0, "pulse_failed_installs": 0, "dep_watch_reports": 0, "dep_watch_failed_installs": 0, "enforcement_emitted": 0, "enforcement_failed_installs": 0}


# --- #407: auto-replay wiring -----------------------------------------------


def test_replay_missed_deliveries_maps_report(monkeypatch):
    """_replay_missed_deliveries calls delivery_replay.replay_since (with a
    window-derived since) and maps the report into replay_* summary keys."""
    import delivery_replay

    captured = {}

    def _fake(since):
        captured["since"] = since
        return delivery_replay.ReplayReport(
            scanned=5, failed_guids=2, redelivered=2, errors=0
        )

    monkeypatch.setattr(poller_handler.delivery_replay, "replay_since", _fake)
    out = poller_handler._replay_missed_deliveries()
    assert out == {
        "replay_scanned": 5,
        "replay_failed_guids": 2,
        "replay_redelivered": 2,
        "replay_errors": 0,
    }
    assert captured["since"].endswith("Z")  # an ISO-8601 UTC instant was passed


def test_handler_merges_replay_counts(monkeypatch):
    """The cron summary carries the replay counts so an operator/DD sees that
    auto-recovery ran each tick."""
    _wire(
        monkeypatch,
        installs=[],
        records_for=lambda iid: [],
        retry=lambda iid, fn: 0,
        poll=lambda *a, **k: 0,
    )
    monkeypatch.setattr(
        poller_handler, "_replay_missed_deliveries",
        lambda: {"replay_scanned": 9, "replay_redelivered": 3, "replay_errors": 0},
    )
    out = poller_handler.handler({}, None)
    assert out["replay_redelivered"] == 3
    assert out["replay_scanned"] == 9


def test_handler_replay_failure_does_not_abort_cron(monkeypatch, caplog):
    """A replay blow-up (GitHub down, JWT error) must NOT abort the reaction
    poll - it's logged and surfaced as replay_error, results otherwise intact."""
    import logging as _logging

    _wire(
        monkeypatch,
        installs=[1],
        records_for=lambda iid: [{"comment_id": iid}],
        retry=lambda iid, fn: fn("tok"),
        poll=lambda records, *, install_id, fetch_token: 1,
    )

    def _boom():
        raise RuntimeError("github down")

    monkeypatch.setattr(poller_handler, "_replay_missed_deliveries", _boom)
    with caplog.at_level(_logging.WARNING):
        out = poller_handler.handler({}, None)
    assert out["submitted"] == 1  # reaction poll still completed
    assert out["replay_error"] == "RuntimeError"
    assert any(r.msg == "delivery_replay_failed" for r in caplog.records)


def test_poller_all_installs_fail_logs_error(monkeypatch, caplog):
    """A SYSTEMIC failure (every install errors — auth/config drift) must
    escalate to log.error, not hide as info — else a status:error monitor
    never fires and it looks like a healthy idle cycle."""
    import logging as _logging

    def _retry(iid, fn):
        raise RuntimeError("systemic token failure")
    _wire(
        monkeypatch,
        installs=[1, 2],
        records_for=lambda iid: [{"comment_id": iid}],
        retry=_retry,
        poll=lambda *a, **k: 0,
    )
    with caplog.at_level(_logging.WARNING):
        out = poller_handler.handler({}, None)
    assert out == {"installs": 2, "records": 2, "submitted": 0, "failed_installs": 2, "pulse_nudges": 0, "pulse_failed_installs": 0, "dep_watch_reports": 0, "dep_watch_failed_installs": 0, "enforcement_emitted": 0, "enforcement_failed_installs": 2}
    errs = [r for r in caplog.records if r.msg == "reaction_poll_all_installs_failed"]
    assert errs and errs[0].levelno == _logging.ERROR
    # a partial failure (not ALL) must NOT escalate to error
    assert not any(r.msg == "reaction_poll_cycle_complete" and r.levelno >= _logging.ERROR
                   for r in caplog.records)


def test_poller_no_installs_is_a_clean_noop(monkeypatch):
    _wire(
        monkeypatch,
        installs=[],
        records_for=lambda iid: [{"x": 1}],
        retry=lambda iid, fn: fn("tok"),
        poll=lambda *a, **k: 1,
    )
    out = poller_handler.handler({}, None)
    assert out == {"installs": 0, "records": 0, "submitted": 0, "failed_installs": 0, "pulse_nudges": 0, "pulse_failed_installs": 0, "dep_watch_reports": 0, "dep_watch_failed_installs": 0, "enforcement_emitted": 0, "enforcement_failed_installs": 0}


# --- #460: enforcement-gauge re-emission pass --------------------------------


def _wire_enforcement(monkeypatch, *, installs, gh_repos, detect, config=None):
    """Wire an idle reactions/pulse/dep-watch cycle with a live enforcement
    pass. `gh_repos` is what GitHub's /installation/repositories returns;
    `config` maps (install_id, repo_id) -> repo-config dict (default {} =
    all defaults, i.e. tpm enabled)."""
    _wire(
        monkeypatch,
        installs=installs,
        records_for=lambda iid: [],
        retry=lambda iid, fn: fn("tok"),
        poll=lambda *a, **k: 0,
    )
    monkeypatch.setattr(
        "github_rulesets_client.list_installation_repos", lambda token: gh_repos,
    )
    monkeypatch.setattr(
        "adapters.install_store.get_repo_config",
        lambda iid, rid: (config or {}).get((iid, rid), {}),
    )
    monkeypatch.setattr("github_rulesets_client.detect_enforcement", detect)


def test_enforcement_pass_emits_live_state_per_github_repo(monkeypatch):
    """#460 v2: the denominator is GitHub's /installation/repositories (a
    defaults-only install has ZERO store rows - verified live), each repo
    emits a LIVE-detected gauge using the listing's default_branch."""
    emitted = []
    detected = []

    def _detect(token, owner, repo, branch, check_name):
        detected.append((owner, repo, branch, check_name))
        return "grug_managed" if repo == "a" else "none"

    _wire_enforcement(
        monkeypatch,
        installs=[1],
        gh_repos=[
            {"id": 10, "full_name": "o/a", "default_branch": "trunk"},
            {"id": 11, "full_name": "o/b", "default_branch": "main"},
        ],
        detect=_detect,
    )
    monkeypatch.setattr(
        "observability.emit_enforcement_metric",
        lambda full, state, **kw: emitted.append((full, state)),
    )
    out = poller_handler.handler({}, None)
    assert emitted == [("o/a", "grug_managed"), ("o/b", "none")]
    from enforcement import GRUG_DOR_CHECK_NAME
    assert detected == [
        ("o", "a", "trunk", GRUG_DOR_CHECK_NAME),
        ("o", "b", "main", GRUG_DOR_CHECK_NAME),
    ]
    assert out["enforcement_emitted"] == 2
    assert out["enforcement_failed_installs"] == 0


def test_enforcement_pass_honors_store_opt_out(monkeypatch):
    """A stored tpm_enabled=false is the per-repo opt-OUT overlay: the repo
    is skipped (not emitted, not detected); missing rows default enabled."""
    emitted = []
    detected = []

    def _detect(token, owner, repo, branch, check_name):
        detected.append(repo)
        return "grug_managed"

    _wire_enforcement(
        monkeypatch,
        installs=[1],
        gh_repos=[
            {"id": 10, "full_name": "o/on", "default_branch": "main"},
            {"id": 11, "full_name": "o/off", "default_branch": "main"},
        ],
        detect=_detect,
        config={(1, 11): {"tpm_enabled": False}},
    )
    monkeypatch.setattr(
        "observability.emit_enforcement_metric",
        lambda full, state, **kw: emitted.append((full, state)),
    )
    out = poller_handler.handler({}, None)
    assert emitted == [("o/on", "grug_managed")]
    assert detected == ["on"]
    assert out["enforcement_emitted"] == 1


def test_enforcement_pass_one_repo_failure_does_not_starve_the_rest(monkeypatch, caplog):
    """Per-REPO best-effort: one repo's GitHub error is logged and the
    install's remaining repos still get their gauge (no install-level
    failure count either - the token was fine)."""
    import logging as _logging

    emitted = []

    def _detect(token, owner, repo, branch, check_name):
        if repo == "bad":
            raise RuntimeError("GH 500")
        return "external"

    _wire_enforcement(
        monkeypatch,
        installs=[1],
        gh_repos=[
            {"id": 1, "full_name": "o/bad", "default_branch": "main"},
            {"id": 2, "full_name": "o/good", "default_branch": "main"},
        ],
        detect=_detect,
    )
    monkeypatch.setattr(
        "observability.emit_enforcement_metric",
        lambda full, state, **kw: emitted.append((full, state)),
    )
    with caplog.at_level(_logging.WARNING):
        out = poller_handler.handler({}, None)
    assert emitted == [("o/good", "external")]
    assert out["enforcement_emitted"] == 1
    assert out["enforcement_failed_installs"] == 0
    assert any(r.msg == "enforcement_emit_repo_failed" for r in caplog.records)


def test_enforcement_pass_one_install_failure_does_not_abort_cycle(monkeypatch, caplog):
    """Per-INSTALL best-effort: a listing/token failure for one install is
    counted and the next install still emits."""
    import logging as _logging

    emitted = []

    def _list(token):
        # First install's listing blows up; the second's succeeds. The
        # closure distinguishes installs via the wired config below.
        if _list.calls == 0:
            _list.calls += 1
            raise RuntimeError("GitHub listing down")
        return [{"id": 5, "full_name": "o/ok", "default_branch": "main"}]
    _list.calls = 0

    _wire_enforcement(
        monkeypatch,
        installs=[1, 2],
        gh_repos=[],
        detect=lambda *a, **k: "grug_managed",
    )
    monkeypatch.setattr("github_rulesets_client.list_installation_repos", _list)
    monkeypatch.setattr(
        "observability.emit_enforcement_metric",
        lambda full, state, **kw: emitted.append((full, state)),
    )
    with caplog.at_level(_logging.WARNING):
        out = poller_handler.handler({}, None)
    assert emitted == [("o/ok", "grug_managed")]
    assert out["enforcement_emitted"] == 1
    assert out["enforcement_failed_installs"] == 1
    assert any(r.msg == "enforcement_emit_install_failed" for r in caplog.records)


def test_enforcement_pass_skips_malformed_full_name(monkeypatch):
    """A listing entry without an owner/name full_name is skipped, not
    crashed on."""
    emitted = []
    _wire_enforcement(
        monkeypatch,
        installs=[1],
        gh_repos=[
            {"id": 1, "full_name": "no-slash", "default_branch": "main"},
            {"id": 2, "full_name": "o/r", "default_branch": "main"},
        ],
        detect=lambda *a, **k: "none",
    )
    monkeypatch.setattr(
        "observability.emit_enforcement_metric",
        lambda full, state, **kw: emitted.append((full, state)),
    )
    out = poller_handler.handler({}, None)
    assert emitted == [("o/r", "none")]
    assert out["enforcement_emitted"] == 1


# ── Roles Anywhere identity proof (#388, audit stage-2 CRITICAL) ──────


def test_identity_proof_skips_without_ra_config(monkeypatch):
    monkeypatch.delenv("AWS_CONFIG_FILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "static")  # irrelevant without the marker
    import poller_handler as ph

    ph._prove_roles_anywhere_identity()  # must not raise, must not call AWS


def test_identity_proof_refuses_static_env_creds(monkeypatch, caplog):
    """Env creds out-rank credential_process - their presence means the
    pod would silently bypass the cert path. Refuse loudly. EITHER
    half of the pair triggers (peer review #504: secret-only partial env
    is still config drift worth refusing)."""
    monkeypatch.setenv("AWS_CONFIG_FILE", "/etc/grug-aws/config")
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "static-half")
    import pytest as _pytest

    import poller_handler as ph

    with _pytest.raises(RuntimeError, match="Roles Anywhere"):
        ph._prove_roles_anywhere_identity()
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "static")
    import pytest

    import poller_handler as ph

    import logging

    import aws_identity

    with caplog.at_level(logging.ERROR, logger=aws_identity.log.name):
        with pytest.raises(RuntimeError, match="Roles Anywhere"):
            ph._prove_roles_anywhere_identity()
    assert any(r.msg == "roles_anywhere_identity_failed" for r in caplog.records)


def test_identity_proof_calls_sts_and_propagates_failure(monkeypatch, caplog):
    """The proof is deliberately UNGUARDED: a credential failure must crash
    the Job (KSM monitor pages) instead of dissolving into the per-install
    best-effort swallow."""
    monkeypatch.setenv("AWS_CONFIG_FILE", "/etc/grug-aws/config")
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    import boto3
    import pytest

    import poller_handler as ph

    class _Sts:
        def get_caller_identity(self):
            raise RuntimeError("CredentialRetrievalError: helper exploded")

    monkeypatch.setattr(boto3, "client", lambda service: _Sts())
    import logging

    import aws_identity

    with caplog.at_level(logging.ERROR, logger=aws_identity.log.name):
        with pytest.raises(RuntimeError, match="helper exploded"):
            ph._prove_roles_anywhere_identity()
    # The monitorable event is HALF the credential monitor's trigger -
    # a rename would disarm it while every test stayed green (stage 7).
    assert any(r.msg == "roles_anywhere_identity_failed" for r in caplog.records)


def test_identity_proof_logs_the_assumed_arn(monkeypatch, caplog):
    import logging

    monkeypatch.setenv("AWS_CONFIG_FILE", "/etc/grug-aws/config")
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    import boto3

    import poller_handler as ph

    class _Sts:
        def get_caller_identity(self):
            return {"Arn": "arn:aws:sts::1:assumed-role/ra-grug/x", "Account": "1"}

    monkeypatch.setattr(boto3, "client", lambda service: _Sts())
    # The proof MOVED to aws_identity (#389) - capture ITS logger, not the
    # poller's (the stale target made this test ordering-dependent: audit).
    import aws_identity

    with caplog.at_level(logging.INFO, logger=aws_identity.log.name):
        ph._prove_roles_anywhere_identity()
    (rec,) = [r for r in caplog.records if r.msg == "roles_anywhere_identity_proven"]
    assert "ra-grug" in rec.assumed_arn


def test_handler_runs_identity_proof_before_any_install_work(monkeypatch):
    """The proof's two production duties (fail-loud cred channel, expiry
    canary) hang on ONE call site at the top of handler(); the four unit
    tests all call the proof directly and would stay green if it were
    deleted (audit stage-7 CRITICAL). Ordering matters: the proof must
    fire BEFORE any per-install best-effort swallow can absorb it."""

    def _boom():
        raise RuntimeError("proof ran")

    monkeypatch.setattr(poller_handler, "_prove_roles_anywhere_identity", _boom)
    monkeypatch.setattr(
        poller_handler,
        "list_allowlisted_installs",
        lambda: pytest.fail("install work reached before the proof"),
    )
    with pytest.raises(RuntimeError, match="proof ran"):
        poller_handler.handler({}, None)


def test_identity_proof_rejects_malformed_expected_arn(monkeypatch):
    """An unsubstituted placeholder or mangled ARN must be an actionable
    ValueError (still logged + raised), not a cryptic IndexError."""
    monkeypatch.setenv("AWS_CONFIG_FILE", "/etc/grug-aws/config")
    monkeypatch.setenv("GRUG_RA_ROLE_ARN", "RA_ROLE_ARN_PLACEHOLDER")
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    import boto3

    monkeypatch.setattr(boto3, "client", lambda s: type("S", (), {"get_caller_identity": lambda self: {"Arn": "x"}})())
    import pytest as _pytest

    import poller_handler as ph

    with _pytest.raises(ValueError, match="not an IAM role ARN"):
        ph._prove_roles_anywhere_identity()


def test_identity_proof_asserts_the_expected_role(monkeypatch):
    """Peer review (confirmed 3x): a wrong-but-VALID identity - swapped
    SSM ARN, ambient instance profile - must FAIL the proof, not pass
    observationally."""
    monkeypatch.setenv("AWS_CONFIG_FILE", "/etc/grug-aws/config")
    monkeypatch.setenv("GRUG_RA_ROLE_ARN", "arn:aws:iam::111122223333:role/ra-grug")
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    import boto3

    class _WrongSts:
        def get_caller_identity(self):
            return {"Arn": "arn:aws:sts::999988887777:assumed-role/other-role/x", "Account": "999988887777"}

    monkeypatch.setattr(boto3, "client", lambda service: _WrongSts())
    with pytest.raises(RuntimeError, match="wrong AWS identity"):
        poller_handler._prove_roles_anywhere_identity()

    class _RightSts:
        def get_caller_identity(self):
            return {"Arn": "arn:aws:sts::111122223333:assumed-role/ra-grug/session1", "Account": "111122223333"}

    monkeypatch.setattr(boto3, "client", lambda service: _RightSts())
    poller_handler._prove_roles_anywhere_identity()  # must not raise
