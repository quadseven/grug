"""pytest config for services/{api,webhook}/ tests.

Adds the parent directory to sys.path so tests can `from hmac_verify
import ...` without a package install (handler files live alongside the
tests folder, not under a package).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


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
