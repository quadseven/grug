"""shepherd.py

This module implements the Shepherd functionality to verify that a merged PR
actually deployed.

It is intentionally minimal for the current exercise but should be
expanded to interact with the registry and the k8s deployments.

The core entry point is :func:`check_deployment` which currently
returns ``False`` to indicate that the deployment is not verified.
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
    _, deployed_digest = get_latest_digest(service)
    _, pushed_digest = get_latest_push_digest(service)
    return bool(deployed_digest and pushed_digest and deployed_digest == pushed_digest)


if __name__ == "__main__":
    import sys
    svc = sys.argv[1] if len(sys.argv) > 1 else "webhook"
    if check_deployment(svc):
        print(f"{svc} is deployed correctly")
    else:
        print(f"{svc} is NOT deployed correctly")
        sys.exit(1)
