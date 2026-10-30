import re

CONTEXT_TEXT = """## Product surface

| Term | Definition |
|---|---|
| **Persona** | A bounded behavior surface Grug applies per PR |
"""

GLOSSARY_TABLE_PATTERN = re.compile(r"^##\s+\w+\s*$")
_TERM_PATTERN = re.compile(r"\|\s*([^|]+?)\s*\|")
_DEFINITION_PATTERN = re.compile(r"\|\s*([^|]*?(?:\|(?!\|)[^|]*)?)\s*\|$")

sections = CONTEXT_TEXT.split("\n## ")
print(f"Number of sections: {len(sections)}")

for i, section in enumerate(sections):
    print(f"\nSection {i}: {repr(section)}")
    if not section.strip():
        continue
    in_table = False
    line_count = 0
    for line in section.split("\n"):
        print(f"  Line {line_count}: {repr(line)}")
        if GLOSSARY_TABLE_PATTERN.match(line):
            print(f"    -> in_table = True")
            in_table = True
            continue
        if in_table and line.strip() == "":
            print(f"    -> break (empty line)")
            break

        if in_table:
            term_match = _TERM_PATTERN.match(line)
            definition_match = _DEFINITION_PATTERN.match(line)
            if term_match and definition_match:
                term = term_match.group(1).strip().replace("**", "").strip()
                definition = definition_match.group(1).strip()
                print(f"      Term: {term}, Definition: {definition}")
            line_count += 1
            if line_count > 5:
                break
