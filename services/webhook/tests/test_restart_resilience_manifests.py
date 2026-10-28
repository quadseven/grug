"""Restart-resilience manifest pins (grug#1275).

A consumer rollout must never drop review capacity or kill a review
mid-flight: surge-first strategy, and a termination grace sized to the
review deadline so the SIGTERM hand-back (SQS lease reset, claim
released) always completes before SIGKILL.
"""
from __future__ import annotations

from pathlib import Path

import yaml

K8S = Path(__file__).resolve().parents[3] / "k8s"


def _load(name: str) -> list[dict]:
    return [d for d in yaml.safe_load_all((K8S / name).read_text()) if d]


def _consumer() -> dict:
    (doc,) = [d for d in _load("consumer-deployment.yaml") if d["kind"] == "Deployment"]
    return doc


def _env(container: dict) -> dict[str, str]:
    return {e["name"]: str(e.get("value", "")) for e in container.get("env", [])}


def test_consumer_rollout_surges_and_never_dips_below_full_capacity():
    spec = _consumer()["spec"]
    assert spec["replicas"] == 2
    assert spec["strategy"]["rollingUpdate"] == {"maxSurge": 1, "maxUnavailable": 0}


def test_consumer_pdb_still_lets_a_node_drain():
    """A rollout ignores PDBs, so capacity through a deploy comes from the
    strategy above. The PDB must keep allowing one voluntary disruption:
    maxUnavailable 0 would block every node drain forever."""
    docs = _load("pdb.yaml")
    consumer = next(d for d in docs if d["metadata"]["name"] == "grug-consumer")
    assert consumer["spec"].get("minAvailable") == 1
    assert consumer["spec"].get("maxUnavailable") != 0


def test_consumer_grace_period_is_sized_to_the_review_deadline():
    doc = _consumer()
    pod = doc["spec"]["template"]["spec"]
    container = pod["containers"][0]
    review_timeout = int(_env(container)["GRUG_REVIEW_JOB_TIMEOUT_S"])
    # Review deadline + shared thread-join (15s) + bounded cleanup (5s),
    # with margin: SIGTERM drain must be able to finish or hand back.
    assert pod["terminationGracePeriodSeconds"] >= review_timeout + 60
    assert "preStop" not in container.get("lifecycle", {}), (
        "no endpoints to drain: a preStop delay would keep the pod taking "
        "new reviews after it was told to stop"
    )
