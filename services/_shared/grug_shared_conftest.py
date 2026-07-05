# Shared pytest fixtures for services/{api,webhook}/tests (ADR-0014).
# Imported (not auto-discovered) by each service's conftest.py shim —
# pytest conftest discovery is directory-bound, so the per-service
# conftest.py files stay as thin sys.path bootstraps that pull the
# fixture logic from this single copy.

from __future__ import annotations

import os

import pytest

# Import-time hermeticity (#389 audit): each service's main.py runs the
# Roles Anywhere boot proof AT IMPORT, i.e. at pytest collection - before
# any fixture executes. Only conftest-import-time code can protect that
# call site from a dev shell exporting AWS_CONFIG_FILE. Lives here (not
# in the per-service shims) so a future service gets it for free; the
# proof tests setenv explicitly and are unaffected.
os.environ.pop("AWS_CONFIG_FILE", None)


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
