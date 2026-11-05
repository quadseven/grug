"""grug#1649: the deterministic lock-source check.

A lock file is not inert data: a changed source can point an install at
another registry, a git repository or a local path. Elder reviews a
lock-file-only diff like any other diff, and this check adds one blocking
finding per lock file whose added entries name a source that is not an
allowed registry, a URL with a disguised authority, or content that its
own parser cannot read.
"""

from __future__ import annotations

import json
import re

import pytest
from personas.code_reviewer.diff_parser import parse_diff
from personas.code_reviewer.lock_sources import (
    ALLOWED_HOSTS,
    RULE,
    is_lock_file_path,
    lock_source_findings,
    url_problem,
)


def _diff(path: str, added: list[str], removed: list[str] = ()) -> str:
    body = "".join(f"-{r}\n" for r in removed) + "".join(f"+{a}\n" for a in added)
    return (
        f"diff --git a/{path} b/{path}\n"
        "index 39dc28b90..1a2b3c4d5 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -1,{len(removed)} +1,{len(added)} @@\n" + body
    )


def _scan(path: str, added: list[str], *, head: str | None = None,
          base: str | None = None, removed: list[str] = (),
          fetch_error: Exception | None = None):
    """Findings for one changed lock file. `head`/`base` are the whole file
    at each ref (None: not there)."""
    files = {"HEAD": head, "BASE": base}

    def fetch(paths, ref):
        if fetch_error is not None:
            raise fetch_error
        text = files[ref]
        return {path: text} if text is not None and path in paths else {}

    hunks = parse_diff(_diff(path, added, removed))
    return lock_source_findings(hunks, fetch=fetch, head_sha="HEAD", base_sha="BASE")


# --- names and hosts -------------------------------------------------------


@pytest.mark.parametrize("path", [
    "uv.lock", "pulumi/stack/uv.lock", "poetry.lock", "Pipfile.lock",
    "Cargo.lock", "web/package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "go.sum", "Gemfile.lock", "infra/.terraform.lock.hcl", "composer.lock",
])
def test_listed_lock_file_names_are_recognised(path):
    assert is_lock_file_path(path)


@pytest.mark.parametrize("path", [
    "src/app.py", "flake.lock", "evil.lock", "uv.lock.bak", "docs/uv.lock.md",
])
def test_unlisted_names_are_not_lock_files(path):
    assert not is_lock_file_path(path)


def test_github_is_not_an_allowed_host():
    assert "github.com" not in ALLOWED_HOSTS


@pytest.mark.parametrize("url", [
    "https://pypi.org/simple",
    "https://files.pythonhosted.org/packages/ab/x-1.0.tar.gz",
    "https://registry.npmjs.org/left-pad/-/left-pad-1.3.0.tgz",
    "https://PyPI.org/simple",
    "https://registry.npmjs.org:443/x.tgz",
])
def test_allowed_registry_urls_pass(url):
    assert url_problem(url) is None


@pytest.mark.parametrize("url", [
    "https://evil.example.net/x.tgz",
    "https://registry.npmjs.org.evil.example/x.tgz",
    "https://github.com/someone/pkg/archive/x.tgz",
    "https://user@registry.npmjs.org/x.tgz",
    "https://registry.npmjs.org\\@evil.example/x.tgz",
    "https://registry.npmjs.org%2f@evil.example/x.tgz",
    "https://registry%2enpmjs.org/x.tgz",
    "https://registry.npmjs.org\\u002f@evil.example/x.tgz",
    "git+https://github.com/a/b.git",
    "git://example.org/a.git",
    "git@github.com:a/b.git",
    "file:../pkg",
    "link:../pkg",
    "../pkg",
    "/abs/pkg",
    "ftp://pypi.org/x",
    "",
])
def test_bad_sources_are_flagged(url):
    assert url_problem(url)


# --- structured formats parse with their real parser ------------------------


_UV_BASE = """version = 1
requires-python = ">=3.12"

[[package]]
name = "app"
version = "0.1.0"
source = { virtual = "." }

[[package]]
name = "requests"
version = "2.31.0"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://files.pythonhosted.org/packages/r/requests-2.31.0.tar.gz", hash = "sha256:aa" }
"""


def _uv_with(extra: str) -> str:
    return _UV_BASE + "\n" + extra


_UV_GOOD = '''[[package]]
name = "idna"
version = "3.7"
source = { registry = "https://pypi.org/simple" }
wheels = [{ url = "https://files.pythonhosted.org/packages/i/idna-3.7-py3-none-any.whl", hash = "sha256:bb" }]
'''


def test_a_registry_bump_in_uv_lock_is_clean():
    head = _uv_with(_UV_GOOD)
    assert _scan("uv.lock", _UV_GOOD.splitlines(), head=head, base=_UV_BASE) == ()


