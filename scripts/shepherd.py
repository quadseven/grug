"""shepherd.py

This module implements the Shepherd functionality to verify that a merged PR
actually deployed.

It is intentionally minimal for the current exercise but should be
expanded to interact with the registry and the k8s deployments.

The CLI reports a digest mismatch as merged-but-not-deployed and missing
reference evidence as unverified.
"""

from typing import Tuple


def get_latest_digest(service: str) -> Tuple[str, str]:
    """Return the image tag and digest of the deployed service.

    In a real implementation this would query the Kubernetes API to
    fetch the image used by the deployment.

    Returns a tuple of (tag, digest). The default stub returns empty
    values.
    """
    return "", ""


def get_latest_push_digest(service: str) -> Tuple[str, str]:
    """Return the image tag and digest that was pushed for the PR.

    This would normally interrogate the container registry.
    The stub returns empty values.
    """
    return "", ""


def check_deployment(service: str) -> bool:
    """Return True if the deployed image matches the pushed image.

    The function compares the digests of the latest deployed
    instance and the most recent tag pushed for the service.
    """
    return deployment_status(service) == "deployed"


def deployment_status(service: str) -> str:
    """Distinguish a stale deployment from missing reference evidence."""
    _, deployed_digest = get_latest_digest(service)
    _, pushed_digest = get_latest_push_digest(service)
    if not deployed_digest or not pushed_digest:
        return "unverified"
    if deployed_digest != pushed_digest:
        return "merged-but-not-deployed"
    return "deployed"


def main(service: str) -> int:
    import sys

    status = deployment_status(service)
    if status == "deployed":
        print(f"{service} is deployed correctly")
        return 0
    if status == "merged-but-not-deployed":
        print(f"MERGED-BUT-NOT-DEPLOYED: {service} differs from the published image", file=sys.stderr)
    else:
        print(f"UNVERIFIED: {service} is missing a published or deployed digest", file=sys.stderr)
    return 1


if __name__ == "__main__":
    import sys
    svc = sys.argv[1] if len(sys.argv) > 1 else "webhook"
    sys.exit(main(svc))
