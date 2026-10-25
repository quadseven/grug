"""Restart-resilience manifest pins (grug#1275).

A consumer rollout must never drop review capacity or kill a review
mid-flight: surge-first strategy, a disruption budget that forbids
taking a consumer down voluntarily below full strength, and a
termination grace sized to the review deadline so SIGTERM drain
(finish in-flight, or hand the lease/claim back) can complete.
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


def test_consumer_pdb_forbids_voluntary_disruption():
    docs = _load("pdb.yaml")
    consumer = next(d for d in docs if d["metadata"]["name"] == "grug-consumer")
    assert consumer["spec"].get("maxUnavailable") == 0
    assert "minAvailable" not in consumer["spec"]


def test_consumer_grace_period_is_sized_to_the_review_deadline():
    doc = _consumer()
    pod = doc["spec"]["template"]["spec"]
    container = pod["containers"][0]
    review_timeout = int(_env(container)["GRUG_REVIEW_JOB_TIMEOUT_S"])
    # Review deadline + shared thread-join (15s) + bounded cleanup (5s),
    # with margin: SIGTERM drain must be able to finish or hand back.
    assert pod["terminationGracePeriodSeconds"] >= review_timeout + 60
    prestop = container.get("lifecycle", {}).get("preStop", {})
    assert prestop, "a preStop hook must run before SIGTERM so endpoints drain first"
