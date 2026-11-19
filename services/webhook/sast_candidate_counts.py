"""Count local SAST candidate matches for the planted rule fixtures.

Run from services/webhook with `python sast_candidate_counts.py`. These are
pre-judge candidate counts, not Guard findings or deployed coverage results.
"""

from __future__ import annotations

import ast
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path

from personas.code_reviewer.diff_parser import DiffHunk, parse_diff
from personas.code_reviewer.sast import Candidate, scan_candidates

FIXTURE_SOURCE = Path(__file__).parent / "tests" / "test_sast_rules.py"
LANGUAGES = ("python", "yaml", "swift")
LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".swift": "swift",
}
Scanner = Callable[..., tuple[Candidate, ...]]


def load_planted(source: Path = FIXTURE_SOURCE) -> dict[str, tuple[str, str]]:
    """Read the PLANTED literal without importing the test module or its deps."""
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    value = next(
        (
            node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "PLANTED"
                for target in node.targets
            )
        ),
        None,
    )
    if value is None:
        raise ValueError("PLANTED fixture mapping is missing")
    fixtures = ast.literal_eval(value)
    if not isinstance(fixtures, dict):
        raise TypeError("PLANTED fixtures must be a dictionary literal")
    return fixtures


def _added_file_diff(path: str, content: str) -> str:
    lines = content.splitlines()
    if not lines:
        raise ValueError("planted fixture content must not be empty")
    return (
        f"diff --git a/{path} b/{path}\n"
        f"new file mode 100644\n"
        f"--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{line}\n" for line in lines)
    )


def candidate_counts(
    fixtures: Mapping[str, tuple[str, str]] | None = None,
    *,
    scanner: Scanner | None = None,
) -> dict[str, tuple[int, int]]:
    """Return (fixtures with a candidate, fixture total) for each language."""
    planted = load_planted() if fixtures is None else fixtures
    scan = scan_candidates if scanner is None else scanner
    contents: dict[str, str] = {}
    languages: dict[str, str] = {}
    hunks: list[DiffHunk] = []
    for path, body in planted.values():
        language = LANGUAGE_BY_SUFFIX.get(Path(path).suffix)
        if language is None:
            raise ValueError(
                f"no language mapping for planted fixture suffix: {Path(path).suffix}"
            )
        if path in contents:
            raise ValueError("planted fixture file paths must be unique")
        contents[path] = body
        languages[path] = language
        hunks.extend(parse_diff(_added_file_diff(path, body)))

    candidates = scan(tuple(hunks), file_contents=contents, engine="opengrep")
    matched_files = {candidate.file for candidate in candidates}
    counts = {language: [0, 0] for language in LANGUAGES}
    for path, language in languages.items():
        counts[language][1] += 1
        if path in matched_files:
            counts[language][0] += 1
    return {language: tuple(counts[language]) for language in LANGUAGES}


def main() -> None:
    if shutil.which("opengrep") is None:
        raise SystemExit("opengrep is required; no candidate counts were produced")
    for language, (matched, total) in candidate_counts().items():
        print(f"{language}: {matched}/{total} planted fixtures matched candidates")


if __name__ == "__main__":
    main()
