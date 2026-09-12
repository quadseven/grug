"""Synth pins for the leak-guard role (grug#921).

The guard's people/product layer needs AWS credentials, and the tempting
shortcut - trust `grug-gha-deploy` from a pull_request - would hand SSM write,
IAM and S3 to anyone who can open a PR. These tests pin the two properties
that keep the shortcut from creeping back in:

  1. the role reads exactly ONE parameter, never the `/grug/*` path that also
     holds the GitHub App private key and the database URL, and
  2. the workflow asks for the same parameter this role is scoped to - a drift
     there means the guard fetches a path it has no permission for, which
     surfaces as a red CI job rather than a silent downgrade, but is still
     better caught here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pulumi

_CAPTURED: dict[str, dict] = {}
_ACCOUNT = "000000000000"


class _PulumiMocks(pulumi.runtime.Mocks):
    def new_resource(self, args):  # type: ignore[override]
        _CAPTURED[args.name] = args.inputs
        return [args.name + "_id", args.inputs]

    def call(self, args):  # type: ignore[override]
        # noqa S105: `args.token` is Pulumi's resource-FUNCTION token (the
        # provider RPC name), not a credential. Same shape as
        # test_oidc_role.py's mock.
        if args.token == "aws:index/getCallerIdentity:getCallerIdentity":  # noqa: S105
            return {"accountId": _ACCOUNT, "id": _ACCOUNT, "arn": "arn:aws:iam::x:root"}
        return {}


# Mocks are installed per-test, NOT at import. `set_mocks` writes to ONE global
# slot, so a module that installs its mocks at import time has them silently
# replaced by whichever test module pytest imports last - and the captures land
# in the other module's dict, which reads as "the component synthesized
# nothing". Installing at the top of each synth keeps the modules independent.
pulumi.runtime.set_mocks(_PulumiMocks())

from components import leak_guard_role  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
GUARD_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "guard.private-leaks.yml"
DENY_LIST_PARAM = "/grug/leak-guard-deny-list"


def _synth():
    pulumi.runtime.set_mocks(_PulumiMocks())
    _CAPTURED.clear()
    return leak_guard_role.create(
        name="grug-gha-leak-guard-test",
        deny_list_param=DENY_LIST_PARAM,
        repos=["quadseven/grug"],
    )


def _policy_doc() -> dict:
    for inputs in _CAPTURED.values():
        if isinstance(inputs.get("policy"), str):
            return json.loads(inputs["policy"])
    raise AssertionError("no RolePolicy synthesized")


@pulumi.runtime.test
def test_role_can_read_only_the_one_deny_list_parameter():
    bundle = _synth()

    def check(_):
        doc = _policy_doc()
        ssm = [
            s for s in doc["Statement"]
            if any(a.startswith("ssm:") for a in s["Action"])
        ]
        assert len(ssm) == 1, ssm
        resources = ssm[0]["Resource"]
        assert resources == [
            f"arn:aws:ssm:*:{_ACCOUNT}:parameter{DENY_LIST_PARAM}"
        ], resources
        # The whole point: no path wildcard. `/grug/*` also holds the GitHub
        # App private key, the database URL and the Cloudflare API token.
        assert not any(r.endswith("/*") or r == "*" for r in resources)
        assert ssm[0]["Action"] == ["ssm:GetParameter"]

    return pulumi.Output.all(bundle.role.urn, bundle.policy.urn).apply(check)


@pulumi.runtime.test
def test_kms_decrypt_is_constrained_to_calls_arriving_through_ssm():
    bundle = _synth()

    def check(_):
        doc = _policy_doc()
        kms = [s for s in doc["Statement"] if any(a.startswith("kms:") for a in s["Action"])]
        assert len(kms) == 1, kms
        via = kms[0]["Condition"]["StringLike"]["kms:ViaService"]
        assert via == "ssm.*.amazonaws.com", via

    return pulumi.Output.all(bundle.role.urn, bundle.policy.urn).apply(check)


@pulumi.runtime.test
def test_trust_is_pull_request_only_and_not_a_branch_or_wildcard():
    """A branch/tag subject here would be dead weight; a `*` would be a hole."""
    bundle = _synth()

    def check(_):
        trust = json.loads(_CAPTURED["grug-gha-leak-guard-test"]["assumeRolePolicy"])
        cond = trust["Statement"][0]["Condition"]
        assert "StringLike" not in cond, cond
        subs = cond["StringEquals"]["token.actions.githubusercontent.com:sub"]
        assert subs == ["repo:quadseven/grug:pull_request"], subs

    return pulumi.Output.all(bundle.role.urn, bundle.policy.urn).apply(check)


def test_workflow_asks_for_the_parameter_this_role_is_scoped_to():
    text = GUARD_WORKFLOW.read_text(encoding="utf-8")
    assert f"DENY_LIST: {DENY_LIST_PARAM}" in text
    assert "role/grug-gha-leak-guard" in text
    # Never the deploy role: it can write SSM/IAM/S3 and this workflow runs on
    # pull_request. Assumed here rather than left to review.
    assert "role/grug-gha-deploy" not in text
