"""Tests to expose the issues mentioned in review findings."""

from __future__ import annotations

from personas.code_reviewer.diff_parser import file_changes, parse_diff
from personas.code_reviewer.sanitizer_review import (
    RULE_EATS_TEXT,
    RULE_MISSING_TEST,
    scan_sanitizers,
    _replacement_for_call,
    _TABLE_PAIR_RE,
    _has_table_driven_passthrough,
    _plain_text_change,
)
import re

# Test 1: _replacement_for_call with function as replacement
def test_replacement_for_call_with_func():
    body = 're.sub(r"pattern", some_func, text)'
    match = re.search(r'\bre\.(?:subn?|compile|search|match|fullmatch|findall|finditer)\s*\(', body)
    assert match is not None
    result = _replacement_for_call(body, match)
    print(f"Test 1 - function as replacement: '{result}' (expected: '')")
    assert result == "", f"Expected empty string for function replacement, got '{result}'"

# Test 2: _replacement_for_call with empty string replacement
def test_replacement_for_call_with_empty_string():
    body = 're.sub(r"pattern", "", text)'
    match = re.search(r'\bre\.(?:subn?|compile|search|match|fullmatch|findall|finditer)\s*\(', body)
    assert match is not None
    result = _replacement_for_call(body, match)
    print(f"Test 2 - empty string replacement: '{result}' (expected: '')")
    assert result == "", f"Expected empty string, got '{result}'"

# Test 3: _replacement_for_call should not pick up third argument
def test_replacement_for_call_third_arg():
    body = 're.sub(r"pattern", func, "input string")'
    match = re.search(r'\bre\.(?:subn?|compile|search|match|fullmatch|findall|finditer)\s*\(', body)
    assert match is not None
    result = _replacement_for_call(body, match)
    print(f"Test 3 - third arg string: '{result}' (expected: '')")
    assert result == "", f"Expected empty string (should not pick up third arg), got '{result}'"

# Test 4: Constant pattern with .sub() call should use actual replacement
def test_constant_pattern_with_sub_call():
    """A sanitiser using a constant pattern with .sub(" ", text) should not be flagged."""
    sanitizer = [
        "import re",
        "",
        "",
        "def sanitize_title(text):",
        '    _ALLOWED = re.compile(r"[^A-Za-z0-9 ,.]")',
        '    return _ALLOWED.sub(" ", text)',
    ]
    tests = [
        "import chat_format",
        "",
        "",
        "def test_strips_markup():",
        '    assert chat_format.sanitize_title("|Hitem:1|h[Sword]|h") == "[Sword]"',
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
    
    diff = _diff(sanitizer, tests)
    findings = scan_sanitizers(file_changes(diff), parse_diff(diff))
    rule_names = {f.rule_name for f in findings}
    print(f"Test 4 - constant with .sub: {rule_names}")
    # This should not flag RULE_EATS_TEXT because the replacement is " " not ""
    # But currently it does because _pattern_for hardcodes "" for constants
    assert RULE_EATS_TEXT not in rule_names, f"Expected no {RULE_EATS_TEXT}, got {rule_names}"

# Test 5: _TABLE_PAIR_RE with mixed quotes
def test_table_pair_re_mixed_quotes():
    text = '("raw", \'want\')'
    matches = list(_TABLE_PAIR_RE.finditer(text))
    print(f"Test 5 - mixed quotes: {len(matches)} matches (expected: 1)")
    # Currently fails because regex expects same quote char
    assert len(matches) == 1, f"Expected 1 match for mixed quotes, got {len(matches)}"

# Test 6: _TABLE_PAIR_RE with multiline
def test_table_pair_re_multiline():
    text = '(\n    "raw",\n    "want"\n)'
    matches = list(_TABLE_PAIR_RE.finditer(text))
    print(f"Test 6 - multiline: {len(matches)} matches (expected: 1)")
    # Currently fails because .*? doesn't match newlines
    assert len(matches) == 1, f"Expected 1 match for multiline, got {len(matches)}"

# Test 7: _has_table_driven_passthrough false positive
def test_has_table_driven_passthrough_false_positive():
    """A test file with 'for ' and ' in cases:' anywhere should not trigger."""
    lines = [
        (1, "def test_something():"),
        (2, "    for i in range(10):"),  # has "for "
        (3, "        pass"),
        (4, ""),
        (5, "def test_sanitize():"),
        (6, "    cases = [('a', 'b')]"),  # has " in cases:"
        (7, "    for raw, want in cases:"),
        (8, "        assert func(raw) == want"),
    ]
    result = _has_table_driven_passthrough(lines)
    print(f"Test 7 - false positive: {result} (expected: False)")
    # Currently returns True because both strings exist in the file
    assert result == False, f"Expected False (no passthrough pair), got {result}"

# Test 8: _plain_text_change with whitespace-only pattern
def test_plain_text_change_whitespace_only():
    result = _plain_text_change(r"\s+", " ")
    print(f"Test 8 - whitespace only pattern: {result} (expected: None)")
    assert result is None, f"Expected None for whitespace-only pattern, got {result}"

# Test 9: _plain_text_change with negated char class
def test_plain_text_change_negated_char_class():
    result = _plain_text_change(r"[^A-Za-z0-9 ,.]", " ")
    print(f"Test 9 - negated char class: {result} (expected: None)")
    assert result is None, f"Expected None for negated char class, got {result}"

# Test 10: Duplicate _plain_text_change call in _eats_text_findings
# This is more of a code quality issue - we'll verify by checking the code

if __name__ == "__main__":
    test_replacement_for_call_with_func()
    test_replacement_for_call_with_empty_string()
    test_replacement_for_call_third_arg()
    test_constant_pattern_with_sub_call()
    test_table_pair_re_mixed_quotes()
    test_table_pair_re_multiline()
    test_has_table_driven_passthrough_false_positive()
    test_plain_text_change_whitespace_only()
    test_plain_text_change_negated_char_class()
    print("\nAll tests completed!")