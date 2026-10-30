import re

CONTEXT_TEXT = """## Product surface

| Term | Definition |
|---|---|
| **Persona** | A bounded behavior surface Grug applies per PR |
"""

GLOSSARY_TABLE_PATTERN = re.compile(r"^##\s+\w+\s*$")
_TERM_PATTERN = re.compile(r"\|\s*([^|]+?)\s*\|")
_DEFINITION_PATTERN = re.compile(r"\|\s*([^|]*?(?:\|(?!\|)[^|]*)?)\s*\|$")

for line in CONTEXT_TEXT.split("\n"):
    print(f"Line: {repr(line)}")
    if GLOSSARY_TABLE_PATTERN.match(line):
        print("  MATCHES table header")
    if _TERM_PATTERN.match(line):
        print(f"  MATCHES term: {repr(_TERM_PATTERN.match(line).group(1))}")
    if _DEFINITION_PATTERN.match(line):
        print(f"  MATCHES definition: {repr(_DEFINITION_PATTERN.match(line).group(1))}")
