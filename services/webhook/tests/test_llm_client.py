"""Tests for the LLM client abstraction."""
from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

import llm_client as lc
from llm_client import Backend, Finding, Hunk, LlmReviewResponse, review_diff


@pytest.fixture(autouse=True)
def _patch_keys(monkeypatch):
    """Avoid the real SSM round-trip in tests."""
    monkeypatch.setattr(lc, "_load_poolside_key", lambda: "test-pool-key")
    monkeypatch.setattr(lc, "_load_openrouter_key", lambda: "test-or-key")


def _hunk(path="src/x.py", body="@@ -1 +1 @@\n-foo\n+bar") -> Hunk:
    return Hunk(path=path, body=body)


def _openai_json_response(findings_json: str) -> dict:
    """OpenAI-compatible chat completion shape both backends return."""
    return {
        "choices": [
            {"message": {"content": findings_json, "role": "assistant"}},
        ],
        "model": "test-model-id",
    }


def test_round_robin_even_installation_picks_poolside() -> None:
    """installation_id % 2 == 0 → Poolside backend."""
    assert lc.select_backend(installation_id=2) == Backend.POOLSIDE
    assert lc.select_backend(installation_id=42) == Backend.POOLSIDE


def test_round_robin_odd_installation_picks_openrouter() -> None:
    assert lc.select_backend(installation_id=1) == Backend.OPENROUTER
    assert lc.select_backend(installation_id=43) == Backend.OPENROUTER


def test_empty_hunks_returns_no_diff_kind_without_llm_call() -> None:
    """Cheap short-circuit — don't burn LLM quota on empty diffs.
    Distinct `kind="no_diff"` so the caller can distinguish from
    `all_failed` (also has empty findings)."""
    with patch.object(httpx, "post") as mock_post:
        out = review_diff([], installation_id=1)
    assert out.kind == "no_diff"
    assert out.findings == ()
    assert out.backend_used is None
    mock_post.assert_not_called()


def test_review_diff_via_poolside_returns_structured_response() -> None:
    findings_json = (
        '{"findings": [{"path": "src/x.py", "line": 1, '
        '"rule": "secret-in-log", "severity": "high", '
        '"message": "API key in log"}]}'
    )
    response = httpx.Response(200, json=_openai_json_response(findings_json))

    with patch.object(httpx, "post", return_value=response):
        out = review_diff([_hunk()], installation_id=2)

    assert isinstance(out, LlmReviewResponse)
    assert out.kind == "reviewed"
    assert out.backend_used == Backend.POOLSIDE
    assert out.model_name == "test-model-id"
    assert len(out.findings) == 1
    assert isinstance(out.findings[0], Finding)
    assert out.findings[0].rule == "secret-in-log"
    assert out.findings[0].severity == "high"


def test_review_diff_via_openrouter_returns_structured_response() -> None:
    findings_json = '{"findings": []}'
    response = httpx.Response(200, json=_openai_json_response(findings_json))

    with patch.object(httpx, "post", return_value=response):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "reviewed"
    assert out.backend_used == Backend.OPENROUTER
    assert out.findings == ()


