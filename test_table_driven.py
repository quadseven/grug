import sys
sys.path.insert(0, 'services/_shared')

import os
os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
os.environ['AWS_ACCESS_KEY_ID'] = 'testing'
os.environ['AWS_SECRET_ACCESS_KEY'] = 'testing'

from personas.code_reviewer.sanitizer_review import _has_table_driven_passthrough

# Test 7: _has_table_driven_passthrough false positive
# The review says: "Grug see the table-pair probe blind. `_has_table_driven_passthrough` 
# scans the whole joined `full` with `if "for " in full and " in cases:" in full` — 
# BOTH conditions scoped on the ENTIRE test file, not on any single test."

# Case 1: A test file with 'for ' in one test and ' in cases:' in another test
# but no actual table-driven test with raw==want
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
print(f"Case 1 - 'for ' and ' in cases:' in different tests: {result} (expected: False)")

# Case 2: A test file with a table-driven test but NO raw==want pair
lines2 = [
    (1, "def test_sanitize():"),
    (2, "    cases = ["),
    (3, "        ('input1', 'output1'),"),
    (4, "        ('input2', 'output2'),"),
    (5, "    ]"),
    (6, "    for raw, want in cases:"),
    (7, "        assert func(raw) == want"),
]
result2 = _has_table_driven_passthrough(lines2)
print(f"Case 2 - table-driven but no raw==want: {result2} (expected: False)")

# Case 3: A test file with a table-driven test WITH raw==want pair
lines3 = [
    (1, "def test_sanitize():"),
    (2, "    cases = ["),
    (3, "        ('input1', 'output1'),"),
    (4, "        ('passthrough', 'passthrough'),"),  # raw == want
    (5, "    ]"),
    (6, "    for raw, want in cases:"),
    (7, "        assert func(raw) == want"),
]
result3 = _has_table_driven_passthrough(lines3)
print(f"Case 3 - table-driven with raw==want: {result3} (expected: True)")

# Case 4: The review mentions the check is on the ENTIRE file
# What if there's a 'for ' loop somewhere else and ' in cases:' in the table?
lines4 = [
    (1, "def test_other():"),
    (2, "    for x in items:"),  # 'for ' here
    (3, "        pass"),
    (4, ""),
    (5, "def test_sanitize():"),
    (6, "    cases = [('a', 'b')]"),  # ' in cases:' here but no raw==want
    (7, "    for raw, want in cases:"),
    (8, "        assert func(raw) == want"),
]
result4 = _has_table_driven_passthrough(lines4)
print(f"Case 4 - 'for ' in other test, ' in cases:' in table (no raw==want): {result4} (expected: False)")