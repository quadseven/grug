"""Tests for the text-sanitiser lane check (#1497).

Recorded shape (anonymized): a lane PR added a chat sanitiser that stripped
the client's link and colour markup. Its tests covered the well-formed
cases only. The fallback for a bare link opener matched any `|H` or `|h`,
so "|Hello there, friend" became empty, "a |hop" became "a op", and
"the help|hint" became "the helpint". Elder passed it.
"""

from __future__ import annotations

from personas.code_reviewer.diff_parser import file_changes, parse_diff
from personas.code_reviewer.lane_work import lane_work_findings
from personas.code_reviewer.sanitizer_review import (
    RULE_EATS_TEXT,
    RULE_MISSING_TEST,
    scan_sanitizers,
)

_SANITIZER = [
    "import re",
    "",
    "",
    "def sanitize_chat(text):",
    '    return re.sub(r"\\|[Hh]|\\|c[0-9a-fA-F]{8}", "", text)',
]
_SOUND_SANITIZER = [
    "import re",
    "",
    "",
    "def sanitize_chat(text):",
    '    return re.sub(r"\\|Hitem:[^|]*\\|h\\[(.*?)\\]", r"\\1", text)',
]
_POSITIVE_TESTS = [
    "import chat_format",
    "",
    "",
    "def test_strips_a_link():",
    '    assert chat_format.sanitize_chat("|Hitem:1|h[Sword]|h") == "[Sword]"',
    "",
    "",
    "def test_strips_a_colour_code():",
    '    assert chat_format.sanitize_chat("|cffff0000red|r") == "red"',
]
_NEGATIVE_TEST = [
    "",
    "",
    "def test_plain_text_is_unchanged():",
    '    assert chat_format.sanitize_chat("a |hop") == "a |hop"',
]


def _added_file(path: str, lines: list[str]) -> str:
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n"
        f"index 0000000..1111111\n--- /dev/null\n+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        + "".join(f"+{line}\n" for line in lines)
    )


def _diff(sanitizer: list[str], tests: list[str]) -> str:
    return _added_file("chat_format.py", sanitizer) + _added_file(
        "tests/test_chat_format.py", tests,
    )


def _scan(diff: str):
    return scan_sanitizers(file_changes(diff), parse_diff(diff))


def _names(findings) -> set[str]:
    return {finding.rule_name for finding in findings}


def test_a_sanitizer_with_no_passthrough_test_is_marked_with_its_name():
    findings = _scan(_diff(_SANITIZER, _POSITIVE_TESTS))
    missing = next(f for f in findings if f.rule_name == RULE_MISSING_TEST)
    assert missing.severity == "medium"
    assert "`sanitize_chat`" in missing.message
    assert "ordinary text" in missing.message
    assert missing.file == "chat_format.py"
    assert missing.line in parse_diff(_diff(_SANITIZER, _POSITIVE_TESTS))[0].new_lines


def test_an_unanchored_pipe_alternative_is_marked_with_the_input_it_eats():
    findings = _scan(_diff(_SANITIZER, _POSITIVE_TESTS))
    eaten = next(f for f in findings if f.rule_name == RULE_EATS_TEXT)
    assert eaten.severity == "high"
    assert "`sanitize_chat`" in eaten.message
    assert "`a |hop`" in eaten.message
    assert "`a op`" in eaten.message
    assert "ordinary text" in eaten.message


def test_the_negative_test_present_silences_the_missing_case_marking():
    diff = _diff(_SANITIZER, _POSITIVE_TESTS + _NEGATIVE_TEST)
    findings = _scan(diff)
    assert RULE_MISSING_TEST not in _names(findings)
    # The regex still eats plain text; that is a separate defect.
    assert RULE_EATS_TEXT in _names(findings)


def test_a_sound_sanitizer_with_the_negative_test_draws_nothing():
    diff = _diff(_SOUND_SANITIZER, _POSITIVE_TESTS + _NEGATIVE_TEST)
    assert _scan(diff) == ()


def test_a_sound_sanitizer_without_the_negative_test_draws_only_the_missing_case():
    diff = _diff(_SOUND_SANITIZER, _POSITIVE_TESTS)
    assert _names(_scan(diff)) == {RULE_MISSING_TEST}


def test_input_and_expected_on_separate_lines_counts_as_the_negative_case():
    tests = _POSITIVE_TESTS + [
        "",
        "",
        "def test_plain_text_is_unchanged():",
        '    kept = chat_format.sanitize_chat("the help|hint")',
        '    assert kept == "the help|hint"',
    ]
    assert _names(_scan(_diff(_SANITIZER, tests))) == {RULE_EATS_TEXT}


def test_a_positive_case_is_not_read_as_a_passthrough_test():
    # The input literal appears in the call, the expected literal on the
    # right of `==`; they differ, so this is not the negative case.
    tests = [
        "import chat_format",
        "",
        "",
        "def test_strips_a_link():",
        '    assert chat_format.sanitize_chat("|Hitem:1|h[Sword]|h") == "[Sword]"',
    ]
    assert RULE_MISSING_TEST in _names(_scan(_diff(_SANITIZER, tests)))


def test_a_regex_function_that_is_not_a_sanitizer_is_left_alone():
    source = [
        "import re",
        "",
        "",
        "def field_names(text):",
        '    return re.sub(r"\\|[Hh]", "", text)',
    ]
    assert _scan(_diff(source, _POSITIVE_TESTS)) == ()


def test_lane_work_findings_runs_the_sanitizer_check():
    diff = _diff(_SANITIZER, _POSITIVE_TESTS)
    found = lane_work_findings(
        diff_text=diff, hunks=parse_diff(diff), file_contents={},
        base_line_counts={}, boxes=(), pr_title="Sanitize chat markup",
        pr_body="## Why\nStrip the client markup.\n",
    )
    assert {RULE_EATS_TEXT, RULE_MISSING_TEST} <= _names(found)
