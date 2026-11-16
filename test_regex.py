import re
_TABLE_PAIR_RE = re.compile(
    r'\(\s*([\'"])(.*?)\1\s*,\s*\1(.*?)\1\s*\)'
)

# Test mixed quotes more carefully
text = '("raw", "want")'
for m in _TABLE_PAIR_RE.finditer(text):
    print(f'  Match: {m.groups()}')

text = "('raw', 'want')"
for m in _TABLE_PAIR_RE.finditer(text):
    print(f'  Match: {m.groups()}')

# Actual mixed quotes
text = '("raw", \'want\')'
print(f'Text: {text}')
for m in _TABLE_PAIR_RE.finditer(text):
    print(f'  Match: {m.groups()}')

# Multiline
text = '''(
    "raw",
    "want"
)'''
for m in _TABLE_PAIR_RE.finditer(text):
    print(f'Multiline match: {m.groups()}')