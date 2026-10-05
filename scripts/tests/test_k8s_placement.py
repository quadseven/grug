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


@pytest.mark.parametrize("name", _DEPLOYMENTS)
def test_prefers_oci_zones_softly(name):
    node_aff = _spec(name)["affinity"].get("nodeAffinity", {})
    assert "requiredDuringSchedulingIgnoredDuringExecution" not in node_aff
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
