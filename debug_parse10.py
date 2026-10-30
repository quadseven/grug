import sys
sys.path.insert(0, 'services')

from _shared.personas.code_reviewer.repo_docs import (
    _GLOSSARY_TABLE_PATTERN,
    _TERM_PATTERN,
    _DEFINITION_PATTERN,
)

CONTEXT_TEXT = """## Product surface

| Term | Definition |
|---|---|
| **Persona** | A bounded behavior surface Grug applies per PR |
"""

print("Testing patterns:")
for i, line in enumerate(CONTEXT_TEXT.split("\n")):
    print(f"\nLine {i}: {repr(line)}")

    if _GLOSSARY_TABLE_PATTERN.match(line):
        print(f"  MATCHES header pattern")

    if _TERM_PATTERN.match(line):
        print(f"  MATCHES term pattern: {repr(_TERM_PATTERN.match(line).group(1))}")

    if _DEFINITION_PATTERN.match(line):
        print(f"  MATCHES definition pattern: {repr(_DEFINITION_PATTERN.match(line).group(1))}")

print("\n" + "="*60)
print("Testing section parsing logic:")
sections = CONTEXT_TEXT.split("\n## ")
print(f"Number of sections: {len(sections)}")

for section in sections:
    if not section.strip():
        continue

    lines = section.split("\n")
    print(f"\nSection has {len(lines)} lines")

    for i, line in enumerate(lines):
        print(f"\n  Line {i}: {repr(line)}")

        if _GLOSSARY_TABLE_PATTERN.match(line):
            print(f"    -> in_table = True")

        if _TERM_PATTERN.match(line):
            print(f"    -> MATCH term: {repr(_TERM_PATTERN.match(line).group(1))}")

        if _DEFINITION_PATTERN.match(line):
            print(f"    -> MATCH definition: {repr(_DEFINITION_PATTERN.match(line).group(1))}")
