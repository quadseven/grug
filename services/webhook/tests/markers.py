"""Custom pytest markers.

This module defines pytest markers for conditional test execution.
"""

import pytest


def _llm_backend_available() -> bool:
    """Check if a live LLM backend is available for live-verified tests."""
    try:
        from llm_client import call_llm
    except ImportError:
        return False

    try:
        # Attempt a lightweight call to check backend availability
        call_llm(
            system="Test.",
            messages=[{"role": "user", "content": "OK"}],
            model="elder",
        )
        return True
    except Exception:
        return False


def _require_live_llm_backend(request: pytest.FixtureRequest) -> None:
    """Skip test if LLM backend is not available.

    This decorator is used for live-verified tests that require a live
    LLM backend to run (issue #989).

    Usage:
        @pytest.mark.live_verified
        def test_live_feature():
            ...
    """
    if not _llm_backend_available():
        pytest.skip("LLM backend not available (see issue #989)")


def _skip_unless_live_llm_backend(request: pytest.FixtureRequest) -> None:
    """Skip test unless LLM backend is available.

    This is the opposite of _require_live_llm_backend — it marks tests that
    should only run when the backend IS available, not when it's not.

    Usage:
        @pytest.mark.live_verified_only
        def test_backend_specific_feature():
            ...
    """
    if _llm_backend_available():
        pytest.skip("LLM backend required for this test")


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers with pytest."""
    config.addinivalue_line(
        "markers", "live_verified: test that requires live LLM backend (issue #989)"
    )
    config.addinivalue_line(
        "markers", "live_verified_only: test that requires live LLM backend AND ONLY RUNS WITH IT"
    )
