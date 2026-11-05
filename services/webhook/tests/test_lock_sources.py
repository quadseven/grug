"""grug#1649: the deterministic lock-source check.

Elder reviews a lock-file-only diff with the model; that review is the
gate. This check is a second layer, and it runs only for formats the
standard library parses: package-lock.json (json) and uv.lock,
poetry.lock, Cargo.lock (tomllib). Every package entry that differs from
base must name only allowed https registry sources and keep its integrity
hash. Other lock formats get a non-blocking note.
"""

from __future__ import annotations

import json
import re

import pytest
from personas.code_reviewer.diff_parser import parse_diff
from personas.code_reviewer.lock_sources import (
    ALLOWED_HOSTS,
    RULE,
    UNCHECKED_NOTE,
    is_lock_file_path,
    is_unchecked_lock_path,
    lock_source_check,
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


def _run(path: str, *, head: str | None, base: str | None,
         added: list[str] = ("x",), removed: list[str] = (),
         fetch_error: Exception | None = None,
         reviewed: frozenset[str] | None = None):
    files = {"HEAD": head, "BASE": base}

    def fetch(paths, ref):
        if fetch_error is not None:
            raise fetch_error
        text = files[ref]
        return {path: text} if text is not None and path in paths else {}

    return lock_source_check(
        parse_diff(_diff(path, list(added), list(removed))),
        fetch=fetch, head_sha="HEAD", base_sha="BASE",
        reviewed=frozenset({path}) if reviewed is None else reviewed,
    )


def _scan(path: str, **kw):
    return _run(path, **kw).findings


# --- names, hosts, URLs ----------------------------------------------------


@pytest.mark.parametrize("path", [
    "uv.lock", "pulumi/stack/uv.lock", "poetry.lock", "Pipfile.lock",
    "Cargo.lock", "web/package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "go.sum", "Gemfile.lock", "infra/.terraform.lock.hcl", "composer.lock",
])
def test_listed_lock_file_names_are_recognised(path):
    assert is_lock_file_path(path)


@pytest.mark.parametrize("path", ["src/app.py", "flake.lock", "evil.lock", "uv.lock.bak"])
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
    "//registry.npmjs.org/x/-/x-1.0.0.tgz",
    "registry+https://github.com/rust-lang/crates.io-index",
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
    "http://registry.npmjs.org/x.tgz",
    "HTTP://pypi.org/simple",
    "//evil.example.net/x.tgz",
    "git+https://github.com/a/b.git",
    "git://example.org/a.git",
    "git@github.com:a/b.git",
    "file:../pkg",
    "link:../pkg",
    "npm:other@1.0.0",
    "../pkg",
    "/abs/pkg",
    "ftp://pypi.org/x",
    "",
])
def test_bad_sources_are_flagged(url):
    assert url_problem(url)


# --- uv.lock ---------------------------------------------------------------


_ROOT_UV = """version = 1

[[package]]
name = "app"
version = "0.1.0"
source = { virtual = "." }
"""

_IDNA_36 = '''
[[package]]
name = "idna"
version = "3.6"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://files.pythonhosted.org/packages/i/idna-3.6.tar.gz", hash = "sha256:aa" }
'''
_IDNA_37 = _IDNA_36.replace("3.6", "3.7").replace("sha256:aa", "sha256:bb")
_UV_BASE = _ROOT_UV + _IDNA_36


def test_a_registry_bump_in_uv_lock_is_clean():
    assert _scan("uv.lock", head=_ROOT_UV + _IDNA_37, base=_UV_BASE) == ()


def test_an_unchanged_virtual_root_is_not_flagged():
    assert _scan("uv.lock", head=_UV_BASE, base=_UV_BASE) == ()


@pytest.mark.parametrize("source", [
    'source = { git = "https://github.com/a/b?rev=1#abc" }',
    'source = { path = "../vendored" }',
    'source = { directory = "libs/x" }',
    'source = { editable = "libs/x" }',
    'source = { url = "https://evil.example.net/x.whl" }',
    'source = { registry = "https://evil.example.net/simple" }',
    'source = { registry = "http://pypi.org/simple" }',
])
def test_a_non_registry_uv_source_is_flagged(source):
    idna = _IDNA_37.replace('source = { registry = "https://pypi.org/simple" }', source)
    found = _scan("uv.lock", head=_ROOT_UV + idna, base=_UV_BASE)
    assert len(found) == 1
    assert (found[0].rule_name, found[0].severity, found[0].file) == (RULE, "high", "uv.lock")


def test_a_uv_sdist_on_another_host_is_flagged():
    idna = _IDNA_37.replace("files.pythonhosted.org", "evil.example.net")
    found = _scan("uv.lock", head=_ROOT_UV + idna, base=_UV_BASE)
    assert found and re.search(r"evil\.example\.net", found[0].message)


