import re

pattern = r"^##\s+\w+\s*$"

lines = [
    '## Product surface',
    '# Product surface',
]

for line in lines:
    print(f"Line: {repr(line)}")
    print(f"Pattern: {repr(pattern)}")
    match = re.match(pattern, line)
    print(f"Match: {match}")
    if match:
        print(f"Groups: {match.groups()}")
    print()