def test_429_triggers_retry_with_backoff(monkeypatch) -> None:
    """OpenRouter free tier sends 429 under burst; client retries."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)  # no real sleep
    seq = [
        httpx.Response(429, json={"error": {"message": "rate limited"}}),
        httpx.Response(429, json={"error": {"message": "rate limited"}}),
        httpx.Response(200, json=_openai_json_response('{"findings":[]}')),
    ]
    idx = {"n": 0}

    def staged_post(*args, **kwargs):
        i = idx["n"]
        idx["n"] += 1
        return seq[i]

    with patch.object(httpx, "post", side_effect=staged_post):
        out = review_diff([_hunk()], installation_id=1)

    assert idx["n"] == 3, "should have made 3 attempts (2 retries after 429)"
    assert out.backend_used == Backend.OPENROUTER


def test_primary_failure_falls_back_to_secondary(monkeypatch) -> None:
    """5xx on primary → no per-backend retry (might be a permanent issue);
    fall back to the other backend immediately."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    seq = [
        httpx.Response(500, json={"error": "upstream"}),  # Poolside
        httpx.Response(200, json=_openai_json_response('{"findings": [{"rule": "x", "path": "p", "line": 1, "severity": "low"}]}')),  # OpenRouter
    ]
    idx = {"n": 0}

    def staged_post(*args, **kwargs):
        i = idx["n"]
        idx["n"] += 1
        return seq[i]

    seq[1] = httpx.Response(
        200,
        json=_openai_json_response(
            '{"findings": [{"rule": "x", "path": "p", "line": 1, '
            '"severity": "low", "message": "msg"}]}'
        ),
    )

    with patch.object(httpx, "post", side_effect=staged_post):
        out = review_diff([_hunk()], installation_id=2)  # even → Poolside first

    assert out.kind == "reviewed"
    assert out.backend_used == Backend.OPENROUTER
    assert len(out.findings) == 1


def test_both_backends_fail_returns_all_failed_kind() -> None:
    """Distinct `kind="all_failed"` so the caller can switch on it
    without colliding with `no_diff`."""
    response = httpx.Response(500, json={"error": "down"})

    with patch.object(httpx, "post", return_value=response), \
         patch.object(lc, "_RETRY_SLEEP", lambda s: None):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "all_failed"
    assert out.findings == ()
    assert out.backend_used is None
    assert out.error  # non-empty