def test_an_unchanged_virtual_root_is_not_flagged():
    # The project's own `virtual = "."` entry is in both refs.
    head = _uv_with(_UV_GOOD)
    assert _scan("uv.lock", ["# touch"], head=head, base=_UV_BASE) == ()


@pytest.mark.parametrize("source", [
    'source = { git = "https://github.com/a/b?rev=1#abc" }',
    'source = { path = "../vendored" }',
    'source = { directory = "libs/x" }',
    'source = { editable = "libs/x" }',
    'source = { url = "https://evil.example.net/x.whl" }',
    'source = { registry = "https://evil.example.net/simple" }',
])
def test_a_non_registry_uv_source_is_flagged(source):
    entry = f'[[package]]\nname = "x"\nversion = "1.0"\n{source}\n'
    found = _scan("uv.lock", entry.splitlines(), head=_uv_with(entry), base=_UV_BASE)
    assert len(found) == 1
    f = found[0]
    assert (f.rule_name, f.severity, f.file) == (RULE, "high", "uv.lock")


def test_a_uv_sdist_on_another_host_is_flagged():
    entry = (
        '[[package]]\nname = "x"\nversion = "1.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        'sdist = { url = "https://evil.example.net/x-1.0.tar.gz", hash = "sha256:cc" }\n'
    )
    found = _scan("uv.lock", entry.splitlines(), head=_uv_with(entry), base=_UV_BASE)
    assert found and re.search(r"evil\.example\.net", found[0].message)


def test_an_unparseable_toml_lock_is_flagged():
    found = _scan("uv.lock", ["[[package"], head="[[package\nname = ", base=_UV_BASE)
    assert len(found) == 1 and "parse" in found[0].message


def test_a_lock_that_cannot_be_read_at_head_is_flagged():
    found = _scan("uv.lock", ["x = 1"], fetch_error=RuntimeError("boom"))
    assert len(found) == 1 and found[0].rule_name == RULE


def test_a_lock_missing_at_head_while_lines_are_added_is_flagged():
    found = _scan("uv.lock", ["x = 1"], head=None, base=_UV_BASE)
    assert len(found) == 1


def test_cargo_crates_io_index_is_the_registry():
    base = 'version = 3\n'
    entry = (
        '[[package]]\nname = "serde"\nversion = "1.0.0"\n'
        'source = "registry+https://github.com/rust-lang/crates.io-index"\n'
        'checksum = "ab"\n'
    )
    assert _scan("Cargo.lock", entry.splitlines(), head=base + entry, base=base) == ()


def test_a_cargo_git_source_is_flagged():
    base = 'version = 3\n'
    entry = (
        '[[package]]\nname = "x"\nversion = "1.0.0"\n'
        'source = "git+https://github.com/a/x?rev=1#abc"\n'
    )
    assert _scan("Cargo.lock", entry.splitlines(), head=base + entry, base=base)


@pytest.mark.parametrize(("source", "bad"), [
    ('[package.source]\ntype = "git"\nurl = "https://github.com/a/b.git"\n', True),
    ('[package.source]\ntype = "directory"\nurl = "../x"\n', True),
    ('[package.source]\ntype = "legacy"\nurl = "https://evil.example.net/simple"\nreference = "e"\n', True),
    ('[package.source]\ntype = "legacy"\nurl = "https://pypi.org/simple"\nreference = "p"\n', False),
    ('', False),
])
def test_poetry_sources(source, bad):
    base = '[metadata]\nlock-version = "2.0"\n'
    entry = f'[[package]]\nname = "x"\nversion = "1.0"\nfiles = []\n{source}'
    found = _scan("poetry.lock", entry.splitlines(), head=entry + base, base=base)
    assert bool(found) is bad, found


def _npm(packages: dict) -> str:
    return json.dumps({"name": "app", "lockfileVersion": 3, "packages": packages}, indent=2)


_NPM_BASE = {"": {"name": "app"}}


@pytest.mark.parametrize(("entry", "bad"), [
    ({"version": "1.3.0", "resolved": "https://registry.npmjs.org/left-pad/-/left-pad-1.3.0.tgz",
      "funding": {"url": "https://github.com/sponsors/x"}}, False),
    ({"version": "1.0.0", "resolved": "https://evil.example.net/x.tgz"}, True),
    ({"version": "1.0.0", "resolved": "git+ssh://git@github.com/a/b.git#abc"}, True),
    ({"resolved": "../local", "link": True}, True),
    ({"version": "file:../x", "resolved": "file:../x"}, True),
])
def test_package_lock_sources(entry, bad):
    head = _npm({**_NPM_BASE, "node_modules/x": entry})
    found = _scan("package-lock.json", ['    "node_modules/x": {'], head=head, base=_npm(_NPM_BASE))
    assert bool(found) is bad, found