def test_a_uv_entry_that_loses_its_hash_is_flagged():
    idna = _IDNA_37.replace(', hash = "sha256:bb"', "")
    found = _scan("uv.lock", head=_ROOT_UV + idna, base=_UV_BASE)
    assert found and "lost its integrity hash" in found[0].message


def test_a_removal_only_change_is_still_checked():
    # The hunk only removes lines, but the head entry now names another host.
    head = _ROOT_UV + _IDNA_36.replace("https://pypi.org/simple", "https://evil.example.net/simple")
    assert _scan("uv.lock", head=head, base=_UV_BASE, added=[], removed=["x"])


def test_a_clean_removal_only_change_passes():
    assert _scan("uv.lock", head=_ROOT_UV, base=_UV_BASE, added=[], removed=["x"]) == ()


def test_an_unparseable_toml_lock_is_flagged():
    found = _scan("uv.lock", head="[[package\nname = ", base=_UV_BASE)
    assert len(found) == 1 and "does not parse" in found[0].message


@pytest.mark.parametrize("head", ["x", None])
def test_a_lock_that_cannot_be_read_at_head_is_flagged(head):
    kw = {"fetch_error": RuntimeError("boom")} if head == "x" else {}
    found = _scan("uv.lock", head=head, base=_UV_BASE, **kw)
    assert len(found) == 1 and found[0].rule_name == RULE


# --- Cargo.lock and poetry.lock ----------------------------------------------


_CRATES = 'source = "registry+https://github.com/rust-lang/crates.io-index"\n'


def test_cargo_crates_io_index_is_the_registry():
    entry = f'[[package]]\nname = "serde"\nversion = "1.0.0"\n{_CRATES}checksum = "ab"\n'
    assert _scan("Cargo.lock", head="version = 3\n" + entry, base="version = 3\n") == ()


def test_a_cargo_git_source_is_flagged():
    entry = '[[package]]\nname = "x"\nversion = "1.0.0"\nsource = "git+https://github.com/a/x?rev=1#abc"\n'
    assert _scan("Cargo.lock", head="version = 3\n" + entry, base="version = 3\n")


def test_a_cargo_entry_that_loses_its_checksum_is_flagged():
    base = f'version = 3\n[[package]]\nname = "x"\nversion = "1.0.0"\n{_CRATES}checksum = "ab"\n'
    head = f'version = 3\n[[package]]\nname = "x"\nversion = "1.0.1"\n{_CRATES}'
    assert _scan("Cargo.lock", head=head, base=base)


@pytest.mark.parametrize(("source", "bad"), [
    ('[package.source]\ntype = "git"\nurl = "https://github.com/a/b.git"\n', True),
    ('[package.source]\ntype = "directory"\nurl = "../x"\n', True),
    ('[package.source]\ntype = "legacy"\nurl = "https://evil.example.net/simple"\nreference = "e"\n', True),
    ('[package.source]\ntype = "legacy"\nurl = "https://pypi.org/simple"\nreference = "p"\n', False),
    ("", False),
])
def test_poetry_sources(source, bad):
    base = '[metadata]\nlock-version = "2.0"\n'
    entry = f'[[package]]\nname = "x"\nversion = "1.0"\nfiles = []\n{source}'
    assert bool(_scan("poetry.lock", head=entry + base, base=base)) is bad


def test_a_poetry_entry_that_loses_its_hashes_is_flagged():
    meta = '[metadata]\nlock-version = "2.0"\n'
    base = f'[[package]]\nname = "x"\nversion = "1.0"\nfiles = [{{file = "x.whl", hash = "sha256:a"}}]\n{meta}'
    head = f'[[package]]\nname = "x"\nversion = "1.1"\nfiles = []\n{meta}'
    assert _scan("poetry.lock", head=head, base=base)


# --- package-lock.json -----------------------------------------------------


def _npm(packages: dict, version: int = 3) -> str:
    return json.dumps({"name": "app", "lockfileVersion": version, "packages": packages})


_ROOT = {"": {"name": "app"}}
_GOOD_NPM = {"version": "1.3.0", "integrity": "sha512-a",
             "resolved": "https://registry.npmjs.org/x/-/x-1.3.0.tgz"}


@pytest.mark.parametrize(("entry", "bad"), [
    ({**_GOOD_NPM, "funding": {"url": "https://github.com/sponsors/x"}}, False),
    ({**_GOOD_NPM, "resolved": "https://evil.example.net/x.tgz"}, True),
    ({**_GOOD_NPM, "resolved": "http://registry.npmjs.org/x.tgz"}, True),
    ({**_GOOD_NPM, "resolved": "//evil.example.net/x.tgz"}, True),
    ({**_GOOD_NPM, "resolved": "git+ssh://git@github.com/a/b.git#abc"}, True),
    ({"resolved": "../local", "link": True}, True),
    ({"version": "file:../x", "resolved": "file:../x"}, True),
])
def test_package_lock_sources(entry, bad):
    found = _scan("package-lock.json", head=_npm({**_ROOT, "node_modules/x": entry}), base=_npm(_ROOT))
    assert bool(found) is bad, found


