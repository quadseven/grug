"""Cloudflare → AWS shared-secret provisioning (parent #173).

Generates a random SSM SecureString that both the CF Worker (sibling
slice #232) and the Lambda middleware (sibling slice #233) consume.
Sourcing both sides from one SSM param means rotation = bump
`keepers["version"]` then `pulumi up` + re-run
`infra/cloudflare/deploy.sh`.

**Rotation ordering trap:** `pulumi up` updates the SSM value
immediately. Lambdas re-read at next cold start (often within seconds
of a deploy). The CF Worker binding only updates when deploy.sh runs.
Run deploy.sh BEFORE forcing a Lambda cold-start to avoid a 401 window.
Sibling slice #233 documents the full runbook.
"""
from __future__ import annotations

from dataclasses import dataclass

import pulumi
import pulumi_aws as aws
import pulumi_random as random


@dataclass
class CfSharedSecretBundle:
    """`ssm_parameter` duck-types as `GetParameterResult` for
    lambda_service.extra_ssm_secrets (both expose `.arn` + `.name`).
    `secret_value` is the raw random string, marked secret — surfaced so
    sibling slice #232's deploy.sh can publish it as a CF Worker binding.
    """
    ssm_parameter: aws.ssm.Parameter
    secret_value: pulumi.Output[str]


def create(*, name: str = "grug-cf-shared-secret") -> CfSharedSecretBundle:
    """Provision the SSM SecureString backing the CF→AWS auth boundary."""
    # Excluding upper + special keeps the value round-trippable through
    # SSM + CF Worker secret-binding HTTP bodies without escaping.
    random_value = random.RandomPassword(
        f"{name}-value",
        length=64,
        special=False,
        upper=False,
        # Bump version + pulumi up = the random provider replaces the
        # resource. Without `keepers`, every up would re-roll the value.
        keepers={"version": "v1"},
    )

    param = aws.ssm.Parameter(
        f"{name}-ssm",
        name="/grug/cf-shared-secret",
        type="SecureString",
        # Defense-in-depth re-wrap. RandomPassword.result is already
        # secret-marked but the explicit wrap survived a leak incident
        # on PR #164's RUM credentials rollout (preview output drift).
        value=pulumi.Output.secret(random_value.result),
        description=(
            "CF→AWS auth-boundary shared secret. CF Workers inject the "
            "value as X-Grug-CF-Secret; both Lambdas validate it via "
            "middleware. Rotation: bump RandomPassword.keepers.version, "
            "pulumi up, then re-run infra/cloudflare/deploy.sh."
        ),
        tags={
            "managed_by": "pulumi",
            "scope": "grug",
            "purpose": "cf-auth-boundary",
        },
    )

    return CfSharedSecretBundle(
        ssm_parameter=param,
        secret_value=random_value.result,
    )
