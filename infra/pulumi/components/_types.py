"""Shared structural types for the Pulumi component factories.

These are typing-only helpers (no runtime resources). They give a NAME to
the duck-typed contracts the factories already rely on, so a future reader
can see what a parameter actually accepts instead of trusting a concrete
annotation that lies.
"""

from __future__ import annotations

from typing import Protocol

import pulumi


class SsmSecretRef(Protocol):
    """An SSM parameter reference that exposes an ARN + name.

    Satisfied structurally by BOTH:
      - `aws.ssm.Parameter` — a created resource; `.arn`/`.name` are
        `pulumi.Output[str]` (unknown until apply).
      - `aws.ssm.GetParameterResult` — a data-source lookup; `.arn`/`.name`
        are plain `str` (resolved during program eval).

    `lambda_service.create(extra_ssm_secrets=...)` accepts either: it only
    reads `.arn` (to grant `ssm:GetParameter`) and the policy doc wraps the
    arns in `Output.all(...).apply(...)`, so a plain `str` and an
    `Output[str]` both resolve correctly. The `| str` arms of each field
    are what let the eager `GetParameterResult` satisfy the protocol.
    """

    arn: pulumi.Output[str] | str
    name: pulumi.Output[str] | str
