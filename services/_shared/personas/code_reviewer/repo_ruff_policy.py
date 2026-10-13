"""Small, fail-closed parser for the Ruff policy relevant to review findings."""
from __future__ import annotations

import fnmatch
import tomllib
from dataclasses import dataclass


@dataclass(frozen=True)
class RepoRuffPolicy:
    select: frozenset[str] = frozenset()
    ignore: frozenset[str] = frozenset()
    per_file_ignores: tuple[tuple[str, frozenset[str]], ...] = ()

    def drops(self, code: str, path: str) -> bool:
        # A selected code is dropped on purpose (#1148), not mistaken for an
        # ignore: the repo's own ruff run already enforces it and fails CI on
        # it, so a thread would only duplicate that failure. The drop is still
        # visible, counted in the check summary.
        if _matches(code, self.ignore) or _matches(code, self.select):
            return True
        return any(fnmatch.fnmatchcase(path, pattern) and _matches(code, codes)
                   for pattern, codes in self.per_file_ignores)


def _codes(value: object) -> frozenset[str]:
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",")]
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError("expected rule-code list")
    return frozenset(value)


def _matches(code: str, patterns: frozenset[str]) -> bool:
    """Ruff selectors are code prefixes ("S" selects S608), plus "ALL". A
    trailing "*" is accepted as the same prefix. The codes come from a
    repo-authored file, so a pattern that is empty once its asterisks are
    stripped ("", "*", "**") is never a match: it cannot widen into "ALL"
    and drop every finding. Only a literal "ALL" means every code."""
    return any(pattern == "ALL" or (pattern.rstrip("*") and code.startswith(pattern.rstrip("*")))
               for pattern in patterns)


def parse_repo_ruff_policy(text: str | None) -> RepoRuffPolicy:
    """Parse TOML policy. Missing, invalid, or unsupported input means no policy."""
    if not text:
        return RepoRuffPolicy()
    try:
        root = tomllib.loads(text)
        tool = root.get("tool")
        ruff = tool.get("ruff", {}) if isinstance(tool, dict) else {}
        if not isinstance(ruff, dict):
            return RepoRuffPolicy()
        lint = ruff.get("lint", {})
        if not isinstance(lint, dict):
            lint = {}
        select = _codes(lint.get("select", ruff.get("select", [])))
        select |= _codes(lint.get("extend-select", ruff.get("extend-select", [])))
        ignore = _codes(lint.get("ignore", ruff.get("ignore", [])))
        ignore |= _codes(lint.get("extend-ignore", ruff.get("extend-ignore", [])))
        pfi = lint.get("per-file-ignores", ruff.get("per-file-ignores", {}))
        if not isinstance(pfi, dict):
            raise ValueError("invalid per-file-ignores")
        entries = tuple((pattern, _codes(codes)) for pattern, codes in pfi.items()
                        if isinstance(pattern, str))
        return RepoRuffPolicy(select, ignore, entries)
    except (tomllib.TOMLDecodeError, ValueError, TypeError, AttributeError):
        return RepoRuffPolicy()
