"""Deployment-specific values come from one place (#1635).

The AWS region and the public domain are read by scripts/deploy_config.sh
(a GRUG_AWS_REGION / GRUG_DOMAIN variable, else the Pulumi stack file), and
the App bot allowlist is a ConfigMap entry. These tests pin that the deploy
workflows, scripts, Makefile and manifests read them instead of carrying
literals, so a stranger's fork deploys with its own values.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "deploy_config.sh"
_WORKFLOWS = _ROOT / ".github" / "workflows"
_DEPLOY_WORKFLOWS = (
    "deploy.k8s.yml",
    "iac.deploy.yml",
    "preview.yml",
    "web.deploy.yml",
    "grugthink.deploy.yml",
)

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")


def _run(key: str, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    full = {k: v for k, v in os.environ.items() if not k.startswith("GRUG_")}
    full.update(env or {})
    return subprocess.run(
        [shutil.which("bash"), _SCRIPT, key, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=full,
        cwd=_ROOT,
    )


def _stack_file(tmp_path: Path, region: str = "eu-west-1", domain: str = "grug.example.test") -> Path:
    f = tmp_path / "Pulumi.test.yaml"
    f.write_text(f"config:\n  aws:region: {region}\n  grug:domain: {domain}\n")
    return f


@needs_bash
def test_reads_region_and_domain_from_the_stack_file(tmp_path):
    env = {"GRUG_PULUMI_STACK_FILE": _stack_file(tmp_path).as_posix()}
    assert _run("region", env=env).stdout.strip() == "eu-west-1"
    assert _run("domain", env=env).stdout.strip() == "grug.example.test"


@needs_bash
def test_environment_variable_overrides_the_stack_file(tmp_path):
    env = {
        "GRUG_PULUMI_STACK_FILE": _stack_file(tmp_path).as_posix(),
        "GRUG_AWS_REGION": "ap-south-1",
        "GRUG_DOMAIN": "mine.example.org",
    }
    assert _run("region", env=env).stdout.strip() == "ap-south-1"
    assert _run("domain", env=env).stdout.strip() == "mine.example.org"


@needs_bash
def test_missing_value_fails_loudly(tmp_path):
    empty = tmp_path / "Pulumi.empty.yaml"
    empty.write_text("config: {}\n")
    r = _run("domain", env={"GRUG_PULUMI_STACK_FILE": empty.as_posix()})
    assert r.returncode == 1 and r.stdout == ""


@needs_bash
@pytest.mark.parametrize("bad", ["a;b", "x y", "$(id)", "a/b", "-bad", "quote\"d"])
def test_value_that_is_not_a_plain_token_is_rejected(bad):
    r = _run("domain", env={"GRUG_DOMAIN": bad})
    assert r.returncode == 1 and r.stdout == ""


@needs_bash
def test_unknown_key_is_a_usage_error():
    assert _run("bogus").returncode == 2


@needs_bash
def test_prod_stack_resolves_for_the_checked_in_deployment():
    # The hosted stack file is the one place those values live.
    assert _run("region").stdout.strip()
    assert _run("domain").stdout.strip()


def _files_that_must_not_carry_literals() -> list[Path]:
    files = [_WORKFLOWS / n for n in _DEPLOY_WORKFLOWS]
    files += [_ROOT / "Makefile", _ROOT / "k8s" / "webhook-deployment.yaml"]
    files += sorted((_ROOT / "infra" / "cloudflare").glob("*.sh"))
    files += sorted((_ROOT / "k8s").glob("*.yaml"))
    files += sorted((_ROOT / "k8s" / "preview").glob("*.yaml"))
    return files


@needs_bash
def test_no_region_or_domain_literal_in_deploy_surfaces():
    region = _run("region").stdout.strip()
    domain = _run("domain").stdout.strip()
    assert region and domain
    for f in _files_that_must_not_carry_literals():
        text = f.read_text(encoding="utf-8")
        assert region not in text, f"{f.relative_to(_ROOT)} carries the region literal"
        assert domain not in text, f"{f.relative_to(_ROOT)} carries the domain literal"


def test_every_deploy_workflow_resolves_config_before_aws_credentials():
    for name in _DEPLOY_WORKFLOWS:
        doc = yaml.safe_load((_WORKFLOWS / name).read_text(encoding="utf-8"))
        found = False
        for job in doc["jobs"].values():
            steps = job.get("steps") or []
            aws_at = [
                i
                for i, s in enumerate(steps)
                if "configure-aws-credentials" in str(s.get("uses", ""))
            ]
            for i in aws_at:
                found = True
                before = [s for s in steps[:i] if s.get("id") == "cfg"]
                assert before, f"{name}: AWS credentials configured before the config step"
                cfg = before[0]
                assert "scripts/deploy_config.sh" in cfg["run"]
                assert steps[i]["with"]["aws-region"] == "${{ steps.cfg.outputs.region }}"
        assert found, f"{name}: no configure-aws-credentials step; update this test"


def test_manifest_region_placeholders_are_substituted_by_their_workflows():
    deploy = (_WORKFLOWS / "deploy.k8s.yml").read_text(encoding="utf-8")
    assert "s|AWS_REGION_PLACEHOLDER|" in deploy
    assert "AWS_REGION_PLACEHOLDER" in (_ROOT / "k8s" / "grug-aws-config.yaml").read_text(encoding="utf-8")
    preview = (_WORKFLOWS / "preview.yml").read_text(encoding="utf-8")
    assert "s|PREVIEW_AWS_REGION|" in preview
    assert "PREVIEW_AWS_REGION" in (_ROOT / "k8s" / "preview" / "deployments.yaml").read_text(encoding="utf-8")


def test_webhook_bot_allowlist_is_wired_from_configuration():
    docs = list(yaml.safe_load_all((_ROOT / "k8s" / "webhook-deployment.yaml").read_text(encoding="utf-8")))
    dep = next(d for d in docs if d and d.get("kind") == "Deployment")
    container = next(c for c in dep["spec"]["template"]["spec"]["containers"] if c["name"] == "webhook")
    env = {e["name"]: e for e in container["env"]}
    entry = env["GRUG_ALLOWED_APP_BOT_LOGINS"]
    assert "value" not in entry, "the allowlist must not be an inline literal"
    ref = entry["valueFrom"]["configMapKeyRef"]
    assert ref == {"name": "grug-webhook-config", "key": "allowed_app_bot_logins", "optional": True}

    deploy = (_WORKFLOWS / "deploy.k8s.yml").read_text(encoding="utf-8")
    assert "vars.GRUG_ALLOWED_APP_BOT_LOGINS" in deploy
    assert "create configmap grug-webhook-config" in deploy
    assert "allowed_app_bot_logins=" in deploy
