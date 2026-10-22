"""Deterministic checks for results no run produced (#1368).

Lane pull requests published numbers nothing measured:

- a benchmark doc gave pass rates of 47.0% and 43.0% for a 30-row slice,
  which no whole count out of 30 gives at one decimal place;
- a model comparison wrote an agreement percentage and a switch-or-not
  verdict from a `--simulate` mode that returns hardcoded tiers;
- an audit doc said two handsets were inspected on a given date and listed
  their certificate digests, with no command output behind it;
- a view model derived a progress percentage from a count of sessions, so
  three sessions read as three percent.

Each shape is one high `fabricated-result` finding, anchored on the added
line that states the result:

- impossible rate: a percentage stated to at least one decimal place, on a
  line that names it a rate (pass, accuracy, agreement, success...), in a
  doc or data file whose hunks state a sample size, when no whole count out
  of any stated size rounds or truncates to it. In a data file the value
  may carry no percent sign: a rate key (`"pass": [47.0, 43.0]`) whose
  values are on the percent scale (one above 1, none above 100) is read
  the same way (#1377);
- simulated result: a results, report, verdict or benchmark doc or data
  file whose added text says its numbers come from a simulate, mock, stub
  or stand-in path and that states a figure or a verdict;
- unbacked audit: a new doc titled as an audit or inspection that claims
  one on a date and carries no command output, with no raw output file or
  run link anywhere in the diff;
- percent from a count: a non-test assignment to a `*percent*`/`*pct*`
  name from `len(...)`, `.length` or a count, with no division.

Bounded: every scan is one pass over the diff's lines, and a line longer
than `MAX_LINE_CHARS` is skipped. Past `MAX_ADDED_CHARS` of hunk text the
checks are not evaluated (no finding).

Pure: file changes and parsed hunks in; findings out.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator

from personas.code_reviewer.diff_parser import DiffHunk, FileChange
from personas.code_reviewer.persona import Finding
from personas.code_reviewer.test_paths import is_test_file

RULE = "fabricated-result"
# Past this much added text the scan is not evaluated: no finding.
MAX_ADDED_CHARS = 4_000_000
# Longer lines (minified or generated text) are skipped, never scanned.
MAX_LINE_CHARS = 4_000
# Stated sample sizes per file considered; more only adds ways to be possible.
MAX_SIZES = 64
# (percentage, sample size) pairs tested across the whole diff; past it the
# rate check is not evaluated for the rest of the diff.
MAX_RATE_CHECKS = 50_000

_RESULT_SUFFIXES = (
    ".md", ".rst", ".txt", ".adoc", ".json", ".jsonl", ".csv", ".tsv",
    ".yaml", ".yml", ".html",
)
_DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")

# A percentage given to at least one decimal place.
_DECIMAL_PCT_RE = re.compile(r"(?<![\w.])(\d{1,3})\.(\d{1,3})\s?%")
# The line says the percentage is a rate of whole outcomes, not an error
# or a mean (MAPE, latency share, utilization).
_RATE_WORD_RE = re.compile(
    r"\bpass(?:es|ed|ing)?\b|\bpass[- ]?rates?\b|\baccuracy\b|\bagree(?:d|ment|s)?\b"
    r"|\bsuccess(?:ful|es)?\b|\bsolved\b|\bresolved\b|\bcorrect(?:ly)?\b"
    r"|\bhit[- ]rate\b|\bwin[- ]rate\b|\bfail(?:ed|ure|ures|s)?[- ]?(?:rate)?\b",
    re.IGNORECASE,
)
# A rate key in a data file and its value: one number or a flat list.
_RATE_KEY_RE = re.compile(
    r"[\"']?\b(?:pass(?:[_-]?rates?)?|accuracy|agreement|success(?:[_-]?rates?)?|solved"
    r"|resolved|correct|hit[_-]?rate|win[_-]?rate)\b[\"']?\s*[:=]\s*"
    r"(\[[^\]\[]{0,400}\]|-?\d{1,3}(?:\.\d{1,3})?(?![\d.]))",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"(?<![\w.])(-?\d{1,6})(?:\.(\d{1,3}))?(?![\w.])")
_DATA_SUFFIXES = (".json", ".jsonl", ".csv", ".tsv", ".yaml", ".yml")
# Ways a doc states its sample size.
_SIZE_RES = (
    re.compile(r"(?<![\w])[\"']?[nN][\"']?\s*[=:]\s*(\d+)"),
    re.compile(
        r"\b(\d+)\s+(?:rows|samples|cases|items|tasks|questions|prompts|examples"
        r"|trials|runs|problems|instances|records|queries|epics|attempts)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b\d+\s+(?:of|out of)\s+(\d+)\b"),
    re.compile(r"(?<![\w./-])\d+/(\d+)(?![\w/.-])"),
)

# A results file: its path names what it holds.
_RESULT_PATH_RE = re.compile(
    r"results?|report|verdict|benchmark|scoreboard|comparison|leaderboard"
    r"|evaluation|findings|audit",
    re.IGNORECASE,
)
_SIMULATED_RE = re.compile(
    r"--simulate\b|--dry-run\b|--mock\b"
    r"|\bsimulat(?:ed|ion|e) (?:mode|responses?|runs?|data|results?|outputs?|stand-?ins?)\b"
    r"|\bstand-?in (?:responses?|data|results?|outputs?|runs?|verdicts?)\b"
    r"|\b(?:mock(?:ed)?|stubbed|fake|canned|hard-?coded) "
    r"(?:responses?|data|results?|outputs?|runs?|verdicts?|tiers?)\b",
    re.IGNORECASE,
)
_FIGURE_RE = re.compile(
    r"\d(?:\.\d+)?\s?%|\b\d+\s+(?:of|out of)\s+\d+\b|\bverdict\b|\brecommend(?:s|ed|ation)?\b",
    re.IGNORECASE,
)

_AUDIT_TITLE_RE = re.compile(r"\baudit\b|\binspection\b|\binspected\b", re.IGNORECASE)
_AUDIT_CLAIM_RE = re.compile(
    r"\baudit date\b|\baudited\b|\binspected\b|\binspection of\b|\bwere checked\b"
    r"|\bwas checked\b|\bmeasured on\b|\bverified on\b",
    re.IGNORECASE,
)
_DATE_RE = re.compile(r"\b20\d\d-[01]\d-[0-3]\d\b")
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
_RUN_LINK_RE = re.compile(r"https?://\S+/(?:actions/)?runs?/\d+")
_RAW_OUTPUT_PATH_RE = re.compile(
    r"(?:^|/)(?:raw|logs?|outputs?|evidence|transcripts?)/|\.(?:log|out)$",
    re.IGNORECASE,
)

# The left side of an assignment: a declaration keyword, an owner, a name,
# a type annotation. Matched against the text before the first `=` only.
_ASSIGN_TARGET_RE = re.compile(
    r"^\s*(?:(?:const|let|var|val|final)\s+)?(?:self\.|this\.)?(\w+)\s*(?::[^=]*)?$",
)
_PCT_NAME_RE = re.compile(r"percent|pct", re.IGNORECASE)
_COUNT_RE = re.compile(r"\blen\(|\.length\b|\.count\b|\.count\(|\bcount\(|\.size\(\)")
_CODE_SUFFIXES = (
    ".py", ".js", ".jsx", ".ts", ".tsx", ".kt", ".java", ".swift", ".go",
    ".rb", ".rs", ".cs", ".dart", ".php",
)


def _suffix(path: str, suffixes: tuple[str, ...]) -> bool:
    return path.lower().endswith(suffixes)


def _walk(hunk: DiffHunk) -> Iterator[tuple[str, int, str]]:
    """("+" | " ", new-side line, text) for each added or context line, and
    ("@", new_start, text after the second @@) for the header."""
    lineno = hunk.new_start
    for raw in hunk.body.splitlines():
        if raw.startswith("@@"):
            yield "@", hunk.new_start, raw.split("@@", 2)[-1]
            continue
        if raw.startswith(("\\", "-")):
            continue
        if len(raw) > MAX_LINE_CHARS:
            lineno += 1
            continue
        yield ("+" if raw.startswith("+") else " "), lineno, raw[1:]
        lineno += 1


def _possible(whole: str, frac: str, sizes: Iterable[int]) -> bool:
    """Some whole count out of some stated size gives this percentage when
    rounded or truncated to the stated decimals."""
    value = float(f"{whole}.{frac}")
    step = 10.0 ** -len(frac)
    for n in sizes:
        k0 = round(value * n / 100)
        for k in (k0 - 1, k0, k0 + 1):
            if 0 <= k <= n and abs(100 * k / n - value) < step - 1e-9:
                return True
    return False


def _finding(path: str, line: int, message: str) -> Finding:
    return Finding(
        file=path,
        line=line,
        severity="high",
        rule_name=RULE,
        message=message,
        suggestion=None,
        effort="heavy-lift",
    )


def _stated_sizes(lines: list[tuple[str, int, str]]) -> set[int]:
    """Sample sizes the file states, at most `MAX_SIZES`."""
    sizes: set[int] = set()
    for _kind, _line, text in lines:
        for size_re in _SIZE_RES:
            for m in size_re.finditer(text):
                n = int(m.group(1))
                if 2 <= n <= 1_000_000 and len(sizes) < MAX_SIZES:
                    sizes.add(n)
    return sizes


def _keyed_rates(text: str) -> list[tuple[str, str]]:
    """(whole, decimals) of each rate a data line states with no percent
    sign: the values of a rate key, when they are on the percent scale (one
    above 1, none above 100 or below 0). A 0-1 fraction is not read."""
    out: list[tuple[str, str]] = []
    for m in _RATE_KEY_RE.finditer(text):
        values = [(w, f or "") for w, f in _NUMBER_RE.findall(m.group(1))]
        numbers = [float(f"{w}.{f}" if f else w) for w, f in values]
        if not numbers or max(numbers) <= 1 or max(numbers) > 100 or min(numbers) < 0:
            continue
        out += [(w, f) for w, f in values if f]
    return out


def _impossible_in(
    text: str, sizes: set[int], budget: list[int], data: bool = False,
) -> list[str] | None:
    """The line's percentages no stated size can give, or None once the
    shared check budget runs out (not evaluated). `data`: the line is in a
    data file, so a rate key's bare values count too."""
    stated = [(m.group(1), m.group(2)) for m in _DECIMAL_PCT_RE.finditer(text)]
    if data:
        stated += _keyed_rates(text)
    bad = []
    for whole, frac in stated:
        budget[0] -= len(sizes)
        if budget[0] < 0:
            return None
        if float(f"{whole}.{frac}") <= 100 and not _possible(whole, frac, sizes):
            bad.append(f"{whole}.{frac}%")
    return list(dict.fromkeys(bad))


