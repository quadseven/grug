import re

CONTEXT_TEXT = """## Product surface

| Term | Definition |
|---|---|
| **Persona** | A bounded behavior surface Grug applies per PR |
"""

GLOSSARY_TABLE_PATTERN = re.compile(r"^##\s+\w+\s*$")
_TERM_PATTERN = re.compile(r"\|\s*([^|]+?)\s*\|")
_DEFINITION_PATTERN = re.compile(r"\|\s*([^|]*?(?:\|(?!\|)[^|]*)?)\s*\|$")

def _parse_glossary_section(section_text: str) -> list:
    """Parse a glossary section into glossary entries."""
    entries: list = []
    in_table = False
    lines = section_text.split("\n")
    i = 0

    print(f"Total lines: {len(lines)}")

    while i < len(lines):
        line = lines[i]
        print(f"Line {i}: {repr(line)}")

        if GLOSSARY_TABLE_PATTERN.match(line):
            print("  -> MATCHES table header")
            in_table = True
            i += 1
            continue

        if in_table:
            print(f"  -> in_table = True, line = {repr(line)}")
            if line.strip() == "":
                print("  -> empty line, skipping")
                i += 1
                continue

            term_match = _TERM_PATTERN.match(line)
            definition_match = _DEFINITION_PATTERN.match(line)
            print(f"  -> term_match: {term_match is not None}, definition_match: {definition_match is not None}")

            if term_match and definition_match:
                term = term_match.group(1).strip().replace("**", "").strip()
                definition = definition_match.group(1).strip()
                print(f"      Term: {term}, Definition: {definition}")
                entries.append((term, definition))
        i += 1

    return entries

sections = CONTEXT_TEXT.split("\n## ")
print(f"Number of sections: {len(sections)}")

for section in sections:
    if not section.strip():
        continue
    entries = _parse_glossary_section(section)
    print(f"Parsed {len(entries)} entries")
    for term, definition in entries:
        print(f"  {term}: {definition}")
