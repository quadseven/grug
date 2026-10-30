import re

context_text = """
## Product surface

| Term | Definition |
|---|---|
| **Persona** | A bounded behavior surface Grug applies per PR |
| **Elder** (the **code-reviewer** persona) | The oldest grug |
"""

_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_STOP_TOKENS = frozenset({
    "md", "py", "ts", "js", "go", "rs", "src", "lib", "app", "web",
    "services", "shared", "test", "tests", "spec", "docs", "doc"
})

def _tokens(text: str) -> set[str]:
    return {
        t
        for t in _TOKEN_SPLIT_RE.split(text.lower())
        if t and t not in _STOP_TOKENS and len(t) > 2
    }

# Parse glossary entries
GLOSSARY_TABLE_PATTERN = re.compile(r"^##\s+\w+\s*$")
_TERM_PATTERN = re.compile(r"\|\s*([^|]+?)\s*\|")
_DEFINITION_PATTERN = re.compile(r"\|\s*([^|]*?(?:\|(?!\|)[^|]*)?)\s*\|$")

sections = context_text.split("\n## ")
for section in sections:
    print(f"Section: {section[:50]}")
    in_table = False
    for line in section.split("\n"):
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
                print(f"  Term: {term}")
                print(f"  Tokens: {_tokens(term + ' ' + definition)}")

changed_tokens = {"persona", "code", "reviewer", "personas"}
print(f"\nChanged tokens: {changed_tokens}")
