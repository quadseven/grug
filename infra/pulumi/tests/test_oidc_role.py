"""Synth pin for the deploy role's SSM read scope (#388).

The scoped grug-gha-deploy role passes `pulumi preview` and then dies at
APPLY time one missing grant at a time (the #88 / audit-6 class), so every
path the deploy WORKFLOW reads must be pinned here - a widened or dropped
entry is a red test, not a mid-deploy 403. #388 added
/infra/roles-anywhere/* (the tenant ARNs deploy.k8s.yml seeds into the
grug-aws-config ConfigMap); this test is its pin AND the allowlist that
stops the exception from silently widening.

Behavior-level per standard-testing Rule 11: the assertion reads the
RolePolicy document the component actually synthesizes under Pulumi mocks,
not the source text.
"""

from __future__ import annotations

import json

import pulumi

_CAPTURED: dict[str, dict] = {}


class _PulumiMocks(pulumi.runtime.Mocks):
    def new_resource(self, args):  # type: ignore[override]
        _CAPTURED[args.name] = args.inputs
        return [args.name + "_id", args.inputs]

    def call(self, args):  # type: ignore[override]
        if args.token == "aws:iam/getOpenIdConnectProvider:getOpenIdConnectProvider":
            return {"arn": "arn:aws:iam::000000000000:oidc-provider/token.actions.githubusercontent.com"}
        return {}


pulumi.runtime.set_mocks(_PulumiMocks())

from components import oidc_role  # noqa: E402


EXPECTED_SSM_READ_PATHS = {
    "arn:aws:ssm:*:*:parameter/grug/*",
    "arn:aws:ssm:*:*:parameter/shared/*",
    "arn:aws:ssm:*:*:parameter/infra/datadog/*",
    "arn:aws:ssm:*:*:parameter/infra/llm/*",
    "arn:aws:ssm:*:*:parameter/infra/discord/*",
    # #388: Roles Anywhere tenant ARNs for the grug-aws-config seed.
    "arn:aws:ssm:*:*:parameter/infra/roles-anywhere/*",
}


@pulumi.runtime.test
def test_deploy_role_ssm_scope_is_exactly_the_pinned_set():
    # Re-install THIS module's mocks: `set_mocks` is a single global slot, so
    # another test module importing later would otherwise capture this
    # component's resources into its own dict and leave `_CAPTURED` empty.
    pulumi.runtime.set_mocks(_PulumiMocks())
    _CAPTURED.clear()
    bundle = oidc_role.create(
        name="grug-gha-deploy-test",
        repo="quadseven/grug",
        branches=["main"],
    )

    def check(_):
        policies = {
            name: inputs
            for name, inputs in _CAPTURED.items()
            if "policy" in inputs and isinstance(inputs.get("policy"), str)
        }
        assert policies, "no RolePolicy synthesized"
        ssm_statements = []
        for inputs in policies.values():
            doc = json.loads(inputs["policy"])
            for stmt in doc.get("Statement", []):
                actions = stmt.get("Action")
                actions = [actions] if isinstance(actions, str) else actions
                if any(a.startswith("ssm:GetParameter") for a in actions):
                    res = stmt.get("Resource")
                    # Normalize: a string Resource (e.g. "*") must be SEEN
                    # by the allowlist, not skipped past it.
                    ssm_statements.append(set(res if isinstance(res, list) else [res]))
        assert ssm_statements, "no ssm:GetParameter* statement found"
        # ONE statement by design: a split-statement refactor must be a
        # conscious test edit, not a silent second allowlist.
        assert len(ssm_statements) == 1, f"expected 1 ssm statement, got {len(ssm_statements)}"
        (scoped,) = ssm_statements
        assert "*" not in scoped, "wildcard SSM resource defeats the allowlist"
        assert scoped == EXPECTED_SSM_READ_PATHS, (
            f"deploy-role SSM scope drifted.\n  extra: {scoped - EXPECTED_SSM_READ_PATHS}"
            f"\n  missing: {EXPECTED_SSM_READ_PATHS - scoped}"
        )

    # The ordering guarantee is the Sleep's DATAFLOW dependency on the
    # RolePolicy (triggers={"role_policy_id": deploy_policy.id} in
    # oidc_role.py) - awaiting its urn means the policy registration has
    # landed in _CAPTURED. That trigger is load-bearing for this test; if
    # it is ever dropped, the `assert policies` anchor fails loud (never
    # vacuous), and this chain must find a new late output.
    return pulumi.Output.all(
        bundle.role.urn, bundle.iam_propagation_wait.urn
    ).apply(check)


@pulumi.runtime.test
def test_deploy_role_can_list_instance_profiles_before_deleting_a_role():
    """grug#952: AWS's DeleteRole internally calls ListInstanceProfilesForRole
    before it will delete a role, even with nothing attached. Missing the
    grant 403s the delete outright, which failed `pulumi up` on EVERY push
    once grug-gha-leak-guard was removed from source (page-tier CI-dead
    alert, 2026-09-12) - a role this scope already grants iam:DeleteRole on
    could not actually be deleted.

    Pinned as a presence check on the IAM-lifecycle statement (found by its
    iam:DeleteRole grant), not the full pinned-set shape
    test_deploy_role_ssm_scope_is_exactly_the_pinned_set uses for SSM - this
    regression is about one missing "list before delete" action, not the
    whole action list drifting."""
    _CAPTURED.clear()
    bundle = oidc_role.create(
        name="grug-gha-deploy-test",
        repo="quadseven/grug",
        branches=["main"],
    )

    def check(_):
        policies = {
            name: inputs
            for name, inputs in _CAPTURED.items()
            if "policy" in inputs and isinstance(inputs.get("policy"), str)
        }
        assert policies, "no RolePolicy synthesized"
        iam_lifecycle_statements = []
        for inputs in policies.values():
            doc = json.loads(inputs["policy"])
            for stmt in doc.get("Statement", []):
                actions = stmt.get("Action")
                actions = [actions] if isinstance(actions, str) else actions
                if any(a == "iam:DeleteRole" for a in actions):
                    iam_lifecycle_statements.append(stmt)
        assert iam_lifecycle_statements, "no iam:DeleteRole statement found"
        assert len(iam_lifecycle_statements) == 1, (
            f"expected 1 IAM-lifecycle statement, got {len(iam_lifecycle_statements)}"
        )
        (stmt,) = iam_lifecycle_statements
        assert "iam:ListInstanceProfilesForRole" in stmt["Action"], (
            "grug-gha-deploy can DeleteRole but not ListInstanceProfilesForRole - "
            "AWS requires the latter before it will process the former, so "
            "every delete of a role in this scope 403s"
        )
        resources = stmt.get("Resource")
        resources = [resources] if isinstance(resources, str) else resources
        assert "arn:aws:iam::*:role/grug-*" in resources, (
            "the grant must cover the same role/grug-* scope DeleteRole "
            "already has, or it does nothing for the roles that need it"
        )

    return pulumi.Output.all(
        bundle.role.urn, bundle.iam_propagation_wait.urn
    ).apply(check)
