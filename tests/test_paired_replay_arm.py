"""Offline tests for the checked-out production comparison arm."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench import replay_arm

MODEL = "benchmark/model"
URL = "https://review.example.com/v1/chat/completions"


def _git(root, *args):
    env = {"PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    return subprocess.check_output(
        ["git", "-C", str(root), "-c", "commit.gpgsign=false", "-c", f"core.hooksPath={os.devnull}", *args],
        env=env, text=True,
    )


@pytest.fixture
def case(tmp_path, monkeypatch):
    repo = tmp_path / "example"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Benchmark")
    _git(repo, "config", "user.email", "benchmark@example.com")
    (repo / "main.py").write_text("def total(amount):\n    return amount\n")
    (repo / "caller.py").write_text("from main import total\n\nprint(total(1))\n")
    (repo / "obsolete.py").write_text("# removed before the pinned head\n")
    _git(repo, "add", "main.py", "caller.py", "obsolete.py")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "main.py").write_text("def total(amount):\n    return amount + 1\n")
    (repo / "obsolete.py").unlink()
    _git(repo, "commit", "-qam", "historical head")
    head = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "caller.py").write_text("WORKTREE ONLY\n")
    diff = _git(repo, "diff", "--full-index", base, head)
    monkeypatch.setenv("GRUG_BENCH_CAVE_URL", URL)
    return {
        "case_id": "change", "repo": "owner/example", "pr": 1, "root": str(repo),
        "base_sha": base, "head_sha": head, "diff": diff,
        "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(), "backend": "cave", "model": MODEL,
    }


def _reply(result, *, model=MODEL, finish_reason="stop", status=200):
    return httpx.Response(status, json={
        "model": model,
        "choices": [{
            "finish_reason": finish_reason,
            "message": {"content": json.dumps(result)},
        }],
    })


def _case_runner(monkeypatch):
    base = Path(__file__).resolve().parents[1] / "services"
    # The subprocess in the CLI sets these to the measured revision.
    monkeypatch.syspath_prepend(str(base / "webhook"))
    monkeypatch.syspath_prepend(str(base / "_shared"))
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


def test_uses_frozen_files_and_real_publication_gates(case, monkeypatch):
    _case_runner(monkeypatch)
    attempts = []

    def post(backend, messages):
        assert backend.model == MODEL
        assert backend.url == URL
        prompt = "\n".join(message["content"] for message in messages)
        assert "WORKTREE ONLY" not in prompt
        if backend.extra_body["response_format"]["type"] == "json_schema":
            attempts.append("review")
            assert "print(total(1))" in prompt
            return _reply({"findings": [{
                "path": "main.py", "line": 2, "rule": "logic-error", "severity": "medium",
                "message": "The returned total includes an extra unit.",
            }]})
        attempts.append("judge")
        return _reply({"verdicts": [{
            "index": 0, "is_real_bug": True, "confidence": 0.99,
            "reasoning": "The addition changes the total.",
        }]})

    result = replay_arm.evaluate(case, post=post)

    assert result["status"] == "scored", result
    assert result["model"] == MODEL
    assert result["verdict_seconds"] >= 0
    assert result["findings"][0]["path"] == "main.py"
    assert result["findings"][0]["line"] == 2
    assert attempts[0] == "review"
    assert "judge" in attempts


@pytest.mark.parametrize("reply,reason", [
    (_reply({"findings": []}, model="different/model"), "served_model_mismatch"),
    (_reply({"findings": []}, finish_reason="length"), "incomplete_model_output"),
    (_reply({"unrelated": []}), "invalid_model_envelope"),
    (_reply({"findings": []}, status=503), "backend_status"),
])
def test_provider_replies_have_to_be_complete(reply, reason):
    from dataclasses import dataclass, field
    from types import SimpleNamespace

    @dataclass
    class Backend:
        model: str = MODEL
        url: str = URL
        extra_body: dict = field(default_factory=dict)

    wire = replay_arm.FixedBackend(Backend(), lambda *_args: reply)
    config = SimpleNamespace(
        model=MODEL, url=URL, backend="cave", extra_body={}, extra_headers={},
    )
    with pytest.raises(replay_arm.InvalidReplay, match=reason):
        wire.call(config, [])
    assert wire.responses == 0


def test_ignores_unsafe_payloads_before_loading_a_backend(case):
    case["diff_sha256"] = "0" * 64
    result = replay_arm.evaluate(case, post=lambda *_args: pytest.fail("unexpected POST"))
    assert result == {"status": "invalid", "reason": "invalid_diff"}


@pytest.mark.parametrize(("configured", "timeouts", "reason"), [
    ("240.5", [240.5], "backend_request_failed"),
    ("300.01", [], "invalid_request_timeout"),
])
def test_worker_uses_the_bounded_http_deadline_and_still_refuses_an_outage(case, configured, timeouts, reason):
    root = Path(__file__).resolve().parents[1]
    env = {
        "PATH": os.defpath,
        "PYTHONPATH": os.pathsep.join(str(root / path) for path in (".", "services/webhook", "services/_shared")),
        "GRUG_BENCH_CAVE_URL": URL,
        "GRUG_BENCH_TIMEOUT_SECONDS": configured,
    }
    # Use the worker's fresh imports so the real transport reads its locked settings.
    command = """