def test_an_unchanged_workspace_link_is_not_flagged():
    packages = {**_ROOT, "node_modules/ws": {"resolved": "packages/ws", "link": True}}
    head = _npm({**packages, "node_modules/y": _GOOD_NPM})
    assert _scan("package-lock.json", head=head, base=_npm(packages)) == ()


def test_a_package_lock_entry_that_loses_its_integrity_is_flagged():
    base = _npm({**_ROOT, "node_modules/x": _GOOD_NPM})
    bumped = {k: v for k, v in _GOOD_NPM.items() if k != "integrity"}
    head = _npm({**_ROOT, "node_modules/x": {**bumped, "version": "1.3.1"}})
    found = _scan("package-lock.json", head=head, base=base)
    assert found and "lost its integrity hash" in found[0].message


def test_a_json_unicode_escape_is_decoded_then_checked():
    resolved = json.loads('"https://registry.npmjs.org\\u005c@evil.example/x.tgz"')
    head = _npm({**_ROOT, "node_modules/x": {**_GOOD_NPM, "resolved": resolved}})
    assert _scan("package-lock.json", head=head, base=_npm(_ROOT))


def _npm_v1(deps: dict) -> str:
    return json.dumps({"name": "app", "lockfileVersion": 1, "dependencies": deps})


@pytest.mark.parametrize(("entry", "bad"), [
    ({"version": "1.3.0", "resolved": "https://registry.npmjs.org/x/-/x-1.3.0.tgz"}, False),
    ({"version": "1.3.0-beta.1+b2", "resolved": "https://registry.npmjs.org/x/-/x.tgz"}, False),
    ({"version": "https://evil.example.net/x.tgz"}, True),
    ({"version": "https://registry.npmjs.org/x/-/x-1.0.0.tgz"}, True),
    ({"version": "github:a/b#abc"}, True),
    ({"version": "a/b#abc"}, True),
    ({"version": "npm:other@1.0.0", "resolved": "https://registry.npmjs.org/other/-/other-1.0.0.tgz"}, True),
])
def test_npm_v1_versions(entry, bad):
    found = _scan("package-lock.json", head=_npm_v1({"x": entry}), base=_npm_v1({}))
    assert bool(found) is bad, found


def test_unparseable_json_lock_is_flagged():
    assert _scan("package-lock.json", head="{", base=_npm(_ROOT))


# --- other formats get a note, never a finding --------------------------------


@pytest.mark.parametrize("path", [
    "yarn.lock", "pnpm-lock.yaml", "Gemfile.lock", "go.sum",
    ".terraform.lock.hcl", "Pipfile.lock", "composer.lock",
])
def test_other_formats_get_a_non_blocking_note(path):
    check = _run(path, head=None, base=None, added=['resolved "https://evil.example.net/x"'])
    assert check.findings == () and check.unreviewed == ()
    assert UNCHECKED_NOTE in check.note and f"`{path}`" in check.note


def test_the_note_names_basenames_only():
    check = _run("private-app/web/yarn.lock", head=None, base=None)
    assert "`yarn.lock`" in check.note
    assert "private-app" not in check.note


def test_the_note_covers_only_files_the_model_received():
    check = _run("web/yarn.lock", head=None, base=None, reviewed=frozenset())
    assert UNCHECKED_NOTE not in check.note
    assert check.unreviewed == ("yarn.lock",)


def test_an_unchecked_lock_dropped_for_size_is_unreviewed():
    check = _run("web/yarn.lock", head=None, base=None, reviewed=frozenset())
    assert check.unreviewed == ("yarn.lock",)
    assert UNCHECKED_NOTE not in check.note


def test_unchecked_lock_paths():
    assert is_unchecked_lock_path("web/yarn.lock")
    assert not is_unchecked_lock_path("uv.lock")
    assert not is_unchecked_lock_path("src/app.py")


def test_checked_formats_get_no_note():
    assert _run("uv.lock", head=_UV_BASE, base=_UV_BASE).note == ""


def test_non_lock_files_are_ignored():
    hunks = parse_diff(_diff("src/app.py", ["URL = 'https://evil.example.net'"]))
    check = lock_source_check(
        hunks, fetch=lambda p, r: {}, head_sha="H", base_sha="B",
        reviewed=frozenset({"src/app.py"}),
    )
    assert (check.findings, check.note, check.unreviewed) == ((), "", ())