def _impossible_rate(
    path: str, lines: list[tuple[str, int, str]], budget: list[int],
) -> Finding | None:
    """`budget` is the shared count of pair checks left, one cell."""
    sizes = _stated_sizes(lines)
    if not sizes:
        return None
    for kind, line, text in lines:
        if kind != "+" or not _RATE_WORD_RE.search(text):
            continue
        bad = _impossible_in(text, sizes, budget, _suffix(path, _DATA_SUFFIXES))
        if bad is None:
            return None
        if bad:
            stated = ", ".join(str(n) for n in sorted(sizes)[:6])
            return _finding(path, line, (
                f"{', '.join(bad)} cannot come from the sample sizes this file "
                f"states ({stated}): no whole count out of any of them gives "
                "that percentage at the stated precision. A real run reports "
                "counts that divide out; commit the raw output and the "
                "percentages computed from it, or remove the row."
            ))
    return None


def _simulated_result(path: str, lines: list[tuple[str, int, str]]) -> Finding | None:
    if not _RESULT_PATH_RE.search(path):
        return None
    added = [(line, text) for kind, line, text in lines if kind == "+"]
    marker = next(((ln, t) for ln, t in added if _SIMULATED_RE.search(t)), None)
    if marker is None or not any(_FIGURE_RE.search(t) for _ln, t in added):
        return None
    return _finding(path, marker[0], (
        f"`{path}` publishes results, but its own text says they come from a "
        f"simulated or stubbed path (\"{marker[1].strip()[:120]}\"). Numbers "
        "and a verdict computed from hardcoded responses are not a "
        "measurement. Run the real path and commit its output, or drop the "
        "results file and leave the criterion open."
    ))


