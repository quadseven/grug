"""The shared conftest pins fake AWS credentials at import (#1340).

Service modules build boto3 clients at import, i.e. at collection, before
any fixture runs. Without keys in the environment botocore walks the
credential chain into the developer's ambient profile: a login or SSO
profile then fails collection with MissingDependencyException, and a
static-key profile would hand real credentials to a moto-backed suite.
CI only passed because check.python.yml sets the fake keys at job level.

The probe runs in a subprocess with CI's keys stripped and an ambient
profile holding static keys, so it fails in CI too when the pin is gone.
Mirrored in services/api/tests/test_hermetic_aws_env.py.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SHARED_CONFTEST = (
    Path(__file__).resolve().parents[2] / "_shared" / "grug_shared_conftest.py"
)

PROBE = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location("grug_shared_conftest", sys.argv[1])
spec.loader.exec_module(importlib.util.module_from_spec(spec))
import boto3
session = boto3.Session()
print(session.get_credentials().access_key, session.region_name)
"""


def test_conftest_import_shadows_an_ambient_profile(tmp_path):
    aws_dir = tmp_path / ".aws"
    aws_dir.mkdir()
    (aws_dir / "config").write_text(
        "[profile laptop]\n"
        "aws_access_key_id = REALKEY\n"
        "aws_secret_access_key = REALSECRET\n"
        "region = eu-west-3\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    env.update(
        HOME=str(tmp_path),
        AWS_PROFILE="laptop",
        AWS_CONFIG_FILE=str(aws_dir / "config"),
        AWS_SHARED_CREDENTIALS_FILE=str(aws_dir / "credentials"),
    )

    out = subprocess.run(
        [sys.executable, "-c", PROBE, str(SHARED_CONFTEST)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout.split()

    assert out == ["testing", "us-east-1"]
