"""pytest bootstrap for this service's tests (ADR-0014).

Adds the service dir + services/_shared/ to sys.path so tests can
`from hmac_verify import ...` / `from adapters.install_store import ...`
without a package install, then loads the SHARED fixture module (fixture
logic lives once, in services/_shared/grug_shared_conftest.py — conftest
discovery is directory-bound, so this shim is per-service by necessity).
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
sys.path.insert(1, str(_HERE.parent / "_shared"))

# AWS_CONFIG_FILE import-time scrub lives in grug_shared_conftest (imported
# below) - see its header.

from grug_shared_conftest import _cf_auth_bringup_mode  # noqa: E402,F401


import pytest  # noqa: E402


class _OfflineHttpx:
    """Stands in for `httpx` inside `comment_reactions`: every call fails
    the way a dead network does, so the best-effort reaction code logs and
    moves on instead of reaching GitHub."""

    HTTPError = httpx.HTTPError

    def post(self, *args, **kwargs):
        raise httpx.ConnectError("offline in tests")

    delete = post


@pytest.fixture(autouse=True)
def _no_live_comment_reactions(monkeypatch):
    """A command-comment reaction is a real GitHub call (#695). Tests that
    do not exercise it must never reach the network; the ones that do
    patch `comment_reactions.httpx` themselves."""
    import comment_reactions

    monkeypatch.setattr(comment_reactions, "httpx", _OfflineHttpx())
