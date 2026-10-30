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
| **Elder** | The oldest grug who reads markings one line at a time |
"""

print("Debugging section parsing:")
sections = CONTEXT_TEXT.split("\n## ")
print(f"Number of sections: {len(sections)}")

for section in sections:
    if not section.strip():
        continue
    print(f"\nSection: {repr(section)}")

    lines = section.split("\n")
    print(f"Number of lines: {len(lines)}")

    in_table = False
    i = 0
    while i < len(lines):
        line = lines[i]
        print(f"\n  i={i}, line={repr(line)}, in_table={in_table}")

        if _GLOSSARY_TABLE_PATTERN.match(line):
            in_table = True
            i += 1
            continue

        if in_table:
            if line.strip() == "":
                i += 1
                continue

            term_match = _TERM_PATTERN.match(line)
            definition_match = _DEFINITION_PATTERN.match(line)
            print(f"    term_match: {term_match is not None}, definition_match: {definition_match is not None}")

            if term_match and definition_match:
                term = term_match.group(1).strip().replace("**", "").strip()
                definition = definition_match.group(1).strip()
                print(f"      Term: {term}, Definition: {definition}")
        i += 1
