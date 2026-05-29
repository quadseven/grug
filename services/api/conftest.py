"""pytest config for services/{api,webhook}/ tests.

Adds the parent directory to sys.path so tests can `from hmac_verify
import ...` without a package install (handler files live alongside the
tests folder, not under a package).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
