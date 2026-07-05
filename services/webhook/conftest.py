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

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
sys.path.insert(1, str(_HERE.parent / "_shared"))

# Import-time hermeticity (#389 audit): main.py runs the Roles Anywhere
# boot proof AT IMPORT, i.e. at pytest collection - BEFORE any fixture can
# scrub the env. A dev shell exporting AWS_CONFIG_FILE would make
# collection do a live STS call (or hard-fail with ambient creds). Only a
# conftest, which imports first, can protect this call site. Proof tests
# setenv explicitly and are unaffected.
import os  # noqa: E402

os.environ.pop("AWS_CONFIG_FILE", None)

from grug_shared_conftest import _cf_auth_bringup_mode  # noqa: E402,F401
