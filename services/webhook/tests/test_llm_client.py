"""Tests for the LLM client abstraction."""
from __future__ import annotations

import json
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
    # 60s timeout (raised from 30s — a large diff review can exceed 30s; 30s
    # caused ReadTimeouts that silently dropped Elder reviews).
    assert captured[0]["timeout"] == 60
    # installation_id=1 is odd -> OpenRouter, which gets NO vendor extra_body.
    assert "chat_template_kwargs" not in body


def test_poolside_request_disables_thinking() -> None:
    """Poolside's laguna-m.1 runs thinking ON by default — it blew past the
    read timeout (72s measured) and leaked reasoning into `content` (broke JSON
    parse), taking Elder dark. The Poolside backend MUST send the vLLM
    `chat_template_kwargs.enable_thinking=false` switch; OpenRouter must NOT
    (claude rejects the key)."""
    captured: list = []

    def capture(url, *, json, headers, timeout):
        captured.append(json)
        return httpx.Response(200, json=_openai_json_response('{"findings":[]}'))

    # installation_id=2 is even -> Poolside.
    with patch.object(httpx, "post", side_effect=capture):
        review_diff([_hunk()], installation_id=2)

    assert captured[0].get("chat_template_kwargs") == {"enable_thinking": False}


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
    `backend_used`. (The secondary is the only backend that produced a 200,
    so there's nothing further to fall back to.) Verify the attribution
    points at whoever actually responded."""
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


def test_parse_failure_on_primary_falls_back_to_secondary(monkeypatch) -> None:
    """A 200-but-unparseable response from the PRIMARY must fall back to the
    secondary — the two backends run different models (claude vs laguna), so a
    parse failure on one doesn't predict the other. Primary parse-fails,
    secondary returns clean JSON → kind=reviewed, attributed to the secondary.
    """
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    good = _openai_json_response(
        '{"findings":[{"path":"x.py","line":1,"rule":"ok",'
        '"severity":"low","message":"m"}]}'
    )
    bad = _openai_json_response("sorry, no JSON here")

    def staged(url, *args, **kwargs):
        # installation_id=1 is odd -> OpenRouter primary, Poolside secondary.
        if "openrouter" in url:
            return httpx.Response(200, json=bad)
        return httpx.Response(200, json=good)

    with patch.object(httpx, "post", side_effect=staged):
        out = review_diff([_hunk()], installation_id=1)

    assert out.kind == "reviewed"
    assert out.backend_used == Backend.POOLSIDE
    assert len(out.findings) == 1


def test_both_parse_fail_returns_parse_failed_attributed_to_primary(monkeypatch) -> None:
    """When BOTH backends return 200-but-unparseable, fall back is exhausted;
    surface the specific parse_failed kind (not all_failed), attributed to the
    PRIMARY (the first parse failure)."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    bad = httpx.Response(200, json=_openai_json_response("nope, prose only"))
    with patch.object(httpx, "post", return_value=bad):
        out = review_diff([_hunk()], installation_id=1)  # odd -> OpenRouter primary
    assert out.kind == "parse_failed"
    assert out.backend_used == Backend.OPENROUTER


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


def test_parse_response_handles_list_shaped_content() -> None:
    """#416: a model returning a bare JSON ARRAY of findings (not the
    documented {"findings": [...]} object) must be parsed as the findings
    list, NOT crash with `'list' object has no attribute 'get'` (which dropped
    a live Elder review, delivery 831476f0)."""
    content = '[{"rule": "x", "path": "p", "line": 1, "severity": "low"}]'
    findings, _model, err = lc._parse_response(
        httpx.Response(200, json=_openai_json_response(content))
    )
    assert err == ""
    assert len(findings) == 1


def test_parse_response_scalar_content_is_graceful_not_crash() -> None:
    """#416: content that is valid JSON but neither dict nor list (a bare
    string/number) returns a graceful parse-failure error, never an unhandled
    exception."""
    findings, _model, err = lc._parse_response(
        httpx.Response(200, json=_openai_json_response('"just a string"'))
    )
    assert findings == ()
    assert err  # non-empty error string, no exception raised


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
    # #191: and the prompt experiment arm, so DD can slice eval results by it.
    assert call["metadata"]["variant_id"] == "v1"  # default mode off → v1


def test_review_diff_llmobs_span_variant_id_reflects_experiment(monkeypatch) -> None:
    """When the experiment forces v2, the span metadata must carry it on the
    success path — the variant_id is what makes the A/B analyzable in DD."""
    monkeypatch.setattr(lc, "get_prompt_experiment_mode", lambda: "all_v2")
    annotate_calls = _capture_llmobs(monkeypatch)
    response = httpx.Response(200, json=_openai_json_response('{"findings":[]}'))
    with patch.object(httpx, "post", return_value=response):
        review_diff([_hunk()], installation_id=1)
    assert annotate_calls[0]["metadata"]["variant_id"] == "v2"


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
        # #191: the A/B arm must ride the ERROR span too, or failure-rate-by-arm
        # is unattributable in DD (default mode off → v1).
        assert call["metadata"]["variant_id"] == "v1"


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
    # Force the v2 arm so we assert the experiment arm rides the config-error
    # span too (not just success) — #191 failure-rate-by-arm depends on it.
    monkeypatch.setattr(lc, "get_prompt_experiment_mode", lambda: "all_v2")
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
        # #191: arm attribution present on the config-error path.
        assert call["metadata"]["variant_id"] == "v2"


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


def test_redact_secrets_strips_aws_github_pem_env_patterns() -> None:
    """Defense-in-depth atop the DD org-level sensitive data scanner.
    We must not ship raw secrets across the wire in the first place —
    a PR diff that touches a .env or accidentally commits a key file
    should not persist in DD storage as plaintext."""
    raw = (
        "AKIAIOSFODNN7EXAMPLE in some code "
        "and ghp_1234567890abcdefghijklmnopqrstuvwxyzAB github token "
        "and PASSWORD=supersecretvalue12345 env line "
        "and -----BEGIN RSA PRIVATE KEY-----\nMIIEpAIB\n-----END RSA PRIVATE KEY----- pem"
    )
    out = lc._redact_secrets(raw)
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "[REDACTED:aws-access-key]" in out
    assert "ghp_1234567890" not in out
    assert "[REDACTED:github-pat]" in out
    assert "supersecretvalue" not in out
    assert "[REDACTED:env-secret]" in out
    assert "MIIEpAIB" not in out
    assert "[REDACTED:pem-private-key]" in out


def test_redact_payload_walks_message_list_structure() -> None:
    """The OpenAI-compat `messages` payload is `list[dict[str, str]]`.
    Redaction must walk the structure — not just stringify it — so
    nested string values get scrubbed without losing the shape."""
    messages = [
        {"role": "system", "content": "You are a reviewer"},
        {"role": "user", "content": "diff: PASSWORD=secret12345 line"},
    ]
    out = lc._redact_payload(messages)
    assert isinstance(out, list)
    assert out[0]["role"] == "system"
    assert "secret12345" not in out[1]["content"]
    assert "[REDACTED:env-secret]" in out[1]["content"]


def test_redact_payload_truncates_after_redaction() -> None:
    """Truncation runs AFTER redaction so a trailing PEM fragment can't
    survive a mid-string cut. Bound exposure even when no patterns
    match (massive lockfile diff)."""
    huge = "x" * 100_000
    out = lc._redact_payload(huge)
    assert len(out) == lc._LLMOBS_PAYLOAD_TRUNC_BYTES


def test_llmobs_input_data_is_redacted_on_success(monkeypatch) -> None:
    """End-to-end: a diff containing a fake AWS key reaches the span
    redacted. Catches a regression where _redact_payload is dropped
    from the input_data argument."""
    annotate_calls = _capture_llmobs(monkeypatch)
    leaky_hunk = lc.Hunk(
        path="bad.py",
        body="+AWS_KEY = 'AKIAIOSFODNN7EXAMPLE'  # oops",
    )
    response = httpx.Response(200, json=_openai_json_response('{"findings":[]}'))
    with patch.object(httpx, "post", return_value=response):
        review_diff([leaky_hunk], installation_id=1)
    span_input_str = json.dumps(annotate_calls[0]["input_data"])
    assert "AKIAIOSFODNN7EXAMPLE" not in span_input_str
    assert "[REDACTED:aws-access-key]" in span_input_str


def test_llmobs_output_data_is_redacted_on_success(monkeypatch) -> None:
    """A hallucinating LLM might echo a secret back in its response.
    output_data must also pass through _redact_payload."""
    annotate_calls = _capture_llmobs(monkeypatch)
    leaky_content = (
        '{"findings": [{"path": "x", "line": 1, "rule": "leak", '
        '"severity": "critical", "message": "found AKIAIOSFODNN7EXAMPLE"}]}'
    )
    response = httpx.Response(200, json=_openai_json_response(leaky_content))
    with patch.object(httpx, "post", return_value=response):
        review_diff([_hunk()], installation_id=1)
    out_str = str(annotate_calls[0]["output_data"])
    assert "AKIAIOSFODNN7EXAMPLE" not in out_str
    assert "[REDACTED:aws-access-key]" in out_str


def test_build_messages_redacts_secrets_in_diff(monkeypatch) -> None:
    """#438: secrets in the diff are masked in the user message BEFORE it is sent
    to the backend (a third-party SaaS endpoint), not just in the DD span."""
    hunks = [lc.Hunk(path="bad.py", body="+AWS_KEY = 'AKIAIOSFODNN7EXAMPLE'")]
    user = lc._build_messages(hunks, "v1")[1]["content"]
    assert "AKIAIOSFODNN7EXAMPLE" not in user
    assert "[REDACTED:aws-access-key]" in user


def test_build_messages_redacts_secrets_in_file_context() -> None:
    """#438: the full-file context block (#336) is also redacted - a secret on an
    UNCHANGED line of a changed file must not reach the backend either."""
    hunks = [lc.Hunk(path="bad.py", body="+x = 1")]
    user = lc._build_messages(hunks, "v1", {"bad.py": "KEY = 'AKIAIOSFODNN7EXAMPLE'\nx = 1\n"})[1]["content"]
    assert "AKIAIOSFODNN7EXAMPLE" not in user


def test_backend_request_body_is_redacted(monkeypatch) -> None:
    """#438 end-to-end: the body sent to _call_backend / httpx.post has secrets
    redacted. This is THE acceptance criterion - the SaaS backend never receives
    a raw secret from the main review."""
    leaky_hunk = lc.Hunk(path="bad.py", body="+AWS_KEY = 'AKIAIOSFODNN7EXAMPLE'  # oops")
    captured: dict = {}

    def _capture_post(url, **kw):
        captured["json"] = kw.get("json")
        return httpx.Response(200, json=_openai_json_response('{"findings":[]}'))

    monkeypatch.setattr(httpx, "post", _capture_post)
    review_diff([leaky_hunk], installation_id=1)
    body_str = json.dumps(captured["json"])
    assert "AKIAIOSFODNN7EXAMPLE" not in body_str
    assert "[REDACTED:aws-access-key]" in body_str


def test_no_diff_short_circuit_does_not_emit_llmobs_span(monkeypatch) -> None:
    """Empty hunks short-circuit before any LLM call — no span should
    be emitted (no LLM call happened)."""
    annotate_calls = _capture_llmobs(monkeypatch)
    out = review_diff([], installation_id=1)
    assert out.kind == "no_diff"
    assert annotate_calls == []


def test_review_diff_carries_exported_span_context_on_success(monkeypatch) -> None:
    """The review span is exported onto the response so the LLM-as-a-
    judge (slice #190) can attach per-finding `is_real_bug` evaluations
    to the exact span whose output produced the findings."""
    _capture_llmobs(monkeypatch)
    monkeypatch.setattr(
        lc, "_llmobs_export", lambda span: {"span_id": "s1", "trace_id": "t1"},
    )
    response = httpx.Response(200, json=_openai_json_response('{"findings":[]}'))
    with patch.object(httpx, "post", return_value=response):
        out = review_diff([_hunk()], installation_id=1)
    assert out.kind == "reviewed"
    assert out.review_span_context == {"span_id": "s1", "trace_id": "t1"}


# ---------------------------------------------------------------------------
# LLM-as-a-judge (#190) — second LLM call scores each finding is_real_bug.
# ---------------------------------------------------------------------------

def test_judge_findings_makes_second_llm_call_and_parses_verdicts(monkeypatch) -> None:
    """judge_findings sends the findings + diff to a second LLM call and
    parses a verdicts array into FindingJudgement objects."""
    _capture_llmobs(monkeypatch)
    verdicts_json = (
        '{"verdicts": ['
        '{"index": 0, "is_real_bug": true, "reasoning": "real null deref"},'
        '{"index": 1, "is_real_bug": false, "reasoning": "style nit, not a bug"}'
        ']}'
    )
    response = httpx.Response(200, json=_openai_json_response(verdicts_json))
    findings_repr = [
        {"rule_name": "null-deref", "file": "x.py", "line": 2, "message": "m1"},
        {"rule_name": "style", "file": "x.py", "line": 3, "message": "m2"},
    ]
    with patch.object(httpx, "post", return_value=response):
        out = lc.judge_findings(findings_repr, [_hunk()], installation_id=1)

    assert len(out) == 2
    assert out[0].finding_index == 0
    assert out[0].is_real_bug is True
    assert out[1].finding_index == 1
    assert out[1].is_real_bug is False
    assert "style nit" in out[1].reasoning


def test_judge_findings_emits_its_own_llmobs_span(monkeypatch) -> None:
    """The judge LLM call is itself traced (own span, name elder_judge)
    so its prompt/latency/tokens/cost show up in DD distinct from the
    review call."""
    annotate_calls = _capture_llmobs(monkeypatch)
    response = httpx.Response(200, json=_openai_json_response('{"verdicts":[]}'))
    findings_repr = [{"rule_name": "r", "file": "x.py", "line": 2, "message": "m"}]
    with patch.object(httpx, "post", return_value=response):
        lc.judge_findings(findings_repr, [_hunk()], installation_id=1)
    # judge call emits a span tagged judge=True (distinct from the
    # review span) even when the LLM returns zero verdicts.
    assert len(annotate_calls) == 1
    assert annotate_calls[0]["metadata"]["judge"] is True


def test_judge_findings_returns_empty_on_llm_failure(monkeypatch) -> None:
    """If the judge LLM call fails (transport / parse), return empty —
    the judge is best-effort observability, never blocks the review."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    _capture_llmobs(monkeypatch)

    def _timeout(*a, **kw):
        raise httpx.ReadTimeout("judge backend down")

    findings_repr = [{"rule_name": "r", "file": "x.py", "line": 2, "message": "m"}]
    with patch.object(httpx, "post", side_effect=_timeout):
        out = lc.judge_findings(findings_repr, [_hunk()], installation_id=1)
    assert out == ()


def test_judge_findings_skips_above_max_findings(monkeypatch, caplog) -> None:
    """Cost guard: a firehose review (> _JUDGE_MAX_FINDINGS) skips the
    judge LLM call entirely — no second-call token spend, logged so the
    skip is visible."""
    _capture_llmobs(monkeypatch)
    too_many = [
        {"rule_name": f"r{i}", "file": "x.py", "line": i + 1, "message": "m"}
        for i in range(lc._JUDGE_MAX_FINDINGS + 1)
    ]
    with caplog.at_level("INFO"):
        with patch.object(httpx, "post") as mock_post:
            out = lc.judge_findings(too_many, [_hunk()], installation_id=1)
    assert out == ()
    mock_post.assert_not_called()
    assert any(
        "judge_skipped_too_many_findings" in r.message for r in caplog.records
    )


def test_judge_findings_at_max_findings_still_runs(monkeypatch) -> None:
    """Exactly _JUDGE_MAX_FINDINGS findings is within budget — the
    judge still runs (boundary is `>`, not `>=`)."""
    _capture_llmobs(monkeypatch)
    at_limit = [
        {"rule_name": f"r{i}", "file": "x.py", "line": i + 1, "message": "m"}
        for i in range(lc._JUDGE_MAX_FINDINGS)
    ]
    response = httpx.Response(200, json=_openai_json_response('{"verdicts":[]}'))
    with patch.object(httpx, "post", return_value=response) as mock_post:
        lc.judge_findings(at_limit, [_hunk()], installation_id=1)
    mock_post.assert_called()


def test_judge_findings_empty_findings_short_circuits(monkeypatch) -> None:
    """No findings → no judge call (nothing to evaluate)."""
    annotate_calls = _capture_llmobs(monkeypatch)
    with patch.object(httpx, "post") as mock_post:
        out = lc.judge_findings([], [_hunk()], installation_id=1)
    # Empty findings is a legit "nothing to judge" — skip the LLM call.
    assert out == ()
    mock_post.assert_not_called()
    assert annotate_calls == []


def test_judge_findings_redacts_secrets_in_judge_span(monkeypatch) -> None:
    """The judge prompt embeds the diff too — must redact before the
    span leaves the process, same as the review span."""
    annotate_calls = _capture_llmobs(monkeypatch)
    response = httpx.Response(200, json=_openai_json_response('{"verdicts":[]}'))
    leaky_hunk = lc.Hunk(path="x.py", body="+key='AKIAIOSFODNN7EXAMPLE'")
    findings_repr = [{"rule_name": "r", "file": "x.py", "line": 1, "message": "m"}]
    with patch.object(httpx, "post", return_value=response):
        lc.judge_findings(findings_repr, [leaky_hunk], installation_id=1)
    span_input = json.dumps(annotate_calls[0]["input_data"])
    assert "AKIAIOSFODNN7EXAMPLE" not in span_input
    assert "[REDACTED:aws-access-key]" in span_input


def test_submit_finding_evaluation_calls_dd_seam(monkeypatch) -> None:
    """submit_finding_evaluation maps is_real_bug → a DD LLM Obs
    categorical evaluation attached to the review span."""
    eval_calls: list[dict] = []
    monkeypatch.setattr(
        lc, "_llmobs_submit_evaluation", lambda **kw: eval_calls.append(kw),
    )
    span_ctx = {"span_id": "s1", "trace_id": "t1"}
    lc.submit_finding_evaluation(
        is_real_bug=True,
        reasoning="real bug",
        review_span_context=span_ctx,
        tags={"rule_name": "null-deref", "file": "x.py", "line": "2"},
    )
    assert len(eval_calls) == 1
    call = eval_calls[0]
    assert call["label"] == "is_real_bug"
    assert call["metric_type"] == "categorical"
    assert call["value"] == "true"
    assert call["span"] == span_ctx
    assert call["tags"]["rule_name"] == "null-deref"
    # reasoning surfaced for the annotation-queue reviewer.
    assert call["reasoning"] == "real bug"


def test_submit_finding_evaluation_false_maps_to_string_false(monkeypatch) -> None:
    eval_calls: list[dict] = []
    monkeypatch.setattr(
        lc, "_llmobs_submit_evaluation", lambda **kw: eval_calls.append(kw),
    )
    lc.submit_finding_evaluation(
        is_real_bug=False, reasoning="fp",
        review_span_context={"span_id": "s"}, tags={},
    )
    assert eval_calls[0]["value"] == "false"


def test_judge_unparseable_response_logs_warning(monkeypatch, caplog) -> None:
    """A judge whose every response is non-JSON must be distinguishable
    in logs from a judge that legitimately returned zero verdicts —
    else the ground-truth dataset stops growing invisibly."""
    _capture_llmobs(monkeypatch)
    response = httpx.Response(200, json=_openai_json_response("not json prose"))
    findings_repr = [{"rule_name": "r", "file": "x.py", "line": 2, "message": "m"}]
    with caplog.at_level("WARNING"):
        with patch.object(httpx, "post", return_value=response):
            out = lc.judge_findings(findings_repr, [_hunk()], installation_id=1)
    assert out == ()
    assert any("judge_verdicts_unparseable" in r.message for r in caplog.records)


def test_judge_unparseable_log_redacts_secrets(monkeypatch, caplog) -> None:
    """The drop-path log captures raw judge content — which (the judge
    saw the diff) may echo a secret. It MUST route through
    `_redact_secrets` before landing in DD logs, same as the span."""
    _capture_llmobs(monkeypatch)
    # 200 + non-JSON content that contains a fake AWS key.
    leaky = "prose not json AKIAIOSFODNN7EXAMPLE trailing"
    response = httpx.Response(200, json=_openai_json_response(leaky))
    fr = [{"rule_name": "r", "file": "x.py", "line": 2, "message": "m"}]
    with caplog.at_level("WARNING"):
        with patch.object(httpx, "post", return_value=response):
            lc.judge_findings(fr, [_hunk()], installation_id=1)
    rec = next(r for r in caplog.records if r.message == "judge_verdicts_unparseable")
    assert "AKIAIOSFODNN7EXAMPLE" not in rec.__dict__["raw"]
    assert "[REDACTED:aws-access-key]" in rec.__dict__["raw"]


def test_judge_partial_drop_logs_count(monkeypatch, caplog) -> None:
    """Some verdicts valid, some malformed → logged drop count so a
    creeping malformation rate is visible."""
    _capture_llmobs(monkeypatch)
    verdicts = (
        '{"verdicts": ['
        '{"index": 0, "is_real_bug": true, "reasoning": "ok"},'
        '{"garbage": "no index"},'
        '"a string not a dict"'
        ']}'
    )
    response = httpx.Response(200, json=_openai_json_response(verdicts))
    fr = [{"rule_name": "r", "file": "x.py", "line": 2, "message": "m"}]
    with caplog.at_level("WARNING"):
        with patch.object(httpx, "post", return_value=response):
            out = lc.judge_findings(fr, [_hunk()], installation_id=1)
    assert len(out) == 1
    rec = next(r for r in caplog.records if r.message == "judge_verdicts_partial_drop")
    assert rec.__dict__["dropped"] == 2
    assert rec.__dict__["kept"] == 1


def test_judge_non_200_returns_empty_no_empty_content_warning(monkeypatch, caplog) -> None:
    """A non-200 judge response (rate-limited / 5xx) → body={}, no
    content, returns (). The `judge_empty_content` warning is gated on
    status==200 so it must NOT fire here (that warning means '200 but
    garbage', a different failure)."""
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    _capture_llmobs(monkeypatch)
    # 500 on both retries — _call_backend returns the 500 response.
    response = httpx.Response(500, json={"error": "down"})
    fr = [{"rule_name": "r", "file": "x.py", "line": 2, "message": "m"}]
    with caplog.at_level("WARNING"):
        with patch.object(httpx, "post", return_value=response):
            out = lc.judge_findings(fr, [_hunk()], installation_id=1)
    assert out == ()
    assert not any("judge_empty_content" in r.message for r in caplog.records)


def test_judge_verdicts_envelope_non_dict_logs_warning(monkeypatch, caplog) -> None:
    """Judge returns valid JSON that decodes to a LIST (not a dict
    envelope) → judge_verdicts_envelope_not_dict warning + ()."""
    _capture_llmobs(monkeypatch)
    response = httpx.Response(200, json=_openai_json_response('[1, 2, 3]'))
    fr = [{"rule_name": "r", "file": "x.py", "line": 2, "message": "m"}]
    with caplog.at_level("WARNING"):
        with patch.object(httpx, "post", return_value=response):
            out = lc.judge_findings(fr, [_hunk()], installation_id=1)
    assert out == ()
    assert any(
        "judge_verdicts_envelope_not_dict" in r.message for r in caplog.records
    )


def test_judge_verdicts_not_a_list_logs_warning(monkeypatch, caplog) -> None:
    """`{"verdicts": "a string"}` → verdicts-not-a-list warning + ()."""
    _capture_llmobs(monkeypatch)
    response = httpx.Response(200, json=_openai_json_response('{"verdicts": "nope"}'))
    fr = [{"rule_name": "r", "file": "x.py", "line": 2, "message": "m"}]
    with caplog.at_level("WARNING"):
        with patch.object(httpx, "post", return_value=response):
            out = lc.judge_findings(fr, [_hunk()], installation_id=1)
    assert out == ()
    assert any(
        "judge_verdicts_not_a_list" in r.message for r in caplog.records
    )


def test_judge_200_with_non_json_body_does_not_crash(monkeypatch) -> None:
    """200 but the envelope body itself isn't JSON (CF interstitial) →
    resp.json() raises, caught, body={}, content empty, returns ()."""
    _capture_llmobs(monkeypatch)
    response = httpx.Response(200, text="<html>not json</html>")
    fr = [{"rule_name": "r", "file": "x.py", "line": 2, "message": "m"}]
    with patch.object(httpx, "post", return_value=response):
        out = lc.judge_findings(fr, [_hunk()], installation_id=1)
    assert out == ()


def test_judge_empty_content_on_200_logs_warning(monkeypatch, caplog) -> None:
    """200 + empty content (broken backend) logs judge_empty_content,
    distinct from a transport failure or a legit empty verdict list."""
    _capture_llmobs(monkeypatch)
    # 200 envelope with no choices → content stays empty.
    response = httpx.Response(200, json={"model": "x", "choices": []})
    fr = [{"rule_name": "r", "file": "x.py", "line": 2, "message": "m"}]
    with caplog.at_level("WARNING"):
        with patch.object(httpx, "post", return_value=response):
            lc.judge_findings(fr, [_hunk()], installation_id=1)
    assert any("judge_empty_content" in r.message for r in caplog.records)


def test_submit_reaction_annotation_maps_human_verdict(monkeypatch) -> None:
    """A developer reaction → `human_verdict` categorical eval (distinct
    label from the judge's is_real_bug), attached to the review span."""
    eval_calls: list[dict] = []
    monkeypatch.setattr(
        lc, "_llmobs_submit_evaluation", lambda **kw: eval_calls.append(kw),
    )
    lc.submit_reaction_annotation(
        verdict="false_positive",
        review_span_context={"span_id": "s", "trace_id": "t"},
        tags={"rule_name": "r"},
    )
    assert len(eval_calls) == 1
    call = eval_calls[0]
    assert call["label"] == "human_verdict"
    assert call["metric_type"] == "categorical"
    assert call["value"] == "false_positive"
    assert call["span"] == {"span_id": "s", "trace_id": "t"}


def test_submit_reaction_annotation_skips_when_no_span(monkeypatch) -> None:
    eval_calls: list[dict] = []
    monkeypatch.setattr(
        lc, "_llmobs_submit_evaluation", lambda **kw: eval_calls.append(kw),
    )
    lc.submit_reaction_annotation(
        verdict="confirmed", review_span_context=None, tags={},
    )
    assert eval_calls == []


def test_submit_finding_evaluation_skips_when_no_span_context(monkeypatch) -> None:
    """No review span context (review degraded / span export failed) →
    can't attach an eval; skip silently rather than crash."""
    eval_calls: list[dict] = []
    monkeypatch.setattr(
        lc, "_llmobs_submit_evaluation", lambda **kw: eval_calls.append(kw),
    )
    lc.submit_finding_evaluation(
        is_real_bug=True, reasoning="x",
        review_span_context=None, tags={},
    )
    assert eval_calls == []


# ── #336: full-file context (kills the #1149 "mitigation outside the hunk"
#    false-positive class without breaking the diff-only backward path) ──

def test_build_messages_diff_only_is_backward_compatible():
    """No file_contents → byte-identical to the pre-#336 diff-only shape."""
    hunks = [Hunk(path="src/x.py", body="@@ -1,2 +1,3 @@\n a\n+b")]
    msgs = lc._build_messages(hunks, "v1")
    assert msgs[1]["content"] == "### src/x.py\n```diff\n@@ -1,2 +1,3 @@\n a\n+b\n```"
    # and explicitly identical whether file_contents is None or {}
    assert lc._build_messages(hunks, "v1", {}) == msgs


def test_build_messages_includes_full_file_when_provided():
    """With file_contents, the numbered full file precedes the diff so the
    Elder can see a cleanup/guard outside the changed lines."""
    hunks = [Hunk(path="ci.yml", body="@@ -5,1 +5,2 @@\n a\n+b")]
    file_contents = {"ci.yml": "line-one\nline-two\nrm -f /tmp/x  # cleanup"}
    content = lc._build_messages(hunks, "v1", file_contents)[1]["content"]
    assert "FULL FILE" in content
    assert "1: line-one" in content                 # 1-based line numbers
    assert "3: rm -f /tmp/x  # cleanup" in content   # the mitigation is visible
    assert "```diff\n@@ -5,1 +5,2 @@" in content     # diff still present


def test_render_file_block_skips_oversized_file():
    """A file beyond the line budget degrades to diff-only (token guard)."""
    big = "\n".join(f"x{i}" for i in range(lc._MAX_FILE_CONTEXT_LINES + 1))
    assert lc._render_file_block("big.py", big) == ""
    assert lc._render_file_block("none.py", None) == ""
    assert lc._render_file_block("none.py", "") == ""


def test_build_messages_renders_file_block_once_per_path():
    """Two hunks in one file → the full-file block appears exactly once."""
    hunks = [
        Hunk(path="a.py", body="@@ -1 +1 @@\n+x"),
        Hunk(path="a.py", body="@@ -9 +9 @@\n+y"),
    ]
    content = lc._build_messages(hunks, "v1", {"a.py": "one\ntwo"})[1]["content"]
    assert content.count("FULL FILE") == 1


def test_build_judge_messages_includes_full_file_when_provided():
    """The judge gets the same whole-file context — a judge blind to the
    cleanup rubber-stamps the FP it exists to catch."""
    hunks = [Hunk(path="ci.yml", body="@@ -5 +5 @@\n+b")]
    msgs = lc._build_judge_messages(
        [{"severity": "medium", "rule_name": "resource-leak",
          "file": "ci.yml", "line": "5", "message": "no cleanup"}],
        hunks,
        {"ci.yml": "open()\nrm -f /tmp/x"},
    )
    assert "FULL FILE" in msgs[1]["content"]
    assert "2: rm -f /tmp/x" in msgs[1]["content"]


def test_review_diff_injects_cached_exemplars(monkeypatch) -> None:
    """#538 end-to-end wiring: cached EXEMPLARS reach the SYSTEM prompt via
    review_diff. Fails on a lazy-import typo in _few_shot_block, a store-fn
    rename, or a dropped few_shot_examples kwarg at the call site - each of
    which would silently ship the feature permanently disabled ("" is a
    no-op append and the fetch is best-effort)."""
    import adapters.pg_install_store as store

    monkeypatch.setattr(
        store,
        "get_repo_exemplars",
        lambda repo: [
            {"class": "correctness", "severity": "HIGH",
             "finding": "cached exemplar finding", "pr": 9}
        ],
    )
    captured: dict = {}

    def fake_post(url, **kwargs):
        captured["messages"] = kwargs["json"]["messages"]
        return httpx.Response(200, json=_openai_json_response('{"findings": []}'))

    with patch.object(httpx, "post", side_effect=fake_post):
        out = review_diff(
            [_hunk()], installation_id=2,
            pr_context={"repo": "o/r", "pr_number": 1},
        )
    assert out.kind == "reviewed"
    system = captured["messages"][0]["content"]
    assert "EXAMPLES OF ACCEPTED FINDINGS" in system
    assert "cached exemplar finding" in system


def test_coerce_finding_parses_suggestion_and_effort() -> None:
    """#553 wire format: optional suggestion (non-empty str else None) and
    effort (closed enum else "")."""
    ok, reason = lc._coerce_finding({
        "path": "x.py", "line": 1, "rule": "null-deref", "severity": "high",
        "message": "m", "suggestion": "fixed line", "effort": "quick-win",
    })
    assert reason == "" and ok is not None
    assert ok.suggestion == "fixed line" and ok.effort == "quick-win"

    # hostile/malformed values degrade, never reject the finding
    ok2, _ = lc._coerce_finding({
        "path": "x.py", "line": 1, "rule": "r", "severity": "low",
        "message": "m", "suggestion": {"not": "a str"}, "effort": "yolo",
    })
    assert ok2 is not None
    assert ok2.suggestion is None and ok2.effort is None

    # unhashable effort must degrade, not TypeError the whole parse
    ok4, _ = lc._coerce_finding({
        "path": "x.py", "line": 1, "rule": "r", "severity": "low",
        "message": "m", "effort": [], "suggestion": ["also", "bad"],
    })
    assert ok4 is not None and ok4.effort is None and ok4.suggestion is None

    # absent fields keep prior behavior
    ok3, _ = lc._coerce_finding({
        "path": "x.py", "line": 1, "rule": "r", "severity": "low", "message": "m",
    })
    assert ok3 is not None and ok3.suggestion is None and ok3.effort is None


def test_coerce_finding_redacts_and_caps_message_and_suggestion() -> None:
    """#553 audit: the model can ECHO a diff secret into message/suggestion,
    and a posted comment outlives a force-push - redact at the coercion
    choke point; cap message length with a VISIBLE marker."""
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n" + "MIIEfake\n" * 5
        + "-----END RSA PRIVATE KEY-----"
    )
    ok, _ = lc._coerce_finding({
        "path": "x.py", "line": 1, "rule": "r", "severity": "high",
        "message": "leak: " + pem + " end " + "x" * 3000,
        "suggestion": "key = " + pem,
    })
    assert ok is not None
    assert "MIIEfake" not in ok.message
    # stage-8 policy: a redaction-ALTERED suggestion is dropped entirely
    # (a committable [REDACTED:...] placeholder would corrupt source).
    assert ok.suggestion is None
    assert "[REDACTED:pem-private-key]" in ok.message
    assert ok.message.endswith("[truncated]")
    assert len(ok.message) <= 1520


def test_coerce_finding_drops_suggestion_redaction_would_alter() -> None:
    """#553 audit stage 8: a suggestion that echoed a secret is DROPPED,
    never rendered - a committable block containing [REDACTED:...] would
    one-click the placeholder into source."""
    # constructed at runtime, not a committed credential-shaped literal
    fake_aws_key = "AKIA" + "".join(["ABCDEFGHIJKLMNOP"[i % 16] for i in range(16)])
    ok, _ = lc._coerce_finding({
        "path": "x.py", "line": 1, "rule": "r", "severity": "high",
        "message": "m",
        "suggestion": f"key = '{fake_aws_key}'",
    })
    assert ok is not None
    assert ok.suggestion is None
