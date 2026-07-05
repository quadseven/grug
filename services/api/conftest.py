"""pytest bootstrap for this service's tests (ADR-0014).

Adds the service dir + services/_shared/ to sys.path so tests can
`from cf_auth import ...` / `from adapters.install_store import ...`
without a package install, then loads the SHARED fixture module (fixture
logic lives once, in services/_shared/grug_shared_conftest.py — conftest
discovery is directory-bound, so this shim is per-service by necessity).
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
sys.path.insert(1, str(_HERE.parent / "_shared"))

# AWS_CONFIG_FILE import-time scrub lives in grug_shared_conftest (imported
# below) - see its header.

from grug_shared_conftest import _cf_auth_bringup_mode  # noqa: E402,F401
