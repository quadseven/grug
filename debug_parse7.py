import sys
sys.path.insert(0, 'services')

from _shared.personas.code_reviewer.repo_docs import (
    _parse_glossary_section,
    _tokens,
    _TERM_PATTERN,
    _DEFINITION_PATTERN,
)

CONTEXT_TEXT = """## Product surface

| Term | Definition |
|---|---|
| **Persona** | A bounded behavior surface Grug applies per PR |
| **Elder** | The oldest grug who reads markings one line at a time |
"""

print("Testing regex patterns:")
for i, line in enumerate(CONTEXT_TEXT.split("\n")):
    print(f"Line {i}: {repr(line)}")
    if _TERM_PATTERN.match(line):
        print(f"  MATCHES term: {repr(_TERM_PATTERN.match(line).group(1))}")
    if _DEFINITION_PATTERN.match(line):
        print(f"  MATCHES definition: {repr(_DEFINITION_PATTERN.match(line).group(1))}")

print("\nTesting section parsing:")
sections = CONTEXT_TEXT.split("\n## ")
print(f"Number of sections: {len(sections)}")

for i, section in enumerate(sections):
    print(f"\nSection {i}: {repr(section)}")
    if not section.strip():
        continue
    entries = _parse_glossary_section(section)
    print(f"Parsed {len(entries)} entries")
    for term, definition in entries:
        print(f"  {term}: {definition}")

changed_tokens = {"persona", "code", "reviewer"}
print(f"\nChanged tokens: {changed_tokens}")

relevant = _select_relevant_entries(CONTEXT_TEXT, changed_tokens)
print(f"\nRelevant entries: {len(relevant)}")
for entry in relevant:
    print(f"  {entry.term}: {entry.definition}")
