"""Roles Anywhere tracer manifest pins (#388, ADR-0008).

Parses the REAL k8s manifests (same pattern as test_smasher_manifests) and
pins the contract values a well-meaning cleanup would break:

- The Certificate MUST carry BOTH `digital signature` AND `client auth`
  usages: `client auth` alone sets EKU but no keyUsage and Roles Anywhere
  rejects the leaf with "Insufficient certificate" (verified live,
  infrastructure#1318 Phase 4 - the gotcha that cost a failed attempt).
- The poller must NOT receive the static AWS key pair: env credentials
  out-rank credential_process in the SDK chain, silently bypassing the
  exact path this tracer proves.
- The credential_process line must pass --intermediates (the trust anchor
  is the offline ROOT; ca.crt is the signing intermediate).
"""

from __future__ import annotations

from pathlib import Path

import yaml

K8S = Path(__file__).resolve().parents[3] / "k8s"


def _load(name: str) -> list[dict]:
    return [d for d in yaml.safe_load_all((K8S / name).read_text()) if d]


def _pod_spec(doc: dict) -> dict:
    tpl = doc["spec"]["jobTemplate"]["spec"]["template"] if doc["kind"] == "CronJob" else doc["spec"]["template"]
    return tpl["spec"]


def test_certificate_pins_the_verified_usages_and_issuer():
    (cert,) = _load("pki-certificate.yaml")
    spec = cert["spec"]
    assert cert["kind"] == "Certificate"
    assert spec["commonName"] == "grug"  # the tenant CN the trust policy pins
    # BOTH usages - see module docstring; order-insensitive on purpose.
    assert set(spec["usages"]) == {"digital signature", "client auth"}
    assert spec["issuerRef"] == {
        "name": "pki-intermediate", "kind": "ClusterIssuer", "group": "cert-manager.io",
    }
    assert spec["secretName"] == "grug-pki-tls"
    # 6h/renew-4h per the proven infra tenant recipe.
    assert spec["duration"] == "6h"
    assert spec["renewBefore"] == "4h"


def test_aws_config_credential_process_shape():
    (cm,) = _load("grug-aws-config.yaml")
    config = cm["data"]["config"]
    line = next(l for l in config.splitlines() if l.startswith("credential_process"))
    for required in (
        "aws_signing_helper credential-process",
        "--certificate /var/run/grug-pki/tls.crt",
        "--private-key /var/run/grug-pki/tls.key",
        "--intermediates /var/run/grug-pki/ca.crt",
        "--trust-anchor-arn RA_TRUST_ANCHOR_ARN_PLACEHOLDER",
        "--profile-arn RA_PROFILE_ARN_PLACEHOLDER",
        "--role-arn RA_ROLE_ARN_PLACEHOLDER",
    ):
        assert required in line, f"missing from credential_process: {required}"


def test_poller_rides_roles_anywhere_not_the_static_key():
    (cron,) = [d for d in _load("poller-cronjob.yaml") if d["kind"] == "CronJob"]
    pod = _pod_spec(cron)
    (container,) = pod["containers"]

    env_from = [e["secretRef"]["name"] for e in container.get("envFrom", []) if "secretRef" in e]
    assert "grug-secrets" in env_from
    assert "grug-aws-static-key" not in env_from, (
        "poller must NOT get the static key - env creds out-rank credential_process"
    )
    env = {e["name"]: e.get("value") for e in container.get("env", [])}
    assert env.get("AWS_CONFIG_FILE") == "/etc/grug-aws/config"
    assert "AWS_ACCESS_KEY_ID" not in env and "AWS_SECRET_ACCESS_KEY" not in env

    mounts = {m["name"]: m for m in container["volumeMounts"]}
    assert mounts["grug-pki"]["mountPath"] == "/var/run/grug-pki"
    assert mounts["grug-pki"].get("readOnly") is True
    assert mounts["aws-config"]["mountPath"] == "/etc/grug-aws"
    vols = {v["name"]: v for v in pod["volumes"]}
    assert vols["grug-pki"]["secret"]["secretName"] == "grug-pki-tls"
    assert vols["aws-config"]["configMap"]["name"] == "grug-aws-config"


def test_static_key_consumers_are_exactly_the_non_tracer_workloads():
    """api/webhook/consumer keep the split-out key Secret until #389; the
    rotator rotates it by name. A drift here either breaks a workload's
    AWS access or silently re-exposes the poller to the static key."""
    for manifest in ("api-deployment.yaml", "consumer-deployment.yaml", "webhook-deployment.yaml"):
        (dep,) = [d for d in _load(manifest) if d["kind"] == "Deployment"]
        (container,) = _pod_spec(dep)["containers"]
        env_from = [e["secretRef"]["name"] for e in container.get("envFrom", []) if "secretRef" in e]
        assert "grug-aws-static-key" in env_from, f"{manifest} lost the static key pre-#389"

    (rotator,) = [d for d in _load("key-rotator-cronjob.yaml") if d["kind"] == "CronJob"]
    (rc,) = _pod_spec(rotator)["containers"]
    renv = {e["name"]: e.get("value") for e in rc.get("env", [])}
    assert renv.get("GRUG_ROTATE_SECRET") == "grug-aws-static-key"
    (role,) = [d for d in _load("key-rotator-cronjob.yaml") if d["kind"] == "Role"]
    (rule,) = [r for r in role["rules"] if "secrets" in r.get("resources", [])]
    assert rule["resourceNames"] == ["grug-aws-static-key"]


