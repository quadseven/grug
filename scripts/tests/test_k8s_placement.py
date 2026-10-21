"""Long-running grug pods prefer the OCI zones over the home LAN nodes.

The lan zone's arm64 nodes are small and also carry pinned stateful
workloads. With only a zone spread, one replica of each grug deployment
landed on one of them and held it above 90% of allocatable memory
(2026-10-05) while an OCI node was 17% requested. The preference must stay
SOFT so a lan node still takes the pod when both OCI nodes are down.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_K8S = Path(__file__).resolve().parents[2] / "k8s"
_DEPLOYMENTS = ["api-deployment.yaml", "consumer-deployment.yaml", "webhook-deployment.yaml"]


def _spec(name: str) -> dict:
    docs = [d for d in yaml.safe_load_all((_K8S / name).read_text()) if d]
    (dep,) = [d for d in docs if d.get("kind") == "Deployment"]
    return dep["spec"]["template"]["spec"]


def _required_terms(name: str) -> list[dict]:
    node_aff = _spec(name)["affinity"].get("nodeAffinity", {})
    return node_aff.get("requiredDuringSchedulingIgnoredDuringExecution", {}).get("nodeSelectorTerms", [])


@pytest.mark.parametrize("name", _DEPLOYMENTS)
def test_never_schedules_on_memory_constrained_nodes(name):
    # infra#5502. Terms are ORed, so EVERY term must carry the exclusion.
    terms = _required_terms(name)
    assert terms
    for term in terms:
        assert any(
            e["key"] == "memory-constrained" and e["operator"] == "DoesNotExist" for e in term["matchExpressions"]
        ), term


@pytest.mark.parametrize("name", _DEPLOYMENTS)
def test_still_requires_arm64(name):
    assert _spec(name)["nodeSelector"]["kubernetes.io/arch"] == "arm64"
    for term in _required_terms(name):
        assert any(
            e["key"] == "kubernetes.io/arch" and e["operator"] == "In" and e["values"] == ["arm64"]
            for e in term["matchExpressions"]
        ), term


@pytest.mark.parametrize("name", _DEPLOYMENTS)
def test_prefers_oci_zones_softly(name):
    node_aff = _spec(name)["affinity"].get("nodeAffinity", {})
    # The only REQUIRED terms are the arch pin and the mini exclusion; a
    # required zone term would turn this soft preference hard.
    for term in _required_terms(name):
        assert all(e["key"] != "topology.kubernetes.io/zone" for e in term["matchExpressions"])
    # Weight 100, the max: a low weight is outscored by the zone spread and
    # the pods drift back onto the lan nodes.
    zones = [
        (p["weight"], set(e["values"]))
        for p in node_aff.get("preferredDuringSchedulingIgnoredDuringExecution", [])
        for e in p["preference"]["matchExpressions"]
        if e["key"] == "topology.kubernetes.io/zone" and e["operator"] == "In"
    ]
    assert (100, {"oci-iad", "oci-ord"}) in zones


@pytest.mark.parametrize("name", _DEPLOYMENTS)
def test_zone_spread_stays_soft(name):
    for c in _spec(name)["topologySpreadConstraints"]:
        assert c["whenUnsatisfiable"] == "ScheduleAnyway"


def _env(name: str) -> dict:
    (container,) = [c for c in _spec(name)["containers"] if c.get("env")][:1]
    return {e["name"]: e.get("value") for e in container["env"]}


def test_cave_models_match_the_webhook():
    # Both services call the same Cave arms; a model only one of them asks for
    # can name a model no Spark serves while the other keeps working.
    consumer, webhook = _env("consumer-deployment.yaml"), _env("webhook-deployment.yaml")
    for key in ("GRUG_CAVE_JUDGE_MODEL", "GRUG_CAVE_REVIEW_MODEL", "GRUG_CAVE_REASONER_MODEL"):
        assert consumer.get(key) == webhook.get(key), key
