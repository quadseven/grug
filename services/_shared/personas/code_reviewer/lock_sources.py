"""Deterministic lock-source check (#1649).

Elder reviews a lock-file-only diff with the model like any other diff;
that review is the gate. This check is a second layer on top of it, and
it runs only where the standard library has a real parser for the format:
`package-lock.json` (`json`) and `uv.lock`, `poetry.lock`, `Cargo.lock`
(`tomllib`). Each file is parsed whole at head and at base, and every
package entry that differs from base must:

- name only https URLs on `ALLOWED_HOSTS` (the two exact crates.io index
  identities Cargo writes count as the registry);
- name no non-registry source (git, file, link, path, directory, alias);
- keep its integrity or hash when the same entry at base had one.

A file its parser cannot read, or that cannot be read at head, is a
finding too. Any other lock format gets one non-blocking summary note
saying its sources are not machine-checked. Pure apart from `fetch`.
"""

from __future__ import annotations

import json
import posixpath
import re
import tomllib
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from personas.code_reviewer.diff_parser import DiffHunk
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

_CARGO_REGISTRIES: frozenset[str] = frozenset({
    "registry+https://github.com/rust-lang/crates.io-index",
    "sparse+https://index.crates.io/",
})

_NON_REGISTRY_PREFIXES: tuple[str, ...] = (
    "git+", "git:", "git@", "github:", "gitlab:", "bitbucket:", "file:",
    "link:", "portal:", "workspace:", "directory:", "exec:", "patch:", "npm:",
)

CHECKED_FORMATS: frozenset[str] = frozenset({
    "package-lock.json", "uv.lock", "poetry.lock", "Cargo.lock",
})

UNCHECKED_NOTE = (
    "registry sources not machine-checked for this format; Elder's review covers it"
)

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
_MAX_LISTED = 5


def is_lock_file_path(path: str) -> bool:
    """True when the file name is exactly one of `LOCK_FILE_NAMES`."""
    return posixpath.basename(path) in LOCK_FILE_NAMES


def is_unchecked_lock_path(path: str) -> bool:
    """A listed lock file this check cannot parse. The model always gets it
    (#1649), in a lock-only or a mixed diff, so Elder's review covers it."""
    return is_lock_file_path(path) and posixpath.basename(path) not in CHECKED_FORMATS


def url_problem(url: str) -> str | None:
    """Why a lock source is not an allowed https registry URL, or None."""
    if url in _CARGO_REGISTRIES:
        return None
    if not url:
        return "an empty source"
    if url.startswith("//"):
        return url_problem("https:" + url)
    if url.lower().startswith(_NON_REGISTRY_PREFIXES):
        return f"non-registry source `{url}`"
    if url.lower().startswith("http://"):
        return f"plain http URL `{url}`"
    m = re.match(r"(?i)https://([^/?#]*)", url)
    if not m:
        return f"non-registry source `{url}`"
    authority = m.group(1)
    if "\\" in url or "%" in authority or "@" in authority or not authority:
        return f"URL with a disguised authority `{url}`"
    host = re.sub(r":\d+$", "", authority).lower()
    try:
        parsed = urlsplit(url).hostname
    except ValueError:
        parsed = None
    if parsed != host:
        return f"URL that does not parse `{url}`"
    if host not in ALLOWED_HOSTS:
        return f"host `{host}` not on the registry allowlist"
    return None


def _str(v: object) -> str:
    return v if isinstance(v, str) else ""


def _urls(*urls: object) -> Iterator[str]:
    for url in urls:
        problem = url_problem(_str(url))
        if problem:
            yield problem


# --- per-format entries ------------------------------------------------------
# Each format has a loader (text -> {key: entry}), a source judge, and a
# probe for whether an entry carries an integrity hash.


