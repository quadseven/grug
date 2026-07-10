"""Elder voice pack (#288/#578): caveman (free) vs sage (paid, entitled).

Covers the four seams the feature spans:
  - voice_pack.apply_voice / resolve_voice (the overlay + config read)
  - llm_client._SYSTEM_PROMPTS_SAGE + _build_messages routing (prompt select)
  - pg_install_store.set_repo_config / get_repo_config (storage + entitlement)

The store's DB write is mocked (_merge_attrs / _get_item), so these run without
a Postgres instance; the round-trip against a real store is exercised by the
GRUG_TEST_DATABASE_URL suite in CI.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

import adapters.pg_install_store as store
import llm_client as lc
import voice_pack as vp
from code_review_prompt import _VOICE as CAVEMAN_VOICE
from llm_client import Hunk


# --- voice_pack.apply_voice -------------------------------------------------

def test_apply_voice_caveman_is_noop():
    prompt = lc._SYSTEM_PROMPTS["v1"]
    assert vp.apply_voice(prompt, "caveman") == prompt


def test_apply_voice_sage_swaps_only_the_voice_block():
    prompt = lc._SYSTEM_PROMPTS["v1"]
    sage = vp.apply_voice(prompt, "sage")
    assert sage != prompt
    assert CAVEMAN_VOICE not in sage          # caveman voice removed
    assert "sage cadence" in sage             # sage voice injected
    assert "RULES:" in sage                   # rules block preserved
    # Everything outside the voice block is untouched, so the swap is exactly
    # len(sage) - len(caveman) apart.
    assert prompt.replace(CAVEMAN_VOICE, vp._SAGE_VOICE, 1) == sage


def test_apply_voice_raises_if_caveman_block_absent():
    with pytest.raises(ValueError, match="caveman VOICE block not found"):
        vp.apply_voice("a prompt with no voice block", "sage")


def test_sage_voice_omits_the_v1_only_false_negative_lever():
    # The voice block must not contain "false negative" or it would confound
    # the #191 v1/v2 confidence experiment (same rule as the caveman _VOICE).
    assert "false negative" not in vp._SAGE_VOICE


def test_sage_voice_is_ascii():
    vp._SAGE_VOICE.encode("ascii")  # raises if any non-ASCII slipped in


# --- voice_pack.resolve_voice -----------------------------------------------

@pytest.mark.parametrize(
    "cfg, expected",
    [
        ({"elder_voice": "sage"}, "sage"),
        ({"elder_voice": "caveman"}, "caveman"),
        ({}, "caveman"),                      # missing -> free default
        ({"elder_voice": None}, "caveman"),   # unset -> free default
        ({"elder_voice": "yoda"}, "caveman"),  # unknown -> fail-safe default
    ],
)
def test_resolve_voice(cfg, expected):
    assert vp.resolve_voice(cfg) == expected


# --- llm_client prompt selection --------------------------------------------

def test_sage_prompt_cache_built_for_every_variant():
    assert set(lc._SYSTEM_PROMPTS_SAGE) == set(lc._SYSTEM_PROMPTS)
    for variant in lc._SYSTEM_PROMPTS:
        assert lc._SYSTEM_PROMPTS_SAGE[variant] != lc._SYSTEM_PROMPTS[variant]
        assert "sage cadence" in lc._SYSTEM_PROMPTS_SAGE[variant]


def test_build_messages_routes_to_the_selected_voice():
    hunks = [Hunk(path="a.py", body="+x = 1")]
    caveman_sys = lc._build_messages(hunks, "v1", voice="caveman")[0]["content"]
    sage_sys = lc._build_messages(hunks, "v1", voice="sage")[0]["content"]
    assert CAVEMAN_VOICE in caveman_sys and "sage cadence" not in caveman_sys
    assert "sage cadence" in sage_sys and CAVEMAN_VOICE not in sage_sys


def test_build_messages_defaults_to_caveman():
    hunks = [Hunk(path="a.py", body="+x = 1")]
    default_sys = lc._build_messages(hunks, "v1")[0]["content"]
    assert CAVEMAN_VOICE in default_sys


# --- pg_install_store: storage + entitlement gate ---------------------------

_CFG = dict(
    install_id=42, repo_id=7, repo_full_name="o/r", updated_by_user_id="u1",
)


def test_set_repo_config_accepts_elder_voice_as_a_known_flag():
    # elder_voice must NOT be rejected as an "unknown persona flag" (the bug
    # that made the whole feature dead: it was never registered).
    with (
        patch.object(store, "_merge_attrs"),
        patch.object(store, "is_install_allowlisted", return_value=True),
    ):
        updated = store.set_repo_config(**_CFG, elder_voice="caveman")
    assert updated.get("elder_voice") == "caveman"


def test_set_repo_config_sage_stored_when_entitled():
    with (
        patch.object(store, "_merge_attrs") as merge,
        patch.object(store, "is_install_allowlisted", return_value=True),
    ):
        updated = store.set_repo_config(**_CFG, elder_voice="sage")
    assert updated.get("elder_voice") == "sage"
    merge.assert_called_once()  # it actually persisted


def test_set_repo_config_sage_rejected_when_not_entitled():
    with (
        patch.object(store, "_merge_attrs") as merge,
        patch.object(store, "is_install_allowlisted", return_value=False),
    ):
        with pytest.raises(ValueError, match="requires an allowlisted"):
            store.set_repo_config(**_CFG, elder_voice="sage")
    merge.assert_not_called()  # rejected BEFORE any write


def test_set_repo_config_rejects_invalid_voice():
    with (
        patch.object(store, "_merge_attrs") as merge,
        patch.object(store, "is_install_allowlisted", return_value=True),
    ):
        with pytest.raises(ValueError, match="elder_voice must be one of"):
            store.set_repo_config(**_CFG, elder_voice="yoda")
    merge.assert_not_called()


def test_set_repo_config_caveman_needs_no_entitlement():
    # The free voice must be selectable by anyone, entitlement never consulted.
    with (
        patch.object(store, "_merge_attrs"),
        patch.object(store, "is_install_allowlisted") as allow,
    ):
        store.set_repo_config(**_CFG, elder_voice="caveman")
    allow.assert_not_called()


def test_get_repo_config_defaults_to_caveman():
    with patch.object(store, "_get_item", return_value={}):
        assert store.get_repo_config(1, 2)["elder_voice"] == "caveman"


def test_get_repo_config_returns_stored_sage():
    with patch.object(store, "_get_item", return_value={"elder_voice": "sage"}):
        assert store.get_repo_config(1, 2)["elder_voice"] == "sage"
