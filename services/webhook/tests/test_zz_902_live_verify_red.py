"""TEMPORARY live-verification for #902 - intentionally red, never merge.

This file exists only so the `tests` check-run fails on this PR's head SHA,
letting us verify that Grug surfaces the PR's own red CI status in Elder's
review context. It will be closed unmerged.
"""


def test_intentionally_red_for_902_live_verification():
    assert False, "intentional failure: live-verification of #902 (red check-run)"