def test_timeout_treated_as_failure(monkeypatch) -> None:
    """A timeout (httpx.ReadTimeout) on the primary should trigger
    fallback to the other backend, not crash the webhook."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    call_log: list = []
    success = httpx.Response(200, json=_openai_json_response('{"findings":[]}'))

    def staged(url, *args, **kwargs):
        call_log.append(url)
        # First 3 calls (Poolside primary + retries) raise timeout;
        # subsequent fallback call to OpenRouter succeeds.
        if "openrouter" in url:
            return success
        raise httpx.ReadTimeout("timeout")

    with patch.object(httpx, "post", side_effect=staged):
        out = review_diff([_hunk()], installation_id=2)

    assert out.kind == "reviewed"
    assert out.backend_used == Backend.OPENROUTER
    assert any("openrouter" in u for u in call_log)


def test_request_uses_openai_chat_completions_shape() -> None:
    captured: list = []

    def capture(url, *, json, headers, timeout):
        captured.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return httpx.Response(200, json=_openai_json_response('{"findings":[]}'))

    with patch.object(httpx, "post", side_effect=capture):
        review_diff([_hunk()], installation_id=1)

    assert len(captured) == 1
    body = captured[0]["json"]
    assert "model" in body
    assert isinstance(body["messages"], list)
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][1]["role"] == "user"
    # OpenAI-compatible JSON-mode hint to coerce structured response
    assert body.get("response_format") == {"type": "json_object"}
    # Authorization header carries the loaded key.
    assert captured[0]["headers"]["Authorization"].startswith("Bearer ")
    # 30s timeout per the existing Poolside convention.
    assert captured[0]["timeout"] == 30


def test_malformed_llm_json_returns_parse_failed_kind() -> None:
    """LLM occasionally returns prose around the JSON or just refuses
    to comply. Don't crash the webhook on a parse error — discriminated
    `kind="parse_failed"` so the caller posts an advisory check-run
    explaining the issue rather than silent "no findings"."""
    response = httpx.Response(200, json=_openai_json_response("sorry, I cannot do that"))

    with patch.object(httpx, "post", return_value=response):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "parse_failed"
    assert out.findings == ()
    assert out.backend_used == Backend.OPENROUTER
    assert "parse" in out.error.lower()


def test_findings_with_bogus_severity_are_dropped() -> None:
    """A hallucinating LLM might return severity='catastrophic' which
    isn't in the Literal. Drop the malformed entry rather than
    iterating over `Any` downstream."""
    findings_json = (
        '{"findings": ['
        '{"path": "x", "line": 1, "rule": "ok", "severity": "high", "message": ""},'
        '{"path": "y", "line": 2, "rule": "bad", "severity": "catastrophic", "message": ""},'
        '{"path": "z", "line": 3, "rule": "also-bad", "severity": "low", "message": ""}'
        ']}'
    )
    response = httpx.Response(200, json=_openai_json_response(findings_json))

    with patch.object(httpx, "post", return_value=response):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "reviewed"
    # Bogus-severity entry dropped; valid two remain.
    assert len(out.findings) == 2
    assert {f.rule for f in out.findings} == {"ok", "also-bad"}


def test_findings_with_missing_fields_are_dropped() -> None:
    """LLM omitting a required field (e.g. `line`) → drop the entry."""
    findings_json = (
        '{"findings": ['
        '{"path": "x", "rule": "no-line", "severity": "high"},'  # missing line
        '{"path": "y", "line": 5, "rule": "ok", "severity": "low", "message": ""}'
        ']}'
    )
    response = httpx.Response(200, json=_openai_json_response(findings_json))

    with patch.object(httpx, "post", return_value=response):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "reviewed"
    assert len(out.findings) == 1
    assert out.findings[0].rule == "ok"


def test_empty_api_key_falls_back_not_crashes(monkeypatch) -> None:
    """Empty key → _BackendConfigError → fall back to the other backend.
    Without the narrow exception split, a misconfig would 500 the
    webhook handler instead of degrading to advisory mode."""
    monkeypatch.setattr(lc, "_load_poolside_key", lambda: "")  # broken
    monkeypatch.setattr(lc, "_load_openrouter_key", lambda: "real-or-key")

    response = httpx.Response(
        200,
        json=_openai_json_response(
            '{"findings": [{"path": "x", "line": 1, "rule": "ok", '
            '"severity": "low", "message": ""}]}'
        ),
    )
    with patch.object(httpx, "post", return_value=response):
        out = review_diff([_hunk()], installation_id=2)  # primary = Poolside

    assert out.kind == "reviewed"
    assert out.backend_used == Backend.OPENROUTER  # fallback


def test_both_backends_misconfigured_returns_all_failed(monkeypatch) -> None:
    monkeypatch.setattr(lc, "_load_poolside_key", lambda: "")
    monkeypatch.setattr(lc, "_load_openrouter_key", lambda: "")
    with patch.object(httpx, "post") as mock_post:
        out = review_diff([_hunk()], installation_id=1)
    assert out.kind == "all_failed"
    assert "misconfigured" in out.error.lower()
    mock_post.assert_not_called()  # never made an HTTP call


def test_503_retried_alongside_429(monkeypatch) -> None:
    """503 is routinely transient on CF edge; retry once before falling
    back. Previous behavior burned the whole backend on a 1-second blip."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    seq = [
        httpx.Response(503, json={"error": "service unavailable"}),
        httpx.Response(200, json=_openai_json_response('{"findings":[]}')),
    ]
    idx = {"n": 0}

    def staged_post(*args, **kwargs):
        i = idx["n"]
        idx["n"] += 1
        return seq[i]

    with patch.object(httpx, "post", side_effect=staged_post):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "reviewed"
    assert out.backend_used == Backend.OPENROUTER
    assert idx["n"] == 2  # one retry + one success


