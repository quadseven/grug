import re
_TABLE_PAIR_RE = re.compile(
    r'\(\s*([\'"])(.*?)\1\s*,\s*\1(.*?)\1\s*\)'
)

# Test mixed quotes more carefully - let's see the full match
text = '("raw", \'want\')'
print(f'Text: {text}')
for m in _TABLE_PAIR_RE.finditer(text):
    print(f'  Full match: {m.group(0)!r}')
    print(f'  Groups: {m.groups()}')

# What about this case?
text = '("raw", "want")'
print(f'Text: {text}')
for m in _TABLE_PAIR_RE.finditer(text):
    print(f'  Full match: {m.group(0)!r}')
    print(f'  Groups: {m.groups()}')

# Let's test with escaped quotes inside
text = r'("raw \" quote", "want")'
print(f'Text: {text}')
for m in _TABLE_PAIR_RE.finditer(text):
    print(f'  Full match: {m.group(0)!r}')
    print(f'  Groups: {m.groups()}')