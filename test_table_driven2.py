import sys
sys.path.insert(0, 'services/_shared')

import os
os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
os.environ['AWS_ACCESS_KEY_ID'] = 'testing'
os.environ['AWS_SECRET_ACCESS_KEY'] = 'testing'

from personas.code_reviewer.sanitizer_review import _has_table_driven_passthrough

# Edge case: raw==want pair in a non-table context, "for " and " in cases:" elsewhere
lines = [
    (1, "def test_tuple_literal():"),
    (2, "    # This is just a tuple, not a table-driven test"),
    (3, "    pair = ('same', 'same')"),  # raw == want but not in a table
    (4, ""),
    (5, "def test_other():"),
    (6, "    for x in items:"),  # 'for ' here
    (7, "        pass"),
    (8, ""),
    (9, "def test_cases_var():"),
    (10, "    cases = [('a', 'b')]"),  # ' in cases:' here
    (11, "    for raw, want in cases:"),
    (12, "        assert func(raw) == want"),
]
result = _has_table_driven_passthrough(lines)
print(f"Edge case - raw==want in tuple, 'for ' and ' in cases:' elsewhere: {result} (expected: False)")