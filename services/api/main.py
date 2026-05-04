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

from fastapi import FastAPI

from auth.github_oauth import router as github_oauth_router
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


@app.get("/livez")
def livez() -> dict[str, str]:
    """Liveness — process running. Restart on fail."""
    return {"status": "ok", "service": "grug-api"}


@app.get("/readyz")
def readyz() -> dict[str, str]:
    """Readiness — downstream deps reachable. v2 always ready (no deps)."""
    return {"status": "ready", "service": "grug-api"}


app.include_router(github_oauth_router)
app.include_router(installations_router)


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
