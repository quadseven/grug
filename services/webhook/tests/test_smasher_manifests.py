"""Smasher k8s-manifest regression guards (#469, peer-review PR #494).

These are the accepted source-presence exception (standard-testing Rule 11):
infra YAML has no executable test seam, and these two properties are
security-load-bearing config that a silent edit could drop -
  - the grug-trial namespace enforces the `restricted` Pod Security Standard at
    the apiserver (blocks a compromised launcher creating a privileged/hostPath
    Job - CNI-independent), and
  - the test pod's egress is denied while only the prep phase opens DNS+443.
"""

from __future__ import annotations

from pathlib import Path

_MANIFEST = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "k8s" / "smasher-trial-namespace.yaml"
)


def _text() -> str:
    return _MANIFEST.read_text()


def test_grug_trial_enforces_restricted_pod_security():
    text = _text()
    assert "pod-security.kubernetes.io/enforce: restricted" in text


def test_egress_default_deny_with_prep_only_opening_443():
    text = _text()
    # default-deny egress for all pods...
    assert "name: default-deny-egress" in text
    # ...and only the prep phase re-opens DNS+443 (the test pod stays denied).
    assert "name: allow-egress-prep" in text
    assert "grug-trial-phase: prep" in text


def test_janitor_uses_an_sa_defined_in_its_own_namespace():
    # SAs are namespace-scoped: the janitor CronJob (in grug-trial) must NOT
    # reference the launcher SA (which lives in `grug`), or it won't start
    # (codex peer-review PR #494). It must use grug-trial-janitor, defined here.
    text = _text()
    assert "serviceAccountName: grug-trial-janitor" in text
    assert "kind: ServiceAccount\nmetadata:\n  name: grug-trial-janitor\n  namespace: grug-trial" in text
    # The CronJob must NOT reference the grug-namespace launcher SA.
    assert "serviceAccountName: grug-smasher-launcher" not in text


def test_launcher_role_lives_in_grug_trial():
    # The launcher's Role + RoleBinding must be in grug-trial (secret-free); the
    # only reference to the `grug` namespace is the RoleBinding SUBJECT (the SA
    # lives in grug, its permissions live here).
    text = _text()
    assert "kind: Role\nmetadata:\n  name: grug-smasher-launcher\n  namespace: grug-trial" in text
    assert "kind: RoleBinding\nmetadata:\n  name: grug-smasher-launcher\n  namespace: grug-trial" in text


def _policy(text: str, name: str) -> str:
    """The YAML document holding the NetworkPolicy `name`."""
    for doc in text.split("\n---\n"):
        if "kind: NetworkPolicy" in doc and f"\n  name: {name}\n" in doc:
            return doc
    raise AssertionError(f"no NetworkPolicy named {name}")


def test_janitor_egress_opens_the_api_server_endpoint_port():
    # An enforcer matches the port after the Service DNAT: the API Service is
    # 443 but its endpoint listens on 6443, so a 443-only rule cuts the
    # janitor off from the API the moment enforcement is turned on.
    assert "port: 6443" in _policy(_text(), "allow-egress-janitor")


def test_prep_and_test_phases_get_no_api_server_port():
    # Only the janitor needs the API. The prep pod fetches over 443 and the
    # test pod runs author code fully offline.
    text = _text()
    assert "6443" not in _policy(text, "allow-egress-prep")
    assert "egress:" not in _policy(text, "default-deny-egress")


def test_grug_namespace_egress_opens_the_api_server_endpoint_port():
    # The webhook and consumer (Smasher launcher SA) and the preview janitor
    # call the API from the grug namespace.
    grug = (_MANIFEST.parent / "networkpolicy.yaml").read_text()
    assert "port: 6443" in _policy(grug, "allow-egress-scoped")