def _dated_audit_claim(path: str, added: list[tuple[int, str]]) -> int | None:
    """The line of an audit claim in a dated audit doc, or None."""
    title = next((t for _ln, t in added if t.lstrip().startswith("#")), "")
    if not (_AUDIT_TITLE_RE.search(path.rsplit("/", 1)[-1]) or _AUDIT_TITLE_RE.search(title)):
        return None
    claim = next((ln for ln, t in added if _AUDIT_CLAIM_RE.search(t)), None)
    if claim is None or not any(_DATE_RE.search(t) for _ln, t in added):
        return None
    return claim


def _unbacked_audit(
    path: str, lines: list[tuple[str, int, str]], raw_elsewhere: bool,
) -> Finding | None:
    added = [(line, text) for kind, line, text in lines if kind == "+"]
    claim = _dated_audit_claim(path, added)
    if claim is None:
        return None
    if raw_elsewhere or any(_FENCE_RE.match(t) or _RUN_LINK_RE.search(t) for _ln, t in added):
        return None
    return _finding(path, claim, (
        f"`{path}` records an audit on a date, but neither it nor the rest of "
        "the diff carries any output from that audit: no command output, "
        "no raw file, no run link. An audit a reader cannot check is a claim, "
        "not a record. Commit the command and its output, or mark the audit "
        "as not yet done."
    ))


