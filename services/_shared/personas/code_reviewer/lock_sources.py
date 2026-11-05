"""Deterministic lock-source check (#1649).

A lock file is not inert data: a changed entry can point an install at
another registry, a git repository or a local path. Elder reviews lock
files like any other file when a diff holds nothing else, and this check
adds one blocking finding per lock file whose ADDED content names:

- a source that is not a registry (git, file, link, path, directory, or a
  bare local path);
- a URL whose authority carries a backslash, a `\\u` escape, a percent
  escape or user info, or whose host the standard parser reads differently;
- a host outside `ALLOWED_HOSTS`;
- or content that its own format's parser cannot read.

JSON and TOML locks are parsed whole at head and at base with `json` and
`tomllib`, and only entries that differ from base are judged, so an
unchanged project root or workspace link does not fire. Other formats are
scanned line by line over the added lines. Pure apart from the injected
`fetch`.
"""

from __future__ import annotations

import json
import posixpath
import re
import tomllib
from collections.abc import Callable, Iterator, Mapping
from typing import Any
from urllib.parse import urlsplit

from personas.code_reviewer.diff_parser import DiffHunk, added_lines
from personas.code_reviewer.persona import Finding

RULE = "lock-untrusted-source"

LOCK_FILE_NAMES: frozenset[str] = frozenset({
    "uv.lock", "poetry.lock", "Pipfile.lock", "package-lock.json",
    "pnpm-lock.yaml", "yarn.lock", "Cargo.lock", "go.sum", "Gemfile.lock",
    ".terraform.lock.hcl", "composer.lock",
})

ALLOWED_HOSTS: frozenset[str] = frozenset({
    "pypi.org", "files.pythonhosted.org", "registry.npmjs.org",
    "registry.yarnpkg.com", "crates.io", "static.crates.io",
    "proxy.golang.org", "sum.golang.org", "rubygems.org",
    "registry.terraform.io", "releases.hashicorp.com",
})

# Cargo names the crates.io registry by its index URL. These exact strings
# are the registry itself, not a git source, so they pass whole; no other
# URL on their hosts does.
_CARGO_REGISTRIES: frozenset[str] = frozenset({
    "registry+https://github.com/rust-lang/crates.io-index",
    "sparse+https://index.crates.io/",
})

_NON_REGISTRY_PREFIXES: tuple[str, ...] = (
    "git+", "git:", "git@", "github:", "gitlab:", "bitbucket:", "file:",
    "link:", "portal:", "workspace:", "directory:",
)
_STRUCTURED = frozenset({
    "uv.lock", "poetry.lock", "Cargo.lock", "package-lock.json",
    "Pipfile.lock", "composer.lock",
})
_MAX_LISTED = 5


def is_lock_file_path(path: str) -> bool:
    """True when the file name is exactly one of `LOCK_FILE_NAMES`."""
    return posixpath.basename(path) in LOCK_FILE_NAMES


def url_problem(url: str) -> str | None:
    """Why a lock source is not an allowed registry URL, or None."""
    if url in _CARGO_REGISTRIES:
        return None
    if not url:
        return "an empty source"
    low = url.lower()
    if low.startswith(_NON_REGISTRY_PREFIXES):
        return f"non-registry source `{url}`"
    m = re.match(r"(?i)(https?)://([^/?#]*)", url)
    if not m:
        return f"non-registry source `{url}`"
    authority = m.group(2)
    if "\\" in url or "%" in authority or "@" in authority or not authority:
        return f"URL with a disguised authority `{url}`"
    host = re.sub(r":\d+$", "", authority).lower()
    try:
        parsed = urlsplit(url).hostname
    except ValueError:
        return f"URL that does not parse `{url}`"
    if parsed != host:
        return f"URL that does not parse `{url}`"
    if host not in ALLOWED_HOSTS:
        return f"host `{host}` not on the registry allowlist"
    return None


# --- structured formats ------------------------------------------------------


def _str(v: object) -> str:
    return v if isinstance(v, str) else ""


def _uv_problems(pkg: Mapping[str, Any]) -> Iterator[str]:
    source = pkg.get("source")
    if isinstance(source, Mapping):
        for kind, value in source.items():
            if kind == "registry":
                yield from filter(None, [url_problem(_str(value))])
            else:
                yield f"non-registry source `{kind} = {value}`"
    sdist = pkg.get("sdist")
    for art in [sdist] + list(pkg.get("wheels") or []):
        if isinstance(art, Mapping):
            if "url" in art:
                yield from filter(None, [url_problem(_str(art["url"]))])
            elif "path" in art:
                yield f"local artifact `{art['path']}`"


