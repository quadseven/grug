"""#527: the best-practices block injects into Elder's system prompt."""

from __future__ import annotations

from best_practices import Practice, practices_block
from code_review_prompt import build_system_prompt


def test_extra_rules_appended_after_static_rules():
    base = build_system_prompt("v1")
    block = practices_block([Practice("silent-failure", "counter every drop", 3, [5], 5)])
    withrules = build_system_prompt("v1", extra_rules=block)
    assert base != withrules
    assert "TEAM-LEARNED PRACTICES" in withrules
    assert withrules.startswith(base.split("\n\n" + "OUTPUT")[0][:50])  # preamble intact
    # the static RULES + output contract still present
    assert "RULES:" in withrules and "counter every drop" in withrules


def test_empty_extra_rules_is_byte_identical_to_base():
    assert build_system_prompt("v1", extra_rules="") == build_system_prompt("v1")


def test_team_practices_block_best_effort_no_repo():
    from llm_client import _team_practices_block
    assert _team_practices_block(None) == ""
    assert _team_practices_block({"pr_number": 1}) == ""  # no repo key
