import re

pattern1 = r"^##"
pattern2 = r"^#\s+"
pattern3 = r"^#\s+\w"
pattern4 = r"^##\s+"

lines = [
    '## Product surface',
    '# Product surface',
]

for pattern in [pattern1, pattern2, pattern3, pattern4]:
    print(f"Pattern: {repr(pattern)}")
    for line in lines:
        match = re.match(pattern, line)
        print(f"  Line {repr(line)}: {match}")
    print()
