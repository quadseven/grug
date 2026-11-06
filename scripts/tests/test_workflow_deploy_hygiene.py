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
5. check.attesters.yml's path filter reaches every path an attester reads;
   the old list drifted and PRs touching unlisted files skipped the run.
"""

from __future__ import annotations

import ast
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


# The weekly fleet drift report lists any pin behind the fleet majority. These
# floors hold the pins it last flagged here, so a
# copy-pasted old step cannot bring one back. Raise a floor when the fleet
# majority moves; a newer pin always passes.
_ACTION_VERSION_FLOORS = {
    "actions/upload-artifact": (7, 0, 2),
    "aws-actions/configure-aws-credentials": (6, 3, 0),
}


def _version(comment: str) -> tuple[int, ...]:
    parts = [int(p) for p in comment.lstrip("v").split(".")]
    return tuple(parts + [0] * (3 - len(parts)))


def test_action_pins_meet_the_fleet_floor():
    pattern = re.compile(r"uses:\s*([\w.-]+/[\w.-]+)@[0-9a-f]{40}\s*#\s*(v[\d.]+)")
    seen = set()
    offenders = []
    for path in _all_workflows():
        for action, comment in pattern.findall(path.read_text()):
            floor = _ACTION_VERSION_FLOORS.get(action)
            if floor is None:
                continue
            seen.add(action)
            if _version(comment) < floor:
                offenders.append(f"{path.name}: {action} {comment}")
    assert seen == set(_ACTION_VERSION_FLOORS), seen
    assert offenders == []


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


def _glob_re(glob: str) -> re.Pattern[str]:
    """A GitHub `paths:` glob as a regex: `**` crosses `/`, `*` and `?` do not."""
    out = []
    i = 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


# Paths an attester reads that are not plain existing-path literals in its
# source: attest_no_vendor_names.py's SCAN_ROOTS (directory roots, probed
# with a file inside) and the files attest_mirror_policy_consistency.py
# asserts stay deleted (so they are not on disk to be found).
_EXTRA_READ_PATHS = (
    "services/probe.py",
    "web/src/probe.ts",
    "infra/probe.py",
    "specs/probe.md",
    "scripts/check-mirrored-files.sh",
    ".github/workflows/check.drift-lint.yml",
)

# Literals that name a path an attester skips rather than reads:
# attest_no_vendor_names.py's EXEMPT_PATHS entry outside its scan roots.
_NOT_READ = frozenset({"docs/research/probe.py"})

# The filter before #1626. The falsification test asserts the coverage
# check rejects it, naming the files the ticket listed.
_PRE_1626_FILTER = [
    "infra/scripts/attest_*.py", "services/**/personas/tpm/**",
    "services/**/adapters/**", "services/**/github_checks_client.py",
    "services/**/github_reviews_client.py",
    "services/**/personas/code_reviewer/**", "services/**/llm_client.py",
    "services/**/crypto/**", "services/**/github_app_auth/**",
    "services/**/dispatcher.py", "web/public/**", "web/index.html",
    "web/app.html", "web/vite.config.ts", "web/src/main.tsx",
    "web/src/rum.ts", "web/package.json",
    ".github/workflows/check.attesters.yml",
]


def _attester_read_paths() -> set[str]:
    """Each repo path an attester names as a string literal and that exists,
    plus the attesters themselves and _EXTRA_READ_PATHS. A directory is
    probed with a synthetic file inside it."""
    paths = set(_EXTRA_READ_PATHS)
    scripts = sorted((_ROOT / "infra" / "scripts").glob("attest_*.py"))
    assert scripts, "no attesters found; the glob is wrong"
    for script in scripts:
        paths.add(script.relative_to(_ROOT).as_posix())
        for node in ast.walk(ast.parse(script.read_text())):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            text = node.value.strip().rstrip("/")
            if "/" not in text or " " in text or "{" in text:
                continue
            target = _ROOT / text
            if target.is_file():
                paths.add(text)
            elif target.is_dir():
                paths.add(f"{text}/probe.py")
    return paths - _NOT_READ


def _uncovered(filters: list[str]) -> list[str]:
    rxs = [_glob_re(g) for g in filters]
    return sorted(p for p in _attester_read_paths() if not any(rx.match(p) for rx in rxs))


def test_attesters_filter_covers_every_path_they_read():
    pr = _on(_load("check.attesters.yml")).get("pull_request") or {}
    assert "paths-ignore" not in pr
    filters = pr.get("paths")
    if filters is None:
        return  # no filter: the attesters run on every PR
    assert _uncovered(filters) == []


def test_coverage_check_rejects_the_pre_1626_filter():
    missed = _uncovered(_PRE_1626_FILTER)
    for path in (
        "services/_shared/cf_auth.py",
        "services/api/main.py",
        "services/webhook/main.py",
        "services/api/installations.py",
        "services/_shared/personas/review_publish.py",
        "web/src/routes/Dashboard.tsx",
        "infra/cloudflare/deploy.sh",
    ):
        assert path in missed, path
