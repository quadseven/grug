"""Coverage for installations.list_activity — the Activity-feed read endpoint
(PRD #301, Slice S2). Calls the route fn directly + patches the store, matching
test_installations_list_repos."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import HTTPException

import installations as inst
from adapters.user_store import UserIdentity


def _user(user_id="100", role="user"):
    return UserIdentity(
        github_user_id=user_id, login="evan", role=role, tier="free",
        allowlisted=True, created_at="",
        allowlisted_at=None, allowlisted_by=None,
    )


def _row(**kw):
    base = dict(
        persona="elder", repo="o/r", pr_number=7, head_sha="abc",
        conclusion="neutral", summary="t", findings_count=0, blocking=False,
        verdict="STALE", created_at="2026-06-05T00:00:00+00:00",
    )
    base.update(kw)
    return base


def test_activity_unknown_install_404():
    with patch("installations.get_installation", return_value=None):
        with pytest.raises(HTTPException) as exc:
            inst.list_activity(install_id=999, user=_user())
    assert exc.value.status_code == 404


def test_activity_stranger_403():
    with patch("installations.get_installation", return_value={"installed_by_user_id": "999"}):
        with pytest.raises(HTTPException) as exc:
            inst.list_activity(install_id=1, user=_user(user_id="100"))
    assert exc.value.status_code == 403


def test_activity_verdict_is_derived_server_side_not_the_stored_value():
    """The endpoint re-derives the badge from each row's RAW facts (ADR-0003) —
    NOT the (possibly stale) stored `verdict` — so a mapping change heals
    history on read. Rows carry `verdict:"STALE"` to prove it's ignored."""
    install = {"installed_by_user_id": "100"}
    rows = [
        _row(persona="elder", conclusion="neutral", findings_count=2,
             created_at="2026-06-05T00:00:00+00:00"),   # -> warn
        _row(persona="chief", conclusion="failure", findings_count=1, blocking=True,
             created_at="2026-06-04T00:00:00+00:00"),   # -> block
    ]
    with patch("installations.get_installation", return_value=install):
        with patch("installations.list_check_verdicts", return_value=rows):
            out = inst.list_activity(install_id=1, user=_user(user_id="100"))
    acts = out["activity"]
    assert acts[0]["verdict"] == "warn"
    assert acts[1]["verdict"] == "block"
    assert acts[0]["persona"] == "elder"
    assert "STALE" not in {a["verdict"] for a in acts}


def test_activity_verdict_filter():
    install = {"installed_by_user_id": "100"}
    rows = [
        _row(head_sha="a", conclusion="neutral", findings_count=0),   # pass
        _row(head_sha="b", conclusion="failure", blocking=True),      # block
    ]
    with patch("installations.get_installation", return_value=install):
        with patch("installations.list_check_verdicts", return_value=rows):
            out = inst.list_activity(install_id=1, verdict="block", user=_user(user_id="100"))
    assert [a["verdict"] for a in out["activity"]] == ["block"]


def test_activity_limit_caps_output():
    install = {"installed_by_user_id": "100"}
    rows = [_row(head_sha=str(i), conclusion="success") for i in range(5)]
    with patch("installations.get_installation", return_value=install):
        with patch("installations.list_check_verdicts", return_value=rows):
            out = inst.list_activity(install_id=1, limit=2, user=_user(user_id="100"))
    assert len(out["activity"]) == 2


def test_activity_filter_does_not_underreturn_when_matches_are_sparse():
    """Regression (code-review S2): filtering must scan ALL rows, not a capped
    window — a `block` stranded past the first `limit` rows must still surface.
    Here only 3 of 60 are block (the last three); ?verdict=block must return 3."""
    install = {"installed_by_user_id": "100"}
    rows = [_row(head_sha=str(i), conclusion="success") for i in range(57)]  # pass
    rows += [_row(head_sha=f"b{i}", conclusion="failure", blocking=True) for i in range(3)]  # block
    with patch("installations.get_installation", return_value=install):
        # The endpoint requests limit=None (all); the store returns everything.
        with patch("installations.list_check_verdicts", return_value=rows):
            out = inst.list_activity(install_id=1, verdict="block", limit=50, user=_user(user_id="100"))
    assert len(out["activity"]) == 3
    assert all(a["verdict"] == "block" for a in out["activity"])
