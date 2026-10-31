"""Keep Grug under its GitHub limit by slowing down before it runs out.

Reviews and Chief are why the hourly budget exists. Below
`NONURGENT_FRACTION` remaining, pollers and sweeps give way so that reviews
keep what is left. That comes well before the 10 percent hold on Elder
re-runs. Below `ALERT_FRACTION`, one `github_budget_low` line is logged per
rate-limit window. Asking GitHub for the budget is free. An unreadable budget
defers nothing: it is not evidence of a low one.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from github_app_auth import core_rate_limit, with_install_token_retry
from observability import emit_gauge

log = logging.getLogger("grug.github_budget")

NONURGENT_FRACTION = 0.40
ALERT_FRACTION = 0.20

_review_calls: ContextVar[list[int] | None] = ContextVar("review_calls", default=None)


def _alert_once(install_id: int, remaining: int, limit: int, reset: int) -> None:
    from adapters.install_store import claim_budget_alert

    try:
        if claim_budget_alert(install_id, reset):
            log.warning(
                "github_budget_low",
                extra={"install_id": install_id, "remaining": remaining,
                       "limit": limit, "reset_epoch": reset},
            )
    except Exception as e:  # noqa: BLE001 - an alert must never stop the work
        log.warning("github_budget_alert_failed", extra={"kind": type(e).__name__})


def budget_is_low(install_id: int) -> bool:
    """Record the installation's budget and say whether non-urgent work
    should wait. Never raises."""
    try:
        budget = with_install_token_retry(install_id, core_rate_limit)
    except Exception as e:  # noqa: BLE001 - an unreadable budget is not a low one
        log.warning("github_budget_unreadable", extra={"kind": type(e).__name__})
        return False
    if not budget or budget[1] <= 0:
        return False
    remaining, limit, reset = budget
    fraction = remaining / limit
    log.info(
        "github_budget",
        extra={"install_id": install_id, "remaining": remaining,
               "limit": limit, "reset_epoch": reset},
    )
    emit_gauge(
        "grug.github.budget_remaining_fraction", fraction,
        {"install_id": str(install_id)},
    )
    if fraction < ALERT_FRACTION:
        _alert_once(install_id, remaining, limit, reset)
    return fraction < NONURGENT_FRACTION


class _GitHubCallCounter(logging.Handler):
    """Counts the charged GitHub calls httpx logs on a counting thread. A 304
    is free, so it does not count."""

    def emit(self, record: logging.LogRecord) -> None:
        counter = _review_calls.get()
        if counter is None:
            return
        message = record.getMessage()
        if "https://api.github.com/" in message and " 304 " not in message:
            counter[0] += 1


_counter_handler = _GitHubCallCounter()


@contextmanager
def count_review_calls(repo: str, pr_number: int) -> Iterator[None]:
    """Log and gauge the GitHub calls made on this thread during a review.
    Calls from helper threads are not included."""
    httpx_log = logging.getLogger("httpx")
    if _counter_handler not in httpx_log.handlers:
        httpx_log.addHandler(_counter_handler)
    counter = [0]
    token = _review_calls.set(counter)
    try:
        yield
    finally:
        _review_calls.reset(token)
        log.info(
            "elder_review_github_calls",
            extra={"repo": repo, "pr": pr_number, "calls": counter[0]},
        )
        emit_gauge("grug.github.calls_per_review", float(counter[0]))
