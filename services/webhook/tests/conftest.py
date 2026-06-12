"""pytest config for services/webhook/ tests.

Adds the parent directory to sys.path so tests can `from hmac_verify
import ...` without a package install (handler files live alongside the
tests folder, not under a package).

Also exposes `mock_transport_client` + `raise_status_error` helpers
that build a real `httpx.Client` whose request handler is driven by
`httpx.MockTransport`. Closes the mock-vs-real gap from
async-blocker-hunter F-01 (issue #105): direct construction of
`httpx.HTTPStatusError(...)` keeps tests green even if the production
`except` clause silently narrows to a sub-class. With MockTransport,
the exception comes from real httpx machinery.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import boto3
import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def _ddb_table(monkeypatch):
    """moto DDB for all webhook tests — prevents accidental prod DDB hits.
    
    install_store uses lazy DDB init (boto3.resource on first _table access),
    so moto's mock_aws is picked up naturally — no module reload needed.
    """
    moto = pytest.importorskip("moto")
    from moto import mock_aws  # type: ignore

    with mock_aws():
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="grug-main",  # matches install_store's _TABLE_NAME default
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "GSI1",
                "KeySchema": [
                    {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        yield


def _build_handler(
    *,
    status_codes: list[int] | None = None,
    raise_exc: BaseException | None = None,
    json_bodies: list[Any] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Build a MockTransport handler that returns / raises in sequence."""

    seq_status = list(status_codes or [])
    seq_json = list(json_bodies or [])
    idx = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if raise_exc is not None:
            raise raise_exc
        i = idx["n"]
        idx["n"] += 1
        status = seq_status[i] if i < len(seq_status) else (seq_status[-1] if seq_status else 200)
        body = seq_json[i] if i < len(seq_json) else (seq_json[-1] if seq_json else {})
        return httpx.Response(status, json=body)

    return handler


@pytest.fixture
def mock_transport_client() -> Iterator[Callable[..., httpx.Client]]:
    """Factory fixture: build a real httpx.Client backed by MockTransport.

    Usage:

        client = mock_transport_client(status_codes=[401, 200],
                                       json_bodies=[{}, {"ok": True}])
        resp = client.get("https://api.github.com/foo")
        # First call returns 401, second returns 200.

        client = mock_transport_client(raise_exc=httpx.ConnectError("boom"))
        # Every call raises ConnectError from real httpx machinery.

    Pass the returned client into a `monkeypatch.setattr(httpx, "get",
    client.get)` (or post) to redirect a production module-level call.
    """
    created: list[httpx.Client] = []

    def factory(
        *,
        status_codes: list[int] | None = None,
        raise_exc: BaseException | None = None,
        json_bodies: list[Any] | None = None,
    ) -> httpx.Client:
        transport = httpx.MockTransport(
            _build_handler(
                status_codes=status_codes,
                raise_exc=raise_exc,
                json_bodies=json_bodies,
            ),
        )
        client = httpx.Client(transport=transport)
        created.append(client)
        return client

    yield factory
    for c in created:
        c.close()
