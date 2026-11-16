import sys
sys.path.insert(0, 'services/_shared')
from personas.code_reviewer.sanitizer_review import _TABLE_PAIR_RE

# Test mixed quotes
text = '("raw", \'want\')'
print(f'Text: {text}')
matches = list(_TABLE_PAIR_RE.finditer(text))
print(f'Matches: {len(matches)}')
for m in matches:
    print(f'  Full match: {m.group(0)!r}')
    print(f'  Groups: {m.groups()}')

# Same double quotes
text = '("raw", "want")'
print(f'Text: {text}')
matches = list(_TABLE_PAIR_RE.finditer(text))
print(f'Matches: {len(matches)}')
for m in matches:
    print(f'  Full match: {m.group(0)!r}')
    print(f'  Groups: {m.groups()}')