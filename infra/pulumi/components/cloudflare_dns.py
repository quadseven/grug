"""Cloudflare DNS factory for grug.lol.

For Slice 1 we only need a CNAME for `webhook.grug.lol` pointed at the
Lambda Function URL host. CF in proxied mode (orange cloud) terminates
TLS at the edge and forwards to the Function URL upstream — Lambda's
built-in `*.lambda-url.us-east-1.on.aws` cert is trusted by CF.

When the broader CF-in-Pulumi sweep happens (separate backlog item),
this module expands to manage all grug.lol records. For now: just the
records this slice needs.
"""

from __future__ import annotations

from urllib.parse import urlparse

import pulumi
import pulumi_cloudflare as cloudflare


def _strip_scheme_and_path(url: str) -> str:
    """Convert `https://<host>/whatever` → `<host>` for CNAME content."""
    parsed = urlparse(url)
    return parsed.hostname or url


def create_proxied_cname(
    zone_id: str,
    name: str,
    domain: str,
    target_url: pulumi.Output[str],
    provider: cloudflare.Provider | None = None,
    proxied: bool = True,
    ignore_content: bool = False,
) -> cloudflare.Record:
    """Create a CNAME `<name>.<domain>` → host of target_url.

    `proxied=True` (default) routes through CF (TLS, WAF, CDN). MUST set
    `proxied=False` for AWS Lambda Function URL upstreams — Lambda
    enforces a strict Host header match against its `*.lambda-url.<region>.
    on.aws` hostname; CF proxy preserves the client Host (`<name>.<domain>`)
    upstream by default, which Lambda rejects with 403 AccessDenied.
    Workaround if proxy needed: use a CF Worker / Origin Rule to rewrite
    the Host header before forwarding.
    """
    return cloudflare.Record(
        f"cf-{name}",
        zone_id=zone_id,
        name=name,
        type="CNAME",
        content=target_url.apply(_strip_scheme_and_path),
        proxied=proxied,
        ttl=1 if proxied else 300,  # 1 = "Auto" when proxied; 300 = 5min DNS-only
        comment=f"grug — managed by Pulumi (slice 1) — {name}.{domain}",
        # ignore_content: the record exists but its VALUE is owned
        # out-of-band (e.g. the webhook record post-#354 - this stack's
        # CF token lost zone-DNS access; see the call site + infra#239).
        opts=pulumi.ResourceOptions(
            provider=provider,
            ignore_changes=["content"] if ignore_content else None,
        ),
    )
