"""The personality prompt keeps remembered lore when external info is present."""

from src.grugthink.bot.prompts import build_personality_prompt
from src.grugthink.personality_engine import PersonalityEngine

LORE = "The cave has a blue door."
EXTERNAL = "Search says the sky is clear."


class FakeDB:
    def __init__(self, facts):
        self.facts = facts

    def search_facts(self, statement, k=5):
        return list(self.facts)


def _engine(tmp_path):
    return PersonalityEngine(str(tmp_path / "personalities.db"), forced_personality="grug")


def test_lore_and_external_info_both_appear(tmp_path):
    prompt = build_personality_prompt("The sky is blue", FakeDB([LORE]), "g1", _engine(tmp_path), EXTERNAL)

    assert LORE in prompt
    assert EXTERNAL in prompt


def test_prompt_without_external_info_is_unchanged(tmp_path):
    engine = _engine(tmp_path)
    prompt = build_personality_prompt("The sky is blue", FakeDB([LORE]), "g1", engine)

    expected_context = engine.get_context_prompt("g1") + f"\n\nGrug also remember these things: {LORE}"
    assert LORE in prompt
    assert EXTERNAL not in prompt
    assert prompt.startswith(expected_context + "\n\nYou MUST be 100% ACCURATE")
