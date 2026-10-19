"""Manifest invariants for grugthink on OKE (grug#1190).

The ``grugthink-data`` local-path PVC node-pinned the pod and blocked
rescheduling during a node outage. Config now lives in Postgres
(``grugthink_config`` table) and ``/data`` is scratch-only, so the manifest
must carry no PersistentVolumeClaim, no local-path storage class, and no
node-affinity-creating volume - while still giving the (read-only-root-FS)
container a writable ``/data`` for per-bot runtime state.
"""

from pathlib import Path

import pytest
import yaml

MANIFEST = Path(__file__).parent.parent / "k8s" / "deployment.yaml"


@pytest.fixture(scope="module")
def docs():
    assert MANIFEST.exists(), f"manifest missing: {MANIFEST}"
    with open(MANIFEST) as f:
        return [d for d in yaml.safe_load_all(f) if d]


@pytest.fixture(scope="module")
def deployment(docs):
    dep = next(
        (d for d in docs if d.get("kind") == "Deployment" and d["metadata"]["name"] == "grugthink"),
        None,
    )
    assert dep is not None, "grugthink Deployment not found in manifest"
    return dep


def test_no_persistent_volume_claim(docs):
    pvcs = [d for d in docs if d.get("kind") == "PersistentVolumeClaim"]
    assert pvcs == [], "grugthink-data PVC must be gone (grug#1190)"


def test_no_pvc_volumes(deployment):
    volumes = deployment["spec"]["template"]["spec"]["volumes"]
    pvc_volumes = [v for v in volumes if "persistentVolumeClaim" in v]
    assert pvc_volumes == [], "no volume may reference a PersistentVolumeClaim"


def test_no_local_path_storage_class(docs):
    for d in docs:
        sc = (d.get("spec") or {}).get("storageClassName")
        assert sc != "local-path", "local-path storage class node-pins the pod"


def test_data_volume_is_ephemeral_scratch(deployment):
    """``/data`` stays mounted (readOnlyRootFilesystem) but must be scratch.

    Per-bot runtime state (personalities.db, chat frequencies) is ephemeral;
    durable memory lives in Postgres+pgvector and config in the
    ``grugthink_config`` table.
    """
    spec = deployment["spec"]["template"]["spec"]
    volumes = {v["name"]: v for v in spec["volumes"]}
    assert "data" in volumes, "/data volume must still exist for runtime scratch"
    assert "emptyDir" in volumes["data"], "/data must be emptyDir, not a PVC"

    mounts = {
        m["mountPath"]: m["name"]
        for c in spec["containers"]
        for m in c.get("volumeMounts", [])
    }
    assert mounts.get("/data") == "data"


def test_no_node_pinning_beyond_arch(deployment):
    """Only the arm64 arch selector may remain; nothing may pin a node."""
    node_selector = deployment["spec"]["template"]["spec"].get("nodeSelector", {})
    assert node_selector == {"kubernetes.io/arch": "arm64"}, (
        f"unexpected nodeSelector (rescheduling hazard): {node_selector}"
    )