def _cargo_problems(pkg: Mapping[str, Any]) -> Iterator[str]:
    if "source" in pkg:
        yield from filter(None, [url_problem(_str(pkg["source"]))])


def _poetry_problems(pkg: Mapping[str, Any]) -> Iterator[str]:
    source = pkg.get("source")
    if isinstance(source, Mapping):
        kind = _str(source.get("type"))
        if kind != "legacy":
            yield f"non-registry source `{kind}` `{_str(source.get('url'))}`"
        else:
            yield from filter(None, [url_problem(_str(source.get("url")))])


def _npm_problems(pkg: Mapping[str, Any]) -> Iterator[str]:
    if pkg.get("link"):
        yield f"linked local package `{_str(pkg.get('resolved'))}`"
        return
    version = _str(pkg.get("version"))
    if version.lower().startswith(_NON_REGISTRY_PREFIXES):
        yield f"non-registry source `{version}`"
    if "resolved" in pkg:
        yield from filter(None, [url_problem(_str(pkg["resolved"]))])


def _pipfile_problems(pkg: Mapping[str, Any]) -> Iterator[str]:
    for key in ("git", "hg", "svn", "path", "file", "editable"):
        if key in pkg:
            yield f"non-registry source `{key} = {pkg[key]}`"


def _composer_problems(pkg: Mapping[str, Any]) -> Iterator[str]:
    for key in ("dist", "source"):
        part = pkg.get(key)
        if isinstance(part, Mapping) and "url" in part:
            yield from filter(None, [url_problem(_str(part["url"]))])


def _npm_entries(doc: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, pkg in (doc.get("packages") or {}).items():
        if key:  # "" is the project itself
            out[f"packages:{key}"] = pkg

    def walk(deps: Mapping[str, Any], prefix: str) -> None:
        for name, pkg in deps.items():
            out[f"dependencies:{prefix}{name}"] = pkg
            if isinstance(pkg, Mapping):
                walk(pkg.get("dependencies") or {}, f"{prefix}{name}/")

    walk(doc.get("dependencies") or {}, "")
    return out


def _entries(name: str, text: str) -> dict[str, tuple[Any, Callable]]:
    """key -> (entry, judge) for every source-bearing entry. Raises on a
    document its parser cannot read."""
    if name in ("uv.lock", "Cargo.lock", "poetry.lock"):
        doc = tomllib.loads(text)
        judge = {"uv.lock": _uv_problems, "Cargo.lock": _cargo_problems,
                 "poetry.lock": _poetry_problems}[name]
        return {
            f"{p.get('name')}@{p.get('version')}@{i}": (p, judge)
            for i, p in enumerate(doc.get("package") or [])
            if isinstance(p, Mapping)
        }
    doc = json.loads(text)
    if not isinstance(doc, Mapping):
        raise TypeError("not an object")
    if name == "package-lock.json":
        return {k: (v, _npm_problems) for k, v in _npm_entries(doc).items()}
    if name == "Pipfile.lock":
        out: dict[str, tuple[Any, Callable]] = {}
        for i, src in enumerate((doc.get("_meta") or {}).get("sources") or []):
            out[f"source:{i}"] = (src, lambda s: filter(None, [url_problem(_str(s.get("url")))]))
        for group in ("default", "develop"):
            for pkg_name, pkg in (doc.get(group) or {}).items():
                out[f"{group}:{pkg_name}"] = (pkg, _pipfile_problems)
        return out
    out = {}
    for group in ("packages", "packages-dev"):
        for i, pkg in enumerate(doc.get(group) or []):
            if isinstance(pkg, Mapping):
                out[f"{group}:{pkg.get('name')}:{i}"] = (pkg, _composer_problems)
    return out


def _structured_problems(name: str, head: str, base: str | None) -> list[str]:
    try:
        entries = _entries(name, head)
    except (ValueError, TypeError, AttributeError) as e:
        return [f"content `{name}` cannot parse ({type(e).__name__})"]
    try:
        before = _entries(name, base) if base is not None else {}
    except (ValueError, TypeError, AttributeError):
        before = {}
    problems: list[str] = []
    for key, (entry, judge) in entries.items():
        if key in before and before[key][0] == entry:
            continue
        if not isinstance(entry, Mapping):
            problems.append(f"entry `{key}` cannot parse")
            continue
        problems.extend(judge(entry))
    return problems


# --- line formats ------------------------------------------------------------


_URL_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>,()\[\]{}]+")
_PREFIX_TOKEN_RE = re.compile(
    r"(?:^|[\s\"'@,{:])((?:git\+|git:|git@|github:|file:|link:|portal:|workspace:)\S*)"
)
_GO_SUM_RE = re.compile(r"^\S+ v\S+ h1:[A-Za-z0-9+/]+=*$")
_TF_PROVIDER_RE = re.compile(r'^\s*provider\s+"([^"]*)"')
_ESCAPE_RE = re.compile(r"\\[uUx]")


