import re

_CONTEXT_PATH = "CONTEXT.md"
_MAX_CONTEXT_CHARS = 8000

CONTEXT_TEXT = """## Product surface

| Term | Definition |
|---|---|
| **Persona** | A bounded behavior surface Grug applies per PR |
"""

_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_STOP_TOKENS = frozenset({
    "md", "py", "ts", "js", "go", "rs", "src", "lib", "app", "web",
    "services", "shared", "test", "tests", "spec", "docs", "doc"
})

GLOSSARY_TABLE_PATTERN = re.compile(r"^##\s+\w+\s*$")
_TERM_PATTERN = re.compile(r"\|\s*([^|]+?)\s*\|")
_DEFINITION_PATTERN = re.compile(r"\|\s*([^|]*?(?:\|(?!\|)[^|]*)?)\s*\|$")

def _tokens(text: str) -> set[str]:
    return {
        t
        for t in _TOKEN_SPLIT_RE.split(text.lower())
        if t and t not in _STOP_TOKENS and len(t) > 2
    }

def _parse_glossary_section(section_text: str) -> list:
    """Parse a glossary section into glossary entries."""
    entries: list = []
    in_table = False

    for line in section_text.split("\n"):
        if GLOSSARY_TABLE_PATTERN.match(line):
            in_table = True
            continue
        if in_table and line.strip() == "":
            break

        if in_table:
            term_match = _TERM_PATTERN.match(line)
            definition_match = _DEFINITION_PATTERN.match(line)
            if term_match and definition_match:
                term = term_match.group(1).strip()
                definition = definition_match.group(1).strip()
                if term and definition:
                    entries.append((term, definition))

    return entries

# Test parsing
sections = CONTEXT_TEXT.split("\n## ")
for section in sections:
    if not section.strip():
        continue
    entries = _parse_glossary_section(section)
    print(f"Parsed {len(entries)} entries in section")
    for term, definition in entries:
        print(f"  {term}: {definition}")

changed_tokens = {"persona", "code", "reviewer"}
relevant = _parse_glossary_section(CONTEXT_TEXT)
print(f"\nChanged tokens: {changed_tokens}")

for term, definition in relevant:
    entry_tokens = _tokens(term + " " + definition)
    if entry_tokens & changed_tokens:
        print(f"Relevant: {term}")
        print(f"  Tokens match: {entry_tokens & changed_tokens}")