def _uv_sources(pkg: Mapping[str, Any]) -> Iterator[str]:
    source = pkg.get("source")
    if isinstance(source, Mapping):
        for kind, value in source.items():
            if kind == "registry":
                yield from _urls(value)
            else:
                yield f"non-registry source `{kind} = {value}`"
    for art in [pkg.get("sdist"), *(pkg.get("wheels") or [])]:
        if isinstance(art, Mapping):
            if "url" in art:
                yield from _urls(art["url"])
            elif "path" in art:
                yield f"local artifact `{art['path']}`"


def _uv_hashed(pkg: Mapping[str, Any]) -> bool:
    arts = [pkg.get("sdist"), *(pkg.get("wheels") or [])]
    return any(isinstance(a, Mapping) and a.get("hash") for a in arts)


def _cargo_sources(pkg: Mapping[str, Any]) -> Iterator[str]:
    if "source" in pkg:
        yield from _urls(pkg["source"])


def _poetry_sources(pkg: Mapping[str, Any]) -> Iterator[str]:
    source = pkg.get("source")
    if not isinstance(source, Mapping):
        return
    if _str(source.get("type")) != "legacy":
        yield f"non-registry source `{_str(source.get('type'))}` `{_str(source.get('url'))}`"
    else:
        yield from _urls(source.get("url"))


def _poetry_hashed(pkg: Mapping[str, Any]) -> bool:
    return any(isinstance(f, Mapping) and f.get("hash") for f in pkg.get("files") or [])


def _npm_sources(pkg: Mapping[str, Any]) -> Iterator[str]:
    if pkg.get("link"):
        yield f"linked local package `{_str(pkg.get('resolved'))}`"
        return
    version = _str(pkg.get("version"))
    if "version" in pkg and not _SEMVER_RE.match(version):
        # Lockfile v1 puts a tarball, git or alias spec in `version`.
        problem = url_problem(version)
        if problem:
            yield problem
        elif "resolved" not in pkg:
            yield f"non-semver version `{version}` with no resolved registry URL"
    if "resolved" in pkg:
        yield from _urls(pkg["resolved"])


def _toml_packages(doc: Mapping[str, Any]) -> dict[str, Any]:
    """Package entries keyed by name, with `#n` for repeats of a name."""
    out: dict[str, Any] = {}
    for pkg in doc.get("package") or []:
        name = _str(pkg.get("name")) if isinstance(pkg, Mapping) else ""
        key, n = name, 1
        while key in out:
            n += 1
            key = f"{name}#{n}"
        out[key] = pkg
    return out


def _npm_packages(doc: Mapping[str, Any]) -> dict[str, Any]:
    out = {f"packages:{k}": v for k, v in (doc.get("packages") or {}).items() if k}

    def walk(deps: Mapping[str, Any], prefix: str) -> None:
        for name, pkg in deps.items():
            out[f"dependencies:{prefix}{name}"] = pkg
            if isinstance(pkg, Mapping):
                walk(pkg.get("dependencies") or {}, f"{prefix}{name}/")

    walk(doc.get("dependencies") or {}, "")
    return out


_FORMATS: dict[str, tuple[Callable, Callable, Callable]] = {
    "package-lock.json": (
        lambda t: _npm_packages(json.loads(t)), _npm_sources,
        lambda p: bool(p.get("integrity")),
    ),
    "uv.lock": (lambda t: _toml_packages(tomllib.loads(t)), _uv_sources, _uv_hashed),
    "poetry.lock": (lambda t: _toml_packages(tomllib.loads(t)), _poetry_sources, _poetry_hashed),
    "Cargo.lock": (
        lambda t: _toml_packages(tomllib.loads(t)), _cargo_sources,
        lambda p: bool(p.get("checksum")),
    ),
}


def _load(name: str, text: str) -> dict[str, Any]:
    entries = _FORMATS[name][0](text)
    if not all(isinstance(e, Mapping) for e in entries.values()):
        raise TypeError("an entry is not a table")
    return entries


