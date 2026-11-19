"""Tests for the dependency-aware readiness check (#404).

The module is the deep unit behind /readyz: it probes the critical deps
(SSM/KMS + Postgres), TTL-caches the result, and FAILS CLOSED. Tests mock
the per-dep check functions so no real AWS/Postgres is needed.
"""

from __future__ import annotations

import pytest

import readiness
from readiness import ReadinessReport, check_readiness


@pytest.fixture(autouse=True)
def _reset_cache():
    readiness._reset_cache()
    yield
    readiness._reset_cache()


def _ok() -> None:
    return None


def _boom() -> None:
    raise RuntimeError("dependency down")


def test_all_deps_reachable_is_ready(monkeypatch):
    monkeypatch.setattr(readiness, "_check_ssm_auth", _ok)
    monkeypatch.setattr(readiness, "_check_postgres", _ok)
    rep = check_readiness()
    assert isinstance(rep, ReadinessReport)
    assert rep.ready is True
    assert rep.deps == {"ssm_auth": True, "postgres": True}


def test_ssm_unreachable_not_ready(monkeypatch):
    monkeypatch.setattr(readiness, "_check_ssm_auth", _boom)
    monkeypatch.setattr(readiness, "_check_postgres", _ok)
    rep = check_readiness()
    assert rep.ready is False
    assert rep.deps["ssm_auth"] is False
    assert rep.deps["postgres"] is True


def test_postgres_unreachable_not_ready(monkeypatch):
    monkeypatch.setattr(readiness, "_check_ssm_auth", _ok)
    monkeypatch.setattr(readiness, "_check_postgres", _boom)
    rep = check_readiness()
    assert rep.ready is False
    assert rep.deps["postgres"] is False


def test_fails_closed_on_unexpected_error(monkeypatch):
    def weird() -> None:
        raise KeyError("unexpected")
    monkeypatch.setattr(readiness, "_check_ssm_auth", weird)
    monkeypatch.setattr(readiness, "_check_postgres", _ok)
    rep = check_readiness()  # must NOT raise; must read as not-ready
    assert rep.ready is False


def test_ttl_cache_avoids_rechecking_within_window(monkeypatch):
    calls = {"n": 0}
    def counting() -> None:
        calls["n"] += 1
    monkeypatch.setattr(readiness, "_check_ssm_auth", counting)
    monkeypatch.setattr(readiness, "_check_postgres", _ok)
    clock = {"t": 1000.0}
    now = lambda: clock["t"]  # noqa: E731
    r1 = check_readiness(now=now)
    r2 = check_readiness(now=now)  # within TTL -> served from cache
    assert calls["n"] == 1
    assert r1 is r2
    clock["t"] += readiness._TTL_SECONDS + 1.0  # past TTL -> re-check
    check_readiness(now=now)
    assert calls["n"] == 2


# --- /readyz handler wiring (503 vs 200) -----------------------------------

def test_readyz_handler_503_when_not_ready(monkeypatch):
    import readiness
    monkeypatch.setattr(
        readiness, "check_readiness",
        lambda: ReadinessReport(ready=False, deps={"ssm_auth": False, "postgres": True}),
    )
    from fastapi.testclient import TestClient
    from main import app
    r = TestClient(app).get("/readyz")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not_ready"
    assert body["deps"]["ssm_auth"] is False


def test_readyz_handler_200_when_ready(monkeypatch):
    import readiness
    monkeypatch.setattr(
        readiness, "check_readiness",
        lambda: ReadinessReport(ready=True, deps={"ssm_auth": True, "postgres": True}),
    )
    from fastapi.testclient import TestClient
    from main import app
    r = TestClient(app).get("/readyz")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"


# --- fail-FAST contract (audit HIGH fixes): probes must not exceed the kubelet
#     timeout and drop the only replica on a slow dep / busy pool -------------

def test_ssm_probe_client_has_tight_timeouts_and_minimal_retries():
    cfg = readiness._ssm.meta.config
    assert cfg.connect_timeout == 1
    assert cfg.read_timeout == 2
    # Minimal retries (boto3 stores max_attempts:1 as total_max_attempts:2),
    # vs the botocore default of several - so the probe fails fast, not in ~60s.
    assert cfg.retries["total_max_attempts"] <= 2


def test_postgres_probe_uses_a_bounded_separate_connection(monkeypatch):
    captured = {}

    class _FakeConn:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def execute(self, q):
            captured["q"] = q

    def fake_connect(url, **kw):
        captured["url"] = url
        captured["kw"] = kw
        return _FakeConn()

    monkeypatch.setenv("GRUG_DATABASE_URL", "postgresql://unit/test")
    monkeypatch.setattr(readiness.psycopg, "connect", fake_connect)
    readiness._check_postgres()
    # bounded connect_timeout (not the pool's 30s), and a SEPARATE connection
    # (psycopg.connect) rather than the request pool, so pool-busy != not-ready
    assert captured["kw"].get("connect_timeout") == readiness._PG_CONNECT_TIMEOUT_S
    assert captured["q"] == "SELECT 1"


def test_readiness_probe_never_decrypts(monkeypatch):
    """infra#2431: the KMS free tier is 20,000 requests a MONTH. This probe runs
    on every kubelet tick on every pod, so decrypting here cost ~11x the entire
    monthly allowance and no cache TTL could fit inside it. If anyone restores
    WithDecryption=True, the bill silently leaves the free tier again - so pin
    the flag rather than trusting the comment."""
    seen = {}

    class _FakeSSM:
        def get_parameter(self, **kw):
            seen.update(kw)
            return {"Parameter": {"Value": "x"}}

    monkeypatch.setenv("GRUG_READYZ_SSM_PROBE", "/grug/github-app-id")
    monkeypatch.setattr(readiness, "_ssm", _FakeSSM())
    readiness._check_ssm_auth()

    assert seen["WithDecryption"] is False, "readiness probe must not bill a KMS request"
    assert seen["Name"] == "/grug/github-app-id"


def test_sandbox_state_is_advisory_and_never_gates_readiness(monkeypatch):
    from personas.code_reviewer import analyzer_sandbox as sb
    monkeypatch.setattr(readiness, "_check_ssm_auth", _ok)
    monkeypatch.setattr(readiness, "_check_postgres", _ok)
    monkeypatch.setattr(
        sb, "sandbox_state",
        lambda: sb.SandboxState(True, False, "landlock_create_ruleset: Operation not supported"),
    )
    rep = check_readiness()
    assert rep.ready is True
    assert rep.advisory["analyzer_sandbox"]["available"] is False
