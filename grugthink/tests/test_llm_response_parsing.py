"""Regression tests for #1045: the TRUE/FALSE verdict layer must never
destroy a real reply, and the primary Ollama timeout must be the value the
code actually uses (configurable, logged truthfully).

The incident: a fallback reply came back as in-character prose followed by
a model-written verdict trailer. The old parser searched for TRUE/FALSE
ANYWHERE in the text (case-insensitive) and kept only what followed it, so
the prose was discarded and the trailer ("TRUE - Grug respond honestly
about daily activities while staying in character.") was posted instead.
The same bug fired on any prose that merely used the word "true" or
"false" in a sentence.
"""

import os

os.environ.setdefault("DISCORD_TOKEN", "fake_token")

from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402
import requests  # noqa: E402

from src.grugthink.bot import llm_clients as llm_clients_module  # noqa: E402
from src.grugthink.bot import prompts as prompts_module  # noqa: E402

INCIDENT_PROSE = (
    "Grug day good. Hunt mammoth this morning. Make fire for Ugga. Bork chase rabbit, "
    "fall in river. Grug pull Bork out. Og help make spear sharp. Family eat well tonight. "
    "Grug strong. Grug protect all."
)
INCIDENT_TRAILER = "TRUE - Grug respond honestly about daily activities while staying in character."


@pytest.fixture(autouse=True)
def _isolate_side_effects():
    """validate_and_process_response caches and stores for cross-bot
    awareness; keep each test independent of the others."""
    prompts_module.response_cache.cache.clear()
    with patch.object(prompts_module, "store_bot_response_for_cross_reference"):
        yield
    prompts_module.response_cache.cache.clear()


def _validate(text: str) -> str | None:
    return prompts_module.validate_and_process_response(text, "cache_key_1802", server_db=None, bot_id="test_bot")


class TestVerdictLayerNeverDestroysContent:
    def test_incident_prose_with_trailing_verdict_keeps_the_prose(self):
        result = _validate(f"{INCIDENT_PROSE} {INCIDENT_TRAILER}")

        assert result == INCIDENT_PROSE
        assert "respond honestly" not in result

    def test_incident_trailer_on_its_own_line_keeps_the_prose(self):
        result = _validate(f"{INCIDENT_PROSE}\n\n{INCIDENT_TRAILER} <END>")

        assert result == INCIDENT_PROSE

    def test_bare_closing_verdict_label_is_dropped(self):
        """Seen live from the resident chat model (2026-09-23)."""
        prose = "Grug good. Sun bright. Og help make spear. Ugga make stew already. Bork sleep. Day good."
        assert _validate(f"{prose} TRUE.") == prose

    def test_bare_uppercase_word_mid_reply_is_kept(self):
        text = "Grug say TRUE. Grug mean it. Fire hot and Bork scared."
        assert _validate(text) == text

    def test_capitalized_closing_word_is_not_a_trailer(self):
        """The trailer marker is case-sensitive: only UPPERCASE TRUE/FALSE
        is the prompt's format label."""
        text = "Grug hunt all morning and find nothing. False: Grug find one rabbit."
        assert _validate(text) == text

    def test_lowercase_true_inside_prose_is_not_a_verdict(self):
        text = "Grug think that is true, fire hot and Bork scared of big flame."
        assert _validate(text) == text

    def test_lowercase_false_inside_prose_is_not_a_verdict(self):
        text = "Og say sky fall today but that false alarm, sky still up there."
        assert _validate(text) == text

    def test_plain_prose_without_any_verdict_is_delivered_verbatim(self):
        assert _validate(INCIDENT_PROSE) == INCIDENT_PROSE


class TestLeadingVerdictStillNormalized:
    def test_leading_true_is_normalized(self):
        assert _validate("TRUE - The Earth orbits the Sun <END>") == "TRUE - The Earth orbits the Sun."

    def test_leading_false_with_colon_is_normalized(self):
        assert _validate("FALSE: Grug know moon not made of cheese.") == "FALSE - Grug know moon not made of cheese."

    def test_leading_markdown_bold_verdict_is_normalized(self):
        assert _validate("**TRUE** - George Washington was first president.") == (
            "TRUE - George Washington was first president."
        )

    def test_leading_em_dash_separator_is_normalized(self):
        assert _validate("TRUE \u2014 Grug know fire hot and bright.") == "TRUE - Grug know fire hot and bright."

    def test_trailer_with_en_dash_separator_is_dropped(self):
        assert _validate(f"{INCIDENT_PROSE} FALSE \u2013 some meta note here.") == INCIDENT_PROSE

    def test_leading_capitalized_word_without_separator_is_prose(self):
        text = "True story, Grug once chase mammoth across whole valley."
        assert _validate(text) == text

    def test_marker_followed_by_several_sentences_is_not_a_trailer(self):
        """Only a single closing label sentence may be dropped; a marker
        with more content after it keeps the whole reply."""
        text = "Grug knows fire is hot. TRUE - fire burns wood. Grug burn hand once. Hurt bad."
        assert _validate(text) == text

    def test_only_the_final_marker_is_a_trailer(self):
        text = f"{INCIDENT_PROSE} FALSE: moon cheese. Grug check moon twice. {INCIDENT_TRAILER}"
        assert _validate(text) == f"{INCIDENT_PROSE} FALSE: moon cheese. Grug check moon twice."

    def test_short_preamble_before_verdict_uses_the_verdict(self):
        """A preamble too short to be an answer on its own is not a reply
        worth keeping - the verdict sentence is the answer."""
        assert _validate("Grug think. TRUE - George Washington was first president of America.") == (
            "TRUE - George Washington was first president of America."
        )


