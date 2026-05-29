"""FastAPI app for the grug-webhook Lambda.

Slice 1 (#22) scope: receive `POST /webhook/github`, verify HMAC signature
against the App webhook secret loaded from SSM, log structured event,
return 200. No business logic, no Checks API call, no DDB lookup.

Slice 4 (#25) extends this with persona dispatch (TPM is the first
persona; future personas — code-reviewer, release-manager, stuck-PR-pulse —
plug in via the same dispatcher).
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Header, HTTPException, Request, status

from cf_auth import CfAuthMiddleware
from hmac_verify import verify_signature
from observability import configure_logging
from secrets_loader import get_webhook_secret

configure_logging()
log = logging.getLogger("grug.webhook")

app = FastAPI(
    title="grug-webhook",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# CF→AWS auth boundary — reject direct Function URL hits that bypass
# Cloudflare. Fail-open when the env var/SSM secret isn't configured
# yet so deploy ordering across Pulumi + Workers + service can race
# without breaking production traffic. GitHub webhook HMAC is end-to-end
# and independent of this header — both must pass for delivery.
app.add_middleware(CfAuthMiddleware)


@app.get("/livez")
def livez() -> dict[str, str]:
    """Liveness probe — process is running. Cheap, no IO. Per
    `feedback_health_endpoint_standard` memory: use /livez + /readyz,
    NOT /healthz (K8s deprecated /healthz in v1.16)."""
    return {"status": "ok", "service": "grug-webhook"}


@app.get("/readyz")
def readyz() -> dict[str, str]:
    """Readiness probe — service can serve traffic. v1 has no downstream
    deps so always returns ready. Slice 2+ adds DDB + KMS reachability
    checks here (return 503 when DDB ping fails or KMS describe times
    out — orchestrator routes traffic away but does NOT restart).
    """
    return {"status": "ready", "service": "grug-webhook"}


@app.post("/webhook/github")
async def receive_github_webhook(
    request: Request,
    x_github_event: str = Header(default=""),
    x_github_delivery: str = Header(default=""),
    x_hub_signature_256: str = Header(default=""),
) -> dict[str, str]:
    # Handler is async because reading raw body via Starlette's Request
    # requires `await request.body()`. Earlier sync version used
    # `body: bytes = Body(...)` but FastAPI 0.115 / Pydantic-v2
    # JSON-decodes the body BEFORE bytes-validation when the
    # Content-Type is application/json (GitHub's default), then 422s
    # on the parsed dict not being bytes — so HMAC verify never runs.
    # Using `Request` keeps the wire bytes intact for HMAC verify.
    #
    # Lambda concurrency is per-container (one in-flight request per
    # warm container). Sync boto3 / httpx calls below don't starve
    # other coroutines because there are none. Closes #68 (the
    # original spirit) and the pre-Slice-11 422 regression.
    body = await request.body()

    secret = get_webhook_secret()
    if not verify_signature(secret, body, x_hub_signature_256):
        log.warning(
            "webhook_signature_invalid",
            extra={
                "delivery_id": x_github_delivery,
                "event": x_github_event,
                "body_len": len(body),
            },
        )
        # 401 — GitHub stops retrying after 4xx (vs 5xx which retries).
        # Bad signature = caller is broken (or hostile); no point in retry.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid signature",
        )

    log.info(
        "webhook_received",
        extra={
            "delivery_id": x_github_delivery,
            "event": x_github_event,
            "body_len": len(body),
            "env": os.getenv("GRUG_ENV", "unknown"),
        },
    )

    # Dispatch to personas (Slice 4 #25). v1: pull_request → TPM.
    import json as _json
    from dispatcher import dispatch  # lazy import keeps cold-start cheap

    try:
        payload = _json.loads(body)
    except _json.JSONDecodeError:
        # Body already passed HMAC verify — non-JSON here is GitHub
        # bug, our own header-stripping middleware, or attack payload
        # that got past sig verify (shouldn't happen). 400 stops GH
        # retries while bumping severity above the 200 'skip' bucket
        # so DD alerts fire. silent-failure-hunter P1 #1.
        log.error(
            "webhook_body_not_json_after_hmac_pass",
            extra={"delivery_id": x_github_delivery, "body_len": len(body)},
        )
        raise HTTPException(status_code=400, detail="body_not_json")

    outcome = dispatch(x_github_event, payload)
    log.info(
        "webhook_dispatched",
        extra={"delivery_id": x_github_delivery, **outcome},
    )
    return {"delivery_id": x_github_delivery, **outcome}