def test_transport_failure_on_both_backends_returns_all_failed(monkeypatch) -> None:
    """Covers the retry-loop terminal `raise` (final attempt without a
    fallback continue). Without this test, a future off-by-one on the
    `attempt < _RETRY_ATTEMPTS - 1` guard would ship green."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    call_log: list[str] = []

    def always_timeout(url, *args, **kwargs):
        call_log.append(url)
        raise httpx.ReadTimeout("timeout")

    with patch.object(httpx, "post", side_effect=always_timeout):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "all_failed"
    assert out.backend_used is None
    # 3 retries × 2 backends = 6 attempts total.
    assert len(call_log) == 6
    # Both backends represented (one of each URL).
    assert any("poolside" in u for u in call_log)
    assert any("openrouter" in u for u in call_log)


def test_parse_failed_attributes_secondary_backend(monkeypatch) -> None:
    """If the primary backend transport-fails and the secondary returns
    200 + non-JSON content, parse_failed must report the secondary as
    `backend_used`. Comment in review_diff explicitly says don't fall
    back further; verify the attribution still points at whoever actually
    responded."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    parse_fail_envelope = _openai_json_response("sorry, I cannot do that")

    def staged(url, *args, **kwargs):
        if "poolside" in url:
            raise httpx.ReadTimeout("primary down")
        return httpx.Response(200, json=parse_fail_envelope)

    with patch.object(httpx, "post", side_effect=staged):
        out = review_diff([_hunk()], installation_id=2)  # even → Poolside primary

    assert out.kind == "parse_failed"
    assert out.backend_used == Backend.OPENROUTER
    assert "parse" in out.error.lower()


def test_non_dict_finding_entries_dropped() -> None:
    """Under JSON-mode pressure, LLMs sometimes emit a string or scalar
    where the schema asks for a dict. Drop the entry rather than crashing
    on attribute access downstream."""
    findings_json = (
        '{"findings": ['
        '"just a string",'
        'null,'
        '42,'
        '{"path": "y", "line": 5, "rule": "ok", "severity": "low", "message": ""}'
        ']}'
    )
    response = httpx.Response(200, json=_openai_json_response(findings_json))

    with patch.object(httpx, "post", return_value=response):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "reviewed"
    assert len(out.findings) == 1
    assert out.findings[0].rule == "ok"


def test_bad_type_finding_entry_dropped() -> None:
    """`line` is non-coercible (a list, not int-castable). `_coerce_finding`
    must catch TypeError/ValueError and drop, not crash."""
    findings_json = (
        '{"findings": ['
        '{"path": "x", "line": [1, 2], "rule": "bad", "severity": "high", "message": ""},'
        '{"path": "y", "line": 5, "rule": "ok", "severity": "low", "message": ""}'
        ']}'
    )
    response = httpx.Response(200, json=_openai_json_response(findings_json))

    with patch.object(httpx, "post", return_value=response):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "reviewed"
    assert len(out.findings) == 1
    assert out.findings[0].rule == "ok"


def test_envelope_json_array_returns_parse_failed() -> None:
    """200 with a JSON array (not a dict) — provider edge case where the
    response shape is wrong. Must not AttributeError on `body['choices']`."""
    response = httpx.Response(200, json=["not", "a", "dict"])

    with patch.object(httpx, "post", return_value=response):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "parse_failed"
    assert "envelope" in out.error.lower() or "dict" in out.error.lower()


def test_envelope_missing_choices_returns_parse_failed() -> None:
    """Both providers return `{"error": {"code": "..."}}` on bad payloads —
    a valid JSON dict without `choices`. Must surface as parse_failed,
    not raise."""
    response = httpx.Response(
        200, json={"error": {"code": "invalid_request", "message": "bad"}}
    )

    with patch.object(httpx, "post", return_value=response):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "parse_failed"
    assert "choices" in out.error.lower() or "missing" in out.error.lower()


def test_non_retryable_5xx_does_not_burn_retry_budget(monkeypatch) -> None:
    """500/502/504 exit the retry loop immediately. A regression that
    adds them to `_RETRYABLE_STATUSES` would 3x latency before fallback —
    catch it by asserting only 1 attempt per backend."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    call_log: list[str] = []

    def staged(url, *args, **kwargs):
        call_log.append(url)
        return httpx.Response(502, text="bad gateway")

    with patch.object(httpx, "post", side_effect=staged):
        out = review_diff([_hunk()], installation_id=2)

    assert out.kind == "all_failed"
    # 1 attempt per backend × 2 backends = 2 calls. Not 6 (would be retried).
    assert len(call_log) == 2


def test_envelope_non_json_returns_parse_failed(monkeypatch) -> None:
    """200 + Cloudflare HTML interstitial (not JSON) must not crash.
    Previously `_parse_response` called `resp.json()` unguarded — the
    JSONDecodeError would bubble through `review_diff` and 500 the
    webhook handler. Now it returns a parse_failed envelope so the
    caller can post an advisory check-run."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    response = httpx.Response(200, text="<html>error</html>")
    with patch.object(httpx, "post", return_value=response):
        out = review_diff([_hunk()], installation_id=1)
    # 200+non-JSON short-circuits to parse_failed (no fallback — the
    # other backend would likely return the same edge HTML).
    assert out.kind == "parse_failed"
    assert "envelope" in out.error.lower() or "json" in out.error.lower()


