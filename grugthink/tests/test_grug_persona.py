"""Behavior tests for Grug's stable product identity."""

from src.grugthink.personality_engine import GRUG_CANONICAL_LORE, PersonalityEngine, accepts_bot_conversation


def test_legacy_grug_personality_receives_current_canonical_lore(tmp_path):
    engine = PersonalityEngine(str(tmp_path / "personalities.db"), forced_personality="grug")
    personality = engine.get_personality("guild-1")
    personality.base_context = "You are Grug from an old persisted row."

    context = engine.get_context_prompt("guild-1")

    assert GRUG_CANONICAL_LORE in context


def test_canonical_lore_names_grugs_family_and_world():
    assert all(name in GRUG_CANONICAL_LORE for name in ("Ugga", "Grog", "Bork", "Og"))
    assert "mammoth" in GRUG_CANONICAL_LORE
    assert "saber-tooth" in GRUG_CANONICAL_LORE
    assert "software services" in GRUG_CANONICAL_LORE


def test_grug_ignores_internal_service_banter_but_keeps_markov_chat():
    assert accepts_bot_conversation("Grug", "Elder") is False
    assert accepts_bot_conversation("Grug", "Warder") is False
    assert accepts_bot_conversation("Grug", "Markov Chain Bot") is True


def test_other_personalities_keep_existing_bot_conversations():
    assert accepts_bot_conversation("Big Rob", "Elder") is True
