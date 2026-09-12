"""Tests for the public-repo leak guard.

EVERY value below is INVENTED. A test for a leak guard is the last place a
real hostname or address should appear - the first draft of this file used
genuine ones and the hook correctly refused the commit.

The cases below are the ACTUAL leaks from 2026-08-15, generalised. Each one
reached a public commit, issue or PR body, and none was caught by secret
scanning, CodeQL, or human review - because none of them is secret-shaped.

The false-positive cases matter just as much. A guard that fires on ordinary
prose gets bypassed with --no-verify within a day, at which point it protects
nothing. 91 of the first 99 hits on the existing tree were prose like
"Post-#77", which is this repo's own issue after a hyphenated word.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import check_private_leaks as guard  # noqa: E402
from check_private_leaks import (  # noqa: E402
    compiled,
    deny_rule,
    load_ssm_deny_list,
    parse_deny_list,
    scan,
)


def _hits(text: str, diff: bool = False) -> list[str]:
    return scan(text, compiled(), label="t", diff_mode=diff)


def _names(text: str, diff: bool = False) -> set[str]:
    out = set()
    for h in _hits(text, diff):
        out.add(h.split("[", 1)[1].split("]", 1)[0])
    return out


# --- the real leaks -------------------------------------------------------

def test_catches_cluster_node_name():
    assert "cluster-node" in _names("deployed to k8s-examplecluster-zone-worker-01 today")  # leak-guard-allow: invented fixture, not a real host


def test_catches_tailnet_hostname():
    assert "tailnet-host" in _names("POST https://sparks.ts.example.me/v1/models")  # leak-guard-allow: invented fixture, not a real host


def test_catches_tailscale_cgnat_ip():
    assert "tailscale-ip" in _names("the host answers on 100.90.11.22")  # leak-guard-allow: invented fixture, not a real host


def test_catches_rfc1918_ip():
    assert "rfc1918-ip" in _names("LAN node is 10.9.9.9")  # leak-guard-allow: invented fixture, not a real host
    assert "rfc1918-ip" in _names("router at 192.168.1.1")  # leak-guard-allow: invented fixture, not a real host


def test_catches_internal_server_hostname():
    assert "server-hostname" in _names("ssh root@srv-example-gpu failed")  # leak-guard-allow: invented fixture, not a real host
    assert "server-hostname" in _names("usr-someone-mbp reported installed=none")  # leak-guard-allow: invented fixture, not a real host


def test_catches_cross_repo_issue_reference():
    assert "private-issue-ref" in _names("tracked in someprivaterepo#4242")  # leak-guard-allow: invented fixture, not a real host


# --- the false positives that would get the guard disabled ----------------

def test_ignores_this_repos_own_issue_refs():
    assert _names("fixed in #101 and #102") == set()


def test_ignores_hyphenated_prose_before_an_issue_ref():
    """"Post-#77" is a hyphenated word then THIS repo's issue, not a repo ref.

    This exact shape was 91 of the first 99 hits against the existing tree.
    """
    assert _names("Post-#111 the loader changed") == set()
    assert _names("pre-#111 behaviour was different") == set()


def test_ignores_ordinary_public_addresses():
    assert _names("see https://github.com/opengrep/opengrep/releases") == set()
    assert _names("resolves via 8.8.8.8 upstream") == set()


def test_ignores_semver_and_ordinary_numbers():
    assert _names("bumped to 9.9.9 and 8.8.7") == set()


def test_allow_marker_suppresses_a_line():
    text = "host is srv-example-gpu  # leak-guard-allow: illustrative only"
    assert _names(text) == set()


# --- diff semantics -------------------------------------------------------

def test_only_added_lines_are_scanned_in_a_diff():
    """A REMOVED private identifier is a scrub, not a leak - never block it."""
    diff = (
        "--- a/x.md\n"
        "+++ b/x.md\n"
        "-old host was srv-example-gpu\n"  # leak-guard-allow: invented fixture, not a real host
        "+now describes it generically\n"
    )
    assert _hits(diff, diff=True) == []


def test_added_line_in_a_diff_is_caught():
    diff = "--- a/x.md\n+++ b/x.md\n+new host srv-example-gpu added\n"  # leak-guard-allow: invented fixture, not a real host
    assert "server-hostname" in _names(diff, diff=True)


def test_diff_headers_never_trigger():
    """+++ / --- lines carry paths and must not be read as content."""
    assert _hits("--- a/srv-thing/x.md\n+++ b/srv-thing/x.md\n", diff=True) == []


# --- layer 2: the runtime deny-list ---------------------------------------
#
# Layer 2 is the half that catches PEOPLE. It shipped unreachable: the CI
# workflow never passed --deny-list-ssm, so for the guard's whole life it ran
# on generic shapes alone and printed "clean" while the operator's first name
# sat in ten source files (#921). Everything below exists so that cannot
# silently recur - the failure paths are asserted FATAL, and the last test
# asserts the workflow still passes the flag.
#
# Every term below is INVENTED, same rule as the fixtures above.

def _deny(*terms):
    return [(guard.DENY, deny_rule(t), "an explicitly denied term") for t in terms]


def test_parse_deny_list_drops_comments_and_blanks():
    raw = "# a comment\n\n  ada  \n\n# another\nlovelace\n"
    assert parse_deny_list(raw) == ["ada", "lovelace"]


def test_parse_deny_list_keeps_a_multi_word_term_whole():
    """A full name is ONE term. Whitespace-splitting would silently shatter it."""
    assert parse_deny_list("ada lovelace\n") == ["ada lovelace"]


def test_parse_deny_list_dedups_case_insensitively():
    assert parse_deny_list("Ada\nada\nADA\n") == ["Ada"]


def test_deny_term_matches_on_word_boundaries():
    rx = deny_rule("ada")
    assert rx.search("ping ada now")
    assert rx.search("that is ADA's box")
    assert rx.search("host ada-mbp reported in")
    assert rx.search("/Users/ada/dev/thing")


def test_deny_term_does_not_match_inside_a_longer_word():
    """The reason layer 2 needs boundaries at all.

    A short first name is a substring of ordinary English. Substring matching
    would fire on prose, and a guard that fires on prose gets bypassed.
    """
    rx = deny_rule("ada")
    assert not rx.search("shipped to canada last week")
    assert not rx.search("a nomadic worker process")
    assert not rx.search("adapter configured")


def test_deny_term_with_non_alphanumeric_edges_still_matches_in_context():
    """Path prefixes and domain suffixes must match mid-string."""
    assert deny_rule("/users/ada").search("see /Users/ada/dev/x.py")
    assert deny_rule(".example.net").search("box.example.net answered")


def test_deny_list_hit_is_reported_but_never_echoed():
    """A layer-2 hit must not republish the term into a PUBLIC Actions log."""
    hits = scan("contact ada about it", _deny("ada"), label="t")
    assert len(hits) == 1
    assert "ada" not in hits[0].lower().replace("deny-list", "")
    assert "redacted" in hits[0]
    assert "t:1" in hits[0]


def test_generic_shape_hits_are_still_shown_verbatim():
    """Only layer 2 is redacted - a shape hit is self-describing and public."""
    hits = scan("router at 192.168.1.1", compiled(), label="t")  # leak-guard-allow: RFC1918 doc address
    assert "192.168.1.1" in hits[0]  # leak-guard-allow: RFC1918 doc address


def test_deny_list_respects_diff_removal_semantics():
    diff = "--- a/x.md\n+++ b/x.md\n-ada was here\n+the operator was here\n"
    assert scan(diff, _deny("ada"), label="t", diff_mode=True) == []


# --- layer 2 failure paths: all FATAL, never a silent downgrade -----------

class _Completed:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _stub_run(monkeypatch, result):
    def fake(*args, **kwargs):
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(guard.subprocess, "run", fake)


def test_ssm_fetch_failure_is_fatal(monkeypatch):
    _stub_run(monkeypatch, _Completed(255, stderr="ParameterNotFound"))
    with pytest.raises(SystemExit) as exc:
        load_ssm_deny_list("/nope")
    assert exc.value.code == 2


def test_missing_aws_cli_is_fatal_not_a_traceback(monkeypatch):
    """Exit 2, not an OSError traceback that exits 1 and reads as 'leak found'."""
    _stub_run(monkeypatch, FileNotFoundError("aws"))
    with pytest.raises(SystemExit) as exc:
        load_ssm_deny_list("/nope")
    assert exc.value.code == 2


def test_empty_deny_list_parameter_is_fatal(monkeypatch):
    """A wiped parameter must NOT degrade the guard to generic-shapes-only.

    This is the exact shape of the bug being fixed: the guard would still run,
    still exit 0, and still print 'clean' while covering nothing it was asked
    to cover.
    """
    _stub_run(monkeypatch, _Completed(0, stdout="\n# only comments\n\n"))
    with pytest.raises(SystemExit) as exc:
        load_ssm_deny_list("/empty")
    assert exc.value.code == 2


def test_populated_deny_list_compiles_one_rule_per_term(monkeypatch):
    _stub_run(monkeypatch, _Completed(0, stdout="# hdr\nada\nlovelace\n"))
    rules = load_ssm_deny_list("/ok")
    assert [n for n, _, _ in rules] == [guard.DENY, guard.DENY]


# --- the regression that started this: CI must PASS the flag --------------

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "guard.private-leaks.yml"


def test_ci_workflow_passes_the_deny_list_on_every_invocation():
    """The bug this whole change exists to close.

    The workflow invoked the guard twice (diff scan, PR-text scan) and passed
    --deny-list-ssm on neither, so CI ran with the people-catching layer off
    while reporting a green check. Asserting it here means dropping the flag
    again fails a test instead of silently un-arming the guard.
    """
    text = GUARD_WORKFLOW.read_text(encoding="utf-8")
    invocations = [
        ln for ln in text.splitlines()
        if "check_private_leaks.py" in ln and not ln.lstrip().startswith("#")
    ]
    # Two: the diff scan and the PR title/body scan. Both must be armed - the
    # prose half is where most of the 2026-08-15 leaks actually were.
    assert len(invocations) == 2, invocations
    for line in invocations:
        assert "--deny-list-ssm" in line, f"guard invoked without layer 2: {line}"


def test_cli_says_out_loud_when_layer_2_is_off(tmp_path):
    """A run with no deny-list must never print a bare 'clean'."""
    body = tmp_path / "body.md"
    body.write_text("nothing interesting here\n", encoding="utf-8")
    out = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_private_leaks.py"),
         "--text-file", str(body)],
        capture_output=True, text=True, check=False,
    )
    assert out.returncode == 0, out.stderr
    assert "deny-list NOT CONFIGURED" in out.stdout
