"""PersonalityEngine save/load and style selection (#1691)."""

import pytest

from src.grugthink.personality_engine import PersonalityEngine


@pytest.fixture(autouse=True)
def _no_ambient_forced_personality(monkeypatch):
    monkeypatch.delenv("FORCE_PERSONALITY", raising=False)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "nested" / "personalities.db")


def test_default_personality_is_grug(db_path):
    engine = PersonalityEngine(db_path=db_path)
    assert engine.get_personality("srv-1").name.lower() == "grug"


def test_forced_personality_selects_template_and_style(db_path):
    engine = PersonalityEngine(db_path=db_path, forced_personality="bigrob")
    state = engine.get_personality("srv-1")
    assert state.name == engine.templates["bigrob"].name
    assert state.response_style == "british_working_class"


@pytest.mark.parametrize("alias", ["big_rob", "biggrob", "rob"])
def test_forced_personality_aliases_resolve_to_bigrob(db_path, alias):
    engine = PersonalityEngine(db_path=db_path, forced_personality=alias)
    assert engine.get_personality("srv-1").name == engine.templates["bigrob"].name


def test_personality_is_saved_and_loaded_by_a_new_engine(db_path):
    first = PersonalityEngine(db_path=db_path, forced_personality="bigrob")
    state = first.get_personality("srv-1")
    state.interaction_count = 7
    state.learned_phrases.append("proper job")
    first._save_personality(state)

    second = PersonalityEngine(db_path=db_path, forced_personality="bigrob")
    assert "srv-1" in second.personalities
    loaded = second.personalities["srv-1"]
    assert loaded.interaction_count == 7
    assert loaded.learned_phrases == ["proper job"]
    assert loaded.name == state.name


def test_servers_keep_separate_personalities(db_path):
    engine = PersonalityEngine(db_path=db_path)
    assert engine.get_personality("a") is not engine.get_personality("b")
    assert engine.get_personality("a") is engine.get_personality("a")


def test_forced_personality_replaces_a_stored_mismatch(db_path):
    PersonalityEngine(db_path=db_path, forced_personality="grug").get_personality("srv-1")
    engine = PersonalityEngine(db_path=db_path, forced_personality="bigrob")
    assert engine.get_personality("srv-1").name == engine.templates["bigrob"].name


def test_big_rob_dialect_applies_only_to_its_style(db_path, monkeypatch):
    monkeypatch.setattr("random.random", lambda: 1.0)  # no catchphrase
    rob = PersonalityEngine(db_path=db_path, forced_personality="bigrob")
    grug = PersonalityEngine(db_path=db_path + ".grug", forced_personality="grug")
    text = "you are going to the thing"
    assert grug.get_response_with_style("s", text) == text
    assert rob.get_response_with_style("s", text) != text


def test_catchphrase_is_appended_when_roll_succeeds(db_path, monkeypatch):
    monkeypatch.setattr("random.random", lambda: 0.0)
    engine = PersonalityEngine(db_path=db_path, forced_personality="grug")
    out = engine.get_response_with_style("s", "hello")
    assert out.startswith("hello ")
    assert out[len("hello ") :] in engine.get_personality("s").catchphrases


def test_error_message_and_info(db_path):
    engine = PersonalityEngine(db_path=db_path, forced_personality="grug")
    assert engine.get_error_message("s") in engine.get_personality("s").error_messages
    info = engine.get_personality_info("s")
    assert set(info) == {"bot_name", "evolution_stage", "interaction_count", "quirks", "style"}
