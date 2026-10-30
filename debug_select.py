import sys
sys.path.insert(0, 'services')

from _shared.personas.code_reviewer.repo_docs import (
    _tokens,
    _select_relevant_entries,
)

CONTEXT_TEXT = """# CONTEXT.md — grug domain glossary

The vocabulary used in `services/`, `infra/`, and `web/`. Terms map to identifiers a contributor will encounter while reading code. New contributors should be able to read this file and use the terms correctly without grepping. Drift between this file and the code is a bug; fix the file in the same PR that renames a concept.

> **Architecture decisions live in [`docs/adr/`](docs/adr/).** This file is the lexicon; the ADRs are the load-bearing choices.

## Product surface

| Term | Definition |
|---|---|
| **Persona** | A bounded behavior surface Grug applies per PR |
| **Elder** (the **code-reviewer** persona) | The oldest grug who reads markings one line at a time |
"""

changed_tokens = {"persona", "code", "reviewer", "personas"}

relevant = _select_relevant_entries(CONTEXT_TEXT, changed_tokens, max_entries=10)
print(f"Relevant entries: {len(relevant)}")

for entry in relevant:
    entry_tokens = _tokens(entry.term + " " + entry.definition)
    print(f"Entry: {entry.term}")
    print(f"  Tokens: {entry_tokens}")
    print(f"  Match: {entry_tokens & changed_tokens}")

print("\nLooking for jargon line in CONTEXT.md:")
with open("CONTEXT.md", "r") as f:
    lines = f.readlines()
    for i, line in enumerate(lines):
        if 'Definition of Ready' in line:
            print(f"Found at line {i}: {line.strip()}")