# ---------------------------------------------------------------------------
# DD LLM Obs tracing — every successful LLM call emits a trace span with
# prompt/response/latency/tokens; failures emit a span with error metadata.
# ---------------------------------------------------------------------------

def _capture_llmobs(monkeypatch):
    """Patch LLMObs.llm + LLMObs.annotate; return a list of all
    annotate calls so tests can introspect the trace shape."""
    from unittest.mock import MagicMock as _MM

    annotate_calls: list[dict] = []

    class _FakeSpan:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(lc, "_llmobs_llm", lambda **kw: _FakeSpan())
    monkeypatch.setattr(
        lc, "_llmobs_annotate",
        lambda **kw: annotate_calls.append(kw),
    )
    return annotate_calls


def test_review_diff_emits_llmobs_span_on_success(monkeypatch) -> None:
    """Every successful LLM call must emit a DD LLM Obs span carrying
    prompt + response + latency_ms + tokens + model + backend."""
    annotate_calls = _capture_llmobs(monkeypatch)
    body = _openai_json_response('{"findings":[]}')
    body["usage"] = {"prompt_tokens": 100, "completion_tokens": 25}
    response = httpx.Response(200, json=body)
    with patch.object(httpx, "post", return_value=response):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "reviewed"
    assert len(annotate_calls) == 1
    call = annotate_calls[0]
    # Prompt = the messages array (system + user).
    assert isinstance(call["input_data"], list)
    assert call["input_data"][0]["role"] == "system"
    # Response = the model's content string.
    assert call["output_data"] == '{"findings":[]}'
    # Metrics include tokens + latency.
    metrics = call["metrics"]
    assert metrics["input_tokens"] == 100
    assert metrics["output_tokens"] == 25
    assert "latency_ms" in metrics
    assert metrics["latency_ms"] >= 0
    # Metadata names the backend.
    assert call["metadata"]["backend"] == "openrouter"  # installation_id=1 → odd


def test_review_diff_llmobs_span_carries_pr_context_tags(monkeypatch) -> None:
    """The PR coords (install_id, repo, pr_number, head_sha) must flow
    into span tags so DD LLM Obs can filter traces by repo or PR."""
    annotate_calls = _capture_llmobs(monkeypatch)
    response = httpx.Response(200, json=_openai_json_response('{"findings":[]}'))
    pr_context = {
        "installation_id": 42,
        "repo": "myorg/myrepo",
        "pr_number": 7,
        "head_sha": "abc123def456",
    }
    with patch.object(httpx, "post", return_value=response):
        review_diff(
            [_hunk()], installation_id=42, pr_context=pr_context,
        )

    tags = annotate_calls[0]["tags"]
    assert tags["installation_id"] == "42"
    assert tags["repo"] == "myorg/myrepo"
    assert tags["pr_number"] == "7"
    # head_sha truncated to 8 chars to keep tag cardinality bounded.
    assert tags["head_sha"] == "abc123de"


