import sys
sys.path.insert(0, 'services')

from _shared.personas.code_reviewer.repo_docs import _tokens

path = "services/_shared/personas/code_reviewer/persona.py"
tokens = _tokens(path.replace("/", " ").replace(".", " "))
print(f"Tokens from path: {tokens}")

path2 = "personas/registry.py"
tokens2 = _tokens(path2.replace("/", " ").replace(".", " "))
print(f"Tokens from path2: {tokens2}")
