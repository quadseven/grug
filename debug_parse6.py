import sys
sys.path.insert(0, 'services')

from _shared.personas.code_reviewer.repo_docs import _parse_glossary_section, _tokens

CONTEXT_TEXT = """## Product surface

| Term | Definition |
|---|---|
| **Persona** | A bounded behavior surface Grug applies per PR |
| **Elder** | The oldest grug who reads markings one line at a time |
"""

# Test parsing
sections = CONTEXT_TEXT.split("\n## ")
for section in sections:
    if not section.strip():
        continue
    entries = _parse_glossary_section(section)
    print(f"Parsed {len(entries)} entries")
    for term, definition in entries:
        print(f"  {term}: {definition}")

changed_tokens = {"persona", "code", "reviewer"}
print(f"\nChanged tokens: {changed_tokens}")

relevant = _parse_glossary_section(CONTEXT_TEXT)
print(f"\nTotal entries: {len(relevant)}")

for entry in relevant:
    entry_tokens = _tokens(entry.term + " " + entry.definition)
    print(f"Entry: {entry.term}")
    print(f"  Tokens: {entry_tokens}")
    print(f"  Match: {entry_tokens & changed_tokens}")
    if entry_tokens & changed_tokens:
        print(f"  RELEVANT!")
