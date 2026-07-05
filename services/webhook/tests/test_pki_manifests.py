"""Roles Anywhere tracer manifest pins (#388, ADR-0008).

Parses the REAL k8s manifests (same pattern as test_smasher_manifests) and
pins the contract values a well-meaning cleanup would break:

- The Certificate MUST carry BOTH `digital signature` AND `client auth`
  usages: `client auth` alone sets EKU but no keyUsage and Roles Anywhere
  rejects the leaf with "Insufficient certificate" (verified live,
  infrastructure#1318 Phase 4 - the gotcha that cost a failed attempt).
- NO workload receives the static AWS key pair (#389 fleet rollout):
  env credentials out-rank credential_process in the SDK chain, silently
  bypassing the path the boot proofs assert.
- The credential_process line must pass --intermediates FROM THE tls.crt
  BUNDLE (the trust anchor is the offline ROOT; ca.crt here is the ROOT,
  not the intermediate - live-debugged, see grug-aws-config.yaml).
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


def _secret_env_from(container: dict) -> list[str]:
    return [e["secretRef"]["name"] for e in container.get("envFrom", []) if "secretRef" in e]


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
    assert spec["privateKey"]["rotationPolicy"] == "Always"


def test_aws_config_credential_process_shape():
    (cm,) = _load("grug-aws-config.yaml")
    config = cm["data"]["config"]
    line = next(l for l in config.splitlines() if l.startswith("credential_process"))
    # Path flags are pinned by the cross-derived test; this one uniquely
    # pins the helper invocation + the ARN flag/placeholder pairings.
    for required in (
        "aws_signing_helper credential-process",
        "--trust-anchor-arn RA_TRUST_ANCHOR_ARN_PLACEHOLDER",
        "--profile-arn RA_PROFILE_ARN_PLACEHOLDER",
        "--role-arn RA_ROLE_ARN_PLACEHOLDER",
    ):
        assert required in line, f"missing from credential_process: {required}"

# DERIVED from k8s/ (audit #389-1): a 5th AWS-talking workload manifest
# joins the fleet test automatically instead of silently escaping a
# hand-list. The exclusions are the point: each names WHY it must never
# ride the Roles Anywhere path.
EXCLUDED_FROM_RA_FLEET = {
    "smasher-trial-namespace.yaml": "trial sandbox: token-free by design, no AWS",
}


def test_excluded_entries_reference_real_manifests():
    # A stale exclusion could later mask a NEW file reusing the name
    # (#389 audit stage-7).
    names = {f.name for f in K8S.glob("*.yaml")}
    assert set(EXCLUDED_FROM_RA_FLEET) <= names


def _fleet_manifests() -> list[str]:
    out = []
    for f in sorted(K8S.glob("*.yaml")):
        if f.name in EXCLUDED_FROM_RA_FLEET:
            continue
        if any(d.get("kind") in ("Deployment", "CronJob") for d in _load(f.name)):
            out.append(f.name)
    return out


def test_no_workload_carries_the_static_key_and_all_ride_roles_anywhere():
    """#389 rollout state: EVERY workload is on the cert path (mounts +
    AWS_CONFIG_FILE + GRUG_RA_ROLE_ARN) and NONE receives the static key
    Secret - env creds would out-rank credential_process and silently
    bypass the path the boot proofs assert."""
    for manifest in _fleet_manifests():
        for doc in _load(manifest):
            if doc["kind"] not in ("Deployment", "CronJob"):
                continue
            pod = _pod_spec(doc)
            (c,) = pod["containers"]
            name = doc["metadata"]["name"]
            env_from = _secret_env_from(c)
            assert "grug-aws-static-key" not in env_from, name
            assert "grug-secrets" in env_from, name  # app config rides along
            env = {e["name"]: e.get("value") for e in c.get("env", [])}
            # The single ABSOLUTE path anchor; the cross-derivation test
            # derives every other path from manifest counterparts.
            assert env.get("AWS_CONFIG_FILE") == "/etc/grug-aws/config", name
            # Sed-pinned from the same SSM role ARN the ConfigMap uses -
            # the exact-identity assertion input.
            assert env.get("GRUG_RA_ROLE_ARN") == "RA_ROLE_ARN_PLACEHOLDER", name
            assert "AWS_ACCESS_KEY_ID" not in env, name
            assert "AWS_SECRET_ACCESS_KEY" not in env, name
            mounts = {m["name"]: m for m in c["volumeMounts"]}
            assert mounts["grug-pki"]["mountPath"] == "/var/run/grug-pki", name
            assert mounts["grug-pki"].get("readOnly") is True, name
            assert mounts["aws-config"]["mountPath"] == "/etc/grug-aws", name
            assert mounts["aws-config"].get("readOnly") is True, name
            vols = {v["name"]: v for v in pod["volumes"]}
            assert vols["grug-pki"]["secret"]["secretName"] == "grug-pki-tls", name
            assert vols["aws-config"]["configMap"]["name"] == "grug-aws-config", name

def test_kustomization_ships_the_new_manifests():
    (kust,) = _load("kustomization.yaml")
    for res in ("pki-certificate.yaml", "grug-aws-config.yaml"):
        assert res in kust["resources"]


def test_webhook_image_bakes_the_signing_helper():
    # Unified single Dockerfile since #389 (ARG SERVICE selects the tree).
    dockerfile = (K8S.parent / "services/Dockerfile").read_text()
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
    # --intermediates = the tls.crt BUNDLE, never ca.crt: in this issuer
    # topology ca.crt is the ROOT and the chain 403s (live-debugged #388).
    for flag, fname in (("--certificate", "tls.crt"), ("--private-key", "tls.key"), ("--intermediates", "tls.crt")):
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

def test_deploy_sed_simulation_leaves_no_sentinel_matches():
    """Audit stage-8 CRITICAL (caught live): a COMMENT in a manifest
    mentioning a placeholder token literally would survive the deploy's
    sed (which only rewrites the real pin sites) and then trip the
    post-sed sentinel grep over ALL of k8s/ - failing every deploy
    BEFORE kubectl apply, with the secret seed already run. Simulate the
    deploy's exact substitutions over every manifest and assert the
    sentinel would pass."""
    import re

    sub = {
        "REGISTRY_PLACEHOLDER/grug-api:TAG_PLACEHOLDER": "reg.example/grug-api@sha256:aaaa",
        "REGISTRY_PLACEHOLDER/grug-webhook:TAG_PLACEHOLDER": "reg.example/grug-webhook@sha256:bbbb",
        "TAG_PLACEHOLDER": "deadbeef",
        "RA_TRUST_ANCHOR_ARN_PLACEHOLDER": "arn:aws:rolesanywhere:x:1:trust-anchor/t",
        "RA_PROFILE_ARN_PLACEHOLDER": "arn:aws:rolesanywhere:x:1:profile/p",
        "RA_ROLE_ARN_PLACEHOLDER": "arn:aws:iam::1:role/r",
    }
    sentinel = re.compile(r"REGISTRY_PLACEHOLDER|TAG_PLACEHOLDER|RA_.*_ARN_PLACEHOLDER")
    offenders = []
    for manifest in sorted(K8S.glob("*.yaml")):
        text = manifest.read_text()
        for old, new in sub.items():  # dict order mirrors the sed -e order
            text = text.replace(old, new)
        for i, line in enumerate(text.splitlines(), 1):
            if sentinel.search(line):
                offenders.append(f"{manifest.name}:{i}: {line.strip()[:80]}")
    assert not offenders, (
        "post-sed sentinel would fail the deploy on: " + "; ".join(offenders)
    )


def test_static_key_apparatus_is_fully_retired():
    """#389 retirement: no manifest, workflow, or component may reference
    the static-key world again. A single surviving reference is either a
    resurrection (drift) or a missed retirement site."""
    forbidden = (
        "grug-aws-static-key", "grug-rotator-secret",
        "key-rotator", "key_rotator",  # BOTH forms - the underscore one slipped this guard once
        "k8s-pod-aws",
    )
    offenders = []
    workflows = K8S.parent / ".github" / "workflows"
    # Guard the guard: a wrong path here silently yields zero files and
    # the workflow leg of the scan goes dead (it DID - audit #506-A).
    assert workflows.is_dir(), workflows
    # LINE-based scan, ZERO exemptions (#507: the one-time cleanup lines
    # are gone after the observed post-retirement deploy).
    for root in (K8S, workflows):
        for f in sorted(root.glob("*.y*ml")):
            for lineno, line in enumerate(f.read_text().splitlines(), 1):
                for needle in forbidden:
                    if needle in line:
                        offenders.append(f"{f.name}:{lineno}: {needle}")
    assert not offenders, f"static-key world referenced post-retirement: {offenders}"
