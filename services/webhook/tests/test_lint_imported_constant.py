"""S608 over an f-string of a constant imported from another module (#1148)."""
from __future__ import annotations

import json
from unittest.mock import patch

from personas.code_reviewer.lint import scan_ruff
from tests.test_lint import _FAKE_DIR, _hunk, _proc, _s_result

STORE = "services/_shared/adapters/store.py"
BASE = "services/_shared/adapters/pg_base.py"
USER = ("from adapters.pg_base import TTL_LIVE\n"
        "def resume(cur):\n"
        "    query = f'SELECT * FROM t WHERE {TTL_LIVE}'\n"
        "    cur.execute(query)\n")


def _scan(files: dict[str, str]):
    result = _s_result(STORE, "S608")
    result["location"]["row"] = 3
    body = "".join(f"+{line}\n" for line in files[STORE].splitlines())
    patch_text = f"@@ -0,0 +1,{len(files[STORE].splitlines())} @@\n{body}"
    with patch("subprocess.run", return_value=_proc(1, json.dumps([result]))), \
         patch("tempfile.TemporaryDirectory") as td:
        td.return_value.__enter__.return_value = _FAKE_DIR
        return scan_ruff((_hunk(STORE, patch_text),), files, enabled=True)


def test_imported_literal_constant_is_medium():
    found = _scan({STORE: USER, BASE: "TTL_LIVE = 'expires_at > now()'\n"})
    assert len(found) == 1
    assert found[0].severity == "medium"


def test_missing_module_source_stays_high():
    assert _scan({STORE: USER})[0].severity == "high"


def test_imported_name_assigned_from_call_stays_high():
    found = _scan({STORE: USER, BASE: "TTL_LIVE = pick()\n"})
    assert found[0].severity == "high"
