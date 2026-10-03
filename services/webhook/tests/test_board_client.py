"""Board read-modify-write: the part that stops personas deleting each other.

The naive version - build a complete body and PATCH it wholesale - loses data
silently the moment a second persona writes. These pin that it cannot.
"""
from __future__ import annotations

import httpx

from personas import board, board_client as bc


def _resp(status, payload):
    return httpx.Response(status, json=payload,
                          request=httpx.Request("GET", "https://x"))


def _wire(monkeypatch, comments):
    """Fake the comment list; capture writes."""
    writes = []
    monkeypatch.setattr(bc.httpx, "get", lambda url, **kw: _resp(200, comments))
    monkeypatch.setattr(
        bc.httpx, "patch",
        lambda url, **kw: writes.append(("patch", url, kw["json"]["body"])) or _resp(200, {}))
    monkeypatch.setattr(
        bc.httpx, "post",
        lambda url, **kw: writes.append(("post", url, kw["json"]["body"])) or _resp(201, {"id": 1}))
    return writes


def test_second_persona_does_not_delete_the_first(monkeypatch):
    """THE reason this module exists. Elder writes, then Chief writes; Elder's
    section must survive byte-for-byte."""
    existing = board.set_header(board.new_board(), "### verdict")
    existing = board.upsert_section(existing, "elder", "ELDER-BODY")
    writes = _wire(monkeypatch, [{"id": 7, "body": existing}])

    bc.upsert_board_section("tok", "o", "r", 5, key="chief", section="CHIEF-BODY")
    verb, url, body = writes[0]
    assert verb == "patch" and url.endswith("/issues/comments/7")
    assert "ELDER-BODY" in body and "CHIEF-BODY" in body


def test_header_is_left_alone_when_not_supplied(monkeypatch):
    """Only the persona that counts findings has standing to rewrite the
    verdict. Chief passing header=None must not blank it."""
    existing = board.set_header(board.new_board(), "### Grug say WAIT.")
    writes = _wire(monkeypatch, [{"id": 7, "body": existing}])
    bc.upsert_board_section("tok", "o", "r", 5, key="chief", section="C")
    assert "Grug say WAIT." in writes[0][2]


def test_header_is_refreshed_when_supplied(monkeypatch):
    existing = board.set_header(board.new_board(), "### old")
    existing = board.upsert_section(existing, "chief", "C")
    writes = _wire(monkeypatch, [{"id": 7, "body": existing}])
    bc.upsert_board_section("tok", "o", "r", 5, key="elder", section="E",
                            header="### new verdict")
    body = writes[0][2]
    assert "new verdict" in body and "old" not in body
    assert "C" in body                       # other sections survive


def test_absent_board_is_created_with_a_header(monkeypatch):
    """A board with no header would email a bare section."""
    writes = _wire(monkeypatch, [])
    out = bc.upsert_board_section("tok", "o", "r", 5, key="elder", section="E",
                                  header="### Grug look hard.")
    assert out["board"] == "created"
    verb, _, body = writes[0]
    assert verb == "post"
    assert body.startswith(board.BOARD_MARKER)
    assert "Grug look hard." in body


def test_legacy_elder_comment_is_converted_not_duplicated(monkeypatch):
    """A pre-board Elder comment must be rewritten IN PLACE. Posting a board
    beside it is the duplicate comment - and duplicate email - the board
    exists to end."""
    legacy = "<!-- grug-elder-stack -->\nold elder text"
    writes = _wire(monkeypatch, [{"id": 9, "body": legacy}])
    out = bc.upsert_board_section("tok", "o", "r", 5, key="chief", section="C")
    assert out["board"] == "updated"
    verb, url, body = writes[0]
    assert verb == "patch" and url.endswith("/issues/comments/9")
    assert "old elder text" in body          # prior content preserved
    assert "C" in body
    assert board.is_board(body)


