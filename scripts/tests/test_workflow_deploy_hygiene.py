"""Workflow hygiene invariants from #1626.

Each test pins one gap the audit found, so a later edit cannot reopen it:

1. web.deploy.yml publishes to production, so a manual dispatch must not
   choose an arbitrary ref and must run only from main (deploy.k8s.yml's
   guard).
2. No workflow triggers on a `feat/*` push: those runs die at the OIDC step
   today and would be a non-PR path to prod if the trust ever widened.
3. wrangler runs with the Cloudflare token in its env, so it is pinned to an
   exact version, like every other dependency in the repo.
4. One configure-aws-credentials SHA, the join-tailnet composite wherever
   the tailnet is joined, and one source for AWS_ACCOUNT_ID.
5. check.attesters.yml runs on every PR: the attesters read files all over
   the tree, and a path list drifted out of date.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOWS = _ROOT / ".github" / "workflows"


def _load(name: str) -> dict:
    return yaml.safe_load((_WORKFLOWS / name).read_text())


def _on(doc: dict) -> dict:
    on = doc.get("on", doc.get(True))  # YAML 1.1 reads a bare `on:` key as True
    return on or {}


def _all_workflows() -> list[Path]:
    files = sorted(_WORKFLOWS.glob("*.yml"))
    assert files, "no workflows found; the glob is wrong"
    return files


def test_web_deploy_dispatch_has_no_free_form_ref():
    doc = _load("web.deploy.yml")
    dispatch = _on(doc).get("workflow_dispatch") or {}
    assert "ref" not in (dispatch.get("inputs") or {})
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            assert "inputs.ref" not in str(step.get("with", {}).get("ref", ""))


def test_web_deploy_dispatch_runs_only_from_main():
    doc = _load("web.deploy.yml")
    guard = doc["jobs"]["build-and-deploy"].get("if", "")
    assert "github.event_name != 'workflow_dispatch'" in guard
    assert "github.ref == 'refs/heads/main'" in guard


def test_no_workflow_triggers_on_feat_push():
    offenders = []
    for path in _all_workflows():
        push = _on(yaml.safe_load(path.read_text())).get("push") or {}
        for branch in push.get("branches") or []:
            if str(branch).startswith("feat/"):
                offenders.append(f"{path.name}: {branch}")
    assert offenders == []


def test_iac_pulumi_up_has_no_empty_config_map():
    doc = _load("iac.deploy.yml")
    for step in doc["jobs"]["pulumi-up"]["steps"]:
        assert "config-map" not in (step.get("with") or {}), step.get("name")


def test_wrangler_is_pinned_exactly():
    text = (_WORKFLOWS / "web.deploy.yml").read_text()
    pins = re.findall(r"wrangler@(\S+)", text)
    assert pins, "web.deploy.yml no longer runs wrangler; update this test"
    for pin in pins:
        assert re.fullmatch(r"\d+\.\d+\.\d+", pin), pin


def test_configure_aws_credentials_uses_one_sha():
    shas = set()
    for path in _all_workflows():
        shas.update(
            re.findall(r"aws-actions/configure-aws-credentials@([0-9a-f]{40})", path.read_text())
        )
    assert len(shas) == 1, shas


def test_tailnet_join_goes_through_the_composite():
    offenders = [
        path.name
        for path in _all_workflows()
        if "uses: tailscale/github-action@" in path.read_text()
    ]
    assert offenders == []


def test_aws_account_id_has_one_source():
    offenders = [
        path.name for path in _all_workflows() if "vars.AWS_ACCOUNT_ID" in path.read_text()
    ]
    assert offenders == []


def test_attesters_run_on_every_pull_request():
    pr = _on(_load("check.attesters.yml")).get("pull_request") or {}
    assert "paths" not in pr
    assert "paths-ignore" not in pr