def test_kustomization_ships_the_new_manifests():
    (kust,) = _load("kustomization.yaml")
    for res in ("pki-certificate.yaml", "grug-aws-config.yaml"):
        assert res in kust["resources"]


def test_webhook_image_bakes_the_signing_helper():
    dockerfile = (K8S.parent / "services/webhook/Dockerfile").read_text()
    assert "rolesanywhere-credential-helper" in dockerfile
    assert "SIGNING_HELPER_VERSION=v" in dockerfile  # pinned tag, not a branch
    assert "COPY --from=signing-helper /aws_signing_helper /usr/local/bin/aws_signing_helper" in dockerfile


def test_paths_and_secret_names_are_cross_derived_not_coincidental():
    """Audit stage-1 MEDIUM: the mount paths, config path, and secret name
    each appeared in two artifacts as twice-hardcoded literals - a rename
    in one place + a matching test edit would leave the OTHER artifact
    stale and green. Derive each from its counterpart instead."""
    (cert,) = _load("pki-certificate.yaml")
    (cm,) = _load("grug-aws-config.yaml")
    (cron,) = [d for d in _load("poller-cronjob.yaml") if d["kind"] == "CronJob"]
    pod = _pod_spec(cron)
    (container,) = pod["containers"]
    mounts = {m["name"]: m for m in container["volumeMounts"]}
    vols = {v["name"]: v for v in pod["volumes"]}
    env = {e["name"]: e.get("value") for e in container.get("env", [])}

    # Certificate secret <-> poller volume: same Secret, by derivation.
    assert vols["grug-pki"]["secret"]["secretName"] == cert["spec"]["secretName"]
    # ConfigMap name <-> poller volume.
    assert vols["aws-config"]["configMap"]["name"] == cm["metadata"]["name"]
    # AWS_CONFIG_FILE = <config mount>/<the ConfigMap's single data key>.
    (data_key,) = cm["data"].keys()
    assert env["AWS_CONFIG_FILE"] == f"{mounts['aws-config']['mountPath']}/{data_key}"
    # credential_process paths = <pki mount>/<tls files>.
    line = next(l for l in cm["data"][data_key].splitlines() if l.startswith("credential_process"))
    pki_mount = mounts["grug-pki"]["mountPath"]
    for flag, fname in (("--certificate", "tls.crt"), ("--private-key", "tls.key"), ("--intermediates", "ca.crt")):
        assert f"{flag} {pki_mount}/{fname}" in line


def test_configmap_placeholders_match_the_deploy_sed_and_sentinel():
    """Audit stage-1 MEDIUM: the placeholder contract spans the ConfigMap,
    the deploy sed, and the post-sed sentinel. This test is the single
    arbiter: every *_PLACEHOLDER token in the ConfigMap must be sed-
    substituted by deploy.k8s.yml AND match the sentinel shape that
    fails the deploy if substitution is ever skipped."""
    import re

    (cm,) = _load("grug-aws-config.yaml")
    (data_key,) = cm["data"].keys()
    workflow = (K8S.parent / ".github/workflows/deploy.k8s.yml").read_text()
    tokens = set(re.findall(r"\b\w+_PLACEHOLDER\b", cm["data"][data_key]))
    assert tokens, "expected ARN placeholders in the ConfigMap"
    for tok in tokens:
        assert f"s|{tok}|" in workflow, f"{tok} has no sed substitution in deploy.k8s.yml"
        assert re.fullmatch(r"RA_.*_ARN_PLACEHOLDER", tok), (
            f"{tok} escapes the post-sed sentinel shape RA_.*_ARN_PLACEHOLDER"
        )


def test_rotator_covers_exactly_the_static_key_consumers():
    """Audit stage-1 LOW: the rotate list and the set of workloads carrying
    the static key must be the SAME set - a workload gaining the key
    without joining the rotation runs on a key deleted at the next cycle."""
    carriers = set()
    for manifest in ("api-deployment.yaml", "consumer-deployment.yaml", "webhook-deployment.yaml", "poller-cronjob.yaml"):
        for doc in _load(manifest):
            if doc["kind"] not in ("Deployment", "CronJob"):
                continue
            (c,) = _pod_spec(doc)["containers"]
            names = [e["secretRef"]["name"] for e in c.get("envFrom", []) if "secretRef" in e]
            if "grug-aws-static-key" in names:
                carriers.add(doc["metadata"]["name"])
    (rotator,) = [d for d in _load("key-rotator-cronjob.yaml") if d["kind"] == "CronJob"]
    (rc,) = _pod_spec(rotator)["containers"]
    renv = {e["name"]: e.get("value") for e in rc.get("env", [])}
    rotated = set(renv["GRUG_ROTATE_DEPLOYMENTS"].split(","))
    assert carriers == rotated, f"carriers {carriers} != rotated {rotated}"