def test_a_humans_comment_quoting_a_marker_is_never_edited(monkeypatch):
    """With app_id set, only this App's own comments count. Editing a human's
    comment because they quoted a marker would be indefensible."""
    monkeypatch.setattr(bc.httpx, "get", lambda url, **kw: _resp(200, [
        {"id": 3, "body": board.BOARD_MARKER + "\nnot ours",
         "performed_via_github_app": None},
    ]))
    writes = []
    monkeypatch.setattr(bc.httpx, "post",
                        lambda url, **kw: writes.append(kw["json"]["body"]) or _resp(201, {"id": 1}))
    out = bc.upsert_board_section("tok", "o", "r", 5, key="elder", section="E",
                                  header="### h", app_id="123")
    assert out["board"] == "created"          # ignored the human comment
    assert writes and "not ours" not in writes[0]


def _grug_comment(body):
    return {"id": 7, "body": body, "performed_via_github_app": {"id": 123}}


def test_in_progress_note_swaps_header_only(monkeypatch):
    """#819: the re-review note swaps the header; prior findings survive
    byte-for-byte underneath it."""
    existing = board.set_header(board.new_board(), "### Grug say WAIT, 3 findings")
    existing = board.upsert_section(existing, "elder", "ELDER-FINDINGS")
    writes = _wire(monkeypatch, [_grug_comment(existing)])

    saved = bc.mark_board_in_progress("tok", "o", "r", 5, app_id="123")

    assert saved == "### Grug say WAIT, 3 findings"
    assert len(writes) == 1
    verb, url, body = writes[0]
    assert verb == "patch" and url.endswith("/issues/comments/7")
    assert bc.IN_PROGRESS_HEADER in body
    assert "ELDER-FINDINGS" in body
    assert "Grug say WAIT, 3 findings" not in body


def test_in_progress_note_ignores_human_comment_with_marker(monkeypatch):
    """#819 requirement 1: a non-Grug comment carrying the marker is never
    selected or modified, even though it matches the board marker."""
    human = board.set_header(board.new_board(), "### human verdict")
    writes = _wire(
        monkeypatch,
        [
            {"id": 9, "body": human, "performed_via_github_app": {"id": 999}},
        ],
    )

    saved = bc.mark_board_in_progress("tok", "o", "r", 5, app_id="123")

    assert saved is None
    assert writes == []


def test_in_progress_note_never_creates(monkeypatch):
    """#819 requirement 5: no board, no note, no email. create_if_absent is
    always False for the in-progress note."""
    writes = _wire(monkeypatch, [])

    saved = bc.mark_board_in_progress("tok", "o", "r", 5, app_id="123")

    assert saved is None
    assert all(verb != "post" for verb, _, _ in writes)


def test_clear_restores_saved_header(monkeypatch):
    """#819 requirement 2: a dead review's note is cleared, prior verdict back."""
    existing = board.set_header(board.new_board(), bc.IN_PROGRESS_HEADER)
    existing = board.upsert_section(existing, "elder", "ELDER-FINDINGS")
    writes = _wire(monkeypatch, [_grug_comment(existing)])

    ok = bc.clear_board_in_progress(
        "tok",
        "o",
        "r",
        5,
        app_id="123",
        saved_header="### Grug say WAIT, 3 findings",
    )

    assert ok is True
    body = writes[0][2]
    assert "Grug say WAIT, 3 findings" in body
    assert bc.IN_PROGRESS_HEADER not in body
    assert "ELDER-FINDINGS" in body


def test_clear_does_not_clobber_a_newer_header(monkeypatch):
    """The settled write (or a newer review) already replaced the note -
    restoring the stale saved header would be a lie."""
    existing = board.set_header(board.new_board(), "### Grug say CLEAR, 0 findings")
    writes = _wire(monkeypatch, [_grug_comment(existing)])

    ok = bc.clear_board_in_progress(
        "tok",
        "o",
        "r",
        5,
        app_id="123",
        saved_header="### Grug say WAIT, 3 findings",
    )

    assert ok is False
    assert writes == []


def test_clear_when_board_vanished_is_quiet(monkeypatch):
    """Board deleted mid-review: nothing to restore, no exception."""
    writes = _wire(monkeypatch, [])

    ok = bc.clear_board_in_progress(
        "tok",
        "o",
        "r",
        5,
        app_id="123",
        saved_header="### Grug say WAIT, 3 findings",
    )

    assert ok is False
    assert writes == []
