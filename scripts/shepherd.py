"""shepherd.py

This module implements the Shepherd functionality to verify that a merged PR
actually deployed, and to escalate a broken chain once, naming the stage that
broke.

It is intentionally minimal for the current exercise but should be
expanded to interact with the registry and the k8s deployments.

The CLI reports a digest mismatch as merged-but-not-deployed and missing
reference evidence as unverified.

Escalation (T832): a chain is a sequence of named stages, each checked by a
stage check returning a StageVerdict. The first broken stage is reported
exactly once per break episode: the shepherd notifies, naming the stage and
the evidence that says so, then stays quiet on later runs while the same
break persists. When the chain recovers (every stage ok after a break), a
recovery notification is sent so a stale alarm cannot linger. Only success
counts as success; a cancelled, failed, or missing-evidence stage is broken.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Callable, Tuple


DEFAULT_STATE_PATH = os.environ.get("GRUG_SHEPHERD_STATE", ".shepherd-state.json")


@dataclass(frozen=True)
class StageVerdict:
    """The outcome of checking one link in a chain.

    ``ok`` is True only when the link succeeded. A failed build, a reference
    that was never bumped, a promised observable that never appeared, or
    silently cancelled scheduled work all count as broken: only success is
    success.
    """

    name: str
    ok: bool
    evidence: str


StageCheck = Callable[[], StageVerdict]


@dataclass(frozen=True)
class ChainState:
    """Persistent memory of whether a break has already been reported."""

    broken_stage: str | None
    notified: bool


def evaluate_chain(checks: list[tuple[str, StageCheck]]) -> StageVerdict | None:
    """Return the first broken stage verdict, or None when every link holds.

    Stages run in order and the first failure wins, so a broken chain names
    exactly one stage with the evidence that says so.
    """
    for name, check in checks:
        verdict = check()
        if not verdict.ok:
            return verdict
    return None


def load_chain_state(path: str = DEFAULT_STATE_PATH) -> ChainState:
    """Load the last-known chain state, treating missing state as fresh.

    Corrupt state is never allowed to hide a break: it resets to
    not-notified so the next broken run is reported. Genuine read errors
    (permissions, and the like) still propagate.
    """
    try:
        with open(path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return ChainState(broken_stage=None, notified=False)
    except json.JSONDecodeError:
        return ChainState(broken_stage=None, notified=False)
    if not isinstance(data, dict):
        return ChainState(broken_stage=None, notified=False)
    return ChainState(
        broken_stage=data.get("broken_stage"),
        notified=bool(data.get("notified")),
    )


def save_chain_state(path: str, state: ChainState) -> None:
    """Persist the chain state so the next run knows what was already said."""
    with open(path, "w") as fh:
        json.dump({"broken_stage": state.broken_stage, "notified": state.notified}, fh)


Notify = Callable[[str, str | None, str], None]


def escalate_once(
    checks: list[tuple[str, StageCheck]],
    *,
    state_path: str = DEFAULT_STATE_PATH,
    notify: Notify,
) -> str:
    """Evaluate the chain and report at most once per break.

    Returns ``"healthy"``, ``"broken"``, or ``"recovered"``.

    A broken chain produces exactly one notification naming the stage that
    broke and the evidence. Later runs while the same break persists stay
    quiet. When the chain recovers, a recovery notification is sent. Only
    success counts as success; a cancelled or otherwise non-ok stage is
    broken.
    """
    state = load_chain_state(state_path)
    broken = evaluate_chain(checks)

    if broken is None:
        if state.broken_stage is not None:
            notify(
                "chain recovered",
                None,
                f"previous break at {state.broken_stage!r}",
            )
            save_chain_state(
                state_path, ChainState(broken_stage=None, notified=False)
            )
            return "recovered"
        save_chain_state(
            state_path, ChainState(broken_stage=None, notified=False)
        )
        return "healthy"

    if state.notified and state.broken_stage == broken.name:
        return "broken"

    notify(f"chain broken at {broken.name!r}", broken.name, broken.evidence)
    save_chain_state(
        state_path, ChainState(broken_stage=broken.name, notified=True)
    )
    return "broken"


def deployment_stage_verdicts(service: str) -> list[StageVerdict]:
    """Stage verdicts for the deployed-versus-published image chain.

    The chain has one load-bearing link: the deployed image must match the
    published image. Missing reference evidence, a missing deployment, or a
    digest mismatch are each broken, and the verdict names the link and the
    evidence.
    """
    _, deployed_digest = get_latest_digest(service)
    _, pushed_digest = get_latest_push_digest(service)

    if not pushed_digest:
        return [
            StageVerdict(
                "reference",
                False,
                "no published image digest; reference never bumped",
            )
        ]
    if not deployed_digest:
        return [
            StageVerdict(
                "deploy",
                False,
                "published image present but nothing deployed; "
                "scheduled work silently cancelled",
            )
        ]
    if deployed_digest != pushed_digest:
        return [
            StageVerdict(
                "deploy",
                False,
                f"deployed digest {deployed_digest} differs from published "
                f"{pushed_digest}; merged-but-not-deployed",
            )
        ]
    return [
        StageVerdict(
            "deploy",
            True,
            f"deployed digest {deployed_digest} matches published "
            f"{pushed_digest}",
        )
    ]


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
