"""#551: Warder + Elder cave-fallback publish via the shared seam.

Both personas hand-rolled the same check-run publish + Activity-verdict
record tail. The shared `publish_persona_check` seam (#549) owns that tail
now; these tests pin that each persona routes through it with its own
verdict fields preserved — and that no bespoke publish tail remains.
"""

from __future__ import annotations

import json

import cave_fallback as cf
from personas.warder import dispatch as warder


def _warder_ok(monkeypatch):
    """Drive warder's dispatch to its publish tail with canned data."""
    monkeypatch.setattr(
        warder,
        "with_install_token_retry",
        lambda iid, fn: fn("tok"),
    )
    monkeypatch.setattr(
        warder,
        "_fetch_commits_since_last_tag",
        lambda token, owner, repo, sha: (("feat: add a thing",), "v1.2.3"),
    )
    monkeypatch.setattr(
        warder,
        "_gate_verdict",
        lambda owner, repo_name, blocking=False: ("", None, False),
    )


def test_warder_publishes_through_the_shared_seam(monkeypatch) -> None:
    """#551: warder's check-run goes through `publish_persona_check` with
    warder's own verdict fields — no bespoke post_check_run/record tail."""
    _warder_ok(monkeypatch)
    calls = []
    monkeypatch.setattr(
        warder,
        "publish_persona_check",
        lambda **kw: (
            calls.append(kw) or {"persona": "warder", "result": kw["success_result"]}
        ),
    )
    # No bespoke tail remains: post_check_run / record_check_verdict are no
    # longer referenced by warder's dispatch at all (imports removed); the
    # only publish path is the seam call asserted below.
    out = warder.dispatch_warder_release(
        installation_id=7,
        owner="acme",
        repo_name="widget",
        head_sha="deadbeef0000",
        pr_number=3,
    )
    assert out == {"persona": "warder", "result": "pass"}
    (kw,) = calls
    assert kw["persona_key"] == "warder"
    assert kw["persona_prefix"] == "warder"
    assert kw["conclusion"] == "neutral"
    assert kw["blocking"] is False
    assert kw["degraded_reason"] is None
    assert kw["publish_failed_log_name"] == "warder_publish_failed"
    assert kw["installation_id"] == 7
    assert kw["head_sha"] == "deadbeef0000"


def test_warder_degraded_inputs_reach_the_seam(monkeypatch) -> None:
    """#551: warder's fetch-degraded path is an INPUT to the seam
    (degraded_reason='fetch_failed', neutral), not a separate tail."""
    monkeypatch.setattr(
        warder,
        "with_install_token_retry",
        lambda iid, fn: (_ for _ in ()).throw(RuntimeError("GH down")),
    )
    monkeypatch.setattr(
        warder,
        "_gate_verdict",
        lambda owner, repo_name, blocking=False: ("", None, False),
    )
    calls = []
    monkeypatch.setattr(
        warder,
        "publish_persona_check",
        lambda **kw: (
            calls.append(kw) or {"persona": "warder", "result": kw["success_result"]}
        ),
    )
    out = warder.dispatch_warder_release(
        installation_id=7,
        owner="acme",
        repo_name="widget",
        head_sha="deadbeef0000",
        pr_number=3,
    )
    assert out == {"persona": "warder", "result": "skipped"}
    (kw,) = calls
    assert kw["degraded_reason"] == "fetch_failed"
    assert kw["conclusion"] == "neutral"


def test_warder_publish_failure_maps_to_seam_sentinel(monkeypatch) -> None:
    """#551: a failed publish surfaces as the seam's reserved
    'publish_failed' result, exactly like warder's old tail."""
    _warder_ok(monkeypatch)
    from personas.publish_check import PUBLISH_FAILED

    monkeypatch.setattr(
        warder,
        "publish_persona_check",
        lambda **kw: {"persona": "warder", "result": PUBLISH_FAILED},
    )
    out = warder.dispatch_warder_release(
        installation_id=7,
        owner="acme",
        repo_name="widget",
        head_sha="deadbeef0000",
        pr_number=3,
    )
    assert out == {"persona": "warder", "result": "publish_failed"}


def _result_body(**over):
    """Same wire shape as test_cave_fallback._result_body."""
    d = {
        "schema_version": 1,
        "persona": "elder",
        "principal_id": "12345",
        "request_id": "acme/widget:7:deadbeef0000",
        "ok": True,
        "result": {
            "findings": [
                {
                    "severity": "high",
                    "rule_name": "r",
                    "file": "a.py",
                    "line": 1,
                    "message": "m",
                },
            ],
            "model": "cave",
        },
        "error": None,
    }
    d.update(over)
    return json.dumps(d)


def _event(*bodies) -> dict:
    return {"Records": [{"eventSource": "aws:sqs", "body": b} for b in bodies]}


def test_cave_fallback_heals_through_the_shared_seam(monkeypatch) -> None:
    """#551: the cave-fallback heal routes through `publish_persona_check`
    with the legacy persona key, the grug-cr: external id, a neutral
    conclusion and no degraded reason — no bespoke CheckRunResult tail."""
    calls = []
    monkeypatch.setattr(
        cf,
        "publish_persona_check",
        lambda **kw: (
            calls.append(kw) or {"persona": "code_reviewer", "result": "healed"}
        ),
    )
    # No bespoke tail remains: post_check_run / record_check_verdict are no
    # longer referenced by cave_fallback at all (imports removed); the only
    # publish path is the seam call asserted below.
    out = cf.handle_fallback_result(_event(_result_body()))
    assert out == {"records": 1, "healed": 1, "failed": 0}
    (kw,) = calls
    assert kw["persona_key"] == "code_reviewer"  # legacy key preserved
    assert kw["persona_prefix"] == "cr"  # grug-cr: external_id
    assert kw["conclusion"] == "neutral"
    assert kw["degraded_reason"] is None
    assert kw["blocking"] is False
    assert kw["findings_count"] == 1


def test_cave_fallback_publish_failure_still_raises(monkeypatch) -> None:
    """#551: when the seam reports a failed publish, the heal still raises
    so handle_fallback_result counts it failed (verdict stays healable,
    re-triggers on the next push) — never silently counted as healed."""
    from personas.publish_check import PUBLISH_FAILED

    monkeypatch.setattr(
        cf,
        "publish_persona_check",
        lambda **kw: {"persona": "code_reviewer", "result": PUBLISH_FAILED},
    )
    out = cf.handle_fallback_result(_event(_result_body()))
    assert out == {"records": 1, "healed": 0, "failed": 1}
