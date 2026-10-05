"""Optional in-repo `.grug.yaml` review configuration.

Three keys, all optional:

  ignore               list of path globs that are never reviewed
  path_instructions    list of {path: glob, instructions: text}; the text is
                       appended to the review prompt for matching files only
  min_inline_severity  low|medium|high|critical; findings below it appear in
                       the check summary but get no inline comment

The caller reads the file from the PR BASE ref (never the head), so a PR
cannot loosen its own review. This module is pure: it takes text and returns
a `RepoConfig`. A missing or blank file is `RepoConfig()` (no change). A
malformed file or unknown keys never raise; they land in `problems`, which
`config_note` renders as ONE advisory paragraph for the Elder summary.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache
from dataclasses import dataclass

import yaml

CONFIG_PATH = ".grug.yaml"

_KNOWN_KEYS = frozenset({"ignore", "path_instructions", "min_inline_severity"})
_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
# Bounds so one file cannot blow the prompt budget.
_MAX_INSTRUCTIONS = 20
_MAX_INSTRUCTION_CHARS = 2000


@dataclass(frozen=True)
class PathInstruction:
    path: str
    instructions: str


@dataclass(frozen=True)
class RepoConfig:
    ignore: tuple[str, ...] = ()
    path_instructions: tuple[PathInstruction, ...] = ()
    min_inline_severity: str | None = None
    problems: tuple[str, ...] = ()


def _string_list(value: object, key: str, problems: list[str]) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(v, str) and v.strip() for v in value
    ):
        problems.append(f"`{key}` must be a list of non-empty strings")
        return ()
    return tuple(v.strip() for v in value)


def _path_instructions(
    value: object, problems: list[str],
) -> tuple[PathInstruction, ...]:
    if not isinstance(value, list):
        problems.append("`path_instructions` must be a list of {path, instructions}")
        return ()
    out: list[PathInstruction] = []
    bad = 0
    for entry in value:
        path = entry.get("path") if isinstance(entry, dict) else None
        text = entry.get("instructions") if isinstance(entry, dict) else None
        if (
            isinstance(path, str) and path.strip()
            and isinstance(text, str) and text.strip()
        ):
            out.append(PathInstruction(
                path.strip(), text.strip()[:_MAX_INSTRUCTION_CHARS],
            ))
        else:
            bad += 1
    if bad:
        problems.append(
            f"{bad} `path_instructions` entr{'y' if bad == 1 else 'ies'} "
            "skipped (each needs a string `path` and string `instructions`)"
        )
    if len(out) > _MAX_INSTRUCTIONS:
        problems.append(
            f"only the first {_MAX_INSTRUCTIONS} `path_instructions` entries are used"
        )
        out = out[:_MAX_INSTRUCTIONS]
    return tuple(out)


def parse_repo_config(text: str | None) -> RepoConfig:
    """Parse `.grug.yaml` text. Never raises."""
    if text is None or not text.strip():
        return RepoConfig()
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return RepoConfig(problems=("the file is not valid YAML and was ignored",))
    if data is None:
        return RepoConfig()
    if not isinstance(data, dict):
        return RepoConfig(
            problems=("the top level must be a mapping and the file was ignored",),
        )

    problems: list[str] = []
    unknown = sorted(str(k) for k in data if k not in _KNOWN_KEYS)
    if unknown:
        problems.append(
            "unknown key(s) ignored: " + ", ".join(f"`{k}`" for k in unknown)
        )
    ignore = (
        _string_list(data["ignore"], "ignore", problems) if "ignore" in data else ()
    )
    instructions = (
        _path_instructions(data["path_instructions"], problems)
        if "path_instructions" in data else ()
    )
    floor: str | None = None
    if "min_inline_severity" in data:
        raw = data["min_inline_severity"]
        if isinstance(raw, str) and raw.strip().lower() in _SEVERITY_RANK:
            floor = raw.strip().lower()
        else:
            problems.append(
                "`min_inline_severity` must be one of low, medium, high, critical"
            )
    return RepoConfig(ignore, instructions, floor, tuple(problems))


@lru_cache(maxsize=256)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    pat = pattern.lstrip("/")
    anywhere = "/" not in pat
    out: list[str] = []
    i = 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    body = "".join(out)
    return re.compile(f"(?:.*/)?{body}" if anywhere else body)


def path_matches(path: str, pattern: str) -> bool:
    """Glob match against a repo-relative path. `*` stays inside one path
    segment, `**` crosses segments, and a pattern with no `/` matches the
    file name at any depth."""
    return _glob_regex(pattern).fullmatch(path) is not None


def is_ignored(path: str, config: RepoConfig) -> bool:
    return any(path_matches(path, g) for g in config.ignore)


def instructions_block(paths: Iterable[str], config: RepoConfig) -> str | None:
    """Prompt text for the changed `paths`, or None when no entry matches."""
    paths = list(paths)
    sections: list[str] = []
    for entry in config.path_instructions:
        matched = [p for p in paths if path_matches(p, entry.path)]
        if matched:
            shown = ", ".join(f"`{p}`" for p in matched[:5])
            more = f" (+{len(matched) - 5} more)" if len(matched) > 5 else ""
            sections.append(
                f"For files matching `{entry.path}` (this diff: {shown}{more}):\n"
                f"{entry.instructions}"
            )
    if not sections:
        return None
    return (
        f"--- review instructions from the repo's {CONFIG_PATH} "
        "(maintainer-written, read from the base branch) ---\n"
        "Apply each instruction only to the files it names.\n\n"
        + "\n\n".join(sections)
    )


def meets_inline_floor(severity: str, config: RepoConfig) -> bool:
    """True when a finding of `severity` should still be posted inline."""
    if config.min_inline_severity is None:
        return True
    return _SEVERITY_RANK.get(severity, 0) >= _SEVERITY_RANK[config.min_inline_severity]


def config_note(config: RepoConfig, ignored_paths: tuple[str, ...] = ()) -> str:
    """ONE advisory summary paragraph (leading blank line), or ''."""
    parts: list[str] = []
    if config.problems:
        parts.append(f"{CONFIG_PATH} problem: " + "; ".join(config.problems) + ".")
    if ignored_paths:
        shown = ", ".join(f"`{p.replace(chr(96), '')}`" for p in ignored_paths[:10])
        more = f" (+{len(ignored_paths) - 10} more)" if len(ignored_paths) > 10 else ""
        parts.append(
            f"{len(ignored_paths)} file(s) skipped by {CONFIG_PATH} `ignore`: "
            f"{shown}{more}."
        )
    return ("\n\n" + " ".join(parts)) if parts else ""


def split_ignored_hunks(hunks, config: RepoConfig):
    """Partition diff hunks into (kept, ignored_paths) by `ignore`.

    Works on anything with a `.file_path`. Ignored paths are deduped and
    order-preserving so the summary can name exactly what was skipped."""
    if not config.ignore:
        return tuple(hunks), ()
    kept = []
    ignored: dict[str, None] = {}
    for h in hunks:
        if is_ignored(h.file_path, config):
            ignored[h.file_path] = None
        else:
            kept.append(h)
    return tuple(kept), tuple(ignored)


def with_instructions(
    prompt_context: str | None, paths: Iterable[str], config: RepoConfig,
) -> str | None:
    """`prompt_context` with the matching path instructions appended."""
    block = instructions_block(paths, config)
    return "\n\n".join(p for p in (prompt_context, block) if p) or None
