"""Read-modify-write for the shared review board.

`board.py` is pure string assembly. This is the thin I/O around it, and it
exists because the naive version silently loses data: Elder used to build a
COMPLETE comment body and PATCH it wholesale, so the moment a second persona
wrote a section, Elder's next pass would overwrite it.

Every persona therefore goes through `upsert_board_section`, which:
  1. finds the board comment (by marker, tolerating the legacy Elder marker),
  2. reads its CURRENT body,
  3. replaces only its own delimited region,
  4. optionally refreshes the header,
  5. writes the merged result back.

Why this is worth a module rather than a helper on each persona: a persona
that forgets step 2 does not fail loudly, it just deletes another persona's
section. That is the exact failure the board was built to prevent, so the
read-modify-write lives in ONE place with the reasoning attached.

CREATE vs UPDATE IS THE EMAIL DECISION. GitHub mails on comment creation and
never on edit, so POST costs the author a notification and PATCH costs nothing.
`create_if_absent=False` lets a persona say "I have nothing worth mailing":
it will still correct an existing board, but will not start one. Measured on
five consecutive PRs, every grug email was a clean review restating "no
findings" five different ways - a push notification with no information in it.

CONCURRENCY. Sections are delimited, so two personas racing can only clobber
their own region - never each other's content. The remaining race is
create-vs-create, handled the same way Elder's original upsert did: look
again immediately before POST, and heal by PATCH if a create loses.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import quote

import httpx

from personas import board

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.personas.board_client")

_TIMEOUT = 10
_MAX_PAGES = 20

# PRs reviewed before the board existed carry an Elder-private comment. Finding
# it means we REWRITE it in place; not finding it would post a board beside it,
# which is the duplicate comment (and duplicate email) the board exists to end.
LEGACY_MARKERS: tuple[str, ...] = (
    "<!-- grug-elder-stack -->",
    "<!-- grug-chief:ticket-compliance -->",
)


# Re-review in-progress note (#819). Push a new commit to a PR and the board
# keeps showing the PREVIOUS verdict for the whole next review. While a
# re-review runs, the header is swapped to this note; the settled write (or
# the clear path) puts the real verdict back. Header-only by construction:
# sections (prior findings) are never touched, and it never creates a board
# (creating mails the author - the note is not worth an email).
IN_PROGRESS_HEADER = "### Grug read new markings. Wait."


def mark_board_in_progress(
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
    *,
    app_id: str,
) -> str | None:
    """Swap the board header to the in-progress note (#819).

    Finds the board with `app_id` (a human quoting the marker is never
    selected), saves the current header, and PATCHes ONLY the header region
    - prior findings stay visible byte-for-byte under the note. Never
    creates: no board means no note (and no email).

    Returns the saved header, or None when there was no board to update or
    the write failed. Never raises: the note is cosmetic, the review is not.
    """
    try:
        found = find_board(token, owner, repo, pr_number, app_id=app_id)
        if found is None:
            return None
        comment_id, existing = found
        saved = board.extract_header(existing)
        if saved is None:
            return None
        body = board.set_header(existing, IN_PROGRESS_HEADER)
        httpx.patch(
            f"https://api.github.com/repos/{_repo(owner, repo)}"
            f"/issues/comments/{comment_id}",
            json={"body": body},
            headers=_headers(token),
            timeout=_TIMEOUT,
        ).raise_for_status()
        log.info(
            "board_in_progress_note_set",
            extra={"repo": f"{owner}/{repo}", "pr": pr_number},
        )
        return saved
    except (httpx.HTTPStatusError, httpx.RequestError):
        log.warning(
            "board_in_progress_note_failed",
            extra={"repo": f"{owner}/{repo}", "pr": pr_number},
            exc_info=True,
        )
        return None


def clear_board_in_progress(
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
    *,
    app_id: str,
    saved_header: str,
) -> bool:
    """Restore a header saved by `mark_board_in_progress` (#819).

    Only restores when the live header is still the in-progress note: if the
    settled write (or a newer review) already updated it, restoring the stale
    saved header would be a lie, so it stays hands-off. Header-only PATCH,
    never creates. Returns True when the header was restored. Never raises.
    """
    try:
        found = find_board(token, owner, repo, pr_number, app_id=app_id)
        if found is None:
            return False
        comment_id, existing = found
        current = board.extract_header(existing)
        if current != IN_PROGRESS_HEADER:
            log.info(
                "board_in_progress_note_already_cleared",
                extra={"repo": f"{owner}/{repo}", "pr": pr_number},
            )
            return False
        body = board.set_header(existing, saved_header)
        httpx.patch(
            f"https://api.github.com/repos/{_repo(owner, repo)}"
            f"/issues/comments/{comment_id}",
            json={"body": body},
            headers=_headers(token),
            timeout=_TIMEOUT,
        ).raise_for_status()
        log.info(
            "board_in_progress_note_cleared",
            extra={"repo": f"{owner}/{repo}", "pr": pr_number},
        )
        return True
    except (httpx.HTTPStatusError, httpx.RequestError):
        log.warning(
            "board_in_progress_note_clear_failed",
            extra={"repo": f"{owner}/{repo}", "pr": pr_number},
            exc_info=True,
        )
        return False


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _repo(owner: str, repo: str) -> str:
    return f"{quote(owner, safe='')}/{quote(repo, safe='')}"


def find_board(
    token: str, owner: str, repo: str, pr_number: int, *, app_id: str | None = None,
) -> tuple[int, str] | None:
    """`(comment_id, body)` of the board, or None.

    Matches the board marker OR a legacy per-persona marker. `app_id`, when
    given, restricts to comments this App authored - a human quoting a marker
    in a review discussion must never be mistaken for the board and edited.
    """
    base = f"https://api.github.com/repos/{_repo(owner, repo)}"
    page = 1
    while page <= _MAX_PAGES:
        resp = httpx.get(
            f"{base}/issues/{pr_number}/comments",
            params={"per_page": 100, "page": page},
            headers=_headers(token), timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        batch = resp.json() or []
        for c in batch:
            if app_id is not None:
                app = c.get("performed_via_github_app")
                if not app or str(app.get("id")) != str(app_id):
                    continue
            body = c.get("body") or ""
            if board.is_board(body) or any(m in body for m in LEGACY_MARKERS):
                return int(c["id"]), body
        if len(batch) < 100:
            return None
        page += 1
    log.warning(
        "board_find_page_cap",
        extra={"repo": f"{owner}/{repo}", "pr": pr_number, "cap": _MAX_PAGES},
    )
    return None


def upsert_board_section(
    token: str, owner: str, repo: str, pr_number: int,
    *, key: str, section: str, header: str | None = None,
    header_on_create: str | None = None,
    app_id: str | None = None, create_if_absent: bool = True,
) -> dict[str, Any]:
    """Merge one persona's section into the board, creating it if absent.

    `header` refreshes the verdict line. Pass None to leave whatever is there,
    which is what a persona with no global verdict (Chief, Sentinel) should do
    - only the reviewer that counts findings has the standing to rewrite it.

    `header_on_create` is for exactly that persona when it happens to be FIRST.
    The board it creates is the email, and a persona with no verdict would mail
    the neutral placeholder ("Grug look.") as the entire subject. So: supply a
    line good enough to be the email if you are the one starting the board, but
    never overwrite a verdict someone with standing already wrote.

    `create_if_absent=False` means "update the board if one exists, but do not
    start one for this". THE point of the distinction: creating a comment mails
    the author, editing one never does. A persona with nothing to report must
    therefore not CREATE - a mail that says "nothing found" is a notification
    with no information in it. It must still UPDATE, because a board left
    saying "3 findings" after they were fixed is stale and wrong, and correcting
    it is free.

    Returns an audit dict; never raises past the caller's own error handling.
    """
    base = f"https://api.github.com/repos/{_repo(owner, repo)}"
    found = find_board(token, owner, repo, pr_number, app_id=app_id)

    if found is None and not create_if_absent:
        log.info(
            "board_create_declined_nothing_to_say",
            extra={"repo": f"{owner}/{repo}", "pr": pr_number, "section": key},
        )
        return {"board": "skipped", "section": key, "reason": "nothing_to_say"}

    if found is None:
        # Creating: a board with no header would email a bare section, so a
        # creator without one gets its create-only line, then a placeholder.
        body = board.set_header(
            board.new_board(),
            next((h for h in (header, header_on_create) if h), "### Grug look."),
        )
        body = board.upsert_section(body, key, section)
        try:
            httpx.post(
                f"{base}/issues/{pr_number}/comments",
                json={"body": body}, headers=_headers(token), timeout=_TIMEOUT,
            ).raise_for_status()
            return {"board": "created", "section": key}
        except httpx.HTTPStatusError:
            # Lost a create race: re-find and merge into the winner instead of
            # posting a second board.
            found = find_board(token, owner, repo, pr_number, app_id=app_id)
            if found is None:
                raise

    comment_id, existing = found
    # A legacy per-persona comment is not a board yet: keep its text as that
    # persona's section rather than discarding what it already said.
    if not board.is_board(existing):
        legacy = existing
        existing = board.set_header(
            board.new_board(), header if header is not None else "### Grug look.",
        )
        if key != "elder":
            existing = board.upsert_section(existing, "elder", legacy)

    merged = board.upsert_section(existing, key, section)
    if header is not None:
        merged = board.set_header(merged, header)

    httpx.patch(
        f"{base}/issues/comments/{comment_id}",
        json={"body": merged}, headers=_headers(token), timeout=_TIMEOUT,
    ).raise_for_status()
    return {"board": "updated", "section": key, "comment_id": str(comment_id)}
