# Shared pytest fixtures for services/{api,webhook}/tests (ADR-0014).
# Imported (not auto-discovered) by each service's conftest.py shim —
# pytest conftest discovery is directory-bound, so the per-service
# conftest.py files stay as thin sys.path bootstraps that pull the
# fixture logic from this single copy.

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _cf_auth_bringup_mode(monkeypatch):
    """Default app-level tests to CF-auth bring-up mode (fail-open).

    CfAuthMiddleware now fail-CLOSES by default (audit #4) when the CF
    shared secret is unconfigured, so any test that drives the full app via
    TestClient without configuring the secret would 503 at the boundary even
    though it is not testing the boundary. This conftest autouse fixture
    runs BEFORE module-level autouse fixtures, so the cf_auth suites - which
    delete this flag in their own autouse fixture - still exercise the real
    fail-closed default.
    """
    monkeypatch.setenv("GRUG_CF_AUTH_FAIL_OPEN", "1")
