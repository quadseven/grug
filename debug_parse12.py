import re

GLOSSARY_TABLE_PATTERN = re.compile(r"^##\s+\w+\s*$")

lines = [
    '## Product surface',
    '## Product surface with more text',
    '# Product surface',
]

for line in lines:
    print(f"Line: {repr(line)}")
    print(f"  Match: {GLOSSARY_TABLE_PATTERN.match(line)}")
