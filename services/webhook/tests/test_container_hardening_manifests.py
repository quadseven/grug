"""Container hardening and namespace network pins for every manifest (#1634).

Source-presence guards over the REAL manifests under k8s/ (same pattern as
test_pki_manifests and test_smasher_manifests): infra YAML has no
executable seam, and these properties are security-load-bearing config a
silent edit could drop.

- Every container under k8s/, preview manifests included, sets resource
  requests and a memory limit, and the container securityContext the main
  deployments carry: no privilege escalation, a read-only root filesystem
  (with a writable emptyDir /tmp), all capabilities dropped.
- The grug namespace's HTTP ingress names its source (the tunnel
  connector), and the DNS and Postgres egress rules name their
  destinations instead of opening the port to anywhere.
"""

from __future__ import annotations

from pathlib import Path

import yaml

K8S = Path(__file__).resolve().parents[3] / "k8s"

_POD_TEMPLATE_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "Job")


def _docs(path: Path) -> list[dict]:
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def _pod_spec(doc: dict) -> dict | None:
    if doc.get("kind") == "CronJob":
        return doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    if doc.get("kind") in _POD_TEMPLATE_KINDS:
        return doc["spec"]["template"]["spec"]
    return None


def _containers() -> list[tuple[str, dict, dict]]:
    """(label, container, pod spec) for every container in every workload."""
    out = []
    for path in sorted(K8S.rglob("*.yaml")):
        for doc in _docs(path):
            pod = _pod_spec(doc)
            if pod is None:
                continue
            for c in pod.get("initContainers", []) + pod["containers"]:
                label = f"{path.relative_to(K8S)}:{doc['metadata']['name']}/{c['name']}"
                out.append((label, c, pod))
    return out


def test_the_walk_finds_the_known_workloads():
    # Guard the guard: a wrong root or kind list would pass on zero items.
    labels = {label for label, _, _ in _containers()}
    for expected in (
        "preview-janitor.yaml:grug-preview-janitor/janitor",
        "preview/deployments.yaml:grug-webhook/webhook",
        "preview/deployments.yaml:grug-api/webhook",
        "webhook-deployment.yaml:grug-webhook/webhook",
    ):
        assert expected in labels, sorted(labels)


def test_every_container_sets_requests_and_a_memory_limit():
    missing = []
    for label, c, _ in _containers():
        res = c.get("resources", {})
        req, lim = res.get("requests", {}), res.get("limits", {})
        if not (req.get("cpu") and req.get("memory") and lim.get("memory")):
            missing.append(label)
    assert not missing, f"containers without requests and a memory limit: {missing}"


def test_every_container_carries_the_hardened_security_context():
    weak = []
    for label, c, _ in _containers():
        sc = c.get("securityContext", {})
        if not (
            sc.get("allowPrivilegeEscalation") is False
            and sc.get("readOnlyRootFilesystem") is True
            and "ALL" in sc.get("capabilities", {}).get("drop", [])
        ):
            weak.append(label)
    assert not weak, f"containers without the hardened securityContext: {weak}"


def test_every_read_only_container_has_a_writable_emptydir_tmp():
    # A read-only root with no /tmp breaks tempfile and Python's own scratch
    # writes at runtime, which no manifest test would otherwise notice.
    broken = []
    for label, c, pod in _containers():
        mounts = {m["mountPath"]: m["name"] for m in c.get("volumeMounts", [])}
        vols = {v["name"]: v for v in pod.get("volumes", [])}
        vol = vols.get(mounts.get("/tmp", ""), {})
        if "emptyDir" not in vol:
            broken.append(label)
    assert not broken, f"containers without an emptyDir /tmp: {broken}"


def test_preview_containers_fit_the_preview_quota_during_a_rollout():
    """The preview ResourceQuota (k8s/preview/namespace.yaml) caps the
    namespace; a rolling update briefly runs old and new pods side by
    side, so twice the deployments' totals must still fit."""
    ns_docs = _docs(K8S / "preview" / "namespace.yaml")
    (quota,) = [d for d in ns_docs if d["kind"] == "ResourceQuota"]
    hard = quota["spec"]["hard"]

    def qty(v: str) -> float:
        v = str(v)
        if v.endswith("Mi"):
            return float(v[:-2])
        if v.endswith("Gi"):
            return float(v[:-2]) * 1024
        if v.endswith("m"):
            return float(v[:-1]) / 1000
        return float(v)

    totals = {"requests.cpu": 0.0, "requests.memory": 0.0, "limits.cpu": 0.0, "limits.memory": 0.0}
    for doc in _docs(K8S / "preview" / "deployments.yaml"):
        pod = _pod_spec(doc)
        if pod is None:
            continue
        for c in pod["containers"]:
            for kind in ("requests", "limits"):
                for res in ("cpu", "memory"):
                    totals[f"{kind}.{res}"] += qty(c["resources"][kind][res])
    for key, used in totals.items():
        assert 2 * used <= qty(hard[key]), f"{key}: 2 x {used} exceeds quota {hard[key]}"


def _policy(name: str) -> dict:
    (doc,) = [
        d for d in _docs(K8S / "networkpolicy.yaml")
        if d.get("kind") == "NetworkPolicy" and d["metadata"]["name"] == name
    ]
    return doc["spec"]


def _ports(rule: dict) -> set[int]:
    return {p["port"] for p in rule.get("ports", [])}


def test_http_ingress_admits_only_the_tunnel_connector():
    (rule,) = _policy("allow-http-ingress")["ingress"]
    assert _ports(rule) == {8080}
    assert rule.get("from") == [{
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "cloudflared"}},
        "podSelector": {"matchLabels": {"app": "cloudflared"}},
    }]


def test_dns_and_postgres_egress_name_their_destinations():
    rules = _policy("allow-egress-scoped")["egress"]
    dns = [r for r in rules if 53 in _ports(r)]
    pg = [r for r in rules if 5432 in _ports(r)]
    assert dns and pg
    for r in dns:
        assert r.get("to") == [{
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
            "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
        }], r
    for r in pg:
        assert r.get("to") == [{
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "databases"}},
        }], r
