"""#528: the /grug command parser - verbs, args, first-wins."""

from __future__ import annotations

from grug_commands import GrugCommand, parse_command


def test_parses_each_verb():
    assert parse_command("/grug recheck").verb == "recheck"
    assert parse_command("/grug improve").verb == "improve"
    assert parse_command("/grug test-gaps").verb == "test-gaps"
    assert parse_command("/grug test_gaps").verb == "test-gaps"  # underscore alias
    assert parse_command("/grug fix").verb == "fix"
    assert parse_command("/GRUG Fix").verb == "fix"


def test_ask_captures_arg():
    c = parse_command("/grug ask why does this rollback restore secrets?")
    assert c.verb == "ask" and c.arg == "why does this rollback restore secrets?"


def test_none_when_no_command():
    assert parse_command("just a normal comment") is None
    assert parse_command("/grug frobnicate") is None  # unknown verb
    assert parse_command("") is None


def test_command_on_its_own_line_in_longer_comment():
    body = "thanks!\n\n/grug improve\n\nplease and thank you"
    assert parse_command(body) == GrugCommand(verb="improve", arg="")


def test_case_insensitive():
    assert parse_command("/GRUG ASK foo").verb == "ask"


def test_first_command_wins():
    assert parse_command("/grug improve\n/grug recheck").verb == "improve"


def test_parses_resume_and_review_verbs():
    """grug#1188: the auto-pause note names both commands."""
    assert parse_command("/grug resume") == GrugCommand(verb="resume", arg="")
    assert parse_command("/grug review") == GrugCommand(verb="review", arg="")
    assert parse_command("/GRUG Resume").verb == "resume"


def test_review_is_a_whole_word():
    """`/grug reviewed it` is prose, not the review command."""
    assert parse_command("/grug reviewed it") is None


def test_learnings_list_and_delete_parse():
    c = parse_command("/grug learnings list")
    assert c.verb == "learnings" and c.arg == "list"
    c = parse_command("/grug learnings delete abc123456789")
    assert c.verb == "learnings" and c.arg == "delete abc123456789"
    c = parse_command("/GRUG LearNInGs DeLeTe deadbeef")
    assert c.verb == "learnings" and c.arg == "DeLeTe deadbeef"


def test_walkthrough_verb_parses():
    assert parse_command("/grug walkthrough").verb == "walkthrough"
    assert parse_command("/grug walkthrough").arg == ""
    assert parse_command("/GRUG WALKTHROUGH").verb == "walkthrough"
    assert parse_command("please\n/grug walkthrough\nthanks").verb == "walkthrough"
