"""Pins the retirement of grug's own leak-guard role (grug#1057).

The private-leaks guard used to read a per-repo deny-list
(`/grug/leak-guard-deny-list`) through a per-repo read role
(`grug-gha-leak-guard`). Since grug#1058 the guard reads the fleet's shared
list, `/infra/leak-scan/deny-list`, through the shared role in the
LEAK_SCAN_ROLE_ARN secret, so nothing assumes the old role. Keeping it
declared meant a second, drifting copy of the list stayed one edit away from
being wired back in. These tests fail if the stack declares it again.

The one property the old role's tests guarded that still matters is kept
here: the guard workflow runs on `pull_request`, so it must never assume
`grug-gha-deploy`, which can write SSM, IAM and S3.
"""

from __future__ import annotations

from pathlib import Path

PULUMI_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PULUMI_ROOT.parents[1]
MAIN = PULUMI_ROOT / "__main__.py"
GUARD_WORKFLOWS = [
    REPO_ROOT / ".github" / "workflows" / "guard.private-leaks.yml",
    REPO_ROOT / ".github" / "workflows" / "guard.private-leaks-full-tree.yml",
]


def _code_lines(path: Path) -> str:
    """Source with comment lines dropped, so a historical note is not a hit."""
    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def test_stack_declares_no_per_repo_leak_guard_role():
    code = _code_lines(MAIN)
    assert "leak_guard_role" not in code
    assert "grug-gha-leak-guard" not in code
    assert "/grug/leak-guard-deny-list" not in code


def test_leak_guard_component_is_removed():
    assert not (PULUMI_ROOT / "components" / "leak_guard_role.py").exists()


def test_guard_workflows_read_the_shared_list_and_never_the_deploy_role():
    for wf in GUARD_WORKFLOWS:
        text = wf.read_text(encoding="utf-8")
        assert "deny-list-ssm-param: /infra/leak-scan/deny-list" in text, wf.name
        assert "aws-role-arn: ${{ secrets.LEAK_SCAN_ROLE_ARN }}" in text, wf.name
        # Never the deploy role: it can write SSM/IAM/S3 and these workflows
        # run on pull_request.
        assert "role/grug-gha-deploy" not in text, wf.name
        assert "grug-gha-leak-guard" not in _code_lines(wf), wf.name