class TestCleanErrorWhenNothingUsable:
    def test_empty_response_is_none(self):
        assert _validate("") is None

    def test_too_short_response_is_none(self):
        assert _validate("ok") is None


class TestFallbackChainDeliversCleanProse:
    def test_poolside_incident_reply_reaches_the_user_intact(self, monkeypatch):
        """End to end through the real validator: primary times out,
        Poolside answers with prose plus a verdict trailer, the user gets
        the prose."""
        from tests.test_llm_fallback import (
            _chat_completion_ok,
            _make_personality_engine,
            _make_router,
            _make_server_db,
            _mock_config,
        )

        router = _make_router(
            ollama=lambda: requests.exceptions.Timeout("read timed out"),
            poolside=lambda: _chat_completion_ok(f"{INCIDENT_PROSE} {INCIDENT_TRAILER}"),
        )
        monkeypatch.setattr(llm_clients_module.session, "post", router)
        cfg = _mock_config()
        with (
            patch("src.grugthink.bot.llm_clients.config", cfg),
            patch("src.grugthink.bot.prompts.config", cfg),
            patch("src.grugthink.bot.prompts.extract_lore_from_response"),
        ):
            result = prompts_module.query_model(
                "hey grug, how's your day going so far?",
                _make_server_db(),
                "server1",
                _make_personality_engine(),
                current_bot_id="test_bot",
            )

        assert result == INCIDENT_PROSE


