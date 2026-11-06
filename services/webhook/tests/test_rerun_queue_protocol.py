"""The durable rerun queue job protocol (grug#584).

`rerun_queue` owns the job vocabulary: one `SCHEMA_VERSION`, a typed
`encode_*` per kind, and one `decode_job` the consumer calls. The wire
format is pinned byte for byte here, because the API and webhook pods
deploy at different times and must read each other's messages.
"""

from __future__ import annotations

import hashlib
import json

import pytest
import rerun_queue as rq

# --- wire format pins: the exact JSON each kind put on the queue before
# the protocol module existed. A change here breaks mixed-version pods. ---


def test_rerun_body_is_byte_identical_to_the_v1_wire_format():
    job = rq.encode_rerun(
        install_id=11, repo="myorg/myrepo", pr_number=7, persona="elder",
    )
    assert job.body == (
        '{"schema_version": 1, "install_id": 11, "repo": "myorg/myrepo", '
        '"pr_number": 7, "persona": "elder"}'
    )
    assert job.group_id == rq.rerun_group_id(11, "myorg/myrepo", 7, "elder")
    assert job.dedup_id == "11:myorg/myrepo:7:elder"


def test_rerun_command_body_carries_the_command_fields_in_order():
    job = rq.encode_rerun(
        install_id=11, repo="myorg/myrepo", pr_number=7, persona="elder",
        comment_id=99, queued_at=1000.0, hold_started_at=1001.0,
        not_before=2000.0,
    )
    assert job.body == (
        '{"schema_version": 1, "install_id": 11, "repo": "myorg/myrepo", '
        '"pr_number": 7, "persona": "elder", "comment_id": 99, '
        '"queued_at": 1000.0, "hold_started_at": 1001.0, '
        '"not_before": 2000.0}'
    )
    material = "11:myorg/myrepo:7:elder\x1fnotbefore2000\x1fkeycmd99"
    assert job.dedup_id == (
        "rerun:" + hashlib.sha256(material.encode("utf-8")).hexdigest()
    )


def test_rerun_dedup_key_without_deferral_hashes_the_key():
    job = rq.encode_rerun(
        install_id=11, repo="myorg/myrepo", pr_number=7, persona="elder",
        dedup_key="accept",
    )
    material = "11:myorg/myrepo:7:elder\x1fkeyaccept"
    assert job.dedup_id == (
        "rerun:" + hashlib.sha256(material.encode("utf-8")).hexdigest()
    )


def test_review_body_is_byte_identical_to_the_v1_wire_format():
    job = rq.encode_review(
        install_id=11, repo="myorg/myrepo", pr_number=7,
        requested_head_sha="head-1", requested_snapshot_id="snap-1",
        settle_seconds=90, redrive_count=2,
    )
    assert job.body == (
        '{"schema_version": 1, "kind": "review", "install_id": 11, '
        '"repo": "myorg/myrepo", "pr_number": 7, "persona": "elder", '
        '"requested_head_sha": "head-1", "requested_snapshot_id": "snap-1", '
        '"settle_seconds": 90, "redrive_count": 2}'
    )
    assert job.group_id == rq.review_group_id(11, "myorg/myrepo", 7)
    material = "11\x1fmyorg/myrepo\x1f7\x1felder\x1fsnap-1"
    assert job.dedup_id == (
        "elder-review:" + hashlib.sha256(material.encode("utf-8")).hexdigest()
    )


def test_ask_body_is_byte_identical_to_the_v1_wire_format():
    job = rq.encode_ask(
        install_id=11, repo="myorg/myrepo", pr_number=7, comment_id=5,
        question="why?", reply_to_comment_id=3,
    )
    assert job.body == (
        '{"schema_version": 1, "kind": "ask", "install_id": 11, '
        '"repo": "myorg/myrepo", "pr_number": 7, "comment_id": 5, '
        '"question": "why?", "reply_to_comment_id": 3}'
    )
    assert job.group_id == rq.ask_group_id(11, "myorg/myrepo", 7)
    assert job.dedup_id == "11:myorg/myrepo:7:ask:5"


def test_learn_body_is_byte_identical_to_the_v1_wire_format():
    job = rq.encode_learn(
        install_id=11, repo="myorg/myrepo", pr_number=7, comment_id=5,
        parent_comment_id=4, reply_text="noted", author="maintainer",
        defer_count=1, not_before=50.0, first_deferred_at=40.0,
        requeue_of="m-9",
    )
    assert job.body == (
        '{"schema_version": 1, "kind": "learn", "install_id": 11, '
        '"repo": "myorg/myrepo", "pr_number": 7, "comment_id": 5, '
        '"parent_comment_id": 4, "reply_text": "noted", '
        '"author": "maintainer", "defer_count": 1, "not_before": 50.0, '
        '"first_deferred_at": 40.0}'
    )
    assert job.group_id == rq.learn_group_id(11, "myorg/myrepo", 7)
    material = "11\x1fmyorg/myrepo\x1f7\x1f5\x1fdefer1\x1frequeuem-9"
    assert job.dedup_id == (
        "learn:" + hashlib.sha256(material.encode("utf-8")).hexdigest()
    )