def test_review_diff_emits_llmobs_span_on_transport_failure(monkeypatch) -> None:
    """A backend timeout must STILL emit an LLM Obs span so the failure
    is visible in DD (latency tail, error rate). Span metadata names
    the error class; output_data is absent."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    annotate_calls = _capture_llmobs(monkeypatch)

    def _timeout(*a, **kw):
        raise httpx.ReadTimeout("hung")

    with patch.object(httpx, "post", side_effect=_timeout):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "all_failed"
    # Two backends tried → two spans.
    assert len(annotate_calls) == 2
    for call in annotate_calls:
        # Error class captured in metadata.
        assert call["metadata"].get("error") == "ReadTimeout"
        # No output content on a transport failure.
        assert call.get("output_data") is None


def test_review_diff_llmobs_span_handles_missing_usage(monkeypatch) -> None:
    """OpenRouter free-tier sometimes omits the `usage` field. Span
    must not crash — token metrics surface as None."""
    annotate_calls = _capture_llmobs(monkeypatch)
    # OpenAI shape but NO usage key.
    body = {"choices": [{"message": {"content": '{"findings":[]}'}}], "model": "x"}
    response = httpx.Response(200, json=body)
    with patch.object(httpx, "post", return_value=response):
        out = review_diff([_hunk()], installation_id=1)
    assert out.kind == "reviewed"
    metrics = annotate_calls[0]["metrics"]
    # latency must still be present even when tokens are missing.
    assert "latency_ms" in metrics
    assert metrics.get("input_tokens") is None
    assert metrics.get("output_tokens") is None


def test_llmobs_span_annotate_called_exactly_once_per_backend_attempt(monkeypatch) -> None:
    """Per backend attempt, the `with _llmobs_llm(...)` block must call
    `_llmobs_annotate` exactly ONCE — no double-annotation across the
    success/config-error/transport-error branches. Future refactors
    adding an early `continue` could double-annotate; lock the count."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    annotate_calls = _capture_llmobs(monkeypatch)
    # Primary backend exhausts its 3 retries on timeout (3 httpx.post
    # calls), then secondary backend succeeds.
    seq: list = [
        httpx.ReadTimeout("p1"), httpx.ReadTimeout("p2"), httpx.ReadTimeout("p3"),
        httpx.Response(200, json=_openai_json_response('{"findings":[]}')),
    ]
    idx = {"n": 0}

    def staged(*a, **kw):
        i = idx["n"]; idx["n"] += 1
        x = seq[i]
        if isinstance(x, Exception): raise x
        return x

    with patch.object(httpx, "post", side_effect=staged):
        review_diff([_hunk()], installation_id=1)

    # Exactly 2 spans (one per backend attempt — primary timeout +
    # secondary success). Not 4 (one per httpx.post retry) — the span
    # wraps the whole `_call_backend`, not each retry.
    assert len(annotate_calls) == 2


def test_llmobs_tags_match_pr_context_keys(monkeypatch) -> None:
    """Lock the tag-key set so a future `PrContext` field addition is
    a deliberate edit to _llmobs_tags, not a silent drop. If PrContext
    grows a `branch` key but _llmobs_tags doesn't, this test fails."""
    annotate_calls = _capture_llmobs(monkeypatch)
    response = httpx.Response(200, json=_openai_json_response('{"findings":[]}'))
    with patch.object(httpx, "post", return_value=response):
        review_diff(
            [_hunk()], installation_id=42,
            pr_context={
                "installation_id": 42, "repo": "o/r", "pr_number": 1,
                "head_sha": "abc123def456",
            },
        )
    assert set(annotate_calls[0]["tags"].keys()) == {
        "installation_id", "repo", "pr_number", "head_sha",
    }


