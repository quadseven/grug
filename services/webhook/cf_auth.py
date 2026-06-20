# MIRRORED — sibling at services/api/cf_auth.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
"""CF→AWS auth-boundary middleware.

CF Workers inject `X-Grug-CF-Secret` from a Worker secret binding on
every upstream request. This middleware validates the header against
the same value sourced from SSM `/grug/cf-shared-secret`. Direct hits
on the Lambda Function URL that bypass CF arrive without the header
and get 401.

Fail-CLOSED by default (audit #4): if the env var is unset OR the SSM
lookup fails (ParameterNotFound, empty value), the middleware logs the
misconfiguration and returns 503 rather than silently disabling the
origin-auth boundary. During the very first Pulumi->Worker->service
rollout the Worker's `GRUG_CF_SECRET` binding may not exist yet (no
header injected), which a fail-closed boundary would 503; set
`GRUG_CF_AUTH_FAIL_OPEN=1` for that bring-up window ONLY, then remove it
once the secret is live so prod runs fail-closed.

`/livez` is always exempt so DD synthetics and post-deploy smoke tests
can reach the Lambda without a header.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from functools import lru_cache
from typing import Callable

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.cf_auth")

# Module-scope clients + cache mirror secrets_loader.py so warm-container
# semantics match the rest of the secret-loading surface.
_ssm = boto3.client("ssm")

SECRET_HEADER = "X-Grug-CF-Secret"
LIVEZ_PATH = "/livez"
# Readiness must be probeable by the ORCHESTRATOR (kubelet sends no CF
# header); a 403 here reads as not-ready while the process is healthy -
# exactly what blocked the first Kubernetes rollout (#354). Same
# no-sensitive-data rationale as /livez.
READYZ_PATH = "/readyz"

# Narrow set of exceptions that put the middleware into fail-open mode.
# Anything outside this set propagates as a 500 — a programmer bug or
# unexpected runtime error should NOT silently disable the auth boundary.
_FAIL_OPEN_ERRORS = (LookupError, ClientError, BotoCoreError)

# Per-process throttle for the "unconfigured" warning log. Unconfigured
# state is a STATIC misconfig — logging it on every request floods DD
# with millions of identical warnings during the rollout window.
#
# Unlocked dict mutation is safe under Mangum's one-event-per-container
# model. If/when this code moves to a multi-coroutine ASGI server
# (uvicorn workers in-process), the race becomes benign-but-real (a
# duplicate log at a throttle boundary). Add a `threading.Lock` then,
# or migrate to `contextvars` if per-request semantics are needed.
_LOG_THROTTLE_SECONDS = 60.0
_last_unconfigured_log_at: dict[str, float] = {}


@lru_cache(maxsize=1)
def _default_secret_loader() -> str:
    """Read the SSM param named by `GRUG_CF_SHARED_SECRET_SSM`.

    Successful reads are cached for the warm container's lifetime.
    Exceptions (LookupError when the env var is unset, ClientError on
    SSM failures) are NOT cached — `functools.lru_cache` only memoizes
    return values. Every fail-open request re-enters this function. The
    middleware's `_log_unconfigured_throttled` throttle is what prevents
    log flooding during a sustained misconfig.
    """
    ssm_name = os.getenv("GRUG_CF_SHARED_SECRET_SSM", "")
    if not ssm_name:
        raise LookupError("GRUG_CF_SHARED_SECRET_SSM env var unset")
    resp = _ssm.get_parameter(Name=ssm_name, WithDecryption=True)
    return resp["Parameter"]["Value"]


def _ssm_cache_clear() -> None:
    """Test hook — purges the loader cache so monkeypatched env vars
    take effect between tests."""
    _default_secret_loader.cache_clear()


def _fail_open_enabled() -> bool:
    """Fail-OPEN only when explicitly opted in for initial bring-up.

    Default is fail-CLOSED (audit #4): a misconfigured / empty / unreachable
    CF secret denies (503) rather than silently disabling the origin-auth
    boundary. Set `GRUG_CF_AUTH_FAIL_OPEN=1` ONLY during the first
    Pulumi->Worker->service rollout, when the Worker secret binding may not
    exist yet; remove it once the secret is live.
    """
    return os.getenv("GRUG_CF_AUTH_FAIL_OPEN", "").strip().lower() in (
        "1", "true", "yes",
    )


def _fail_response() -> Response:
    """503 returned when the boundary can't verify and fail-open is off.

    503 (not 401): the cause is a server-side misconfig, not a bad client
    credential. /livez + /readyz are exempt earlier in dispatch, so the pod
    stays schedulable even while app traffic is denied."""
    return JSONResponse(
        {"detail": "auth boundary unavailable"},
        status_code=503,
    )


class CfAuthMiddleware(BaseHTTPMiddleware):
    """Starlette/FastAPI middleware that enforces the auth boundary.

    `secret_loader` is injected for tests (default: read SSM via env
    var). The loader is expected to either return the secret string OR
    raise — any raise puts the middleware into fail-open mode for that
    request and logs the misconfiguration.
    """

    def __init__(
        self,
        app,
        secret_loader: Callable[[], str] = _default_secret_loader,
    ) -> None:
        """
        Args:
          secret_loader: zero-arg callable returning the SSM secret value.
            - returns a non-empty `str` -> strict mode for this request
            - returns `""` -> log `cf_shared_secret_unconfigured`
              (reason="empty_ssm_value") then deny 503, UNLESS
              `GRUG_CF_AUTH_FAIL_OPEN=1` (bring-up only), in which case
              fail-open
            - raises any exception in `_FAIL_OPEN_ERRORS` (LookupError,
              ClientError, BotoCoreError) -> same log with
              `reason=<type name>` then deny 503 (or fail-open under the
              bring-up flag)
            Anything else (`AttributeError`, `NameError`, `TypeError`,
            ...) propagates as 500. Programmer bugs MUST NOT silently
            disable the auth boundary — see `_FAIL_OPEN_ERRORS` for the
            whitelist rationale.
        """
        super().__init__(app)
        self._secret_loader = secret_loader

    async def dispatch(
        self, request: Request, call_next: Callable
    ) -> Response:
        # Liveness probe stays open so DD synthetics + smoke tests work
        # whether or not CF is in front of us. Normalize trailing slash
        # + case so `/livez/` and `/LIVEZ` also bypass — FastAPI's
        # auto-redirect on trailing slash issues 307 from the route
        # layer, but the middleware sees the raw path first.
        if request.url.path.rstrip("/").lower() in (LIVEZ_PATH, READYZ_PATH):
            return await call_next(request)

        try:
            # Cold-start path calls sync boto3 ssm.get_parameter — wrap
            # in asyncio.to_thread to keep the event loop unblocked.
            # Warm-path is a memoized dict lookup, ~microseconds, so the
            # thread hop is wasted but negligible. Mangum runs one event
            # per container; this matters most for any future lifespan
            # or background-task coroutines that would otherwise stall.
            expected = await asyncio.to_thread(self._secret_loader)
        except _FAIL_OPEN_ERRORS as e:
            # Known misconfig classes. Programmer bugs (AttributeError,
            # NameError, TypeError, etc.) are NOT in _FAIL_OPEN_ERRORS and
            # propagate as 500. Default: fail CLOSED (503). Only the
            # explicit bring-up flag lets the request through.
            _log_unconfigured_throttled(type(e).__name__, request.url.path)
            if _fail_open_enabled():
                return await call_next(request)
            return _fail_response()

        if not expected:
            # Empty-string secret — same handling. Distinguishing from
            # "unconfigured" only matters for the log signal.
            _log_unconfigured_throttled("empty_ssm_value", request.url.path)
            if _fail_open_enabled():
                return await call_next(request)
            return _fail_response()

        # Strict mode from here on.
        received = request.headers.get(SECRET_HEADER, "")
        if not hmac.compare_digest(received, expected):
            # Per-request log is intentional — this is the audit trail
            # for the auth boundary. Volume is naturally bounded by
            # attacker probing rate and surfaces in the DD monitor.
            log.info(
                "cf_shared_secret_mismatch",
                extra={
                    "path": request.url.path,
                    "had_header": bool(received),
                },
            )
            return JSONResponse(
                {"detail": "Unauthorized"},
                status_code=401,
            )

        return await call_next(request)


def _log_unconfigured_throttled(reason: str, path: str) -> None:
    """Emit `cf_shared_secret_unconfigured` at most once per
    `_LOG_THROTTLE_SECONDS` per reason. Unconfigured is a STATIC misconfig
    — logging on every request floods DD with identical warnings during
    a rollout window. The reason key prevents one fail mode from
    starving another (`empty_ssm_value` and `LookupError` log
    independently).
    """
    now = time.monotonic()
    # -inf, NOT 0.0: the comparison is against time.monotonic() (seconds since
    # BOOT, not epoch). On a freshly-booted pod/runner monotonic() can be < the
    # throttle window, so a 0.0 default makes `now - 0.0 < WINDOW` True and
    # SILENTLY DROPS the FIRST warning per reason - exactly the
    # `cf_shared_secret_unconfigured` misconfig signal you want during a rollout.
    # -inf means the first occurrence of each reason always logs. (#359: this was
    # the green-local / red-CI flake, NOT a ddtrace logger-rebinding race.)
    last = _last_unconfigured_log_at.get(reason, float("-inf"))
    if now - last < _LOG_THROTTLE_SECONDS:
        return
    _last_unconfigured_log_at[reason] = now
    log.warning(
        "cf_shared_secret_unconfigured",
        extra={"reason": reason, "path": path},
    )