class _FakeClock:
    """Stands in for the `time` module inside llm_clients so a test can move
    the primary deadline without sleeping."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def monotonic_ns(self) -> int:
        return int(self.now * 1_000_000_000)


class _StreamedResponse:
    """A 200 whose body arrives as the given chunks, advancing the fake
    clock by `step_s` before each one (the gateway's queued-request filler
    bytes look like this: one space every ~15s)."""

    def __init__(self, clock: _FakeClock, chunks, step_s: float) -> None:
        self.status_code = 200
        self._clock = clock
        self._chunks = chunks
        self._step_s = step_s
        self.chunks_served = 0
        self.closed = False

    def iter_content(self, chunk_size: int = 1):
        for chunk in self._chunks:
            self._clock.now += self._step_s
            self.chunks_served += 1
            yield chunk

    def close(self) -> None:
        self.closed = True


class TestOllamaDeadline:
    @pytest.fixture
    def clock(self, monkeypatch):
        fake = _FakeClock()
        monkeypatch.setattr(llm_clients_module, "time", fake)
        return fake

    def _run(self, monkeypatch, post, urls=("http://localhost:11434",)):
        from tests.test_llm_fallback import _mock_config

        monkeypatch.setattr(llm_clients_module.session, "post", post)
        cfg = _mock_config(OLLAMA_URLS=list(urls), OLLAMA_MODELS=["m"] * len(urls))
        with (
            patch("src.grugthink.bot.llm_clients.config", cfg),
            patch("src.grugthink.bot.prompts.validate_and_process_response", side_effect=lambda r, *a, **k: r or None),
        ):
            return llm_clients_module.query_ollama_api("hi", "k", bot_id="test_bot")

    def _timeout_recorder(self, clock, seen, spend_s=0.0):
        def _post(url, json=None, headers=None, timeout=None, stream=False, **kwargs):
            seen.append({"url": url, "timeout": timeout, "stream": stream})
            clock.now += spend_s
            raise requests.exceptions.Timeout("read timed out")

        return _post

    def test_default_budget_and_streaming(self, monkeypatch, clock):
        monkeypatch.delenv("GRUGTHINK_OLLAMA_TIMEOUT_S", raising=False)
        seen = []
        assert self._run(monkeypatch, self._timeout_recorder(clock, seen)) is None
        assert seen == [{"url": "http://localhost:11434/api/generate", "timeout": (10, 60), "stream": True}]

    def test_budget_is_configurable(self, monkeypatch, clock):
        monkeypatch.setenv("GRUGTHINK_OLLAMA_TIMEOUT_S", "180")
        seen = []
        self._run(monkeypatch, self._timeout_recorder(clock, seen))
        assert seen[0]["timeout"] == (10, 180)

    @pytest.mark.parametrize("bad", ["", "abc", "0", "-5", "401", "100000", "nan", "inf"])
    def test_invalid_budget_falls_back_to_default(self, monkeypatch, clock, bad):
        monkeypatch.setenv("GRUGTHINK_OLLAMA_TIMEOUT_S", bad)
        seen = []
        self._run(monkeypatch, self._timeout_recorder(clock, seen))
        assert seen[0]["timeout"] == (10, 60)

    def test_max_budget_leaves_room_for_every_fallback_tier(self):
        """Worst case at the largest accepted override (2x the budget, see
        the llm_clients comment) plus Poolside, OpenRouter and Gemini (30s)
        must still finish inside Discord's 900s interaction-followup window."""
        worst_primary_s = 2 * llm_clients_module._OLLAMA_BUDGET_MAX_S
        fallback_s = 2 * sum(llm_clients_module._FALLBACK_TIMEOUT) + 30
        assert worst_primary_s + fallback_s < 900

    def test_later_urls_get_only_the_remaining_budget(self, monkeypatch, clock):
        monkeypatch.delenv("GRUGTHINK_OLLAMA_TIMEOUT_S", raising=False)
        seen = []
        self._run(monkeypatch, self._timeout_recorder(clock, seen, spend_s=45), urls=("http://a", "http://b"))
        assert [c["timeout"] for c in seen] == [(10, 60), (10, 15)]

    def test_exhausted_budget_skips_remaining_urls(self, monkeypatch, clock):
        monkeypatch.delenv("GRUGTHINK_OLLAMA_TIMEOUT_S", raising=False)
        seen = []
        self._run(monkeypatch, self._timeout_recorder(clock, seen, spend_s=61), urls=("http://a", "http://b"))
        assert [c["url"] for c in seen] == ["http://a/api/generate"]

    def test_filler_bytes_cannot_hold_the_request_past_the_deadline(self, monkeypatch, clock):
        """Endless one-space filler every 15s: a socket read timeout would
        never fire, the deadline must."""
        monkeypatch.delenv("GRUGTHINK_OLLAMA_TIMEOUT_S", raising=False)

        def _endless_filler():
            while True:
                yield b" "

        resp = _StreamedResponse(clock, _endless_filler(), step_s=15)
        assert self._run(monkeypatch, lambda *a, **k: resp) is None
        assert resp.chunks_served == 5  # 15, 30, 45, 60, then 75 > 60 trips it
        assert resp.closed

    def test_filler_then_body_inside_the_deadline_is_a_reply(self, monkeypatch, clock):
        monkeypatch.delenv("GRUGTHINK_OLLAMA_TIMEOUT_S", raising=False)
        body = b'{"response": "TRUE - Grug fine today."}'
        resp = _StreamedResponse(clock, [b" ", b" ", body], step_s=15)
        assert self._run(monkeypatch, lambda *a, **k: resp) == "TRUE - Grug fine today."
        assert resp.closed

    def test_non_json_200_body_is_a_named_request_failure(self, monkeypatch, clock):
        """A truncated or HTML 200 body must surface as a RequestException
        (so the transport handler names it), not as an 'unexpected' error."""
        monkeypatch.delenv("GRUGTHINK_OLLAMA_TIMEOUT_S", raising=False)
        resp = _StreamedResponse(clock, [b"<html>bad gateway"], step_s=1)
        with patch.object(llm_clients_module.log, "error") as log_error:
            assert self._run(monkeypatch, lambda *a, **k: resp) is None

        messages = [c.args[0] for c in log_error.call_args_list]
        assert "Ollama request failed" in messages
        assert "Unexpected error in Ollama request" not in messages
        assert resp.closed

    def test_timeout_log_reports_the_budget(self, monkeypatch, clock):
        monkeypatch.setenv("GRUGTHINK_OLLAMA_TIMEOUT_S", "120")
        with patch.object(llm_clients_module.log, "error") as log_error:
            self._run(monkeypatch, self._timeout_recorder(clock, []))

        timeout_logs = [c for c in log_error.call_args_list if c.args[0] == "Ollama request timed out"]
        assert len(timeout_logs) == 1
        assert timeout_logs[0].kwargs["extra"]["budget_s"] == 120
