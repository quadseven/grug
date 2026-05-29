# MIRRORED — sibling at services/webhook/cf_auth.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
"""CF→AWS auth-boundary middleware.

CF Workers inject `X-Grug-CF-Secret` from a Worker secret binding on
every upstream request. This middleware validates the header against
the same value sourced from SSM `/grug/cf-shared-secret`. Direct hits
on the Lambda Function URL that bypass CF arrive without the header
and get 401.

Fail-open is the safe default during deploy churn — if the env var is
unset OR the SSM lookup fails (ParameterNotFound, empty value), the
middleware logs the misconfiguration and lets requests through. The
Worker side mirrors this: when its `GRUG_CF_SECRET` binding is absent,
no header is injected, so a strict-mode middleware would 401 every
legitimate request. Pulumi-first rollout depends on this symmetry.

`/livez` is always exempt so DD synthetics and post-deploy smoke tests
can reach the Lambda without a header.
"""
from __future__ import annotations

import hmac
import logging
import os
from functools import lru_cache
from typing import Callable

import boto3
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.cf_auth")

# Module-scope clients + cache mirror secrets_loader.py so warm-container
# semantics match the rest of the secret-loading surface.
_ssm = boto3.client("ssm")

SECRET_HEADER = "X-Grug-CF-Secret"
LIVEZ_PATH = "/livez"


@lru_cache(maxsize=1)
def _default_secret_loader() -> str:
    """Read the SSM param named by `GRUG_CF_SHARED_SECRET_SSM`.

    Cached for the warm container's lifetime. `LookupError` if the env
    var is unset — middleware fails-open on that.
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
            Three outcomes are treated uniformly as fail-open:
              - returns a non-empty `str` → strict mode for this request
              - returns `""` → fail-open + `cf_shared_secret_empty` log
              - raises any `Exception` → fail-open + `cf_shared_secret_unconfigured` log
            Any subclass of `BaseException` that is NOT an `Exception`
            (KeyboardInterrupt, SystemExit) propagates as normal.
        """
        super().__init__(app)
        self._secret_loader = secret_loader

    async def dispatch(
        self, request: Request, call_next: Callable
    ) -> Response:
        # Liveness probe stays open so DD synthetics + smoke tests work
        # whether or not CF is in front of us.
        if request.url.path == LIVEZ_PATH:
            return await call_next(request)

        try:
            expected = self._secret_loader()
        except Exception as e:
            # Fail-open: log once per request at WARNING. Operator will
            # see the rate in DD logs and can react.
            log.warning(
                "cf_shared_secret_unconfigured",
                extra={"reason": type(e).__name__, "path": request.url.path},
            )
            return await call_next(request)

        if not expected:
            # Same fail-open behavior for empty-string. Distinguishing
            # from "unconfigured" only matters for the log signal.
            log.warning(
                "cf_shared_secret_empty",
                extra={"path": request.url.path},
            )
            return await call_next(request)

        # Strict mode from here on.
        received = request.headers.get(SECRET_HEADER, "")
        if not hmac.compare_digest(received, expected):
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
