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

# Test with a constant pattern that is NOT a negated char class
# but uses a non-empty replacement in .sub()
sanitizer = [
    "import re",
    "",
    "",
    "def sanitize_chat(text):",
    '    _FALLBACK = re.compile(r"\\|[Hh]")',  # This pattern eats plain text
    '    return _FALLBACK.sub(" ", text)',  # But replacement is " " not ""
]
tests = [
    "import chat_format",
    "",
    "",
    "def test_strips_a_link():",
    '    assert chat_format.sanitize_chat("|Hitem:1|h[Sword]|h") == "[Sword]"',
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
print(f"Findings: {rule_names}")
for f in findings:
    print(f"  {f.rule_name}: {f.message}")

# The pattern \|[Hh] with replacement " " changes "a |hop" to "a  hop" (two spaces)
# This should be flagged because it changes ordinary text
# But the constant path hardcodes replacement="", so it would think the result is "aop"
# Let's see what actually happens
assert RULE_EATS_TEXT in rule_names, f"Expected {RULE_EATS_TEXT}, got {rule_names}"
print("Test passed - correctly flags!")