import json
import sys
from unittest.mock import patch

import httpx

from bench.replay_arm import evaluate

timeouts = []

def post(_url, *, timeout, **_kwargs):
    timeouts.append(timeout)
    raise httpx.ReadTimeout("simulated outage")

with patch.object(httpx, "post", post):
    result = evaluate(json.load(sys.stdin))
print(json.dumps({"timeouts": timeouts, "result": result}))
"""
    result = subprocess.run([sys.executable, "-B", "-c", command], cwd=root, env=env,
                            input=json.dumps(case), capture_output=True, text=True, check=True, timeout=15)
    assert json.loads(result.stdout) == {
        "timeouts": timeouts, "result": {"status": "invalid", "reason": reason},
    }


def test_paired_budget_reviews_every_hunk_beyond_the_live_plan_limit(case, monkeypatch):
    _case_runner(monkeypatch)
    import llm_client
    from personas.code_reviewer.diff_parser import parse_diff
    from review_pipeline import plan_review

    root = Path(case["root"])
    src = root / "src"
    src.mkdir()
    for number in range(64):
        (src / f"part_{number:02}.py").write_text(f"benchmark_value_{number} = {number}\n")
    _git(root, "add", "src")
    _git(root, "commit", "-qm", "more changed paths than the live budget")
    head = _git(root, "rev-parse", "HEAD").strip()
    diff = _git(root, "diff", "--full-index", case["base_sha"], head)
    case.update(head_sha=head, diff=diff, diff_sha256=hashlib.sha256(diff.encode()).hexdigest())
    hunks = parse_diff(diff)
    live_plan = plan_review(
        [llm_client.Hunk(path=h.file_path, body=h.body) for h in hunks], max_cohorts=10,
    )
    assert 10 < live_plan.total_cohorts_planned <= replay_arm.REPLAY_MAX_COHORTS
    expected_paths = {hunk.file_path for hunk in hunks}

    review = llm_client.review_diff
    coverage = []

    def observe(evidence, *args, **kwargs):
        assert tuple(h.body for h in evidence.hunks) == tuple(h.body for h in hunks)
        response = review(evidence, *args, **kwargs)
        coverage.append(response.coverage)
        return response

    monkeypatch.setattr(llm_client, "review_diff", observe)
    result = replay_arm.evaluate(case, post=lambda *_args: _reply({"findings": []}))

    assert result["status"] == "scored", result
    assert coverage[0].complete
    assert coverage[0].completed_cohorts == live_plan.total_cohorts_planned
    assert set(coverage[0].reviewed_paths) == expected_paths


def test_missing_published_verdict_does_not_become_a_score(case, monkeypatch):
    _case_runner(monkeypatch)

    def post(backend, _messages):
        if backend.extra_body["response_format"]["type"] == "json_schema":
            return _reply({"findings": [{
                "path": "main.py", "line": 2, "rule": "logic-error", "severity": "medium",
                "message": "The returned total includes an extra unit.",
            }]})
        return _reply({"verdicts": []})

    assert replay_arm.evaluate(case, post=post) == {
        "status": "invalid", "reason": "incomplete_publication_verdicts",
    }


def test_invalid_response_cannot_trigger_a_fallback(case, monkeypatch):
    _case_runner(monkeypatch)
    attempts = []

    def post(backend, _messages):
        attempts.append((backend.name, backend.url, backend.model))
        return _reply({"findings": []}, model="different/model")

    result = replay_arm.evaluate(case, post=post)

    assert result == {"status": "invalid", "reason": "served_model_mismatch"}
    assert attempts == [("cave", URL, MODEL)]


def test_transport_refuses_redirect_destination(case, monkeypatch):
    _case_runner(monkeypatch)
    from dataclasses import dataclass

    import llm_client

    @dataclass
    class Backend:
        model: str = MODEL
        url: str = URL

    wire = replay_arm.FixedBackend(Backend(), lambda *_args: pytest.fail("unexpected POST"))
    requested = []
    redirected = "https://other.example.com/away"

    def transport(request):
        requested.append(str(request.url))
        return httpx.Response(307, headers={"Location": redirected})

    with wire.fenced(llm_client), httpx.Client(transport=httpx.MockTransport(transport)) as client:
        token = wire.allowed.set(True)
        try:
            response = client.post(URL, json={"model": MODEL}, follow_redirects=True)
            assert response.status_code == 307
            # The lower guard also covers a caller bypassing Client.send.
            request = httpx.Request("POST", redirected, json={"model": MODEL})
            with pytest.raises(replay_arm.InvalidReplay, match="unexpected_network_request"):
                client._send_single_request(request)
        finally:
            wire.allowed.reset(token)
    assert requested == [URL]
