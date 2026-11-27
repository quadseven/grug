"""The production stack provisions the dashboard signing key consumed by the API."""

import runpy
from pathlib import Path

import pulumi
from pulumi.runtime import config as pulumi_config, rpc


_SIGNING_PATH = "/grug/session-signing-secret"


class _ProgramMocks(pulumi.runtime.Mocks):
    def __init__(self) -> None:
        self.parameters = []
        self.generated_passwords = set()

    def new_resource(self, args):
        outputs = dict(args.inputs)
        if args.typ == "random:index/randomPassword:RandomPassword":
            password = f"mock-generated-{args.name}"
            outputs["result"] = password
            self.generated_passwords.add(password)
        if args.typ == "aws:ssm/parameter:Parameter":
            self.parameters.append(args.inputs)
        return args.name + "_id", outputs

    def call(self, args):
        if args.token == "aws:ssm/getParameter:getParameter":
            name = args.args["name"]
            assert name != _SIGNING_PATH
            return {
                "name": name,
                "value": "mock-value",
                "arn": f"arn:aws:ssm:us-east-1:000000000000:parameter{name}",
            }
        if args.token == "aws:iam/getOpenIdConnectProvider:getOpenIdConnectProvider":
            return {"arn": "arn:aws:iam::000000000000:oidc-provider/token.actions.githubusercontent.com"}
        return {}


def test_production_stack_provisions_the_api_signing_key_as_a_secure_string() -> None:
    mocks = _ProgramMocks()

    @pulumi.runtime.test
    def deploy() -> None:
        pulumi.runtime.set_mocks(mocks, project="grug", stack="prod", preview=False)
        pulumi_config.set_all_config({"grug:env": "prod", "grug:domain": "example.com"})
        runpy.run_path(str(Path(__file__).resolve().parents[1] / "__main__.py"), run_name="__main__")

    deploy()

    signing_parameters = [p for p in mocks.parameters if p["name"] == _SIGNING_PATH]
    assert len(signing_parameters) == 1
    parameter = signing_parameters[0]
    assert parameter["type"] == "SecureString"
    assert rpc.is_rpc_secret(parameter["value"])
    signing_key = rpc.unwrap_rpc_secret(parameter["value"])
    assert signing_key in mocks.generated_passwords
    other_values = [rpc.unwrap_rpc_secret(p["value"]) for p in mocks.parameters if p is not parameter]
    assert signing_key not in other_values
