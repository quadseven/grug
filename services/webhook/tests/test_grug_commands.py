"""#528: the /grug command parser - verbs, args, first-wins."""

from __future__ import annotations

from grug_commands import GrugCommand, parse_command


def test_parses_each_verb():
    assert parse_command("/grug recheck").verb == "recheck"
    assert parse_command("/grug improve").verb == "improve"
    assert parse_command("/grug test-gaps").verb == "test-gaps"
    assert parse_command("/grug test_gaps").verb == "test-gaps"  # underscore alias


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
