"""Datadog RUM Application + SSM credential export for grug.lol.

Per spec 0013 (RumInstrumentation) — the spec encodes the contract; this
component declares the resource.

Output of `create()`:

  - A `datadog.RumApplication` Pulumi resource (type "browser",
    `rum_event_processing_state="ALL"` — full session capture).
  - Two SSM SecureString parameters under `/grug/dd-rum-*`:
      - `/grug/dd-rum-application-id`  (Output[str] of `app.id`)
      - `/grug/dd-rum-client-token`    (Output[str] of `app.client_token`)
    Both `Output.secret`-wrapped so they don't leak to `pulumi preview`.

The RUM clientToken + applicationId are PUBLIC-by-design (they live in
the browser bundle) — but we still source them from SSM at build time so
a rotation is `pulumi up`, not a git commit.

The web.deploy.yml workflow reads these SSM params before `npm run build`
and substitutes them into the RUM init snippets.
"""
from __future__ import annotations

from dataclasses import dataclass

import pulumi
import pulumi_aws as aws
import pulumi_datadog as datadog


@dataclass
class RumBundle:
    """Resources created by `dd_rum.create()` — kept together so the
    composition root can export both the app + SSM params via a single
    return value."""
    application: datadog.RumApplication
    ssm_application_id: aws.ssm.Parameter
    ssm_client_token: aws.ssm.Parameter


def create(
    *,
    name: str,
    provider: datadog.Provider,
) -> RumBundle:
    """Provision the DD RUM Application + export its credentials to SSM.

    Args:
      name: Browser RUM application name shown in the DD UI. Doubles as
        the canonical `service:` tag the SDK init uses (spec 0013 bool
        `rum_service_tag_is_grug_web_canonical_per_dd_naming_canon`).
      provider: The pre-configured `datadog.Provider` from `__main__.py`
        — same one used by `dd_monitors`.
    """
    app = datadog.RumApplication(
        f"{name}-rum-app",
        name=name,
        type="browser",
        # ALL = capture every RUM event type (view/action/error/resource/
        # long_task). The narrower ERROR_FOCUSED_MODE drops view/action
        # events which would defeat the point of installing RUM.
        rum_event_processing_state="ALL",
        # MAX = keep Product Analytics aggregations long-term. NONE
        # would disable Product Analytics derivation entirely.
        product_analytics_retention_state="MAX",
        opts=pulumi.ResourceOptions(provider=provider),
    )

    # SSM SecureString export. `Output.secret` so values stay encrypted
    # in Pulumi state + masked in preview output (per
    # `feedback_pulumi_preview_secret_leak_guard` memory — leaked DD APP
    # key 2026-05-17 made the unwrapped-secret pattern a hard rule).
    ssm_app_id = aws.ssm.Parameter(
        f"{name}-rum-application-id",
        name=f"/grug/dd-rum-application-id",
        type="SecureString",
        value=pulumi.Output.secret(app.id),
        description="DD RUM Application ID for grug-web. Public by design (lives in browser bundle); sourced from SSM so rotation is `pulumi up` not a git commit.",
        tags={"managed_by": "pulumi", "scope": "grug", "service": "grug-web"},
    )
    ssm_client_token = aws.ssm.Parameter(
        f"{name}-rum-client-token",
        name=f"/grug/dd-rum-client-token",
        type="SecureString",
        value=pulumi.Output.secret(app.client_token),
        description="DD RUM client token for grug-web. Public by design; sourced from SSM so rotation is `pulumi up` not a git commit.",
        tags={"managed_by": "pulumi", "scope": "grug", "service": "grug-web"},
    )

    return RumBundle(
        application=app,
        ssm_application_id=ssm_app_id,
        ssm_client_token=ssm_client_token,
    )
