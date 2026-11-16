import sys
sys.path.insert(0, 'services/_shared')

import os
os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
os.environ['AWS_ACCESS_KEY_ID'] = 'testing'
os.environ['AWS_SECRET_ACCESS_KEY'] = 'testing'

from personas.code_reviewer.diff_parser import file_changes, parse_diff
from personas.code_reviewer.sanitizer_review import (
    RULE_EATS_TEXT,
    RULE_MISSING_TEST,
    scan_sanitizers,
)

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
    for f in findings:
        print(f"  Finding: {f.rule_name} - {f.message}")
    # This should not flag RULE_EATS_TEXT because the replacement is " " not ""
    # But currently it does because _pattern_for hardcodes "" for constants
    assert RULE_EATS_TEXT not in rule_names, f"Expected no {RULE_EATS_TEXT}, got {rule_names}"

if __name__ == "__main__":
    test_constant_pattern_with_sub_call()
    print("Test passed!")