def _percent_from_count(path: str, lines: list[tuple[str, int, str]]) -> Finding | None:
    for kind, line, text in lines:
        if kind != "+":
            continue
        target, eq, value = text.partition("=")
        if not eq or value.startswith("=") or target.endswith(("!", "<", ">", "=")):
            continue
        m = _ASSIGN_TARGET_RE.match(target)
        if not m or not _PCT_NAME_RE.search(m.group(1)):
            continue
        if _COUNT_RE.search(value) and "/" not in value:
            return _finding(path, line, (
                f"`{m.group(1)}` is a percentage, but it is set from a count "
                f"(`{value.strip()[:100]}`) with no division by a total, "
                "so three items show as three percent. Divide by the total "
                "the percentage is out of, or name and show it as a count."
            ))
    return None


def scan_fabricated_results(
    changes: Iterable[FileChange], hunks: tuple[DiffHunk, ...],
) -> tuple[Finding, ...]:
    """At most one finding per file, in diff order."""
    if sum(len(h.body) for h in hunks) > MAX_ADDED_CHARS:
        return ()
    by_path: dict[str, list[tuple[str, int, str]]] = {}
    for hunk in hunks:
        by_path.setdefault(hunk.file_path, []).extend(_walk(hunk))
    changes = tuple(changes)
    added_files = {c.path for c in changes if c.status == "added"}
    paths = {c.path for c in changes} | set(by_path)
    raw_elsewhere = any(_RAW_OUTPUT_PATH_RE.search(p) for p in paths)
    budget = [MAX_RATE_CHECKS]
    found = (
        _file_finding(path, lines, budget, path in added_files, raw_elsewhere)
        for path, lines in by_path.items() if not is_test_file(path)
    )
    return tuple(f for f in found if f is not None)


def _file_finding(
    path: str, lines: list[tuple[str, int, str]], budget: list[int],
    added: bool, raw_elsewhere: bool,
) -> Finding | None:
    """The one finding a results file or a code file can carry, or None."""
    if _suffix(path, _CODE_SUFFIXES) and not _suffix(path, _RESULT_SUFFIXES):
        return _percent_from_count(path, lines)
    if not _suffix(path, _RESULT_SUFFIXES):
        return None
    found = _impossible_rate(path, lines, budget) or _simulated_result(path, lines)
    if found is None and added and _suffix(path, _DOC_SUFFIXES):
        found = _unbacked_audit(path, lines, raw_elsewhere)
    return found