def test_llmobs_config_error_annotates_with_error_config(monkeypatch) -> None:
    """_BackendConfigError path must annotate with metadata.error=`config`.
    Without this signal DD dashboards see only transport errors and
    can't tell `secret missing` from `backend down`."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    monkeypatch.setattr(lc, "_load_poolside_key", lambda: "")
    monkeypatch.setattr(lc, "_load_openrouter_key", lambda: "")
    annotate_calls = _capture_llmobs(monkeypatch)

    with patch.object(httpx, "post") as mock_post:
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "all_failed"
    mock_post.assert_not_called()
    # Two backends each fail config check → 2 spans, both error=config.
    assert len(annotate_calls) == 2
    for call in annotate_calls:
        assert call["metadata"].get("error") == "config"
        # output_data absent on config error.
        assert call.get("output_data") is None


def test_llmobs_metadata_kind_parse_failed_on_200_with_bad_content(monkeypatch) -> None:
    """When the LLM returns 200 + non-JSON content, the span metadata
    must tag kind="parse_failed" (not "reviewed", not "http_error").
    Locks the ternary order on the success-annotate path."""
    annotate_calls = _capture_llmobs(monkeypatch)
    # 200 envelope is valid JSON, but the message.content is not JSON.
    response = httpx.Response(200, json=_openai_json_response("sorry I cannot"))
    with patch.object(httpx, "post", return_value=response):
        out = review_diff([_hunk()], installation_id=1)
    assert out.kind == "parse_failed"
    assert annotate_calls[0]["metadata"]["kind"] == "parse_failed"


def test_llmobs_metadata_kind_http_error_on_non_200(monkeypatch) -> None:
    """Non-200 status → metadata.kind="http_error" (not "parse_failed"
    or "reviewed"). DD dashboards aggregate by this facet — a
    mislabel would undercount backend health rate."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    annotate_calls = _capture_llmobs(monkeypatch)
    # 500 with no retryable status; both backends will return 500.
    response = httpx.Response(500, json={"error": "down"})
    with patch.object(httpx, "post", return_value=response):
        review_diff([_hunk()], installation_id=1)
    # Both backends tagged http_error.
    for call in annotate_calls:
        assert call["metadata"]["kind"] == "http_error"
        assert call["metadata"]["status_code"] == 500


def test_extract_usage_metrics_handles_non_dict_usage() -> None:
    """A future backend that returns `usage` as a string or list must
    not crash. The `isinstance(usage, dict)` guard is the load-bearing
    one — removing it would AttributeError on `.get()`."""
    # body with usage=list (degenerate).
    out = lc._extract_usage_metrics({"usage": [1, 2, 3]})
    assert out == {"input_tokens": None, "output_tokens": None}
    # body that itself isn't a dict.
    out = lc._extract_usage_metrics("not a dict")
    assert out == {"input_tokens": None, "output_tokens": None}
    # body=None (defensive — the upstream re-parse fallback sets body={}
    # but a future caller might pass None).
    out = lc._extract_usage_metrics(None)
    assert out == {"input_tokens": None, "output_tokens": None}


def test_llmobs_body_reparse_failure_logs_warning(monkeypatch, caplog) -> None:
    """The re-parse except branch logs `llm_body_reparse_failed`. A
    regression that drops the log line (or that swaps `except` to
    `Exception` and masks an unrelated bug) silently misses the DD
    alert. Pin the log emission to a discriminator the test can read."""
    annotate_calls = _capture_llmobs(monkeypatch)

    # Build a Response where the first .json() succeeds (during
    # _parse_response — invalid `choices` shape returns err=missing
    # choices) AND the second .json() (the re-parse) also returns 200.
    # We force a divergence by stubbing _parse_response to return
    # success (err="") but stubbing .json() the second time to raise.
    call_count = {"n": 0}

    def fake_json(self):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First call from _parse_response — return a valid envelope.
            return _openai_json_response('{"findings":[]}')
        raise ValueError("divergent")

    response = httpx.Response(200, json=_openai_json_response('{"findings":[]}'))
    monkeypatch.setattr(httpx.Response, "json", fake_json)
    with caplog.at_level("WARNING"):
        with patch.object(httpx, "post", return_value=response):
            review_diff([_hunk()], installation_id=1)
    assert any(
        "llm_body_reparse_failed" in r.message for r in caplog.records
    )
    # Span still emitted, just with empty output.
    assert annotate_calls[0]["metadata"]["backend"] == "openrouter"


def test_no_diff_short_circuit_does_not_emit_llmobs_span(monkeypatch) -> None:
    """Empty hunks short-circuit before any LLM call — no span should
    be emitted (no LLM call happened)."""
    annotate_calls = _capture_llmobs(monkeypatch)
    out = review_diff([], installation_id=1)
    assert out.kind == "no_diff"
    assert annotate_calls == []
