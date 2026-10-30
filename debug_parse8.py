import sys
sys.path.insert(0, 'services')

from _shared.personas.code_reviewer.repo_docs import (
    _parse_glossary_section,
    _TERM_PATTERN,
    _DEFINITION_PATTERN,
)

CONTEXT_TEXT = """## Product surface

| Term | Definition |
|---|---|
| **Persona** | A bounded behavior surface Grug applies per PR |
| **Elder** | The oldest grug who reads markings one line at a time |
"""

print("Testing section parsing:")
sections = CONTEXT_TEXT.split("\n## ")
print(f"Number of sections: {len(sections)}")

for section in sections:
    print(f"\nSection: {repr(section)}")
    if not section.strip():
        continue

    lines = section.split("\n")
    print(f"Number of lines in section: {len(lines)}")

    for i, line in enumerate(lines):
        print(f"\n  Line {i}: {repr(line)}")
        if _TERM_PATTERN.match(line):
            print(f"    MATCHES term: {repr(_TERM_PATTERN.match(line).group(1))}")
        if _DEFINITION_PATTERN.match(line):
            print(f"    MATCHES definition: {repr(_DEFINITION_PATTERN.match(line).group(1))}")

    entries = _parse_glossary_section(section)
    print(f"Parsed {len(entries)} entries")
