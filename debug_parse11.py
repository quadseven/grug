import re

GLOSSARY_TABLE_PATTERN = re.compile(r"^##\s+\w+\s*$")
_TERM_PATTERN = re.compile(r"\|\s*([^|]+?)\s*\|")
_DEFINITION_PATTERN = re.compile(r"\|\s*([^|]*?(?:\|(?!\|)[^|]*)?)\s*\|$")

line1 = '## Product surface'
line2 = '| Term | Definition |'

print(f"Line 1: {repr(line1)}")
print(f"Pattern match: {GLOSSARY_TABLE_PATTERN.match(line1)}")

print(f"\nLine 2: {repr(line2)}")
print(f"Term pattern match: {_TERM_PATTERN.match(line2)}")
print(f"Definition pattern match: {_DEFINITION_PATTERN.match(line2)}")
