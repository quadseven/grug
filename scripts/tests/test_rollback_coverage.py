"""#499 (codex round 2): every image-bearing workload the deploy pins via
REGISTRY_PLACEHOLDER must be covered by BOTH rollback paths - a workload
added to k8s/ without joining the rollback keeps running the failed image
after a "successful" rollback (the grug-trial janitor was the live miss).

This is a config-coverage cross-check on data files (manifests vs the
rollback shell blocks), the same class as the monitor/emitter guard - not
a source-presence-as-behavior test.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]

_KIND_PATH = {
    "Deployment": ("spec", "template", "spec", "containers"),
    "CronJob": ("spec", "jobTemplate", "spec", "template", "spec", "containers"),
}


def _placeholder_consumers() -> set[tuple[str, str, str, str]]:
    """(namespace, kind-lower, name, container) for every container whose
    image carries REGISTRY_PLACEHOLDER across k8s/*.yaml."""
    out = set()
    for f in sorted((_ROOT / "k8s").glob("*.yaml")):
        for doc in yaml.safe_load_all(f.read_text()):
            if not doc or doc.get("kind") not in _KIND_PATH:
                continue
            node = doc
            for key in _KIND_PATH[doc["kind"]]:
                node = (node or {}).get(key)
            for c in node or []:
                if "REGISTRY_PLACEHOLDER" in str(c.get("image", "")):
                    out.add((
                        doc["metadata"].get("namespace", "grug"),
                        doc["kind"].lower(),
                        doc["metadata"]["name"],
                        c["name"],
                    ))
    return out


def _placeholder_env_consumers() -> set[tuple[str, str, str, str]]:
    """(namespace, kind-lower, name, ENV-NAME) for every container ENV VAR
    whose value carries REGISTRY_PLACEHOLDER - image pins ride env too
    (the Smasher job image; codex r4) and `set image` never touches them."""
    out = set()
    for f in sorted((_ROOT / "k8s").glob("*.yaml")):
        for doc in yaml.safe_load_all(f.read_text()):
            if not doc or doc.get("kind") not in _KIND_PATH:
                continue
            node = doc
            for key in _KIND_PATH[doc["kind"]]:
                node = (node or {}).get(key)
            for c in node or []:
                for e in c.get("env") or []:
                    if "REGISTRY_PLACEHOLDER" in str(e.get("value", "")):
                        out.add((
                            doc["metadata"].get("namespace", "grug"),
                            doc["kind"].lower(),
                            doc["metadata"]["name"],
                            e["name"],
                        ))
    return out


_SET_ENV = re.compile(
    r"kubectl\s+-n\s+(\S+)\s+set\s+env\s+(deploy|deployment|cronjob)/(\S+)\s+([A-Z0-9_]+)="
)


def _rollback_env_targets(workflow: str) -> set[tuple[str, str, str, str]]:
    text = (_ROOT / ".github" / "workflows" / workflow).read_text()
    out = set()
    for ns, kind, name, env_name in _SET_ENV.findall(text):
        kind = "deployment" if kind in ("deploy", "deployment") else kind
        out.add((ns, kind, name, env_name))
    return out


def test_every_placeholder_env_consumer_is_rolled_back():
    consumers = _placeholder_env_consumers()
    assert consumers, "no REGISTRY_PLACEHOLDER env consumers found - parser broke?"
    for wf in ("deploy.k8s.yml", "deploy.rollback.yml"):
        targets = _rollback_env_targets(wf)
        missing = consumers - targets
        assert not missing, (
            f"{wf} rollback misses image-bearing env vars: {sorted(missing)}"
        )


_SET_IMAGE = re.compile(
    r"kubectl\s+-n\s+(\S+)\s+set\s+image\s+(deploy|deployment|cronjob)/(\S+)\s+(\S+)="
)


def _rollback_targets(workflow: str) -> set[tuple[str, str, str, str]]:
    text = (_ROOT / ".github" / "workflows" / workflow).read_text()
    out = set()
    for ns, kind, name, container in _SET_IMAGE.findall(text):
        kind = "deployment" if kind in ("deploy", "deployment") else kind
        out.add((ns, kind, name, container))
    return out


def test_every_placeholder_consumer_is_rolled_back():
    consumers = _placeholder_consumers()
    assert consumers, "no REGISTRY_PLACEHOLDER consumers found - parser broke?"
    for wf in ("deploy.k8s.yml", "deploy.rollback.yml"):
        targets = _rollback_targets(wf)
        missing = consumers - targets
        assert not missing, (
            f"{wf} rollback misses image-bearing workloads: {sorted(missing)}"
        )


def test_deploy_failure_lattice_structure():
    """#499 (codex r6+r10): the deploy's mutation steps must form the
    capture lattice - anchor BEFORE the destructive seed (a pre-mutation
    failure is a safe abort), seed+apply continue-on-error, and the
    rollback condition referencing all three outcomes. Ordering/structure
    pin on the workflow data, so a refactor cannot silently strand
    mutated secrets outside the restore path again."""
    text = (_ROOT / ".github" / "workflows" / "deploy.k8s.yml").read_text()
    i_anchor = text.index("Record last-good digests")
    i_seed = text.index("Seed app secrets")
    i_apply = text.index("Pin image placeholders + apply")
    i_rollback = text.index("Auto-rollback to last-good")
    assert i_anchor < i_seed < i_apply < i_rollback

    def step_block(marker: str) -> str:
        start = text.index(marker)
        nxt = text.find("\n      - name:", start)
        return text[start:nxt]

    assert "continue-on-error: true" in step_block("Seed app secrets")
    assert "continue-on-error: true" in step_block("Pin image placeholders + apply")
    rb = step_block("Auto-rollback to last-good")
    for outcome in ("steps.seed.outcome == 'failure'",
                    "steps.apply.outcome == 'failure'",
                    "steps.synthetic.outcome == 'failure'"):
        assert outcome in rb, f"rollback missing trigger: {outcome}"


def test_rollback_incomplete_flag_gates_completed_telemetry():
    """#499 (codex r12): a failed-snapshot restore must poison the
    completed signal - the flag is set in the failed branch and consumed
    before the phase metric."""
    text = (_ROOT / ".github" / "workflows" / "deploy.k8s.yml").read_text()
    start = text.index("Auto-rollback to last-good")
    block = text[start:text.find("\n      - name:", start)]
    assert "RESTORE_INCOMPLETE=1" in block
    assert block.index("RESTORE_INCOMPLETE=1") < block.index("phase=")
    assert 'RESTORE_INCOMPLETE:-0' in block and "incomplete" in block


def test_release_bundle_generations_structure():
    """#499 (codex r13): the verified-release bundle is recorded AFTER the
    synthetic (two-generation shift) and both rollback paths consume the
    PREVIOUS generation before the image re-pins."""
    text = (_ROOT / ".github" / "workflows" / "deploy.k8s.yml").read_text()
    i_synth = text.index("Post-deploy synthetic self-test")
    i_bundle = text.index("Record verified release bundle")
    i_rollback = text.index("Auto-rollback to last-good")
    assert i_synth < i_bundle < i_rollback
    assert "steps.synthetic.outcome == 'success'" in text[i_bundle:i_rollback]
    # AUTO-rollback (deploy.k8s) restores the RUNNING verified release =
    # 'current' (pairs with the pre-mutation grug-last-good images = same
    # generation, no hybrid); MANUAL rollback (deploy.rollback) leaves the
    # running release, so it restores 'previous' (codex r14).
    auto = (_ROOT / ".github" / "workflows" / "deploy.k8s.yml").read_text()
    i_gen = auto.index("{.data.current}", auto.index("Auto-rollback to last-good"))
    i_pin = auto.index("set image deploy/grug-webhook", auto.index("Auto-rollback to last-good"))
    assert i_gen < i_pin, "auto-rollback: bundle must apply before image re-pins"
    manual = (_ROOT / ".github" / "workflows" / "deploy.rollback.yml").read_text()
    i_prev = manual.index("{.data.previous}")
    i_pin_m = manual.index("set image deploy/grug-webhook")
    assert i_prev < i_pin_m, "manual rollback: bundle must apply before image re-pins"