def test_an_unchanged_workspace_link_is_not_flagged():
    packages = {**_NPM_BASE, "node_modules/ws": {"resolved": "packages/ws", "link": True}}
    head = _npm({**packages, "node_modules/y": {
        "version": "1.0.0", "resolved": "https://registry.npmjs.org/y/-/y-1.0.0.tgz"}})
    assert _scan("package-lock.json", ["  x"], head=head, base=_npm(packages)) == ()


def test_unparseable_json_lock_is_flagged():
    assert _scan("package-lock.json", ["{"], head="{", base=_npm(_NPM_BASE))


def test_pipfile_lock_git_and_index():
    base = json.dumps({"_meta": {"sources": [{"name": "pypi", "url": "https://pypi.org/simple"}]},
                       "default": {}, "develop": {}})
    head = json.loads(base)
    head["default"]["x"] = {"git": "https://github.com/a/x.git", "ref": "abc"}
    assert _scan("Pipfile.lock", ["  x"], head=json.dumps(head), base=base)
    head = json.loads(base)
    head["_meta"]["sources"].append({"name": "evil", "url": "https://evil.example.net/simple"})
    assert _scan("Pipfile.lock", ["  x"], head=json.dumps(head), base=base)


# --- line formats ---------------------------------------------------------


@pytest.mark.parametrize(("path", "line", "bad"), [
    ("yarn.lock", '  resolved "https://registry.yarnpkg.com/x/-/x-1.0.0.tgz#ab"', False),
    ("yarn.lock", '  resolved "https://evil.example.net/x.tgz"', True),
    ("yarn.lock", '  resolved "git+https://github.com/a/b.git#abc"', True),
    ("yarn.lock", '"x@file:../x":', True),
    ("yarn.lock", '"x@link:../x":', True),
    ("yarn.lock", '"lodash@^4.17.21":', False),
    ("pnpm-lock.yaml", "    resolution: {integrity: sha512-abc}", False),
    ("pnpm-lock.yaml", "    resolution: {tarball: https://evil.example.net/x.tgz}", True),
    ("pnpm-lock.yaml", "      x: link:../x", True),
    ("pnpm-lock.yaml", "    resolution: {directory: ../x, type: directory}", True),
    ("Gemfile.lock", "  remote: https://rubygems.org/", False),
    ("Gemfile.lock", "GIT", True),
    ("Gemfile.lock", "PATH", True),
    ("Gemfile.lock", "  remote: https://gems.evil.example/", True),
    ("go.sum", "golang.org/x/text v0.3.0 h1:g61tztE5qeGQ89tm6NTjjM9VPIm088od1l6aSorWRWg=", False),
    ("go.sum", "golang.org/x/text v0.3.0/go.mod h1:NqM8EUOU14njkJ3fqMW+pc6Ldnwhi/IjpwHt7yyuwOQ=", False),
    ("go.sum", "replace golang.org/x/text => ../text", True),
    (".terraform.lock.hcl", 'provider "registry.terraform.io/hashicorp/aws" {', False),
    (".terraform.lock.hcl", 'provider "evil.example.net/hashicorp/aws" {', True),
    ("yarn.lock", '  resolved "https://registry.yarnpkg.com\\@evil.example/x.tgz"', True),
])
def test_line_formats(path, line, bad):
    found = _scan(path, [line])
    assert bool(found) is bad, found


def test_a_removed_bad_line_does_not_count():
    assert _scan("yarn.lock", ['  resolved "https://registry.yarnpkg.com/x.tgz"'],
                 removed=['  resolved "https://evil.example.net/x.tgz"']) == ()


def test_one_finding_per_file_anchored_on_an_added_line():
    found = _scan("yarn.lock", ["# ok", '  resolved "https://a.evil/x"', '  resolved "https://b.evil/y"'])
    assert len(found) == 1
    assert found[0].line == 2
    assert re.search(r"`a\.evil`", found[0].message)
    assert re.search(r"`b\.evil`", found[0].message)


def test_non_lock_files_are_ignored():
    hunks = parse_diff(_diff("src/app.py", ["URL = 'https://evil.example.net'"]))
    assert lock_source_findings(
        hunks, fetch=lambda p, r: {}, head_sha="HEAD", base_sha="BASE",
    ) == ()


def test_a_json_unicode_escape_in_an_added_line_is_flagged():
    line = '      "resolved": "https://registry.npmjs.org\\u002f@evil.example/x.tgz",'
    head = _npm({**_NPM_BASE, "node_modules/x": {
        "version": "1.0.0", "resolved": json.loads('"https://registry.npmjs.org\\u002f@evil.example/x.tgz"')}})
    found = _scan("package-lock.json", [line], head=head, base=_npm(_NPM_BASE))
    assert found and "escaped" in found[0].message
