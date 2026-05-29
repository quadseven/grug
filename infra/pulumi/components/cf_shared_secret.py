"""Cloudflare → AWS shared-secret provisioning (per spec 0014 / issue #173).

Provisions the random secret value the CF Worker injects on every upstream
request and the Lambda middleware validates at the handler entry. Sourcing
both sides from the same SSM SecureString makes the rotation story trivial:
bump `keepers["version"]` and run `pulumi up`, then re-run
`infra/cloudflare/deploy.sh` to sync the new value into the CF Worker
secret bindings.

Acceptance criteria gate (issue #231):
  * SecureString param exists at `/grug/cf-shared-secret`
  * Value is 64 lowercase alphanumeric chars (random.RandomPassword)
  * Lambda IAM grant via the existing lambda_service.extra_ssm_secrets path

The Lambda middleware is sibling work (#233); the CF Worker injection is
sibling work (#232). This component is safe to deploy alone — until #233
ships, the env var is loaded but no path validates against it.
"""
from __future__ import annotations

from dataclasses import dataclass

import pulumi
import pulumi_aws as aws
import pulumi_random as random


@dataclass
class CfSharedSecretBundle:
    """Outputs from `cf_shared_secret.create()`.

    `ssm_parameter` is the SSM SecureString resource — its `.arn` and
    `.name` are duck-compatible with the `GetParameterResult` shape used
    by lambda_service.extra_ssm_secrets, so the caller can pass it
    directly into both Lambda definitions.

    `secret_value` is the raw random string, marked secret. Surfaced so
    the deploy.sh flow can publish it as a CF Worker secret binding via
    the Pulumi → SSM → deploy.sh chain (sibling slice #232).
    """
    ssm_parameter: aws.ssm.Parameter
    secret_value: pulumi.Output[str]


def create(*, name: str = "grug-cf-shared-secret") -> CfSharedSecretBundle:
    """Provision the SSM SecureString backing the CF→AWS auth boundary."""
    # 64-char lowercase alphanumeric. ~380 bits of entropy — overkill for
    # a constant-time header compare but cheap. Excluding upper + special
    # keeps the value safe to inline in CF Worker secret binding HTTP
    # bodies and round-trip through SSM without any escaping concerns.
    random_value = random.RandomPassword(
        f"{name}-value",
        length=64,
        special=False,
        upper=False,
        # Rotate by bumping `version` and `pulumi up` — random provider
        # replaces the resource. We don't rotate on every up; only when
        # an operator deliberately retires the value.
        keepers={"version": "v1"},
    )

    param = aws.ssm.Parameter(
        f"{name}-ssm",
        name="/grug/cf-shared-secret",
        type="SecureString",
        # Output.secret-wrap so the value never appears in `pulumi preview`
        # diffs. The random provider's `.result` is already marked secret
        # but the extra wrap is defense-in-depth per the leak-guard memory
        # from PR #164's RUM credentials rollout.
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
