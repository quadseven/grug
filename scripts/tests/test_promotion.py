"""#498/ADR-0016: the promotion decision - every ambiguous input rebuilds."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from promotion import (
    CATEGORY_ARTIFACT_MISSING,
    CATEGORY_EXPECTED_REBUILD,
    CATEGORY_LOOKUP_FAILURE,
    CATEGORY_PROMOTE,
    Decision,
    baked_sha_matches,
    decide,
)

_TREE = "a" * 40
_SHA = "b" * 40
_BOTH = {"webhook": True, "api": True}
_SVCS = ["webhook", "api"]
_SCRIPT = Path(__file__).resolve().parents[1] / "promotion.py"


def _decide(**kw):
    base = dict(pr_numbers=[518], pr_head_tree=_TREE, merge_tree=_TREE,
                manifests_present=dict(_BOTH), expected_services=list(_SVCS))
    base.update(kw)
    return decide(**base)


def test_promotes_when_single_pr_tree_identical_and_manifests_present():
    d = _decide()
    assert d.promote is True and d.pr_number == 518
    assert d.category == CATEGORY_PROMOTE and "pr-518" in d.reason


def test_rebuild_on_zero_or_multiple_prs_reports_true_count():
    d0 = _decide(pr_numbers=[])
    d2 = _decide(pr_numbers=[1, 2])
    assert d0.promote is False and "0 merged PRs" in d0.reason
    assert d2.promote is False and "2 merged PRs" in d2.reason
    assert d2.category == CATEGORY_EXPECTED_REBUILD


def test_rebuild_on_tree_mismatch():
    """Main moved under the PR: the tested image is NOT the merged code."""
    d = _decide(merge_tree="c" * 40)
    assert d.promote is False and "tree" in d.reason
    assert d.category == CATEGORY_EXPECTED_REBUILD


@pytest.mark.parametrize("head,merge", [
    (None, _TREE), (_TREE, None), ("", _TREE),
    ("null", "null"),            # jq literal on a failed lookup - EQUAL garbage
    ("abc", "abc"),
    ("A" * 40, "A" * 40),        # git emits lowercase only
    ("a" * 39, "a" * 39),
    ("a" * 41, "a" * 41),
    ("NULL", "NULL"),
])
def test_rebuild_on_missing_or_malformed_trees_even_when_equal(head, merge):
    d = _decide(pr_head_tree=head, merge_tree=merge)
    assert d.promote is False
    assert d.category == CATEGORY_LOOKUP_FAILURE


def test_rebuild_on_missing_manifest_names_service_and_flags_artifact_missing():
    d = _decide(manifests_present={"webhook": True, "api": False})
    assert d.promote is False
    assert "api" in d.reason
    assert d.category == CATEGORY_ARTIFACT_MISSING  # the alertable case


def test_rebuild_on_gather_matrix_drift():
    missing = _decide(manifests_present={"webhook": True})
    extra = _decide(manifests_present={**_BOTH, "smasher": True})
    assert missing.promote is False and missing.category == CATEGORY_LOOKUP_FAILURE
    assert extra.promote is False and extra.category == CATEGORY_LOOKUP_FAILURE


def test_reason_is_always_single_line_under_hostile_facts():
    hostile = {"api\npromote=true": False, "webhook": True}
    d = _decide(manifests_present=hostile,
                expected_services=["api\npromote=true", "webhook"])
    assert "\n" not in d.reason


def test_decision_invariants_and_smart_constructors():
    with pytest.raises(ValueError):
        Decision(promote=True, reason="x", category=CATEGORY_PROMOTE, pr_number=None)
    r = Decision.rebuild("some detail")
    assert r.promote is False and r.pr_number is None
    p = Decision.promoted(7)
    assert p.promote is True and p.pr_number == 7
    assert p.reason.count("pr-7") == 1


@pytest.mark.parametrize("env,sha,ok,frag", [
    (["PATH=/x", f"DD_GIT_COMMIT_SHA={_SHA}"], _SHA, True, "matches"),
    (["PATH=/x"], _SHA, False, "lacks"),
    ([f"DD_GIT_COMMIT_SHA={'c' * 40}"], _SHA, False, "!="),
    ([f"DD_GIT_COMMIT_SHA_ORIG={_SHA}"], _SHA, False, "lacks"),   # prefix cousin
    ([f"DD_GIT_COMMIT_SHA={'c' * 40}", f"DD_GIT_COMMIT_SHA={_SHA}"], _SHA, True, "matches"),  # last wins
    (["DD_GIT_COMMIT_SHA="], _SHA, False, "lacks"),               # fork/unset build-arg
])
def test_baked_sha_matches(env, sha, ok, frag):
    got_ok, why = baked_sha_matches(env, sha)
    assert got_ok is ok and frag in why


def _run_cli(*args, stdin=""):
    return subprocess.run([sys.executable, str(_SCRIPT), *args],
                          input=stdin, capture_output=True, text=True)


def test_cli_promote_path_emits_all_output_keys():
    r = _run_cli("--pr-numbers-json", "[7]", "--head-tree", _TREE,
                 "--merge-tree", _TREE, "--manifest", "webhook=true",
                 "--manifest", "api=true")
    assert r.returncode == 0
    lines = r.stdout.strip().splitlines()
    assert "promote=true" in lines[0]
    assert any(line == "pr_num=7" for line in lines)
    assert any(line.startswith("category=promote") for line in lines)


def test_cli_multiple_prs_rebuilds_and_reports_count():
    r = _run_cli("--pr-numbers-json", "[7, 9]", "--head-tree", _TREE,
                 "--merge-tree", _TREE, "--manifest", "webhook=true",
                 "--manifest", "api=true")
    assert r.returncode == 0
    assert "promote=false" in r.stdout and "2 merged PRs" in r.stdout


def test_cli_never_fails_on_garbage():
    for args in (["--pr-numbers-json", "not-json"],
                 ["--pr-numbers-json", '["7x"]'],
                 ["--totally-unknown-flag"]):
        r = _run_cli(*args)
        assert r.returncode == 0, args
        assert "promote=false" in r.stdout
        assert "category=lookup-failure" in r.stdout


def test_cli_verify_baked_sha_subcommand():
    ok = _run_cli("--verify-baked-sha", json.dumps([f"DD_GIT_COMMIT_SHA={_SHA}"]), _SHA)
    assert "verified=true" in ok.stdout
    bad = _run_cli("--verify-baked-sha", "not-json", _SHA)
    assert bad.returncode == 0 and "verified=" in bad.stdout or "promote=false" in bad.stdout