def test_long_repo_names_keep_every_dedup_id_inside_the_sqs_limit():
    repo = "o" * 60 + "/" + "r" * 80
    for job in (
        rq.encode_rerun(install_id=1, repo=repo, pr_number=2, persona="elder"),
        rq.encode_ask(
            install_id=1, repo=repo, pr_number=2, comment_id=3, question="q",
        ),
    ):
        assert len(job.dedup_id) <= 128
        assert len(job.group_id) <= 128


def test_message_kwargs_feed_send_message_directly():
    job = rq.encode_ask(
        install_id=11, repo="myorg/myrepo", pr_number=7, comment_id=5,
        question="q",
    )
    assert job.message_kwargs() == {
        "MessageBody": job.body,
        "MessageGroupId": job.group_id,
        "MessageDeduplicationId": job.dedup_id,
    }


# --- decode: every encoded kind round-trips into a typed job ---


@pytest.mark.parametrize(
    ("encoded", "kind"),
    [
        (rq.encode_rerun(
            install_id=11, repo="myorg/myrepo", pr_number=7, persona="guard",
        ), "rerun"),
        (rq.encode_review(
            install_id=11, repo="myorg/myrepo", pr_number=7,
            requested_head_sha="h", requested_snapshot_id="s",
            settle_seconds=0,
        ), "review"),
        (rq.encode_ask(
            install_id=11, repo="myorg/myrepo", pr_number=7, comment_id=5,
            question="q",
        ), "ask"),
        (rq.encode_learn(
            install_id=11, repo="myorg/myrepo", pr_number=7, comment_id=5,
            parent_comment_id=4, reply_text="t",
        ), "learn"),
    ],
)
def test_every_kind_round_trips_through_decode(encoded, kind):
    job = rq.decode_job(encoded.body)
    assert job.kind == kind
    assert (job.install_id, job.repo, job.pr_number) == (11, "myorg/myrepo", 7)
    assert job.payload == json.loads(encoded.body)


def test_a_rerun_without_kind_decodes_as_rerun_with_its_persona():
    job = rq.decode_job(rq.encode_rerun(
        install_id=11, repo="myorg/myrepo", pr_number=7, persona="guard",
    ).body)
    assert job.kind == rq.KIND_RERUN
    assert job.persona == "guard"


@pytest.mark.parametrize(
    "body",
    [
        '{"install_id": 11, "repo": "a/b", "pr_number": 7}',
        '{"schema_version": 2, "install_id": 11, "repo": "a/b", "pr_number": 7}',
        '{"schema_version": "1", "install_id": 11, "repo": "a/b", "pr_number": 7}',
        '{"schema_version": true, "install_id": 11, "repo": "a/b", "pr_number": 7}',
    ],
)
def test_missing_or_unknown_schema_version_is_rejected_loudly(body):
    with pytest.raises(rq.UnknownJobVersion) as exc:
        rq.decode_job(body)
    assert isinstance(exc.value, rq.InvalidJob)
    assert "schema_version" in exc.value.reason


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        "[]",
        ('{"schema_version": 1, "kind": "teleport", "install_id": 11, '
         '"repo": "a/b", "pr_number": 7}'),
        '{"schema_version": 1, "repo": "a/b", "pr_number": 7}',
        ('{"schema_version": 1, "install_id": 11, "repo": "a/b", '
         '"pr_number": "+0,0"}'),
        '{"schema_version": 1, "install_id": 11, "repo": "a/b", "pr_number": 0}',
        '{"schema_version": 1, "install_id": 11, "repo": "nope", "pr_number": 7}',
    ],
)
def test_malformed_jobs_raise_the_one_invalid_job_error(body):
    with pytest.raises(rq.InvalidJob):
        rq.decode_job(body)


def test_schema_version_has_one_definition():
    """The API and webhook producers import the shared constant rather than
    defining their own (grug#584 criterion 3)."""
    import pathlib

    services = pathlib.Path(__file__).resolve().parents[2]
    owners = [
        p.relative_to(services).as_posix()
        for p in services.rglob("*.py")
        if "spark_cave" not in p.parts and "tests" not in p.parts
        and any(
            line.startswith("SCHEMA_VERSION")
            for line in p.read_text(encoding="utf-8").splitlines()
        )
    ]
    assert owners == ["_shared/rerun_queue.py"]
