# MIRRORED — sibling at services/webhook/adapters/install_store.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
"""Install store - Postgres-backed since the #354 store swap.

This module is a FACADE re-exporting the Postgres implementation so the
~30 call sites and their patch targets keep their import paths. The
DynamoDB implementation this replaced lives in git history (pre-swap);
its semantics were ported 1:1 (see pg_install_store's docstring and the
real-Postgres parity suite in services/api/tests/test_pg_stores.py).
"""

from adapters.pg_install_store import *  # noqa: F401,F403
