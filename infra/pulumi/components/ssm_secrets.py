"""SSM Parameter Store reference factory.

This factory references existing SSM parameters seeded outside this stack,
per docs/HITL_PREREQUISITES.md. It resolves their ARNs so other components
can grant least-privilege access.

Per memory `feedback_prefer_ssm_over_1p` — SSM is the canonical secret
store; this factory is the standard intake point.
"""

from __future__ import annotations

import pulumi_aws as aws


def reference_existing(
    name_prefix: str,
    parameters: list[str],
) -> dict[str, aws.ssm.GetParameterResult]:
    """Look up pre-loaded SecureString params by `<prefix>/<name>`.

    Returns a dict keyed by the bare parameter name (no prefix) so
    callers can do `secrets["github-app-id"].arn` etc.

    Raises at `pulumi up` time if any parameter is missing — fail loud
    rather than deploy with broken refs.
    """
    out: dict[str, aws.ssm.GetParameterResult] = {}
    for name in parameters:
        full = f"{name_prefix}/{name}"
        out[name] = aws.ssm.get_parameter(name=full, with_decryption=False)
    return out
