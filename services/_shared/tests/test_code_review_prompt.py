"""Tests for code_review_prompt.py, including voice selection."""
import pytest

from code_review_prompt import (
    VoiceSelection,
    _DEFAULT_VOICE,
    build_system_prompt,
)


class TestVoiceSelection:
    """Test voice selection feature."""

    def test_default_voice_is_caveman(self) -> None:
        assert _DEFAULT_VOICE == "caveman"

    def test_voice_selection_type(self) -> None:
        assert VoiceSelection.__args__ == ("caveman", "yoda")  # type: ignore

    def test_build_system_prompt_with_caveman(self) -> None:
        prompt = build_system_prompt(variant="v1", voice="caveman")
        assert "VOICE" in prompt
        # Caveman has short plain clauses, first person 'Grug'
        assert "Grug" in prompt or "grug" in prompt.lower()

    def test_build_system_prompt_with_yoda(self) -> None:
        prompt = build_system_prompt(variant="v1", voice="yoda")
        # Yoda has inverted cadence (object-subject-verb), Hmm/yes particles
        assert "VOICE" in prompt
        assert "Hmm" in prompt or "Yes" in prompt
        # Should mention object-subject-verb inversion
        assert "inverted" in prompt.lower() or "object-subject-verb" in prompt.lower()

    def test_build_system_prompt_yoda_technical_tokens_unchanged(self) -> None:
        prompt = build_system_prompt(variant="v1", voice="yoda")
        # Technical tokens instruction should be present
        assert "technical core is verbatim" in prompt.lower() or \
               "identifiers, exception/class/function names" in prompt

    def test_build_system_prompt_yoda_structure(self) -> None:
        prompt = build_system_prompt(variant="v1", voice="yoda")
        # Yoda structure requirements should be present
        assert "STRUCTURE" in prompt or "structure" in prompt.lower()
        assert "Hmm..." in prompt or "Hmm." in prompt

    def test_build_system_prompt_invalid_voice_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown voice"):
            build_system_prompt(variant="v1", voice="invalid")


class TestYodaPromptContent:
    """Test that Yoda voice has the expected cadence instructions."""

    def test_yoda_has_inverted_cadence_description(self) -> None:
        prompt = build_system_prompt(voice="yoda")
        # Should describe the inverted word order
        assert "inverted" in prompt.lower() or "object-subject-verb" in prompt.lower()

    def test_yoda_has_hmm_yes_particles(self) -> None:
        prompt = build_system_prompt(voice="yoda")
        assert "Hmm" in prompt or "yes" in prompt.lower() or "Yes" in prompt

    def test_yoda_has_mandatory_structure_requirement(self) -> None:
        prompt = build_system_prompt(voice="yoda")
        # Yoda has structured format requirements
        assert "STRUCTURE" in prompt or "structure" in prompt.lower()

    def test_yoda_keeps_technical_tokens_verbatim(self) -> None:
        prompt = build_system_prompt(voice="yoda")
        # Should explicitly state technical tokens remain unchanged
        assert "verbatim" in prompt.lower() or "EXACTLY" in prompt


class TestVoiceSelectionIntegration:
    """Test that voice is properly integrated throughout the review pipeline."""

    def test_build_system_prompt_caveman_has_grug(self) -> None:
        prompt = build_system_prompt(voice="caveman")
        # Caveman uses "Grug" as self-reference
        assert "Grug" in prompt

    def test_build_system_prompt_yoda_has_hmm_yes(self) -> None:
        prompt = build_system_prompt(voice="yoda")
        # Yoda has Hmm/Yes particles and inverted cadence
        assert "Hmm" in prompt or "yes" in prompt.lower() or "Yes" in prompt

    def test_build_system_prompt_yoda_has_inverted_cadence_text(self) -> None:
        prompt = build_system_prompt(voice="yoda")
        # Should describe the inverted word order
        assert "inverted" in prompt.lower() or "object-subject-verb" in prompt.lower()

    def test_build_system_prompt_yoda_keeps_technical_tokens_unchanged(self) -> None:
        prompt = build_system_prompt(voice="yoda")
        # Should explicitly state technical tokens remain unchanged
        assert "verbatim" in prompt.lower() or "EXACTLY" in prompt

    def test_voice_selection_default_is_caveman(self) -> None:
        from code_review_prompt import _DEFAULT_VOICE
        
        assert _DEFAULT_VOICE == "caveman"
