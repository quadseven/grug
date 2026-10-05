"""Throwaway file for a live Elder smoke test; this PR is closed unmerged."""


def retry_delay(attempt):
    """Seconds to wait before retry number `attempt`."""
    return 10 * attempt
