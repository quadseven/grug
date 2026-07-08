"""FastAPI app for the grug-webhook Lambda.

Slice 1 (#22) scope: receive `POST /webhook/github`, verify HMAC signature
against the App webhook secret loaded from SSM, log structured event,
return 200. No business logic, no Checks API call, no DDB lookup.

Slice 4 (#25) extends this with persona dispatch (TPM is the first
persona; future personas — code-reviewer, release-manager, stuck-PR-pulse —
plug in via the same dispatcher).
"""
# no-op: touches services/** to trigger the pr-<n> image-build gate for a
# live verification of PR #571's preview.yml fix (throwaway PR, will close)

from __future__ import annotations

import asyncio
import logging
import os

from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response, status

from cf_auth import CfAuthMiddleware
from hmac_verify import verify_signature
from aws_identity import prove_roles_anywhere_identity
from observability import configure_logging
from secrets_loader import get_webhook_secret

configure_logging()
log = logging.getLogger("grug.webhook")

# Roles Anywhere boot proof (#389) - rationale in aws_identity's docstring.
prove_roles_anywhere_identity()

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
def readyz(response: Response) -> dict[str, object]:
    """Readiness probe — SSM/KMS + Postgres reachable (#404). Returns 503 when
    a dependency is down so the orchestrator routes traffic away AND a rollout
    of broken pods never completes (last-good pods keep serving); does NOT
    restart (that's /livez). Health logic lives in `readiness` (TTL-cached,
    fail-closed)."""
    from readiness import check_readiness

    rep = check_readiness()
    if not rep.ready:
        response.status_code = 503
    return {
        "status": "ready" if rep.ready else "not_ready",
        "service": "grug-webhook",
        "deps": rep.deps,
    }


@app.post("/webhook/github")
async def receive_github_webhook(
    request: Request,
    x_github_event: str = Header(default=""),
    x_github_delivery: str = Header(default=""),
    x_hub_signature_256: str = Header(default=""),
) -> dict[str, Any]:
    # dict[str, Any], NOT dict[str, str]: dispatch() returns an aggregated
    # {status, personas: [...]} for pull_request events (list-shaped - see
    # dispatcher.py's own contract comment). The narrower annotation made
    # FastAPI's response validation 500 every dispatched PR delivery on the
    # k8s image (caught by the #368 live proof; the Lambda image's older
    # dependency set never enforced it).
    # Handler is async because reading raw body via Starlette's Request
    # requires `await request.body()`. Earlier sync version used
    # `body: bytes = Body(...)` but FastAPI 0.115 / Pydantic-v2
    # JSON-decodes the body BEFORE bytes-validation when the
    # Content-Type is application/json (GitHub's default), then 422s
    # on the parsed dict not being bytes — so HMAC verify never runs.
    # Using `Request` keeps the wire bytes intact for HMAC verify.
    #
    # This is `async def` (it must be: reading the raw HMAC bytes needs
    # `await request.body()` — see the 422 note above), so the sync boto3 /
    # httpx calls below run on the event loop, and async handlers do NOT get
    # Starlette's run_in_threadpool offload. On Lambda that was safe (one
    # invocation per warm container, no peer coroutines to starve), but on the
    # k8s runtime (#354) uvicorn serves CONCURRENT deliveries on one loop, so a
    # sync call here would block every other in-flight delivery's ACK toward
    # GitHub's ~10s timeout. So the blocking calls — the SSM secret fetch and
    # `dispatch` (its remaining sync httpx/store work; the heavy Elder review is
    # already off-loop via #368) — run in `await asyncio.to_thread(...)`, the
    # same offload cf_auth.py's middleware uses for its sync ssm.get_parameter.
    # See docs/RUNBOOK.md#sync-vs-async-route-handlers. Closes #371 + #68
    # (spirit) + the pre-Slice-11 422 regression.
    body = await request.body()

    secret = await asyncio.to_thread(get_webhook_secret)
    if not verify_signature(secret, body, x_hub_signature_256):
        # Split the alert signal from internet noise. A genuine GitHub delivery
        # being REJECTED (webhook secret rotated / SSM drift — actionable)
        # ALWAYS carries an X-Hub-Signature-256 header; an unsigned scanner
        # poking the public Function URL sends none. Only the signed-but-invalid
        # case is `webhook_signature_invalid` (the monitored alert), so a
        # rotated-secret outage pages while background probes (which dominate a
        # public endpoint) stay out of the alert path as a quieter
        # `webhook_unsigned_probe`. Both still 401.
        if x_hub_signature_256:
            log.warning(
                "webhook_signature_invalid",
                extra={
                    "delivery_id": x_github_delivery,
                    "event": x_github_event,
                    "body_len": len(body),
                },
            )
        else:
            log.info(
                "webhook_unsigned_probe",
                extra={"event": x_github_event, "body_len": len(body)},
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

    outcome = await asyncio.to_thread(
        dispatch, x_github_event, payload, delivery_id=x_github_delivery
    )
    log.info(
        "webhook_dispatched",
        extra={"delivery_id": x_github_delivery, **outcome},
    )
    return {"delivery_id": x_github_delivery, **outcome}
