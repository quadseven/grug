"""FastAPI app for the grug-api Lambda.

Slice 2 (#23) scope: stand up the api Lambda with /livez + /readyz +
/api/v1/health. No business logic yet — Slice 3 (#24) wires GitHub
OAuth, Slice 4 (#25) wires DDB store + persona dispatch.

`/livez` + `/readyz` per `feedback_health_endpoint_standard` memory:
- `/livez`: process is running. Cheap, no IO.
- `/readyz`: downstream deps reachable. v2 stub returns ready always
  (no deps yet); Slice 3 adds DDB ping; Slice 4 adds KMS describe.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from admin import router as admin_router
from auth.github_oauth import router as github_oauth_router
from cf_auth import CfAuthMiddleware
from installations import router as installations_router
from observability import configure_logging

configure_logging()
log = logging.getLogger("grug.api")

_BUILD_SHA = os.getenv("GRUG_BUILD_SHA", "unknown")
_STARTED_AT = datetime.now(timezone.utc)

app = FastAPI(
    title="grug-api",
    version=_BUILD_SHA,
    docs_url=None,  # public docs UI deferred to v1.5
    redoc_url=None,
    openapi_url=None,
)

# CF→AWS auth boundary — reject direct Function URL hits that bypass
# Cloudflare. Fail-open when the env var/SSM secret isn't configured
# yet so deploy ordering across Pulumi + Workers + service can race
# without breaking production traffic.
app.add_middleware(CfAuthMiddleware)

# CORS for the grug.lol SPA -> api.grug.lol cross-origin (different
# subdomain = different origin). The SPA fetches /api/v1/me etc. with
# `credentials: "include"`; without these headers the browser blocks the
# credentialed response, the SPA reads it as logged-out, and the dashboard
# loops back into OAuth (which trips GitHub's secondary rate limit).
# The Lambda Function URL carried this CORS config (FunctionUrlCorsArgs);
# the Lambda->k8s migration dropped it since FastAPI never added the
# middleware. allow_origins MUST be explicit (not "*") when
# allow_credentials=True — the browser rejects "*" with credentials.
# Added last so CORS is the OUTERMOST layer: it answers the OPTIONS
# preflight and stamps headers even on CfAuth short-circuits.
_SPA_DOMAIN = os.environ.get("GRUG_DOMAIN", "grug.lol")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[f"https://{_SPA_DOMAIN}", f"https://www.{_SPA_DOMAIN}"],
    allow_credentials=True,
    # Enumerate the methods/headers the SPA actually uses rather than "*".
    # The origin allowlist above is the real gate, but with credentials
    # enabled there's no reason to reflect a wildcard surface into the
    # preflight response. (Audit #9.)
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)


@app.get("/livez")
def livez() -> dict[str, str]:
    """Liveness — process running. Restart on fail."""
    return {"status": "ok", "service": "grug-api"}


@app.get("/readyz")
def readyz(response: Response) -> dict[str, object]:
    """Readiness — SSM/KMS + Postgres reachable (#404). Returns 503 when a
    dependency is down so k8s stops routing here AND a rollout of broken pods
    never completes (the last-good pods keep serving). Health logic lives in
    `readiness` (TTL-cached, fail-closed); /livez stays a cheap process-up
    check so a transient dep blip restarts nothing."""
    from readiness import check_readiness

    rep = check_readiness()
    if not rep.ready:
        response.status_code = 503
    return {
        "status": "ready" if rep.ready else "not_ready",
        "service": "grug-api",
        "deps": rep.deps,
    }


app.include_router(github_oauth_router)
app.include_router(installations_router)
app.include_router(admin_router)


@app.get("/api/v1/health")
def health() -> dict[str, str | float]:
    """Build + uptime probe for monitoring."""
    uptime = (datetime.now(timezone.utc) - _STARTED_AT).total_seconds()
    return {
        "service": "grug-api",
        "build": _BUILD_SHA,
        "env": os.getenv("GRUG_ENV", "unknown"),
        "uptime_seconds": uptime,
    }
