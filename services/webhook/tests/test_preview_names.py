"""#500: preview naming + reap logic - injection-safe, correct reaping."""

from __future__ import annotations

import pytest

from preview_names import (
    DEFAULT_TTL_HOURS,
    pr_of_namespace,
    preview_namespace,
    preview_schema,
    reap_targets,
)


def test_names_derived_from_pr():
    assert preview_namespace(42) == "grug-pr-42"
    assert preview_schema(42) == "grug_pr_42"


@pytest.mark.parametrize("bad", [0, -1, "42", 4.2, True, None, "42; DROP SCHEMA"])
def test_names_reject_non_positive_int(bad):
    with pytest.raises((ValueError, TypeError)):
        preview_namespace(bad)
    with pytest.raises((ValueError, TypeError)):
        preview_schema(bad)


def test_pr_of_namespace():
    assert pr_of_namespace("grug-pr-7") == 7
    for notpreview in ("grug", "grug-webhook", "grug-pr-", "grug-pr-x", "kube-system"):
        assert pr_of_namespace(notpreview) is None


def test_reap_closed_pr():
    prevs = [{"namespace": "grug-pr-1", "age_hours": 2}]
    assert reap_targets(prevs, open_pr_numbers=set()) == ["grug-pr-1"]
    assert reap_targets(prevs, open_pr_numbers={1}) == []


def test_reap_ttl_lapsed_even_if_open():
    prevs = [{"namespace": "grug-pr-1", "age_hours": DEFAULT_TTL_HOURS + 1}]
    assert reap_targets(prevs, open_pr_numbers={1}) == ["grug-pr-1"]


def test_reap_never_touches_non_preview_namespace():
    prevs = [
        {"namespace": "grug", "age_hours": 9999},
        {"namespace": "kube-system", "age_hours": 9999},
        {"namespace": "grug-pr-5", "age_hours": 1},
    ]
    # even with an empty open set (everything "closed"), only the real
    # preview is a candidate - and it's young + open-less so it reaps;
    # the prod/system namespaces are never candidates.
    assert reap_targets(prevs, open_pr_numbers=set()) == ["grug-pr-5"]


def test_reap_mixed():
    prevs = [
        {"namespace": "grug-pr-1", "age_hours": 1},    # open, young -> keep
        {"namespace": "grug-pr-2", "age_hours": 1},    # closed -> reap
        {"namespace": "grug-pr-3", "age_hours": 100},  # open but stale -> reap
    ]
    assert sorted(reap_targets(prevs, open_pr_numbers={1, 3})) == ["grug-pr-2", "grug-pr-3"]
