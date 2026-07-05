"""#498/ADR-0016: the promotion decision - every ambiguous input rebuilds."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from promotion import decide

_TREE = "a" * 40
_BOTH = {"webhook": True, "api": True}


def test_promotes_when_single_pr_tree_identical_and_manifests_present():
    d = decide(pr_numbers=[518], pr_head_tree=_TREE, merge_tree=_TREE,
               manifests_present=_BOTH)
    assert d.promote is True
    assert "pr-518" in d.reason


def test_rebuild_on_zero_or_multiple_prs():
    for prs in ([], [1, 2]):
        d = decide(pr_numbers=prs, pr_head_tree=_TREE, merge_tree=_TREE,
                   manifests_present=_BOTH)
        assert d.promote is False
        assert "rebuild" in d.reason


def test_rebuild_on_tree_mismatch():
    """Main moved under the PR: the tested image is NOT the merged code -
    the load-bearing correctness rule of ADR-0016."""
    d = decide(pr_numbers=[518], pr_head_tree=_TREE, merge_tree="b" * 40,
               manifests_present=_BOTH)
    assert d.promote is False
    assert "tree" in d.reason


def test_rebuild_on_missing_tree_hashes():
    for head, merge in ((None, _TREE), (_TREE, None), ("", _TREE)):
        d = decide(pr_numbers=[518], pr_head_tree=head, merge_tree=merge,
                   manifests_present=_BOTH)
        assert d.promote is False


def test_rebuild_on_missing_manifest_names_the_service():
    d = decide(pr_numbers=[518], pr_head_tree=_TREE, merge_tree=_TREE,
               manifests_present={"webhook": True, "api": False})
    assert d.promote is False
    assert "api" in d.reason and "webhook" not in d.reason.split("for:")[1]


def test_rebuild_on_empty_service_map():
    d = decide(pr_numbers=[518], pr_head_tree=_TREE, merge_tree=_TREE,
               manifests_present={})
    assert d.promote is False


def test_cli_emits_output_lines_and_never_fails(tmp_path):
    import json
    import subprocess

    script = Path(__file__).resolve().parents[1] / "promotion.py"
    ok = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps({"pr_numbers": [7], "pr_head_tree": _TREE,
                          "merge_tree": _TREE,
                          "manifests_present": {"webhook": True, "api": True}}),
        capture_output=True, text=True,
    )
    assert ok.returncode == 0
    assert "promote=true" in ok.stdout and "reason=promote: pr-7" in ok.stdout
    garbage = subprocess.run(
        [sys.executable, str(script)], input="not json",
        capture_output=True, text=True,
    )
    assert garbage.returncode == 0
    assert "promote=false" in garbage.stdout and "unparseable" in garbage.stdout