def file_problems(name: str, head: str, base: str | None) -> list[str]:
    """Problems in every entry of `head` that differs from `base`."""
    try:
        entries = _load(name, head)
    except (ValueError, TypeError, AttributeError) as e:
        return [f"`{name}` does not parse ({type(e).__name__})"]
    try:
        before = _load(name, base) if base is not None else {}
    except (ValueError, TypeError, AttributeError):
        before = {}
    _, sources, hashed = _FORMATS[name]
    problems: list[str] = []
    for key, entry in entries.items():
        prior = before.get(key)
        if prior == entry:
            continue
        problems.extend(sources(entry))
        if prior is not None and hashed(prior) and not hashed(entry):
            problems.append(f"entry `{key}` lost its integrity hash")
    return problems


# --- entry point -------------------------------------------------------------


def _finding(path: str, line: int, problems: list[str]) -> Finding:
    problems = list(dict.fromkeys(problems))
    listed = "; ".join(problems[:_MAX_LISTED])
    more = f"; and {len(problems) - _MAX_LISTED} more" if len(problems) > _MAX_LISTED else ""
    return Finding(
        file=path,
        line=line,
        severity="high",
        rule_name=RULE,
        message=(
            "This lock change installs from somewhere other than an allowed "
            f"registry: {listed}{more}. Pin the dependency to an allowed "
            "registry, or a maintainer confirms the source is intended."
        ),
        suggestion=None,
        effort="quick-win",
        category="security",
    )


def _fetch_both(
    paths: tuple[str, ...], fetch: Callable[[tuple[str, ...], str], dict[str, str]],
    head_sha: str, base_sha: str,
) -> tuple[dict[str, str], dict[str, str]] | None:
    try:
        return fetch(paths, head_sha), fetch(paths, base_sha)
    except Exception:  # noqa: BLE001 - an unread lock is a finding, never a pass
        return None


def _basenames(paths: Iterator[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(posixpath.basename(p) for p in paths))


def _unchecked_note(names: tuple[str, ...]) -> str:
    if not names:
        return ""
    return "\n\nLock files " + ", ".join(f"`{n}`" for n in names) + f": {UNCHECKED_NOTE}."


def _path_problems(
    path: str, fetched: tuple[dict[str, str], dict[str, str]] | None,
) -> list[str]:
    name = posixpath.basename(path)
    if fetched is None or path not in fetched[0]:
        return [f"`{name}` could not be read at head"]
    return file_problems(name, fetched[0][path], fetched[1].get(path))


@dataclass(frozen=True)
class LockCheck:
    """`findings` block; `note` names, by file name only, the unchecked
    lock files the model received; `unreviewed` names unchecked lock files
    the size rules kept from the model, which keeps the check from
    concluding success."""

    findings: tuple[Finding, ...] = ()
    note: str = ""
    unreviewed: tuple[str, ...] = ()


def lock_source_check(
    hunks: tuple[DiffHunk, ...], *,
    fetch: Callable[[tuple[str, ...], str], dict[str, str]],
    head_sha: str, base_sha: str,
    reviewed: frozenset[str], oversized: frozenset[str],
) -> LockCheck:
    """Check every changed lock file. `reviewed` is the paths the model
    received, `oversized` the paths the size rules dropped. `fetch(paths,
    ref)` returns path -> text for files present at `ref`, omits absent
    ones, and raises when it cannot tell."""
    first: dict[str, int] = {}
    for h in hunks:
        if is_lock_file_path(h.file_path):
            first.setdefault(h.file_path, max(1, h.new_start))
    checked = tuple(p for p in first if posixpath.basename(p) in CHECKED_FORMATS)
    unchecked = tuple(p for p in first if p not in checked)
    note = _unchecked_note(_basenames(p for p in unchecked if p in reviewed))
    unreviewed = _basenames(p for p in unchecked if p in oversized and p not in reviewed)
    found = []
    if checked:
        fetched = _fetch_both(checked, fetch, head_sha, base_sha)
        for path in checked:
            problems = _path_problems(path, fetched)
            if problems:
                found.append(_finding(path, first[path], problems))
    return LockCheck(tuple(found), note, unreviewed)