def _line_problems(name: str, text: str) -> list[str]:
    stripped = text.strip()
    if name == "go.sum":
        ok = not stripped or _GO_SUM_RE.match(stripped)
        return [] if ok else [f"line that does not parse `{stripped}`"]
    if name == ".terraform.lock.hcl":
        m = _TF_PROVIDER_RE.match(text)
        if m:
            host = m.group(1).split("/", 1)[0].lower()
            if host not in ALLOWED_HOSTS:
                return [f"provider host `{host}` not on the registry allowlist"]
        return []
    if name == "Gemfile.lock" and stripped in ("GIT", "PATH", "PLUGIN SOURCE"):
        return [f"non-registry `{stripped}` section"]
    problems: list[str] = []
    if "\\" in text:
        problems.append(f"line with a backslash `{stripped}`")
    for m in _URL_TOKEN_RE.finditer(text):
        problem = url_problem(m.group(0).rstrip(".;:#"))
        if problem:
            problems.append(problem)
    for m in _PREFIX_TOKEN_RE.finditer(text):
        problems.append(f"non-registry source `{m.group(1).strip(chr(34) + chr(39) + ':,')}`")
    if name == "pnpm-lock.yaml" and re.search(r"\b(?:directory|type):\s*(?:directory|git)\b|\bdirectory:", text):
        problems.append(f"non-registry source `{stripped}`")
    if name == "Gemfile.lock" and stripped.startswith("remote:"):
        remote = stripped.split(":", 1)[1].strip()
        if not _URL_TOKEN_RE.match(remote):
            problems.append(f"non-registry remote `{remote}`")
    return problems


# --- entry point -------------------------------------------------------------


def lock_source_findings(
    hunks: tuple[DiffHunk, ...], *,
    fetch: Callable[[tuple[str, ...], str], dict[str, str]],
    head_sha: str, base_sha: str,
) -> tuple[Finding, ...]:
    """One high finding per changed lock file whose added content fails the
    check. `fetch(paths, ref)` returns path -> text for files present at
    `ref`, omits absent ones, and raises when it cannot tell."""
    added: dict[str, list[tuple[int, str]]] = {}
    for h in hunks:
        if is_lock_file_path(h.file_path):
            added.setdefault(h.file_path, []).extend(added_lines(h))
    added = {p: lines for p, lines in added.items() if lines}
    if not added:
        return ()
    structured = tuple(p for p in added if posixpath.basename(p) in _STRUCTURED)
    heads: dict[str, str] = {}
    bases: dict[str, str] = {}
    fetch_failed = False
    if structured:
        try:
            heads = fetch(structured, head_sha)
            bases = fetch(structured, base_sha)
        except Exception:  # noqa: BLE001 - an unread lock is a finding, never a pass
            fetch_failed = True
    found: list[Finding] = []
    for path, lines in added.items():
        name = posixpath.basename(path)
        anchor = lines[0][0]
        problems: list[str] = []
        if path in structured:
            if fetch_failed or path not in heads:
                problems.append(f"content `{name}` could not be read at head")
            else:
                problems.extend(_structured_problems(name, heads[path], bases.get(path)))
            # The parser decodes escapes, so look for them in the raw text.
            for lineno, text in lines:
                if _ESCAPE_RE.search(text):
                    problems.append(f"escaped characters on line {lineno} `{text.strip()}`")
        else:
            for lineno, text in lines:
                line_found = _line_problems(name, text)
                if line_found and not problems:
                    anchor = lineno
                problems.extend(line_found)
        problems = list(dict.fromkeys(problems))
        if not problems:
            continue
        listed = "; ".join(problems[:_MAX_LISTED])
        more = f"; and {len(problems) - _MAX_LISTED} more" if len(problems) > _MAX_LISTED else ""
        found.append(Finding(
            file=path,
            line=anchor,
            severity="high",
            rule_name=RULE,
            message=(
                f"This lock change installs from somewhere other than an allowed "
                f"registry: {listed}{more}. A lock file can redirect an install "
                "to any source. Pin the dependency to an allowed registry, or a "
                "maintainer confirms the source is intended."
            ),
            suggestion=None,
            effort="quick-win",
            category="security",
        ))
    return tuple(found)
