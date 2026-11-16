"""Tests for false-positive fixes in the sanitizer rules (#1718).

The rule `sanitizer-eats-plain-text` must not flag:
- A whitespace collapse: re.sub(r"\s+", " ", text)
- An allow-list: re.compile(r"[^A-Za-z0-9 ,.]") with replacement " "

The rule `sanitizer-missing-negative-test` must accept a table-driven test
where at least one (raw, want) pair has raw == want.
"""

from __future__ import annotations

from personas.code_reviewer.diff_parser import file_changes, parse_diff
from personas.code_reviewer.sanitizer_review import (
    RULE_EATS_TEXT,
    RULE_MISSING_TEST,
    scan_sanitizers,
)


_WHITESPACE_COLLAPSE = [
    "import re",
    "",
    "",
    "def sanitize_title(text):",
    '    return re.sub(r"\\s+", " ", text)',
]

_ALLOW_LIST = [
    "import re",
    "",
    "",
    "def sanitize_title(text):",
    '    _ALLOWED = re.compile(r"[^A-Za-z0-9 ,.]")',
    '    return _ALLOWED.sub(" ", text)',
]

_MODULE_ALLOW_LIST = [
    "import re",
    "",
    '_ALLOWED = re.compile(r"[^A-Za-z0-9 ,.]")',
    "",
    "",
    "def sanitize_title(text):",
    '    return _ALLOWED.sub(" ", text)',
]

_MODULE_LINK_STRIPPER = [
    "import re",
    "",
    '_LINK = re.compile(r"\\|h")',
    "",
    "",
    "def sanitize_chat(text):",
    '    return _LINK.sub("", text)',
]

_SOUND_SANITIZER = [
    "import re",
    "",
    "",
    "def sanitize_chat(text):",
    '    return re.sub(r"\\|Hitem:[^|]*\\|h\\[(.*?)\\]", r"\\1", text)',
]

_TABLE_DRIVEN_WITH_PASSTHROUGH = [
    "import chat_format",
    "",
    "",
    "def test_sanitize_table_driven():",
    "    cases = [",
    '        ("|Hitem:1|h[Sword]|h", "[Sword]"),',
    '        ("|cffff0000red|r", "red"),',
    '        ("a |hop", "a |hop"),',
    '        ("the help|hint", "the help|hint"),',
    "    ]",
    "    for raw, want in cases:",
    "        assert chat_format.sanitize_chat(raw) == want",
]

_TABLE_DRIVEN_NO_PASSTHROUGH = [
    "import chat_format",
    "",
    "",
    "def test_sanitize_table_driven():",
    "    cases = [",
    '        ("|Hitem:1|h[Sword]|h", "[Sword]"),',
    '        ("|cffff0000red|r", "red"),',
    "    ]",
    "    for raw, want in cases:",
    "        assert chat_format.sanitize_chat(raw) == want",
]

_POSITIVE_TESTS_ONLY = [
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


def test_whitespace_collapse_yields_no_eats_text_finding():
    """A sanitiser that only collapses whitespace must not be flagged."""
    diff = _diff(_WHITESPACE_COLLAPSE, _POSITIVE_TESTS_ONLY)
    findings = _scan(diff)
    assert RULE_EATS_TEXT not in _names(findings), (
        f"Expected no {RULE_EATS_TEXT}, got {[f.message for f in findings]}"
    )


def test_allow_list_yields_no_eats_text_finding():
    """An allow-list sanitiser (negated char class) must not be flagged."""
    diff = _diff(_ALLOW_LIST, _POSITIVE_TESTS_ONLY)
    findings = _scan(diff)
    assert RULE_EATS_TEXT not in _names(findings), (
        f"Expected no {RULE_EATS_TEXT}, got {[f.message for f in findings]}"
    )


def test_table_driven_with_passthrough_silences_missing_test():
    """A table-driven test with a raw==want pair must satisfy the negative test."""
    diff = _diff(_SOUND_SANITIZER, _TABLE_DRIVEN_WITH_PASSTHROUGH)
    findings = _scan(diff)
    assert RULE_MISSING_TEST not in _names(findings), (
        f"Expected no {RULE_MISSING_TEST}, got {[f.message for f in findings]}"
    )


def test_table_driven_without_passthrough_still_flagged():
    """A table-driven test with no raw==want pair must still be flagged."""
    diff = _diff(_SOUND_SANITIZER, _TABLE_DRIVEN_NO_PASSTHROUGH)
    findings = _scan(diff)
    assert RULE_MISSING_TEST in _names(findings), (
        f"Expected {RULE_MISSING_TEST}, got {[f.rule_name for f in findings]}"
    )


def test_sound_sanitizer_with_table_driven_passthrough_draws_nothing():
    """A sound sanitiser with a table-driven passthrough test draws no findings."""
    diff = _diff(_SOUND_SANITIZER, _TABLE_DRIVEN_WITH_PASSTHROUGH)
    assert _scan(diff) == ()

_NAME_REPLACEMENT = [
    "import re",
    "",
    "",
    "def sanitize_chat(text):",
    '    return re.sub(r"\\|h", _blank, text)',
]

_INLINE_ALLOW_LIST = [
    "import re",
    "",
    "",
    "def sanitize_title(text):",
    '    return re.sub(r"[^A-Za-z0-9 ,.]", " ", text)',
]


def test_non_literal_replacement_is_not_read_as_a_later_string():
    """A name in the replacement slot must not borrow a later string literal."""
    diff = _diff(_NAME_REPLACEMENT, _POSITIVE_TESTS_ONLY)
    assert RULE_EATS_TEXT in _names(_scan(diff))


def test_inline_allow_list_call_yields_no_eats_text_finding():
    """The allow-list given straight to re.sub is not evaluated either."""
    diff = _diff(_INLINE_ALLOW_LIST, _POSITIVE_TESTS_ONLY)
    assert RULE_EATS_TEXT not in _names(_scan(diff))


def test_module_level_allow_list_yields_no_eats_text_finding():
    """The production shape: a module-level allow-list constant used by name."""
    diff = _diff(_MODULE_ALLOW_LIST, _POSITIVE_TESTS_ONLY)
    assert RULE_EATS_TEXT not in _names(_scan(diff))


def test_module_level_constant_is_read_by_the_rule():
    """A module-level raw-string constant that eats `|h` is still flagged,
    so the allow-list case above is a real skip, not an unread constant."""
    diff = _diff(_MODULE_LINK_STRIPPER, _POSITIVE_TESTS_ONLY)
    assert RULE_EATS_TEXT in _names(_scan(diff))
