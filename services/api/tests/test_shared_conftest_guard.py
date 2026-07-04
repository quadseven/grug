"""Api-side twin of the webhook conftest fixture-coverage guard (#77).

The per-service conftest shims import shared fixtures BY NAME. A
non-autouse fixture forgotten in a shim fails loud ("fixture not
found"), but a forgotten AUTOUSE fixture is silently inert for that
service's whole suite - this guard makes it red instead. The webhook
twin lives in tests/test_shared_no_shadowing.py.
"""

from __future__ import annotations


def test_conftest_shim_exposes_every_shared_fixture():
    import conftest
    import grug_shared_conftest

    missing = [
        name
        for name, obj in vars(grug_shared_conftest).items()
        if hasattr(obj, "_pytestfixturefunction") and name not in vars(conftest)
    ]
    assert not missing, (
        f"shared fixtures not imported by the conftest shim: {missing}"
    )
