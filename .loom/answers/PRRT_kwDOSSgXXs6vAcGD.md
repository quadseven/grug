Not valid against this code: services/webhook/interactive.py:71 is inside the
`_elder_review_state` docstring, not an except block, and no except block in
this file logs a warning and then returns a dict.

The two blocks the finding describes are now the other way round - they
report what happened instead of hiding it:

- `_elder_review_state` catches only `httpx.HTTPStatusError` /
  `httpx.RequestError` and returns "unknown" when the check-run listing
  cannot be read. "unknown" is not read as "idle" any more, so an
  unreadable listing can no longer queue a second review of a commit Elder
  may already be on.
- `_reply` catches only the same two transport errors and returns whether
  the answer landed. When it did not, the command is left UNCLAIMED
  (`_already_reviewing` returns `{"status": "skip", "reason":
  "reply_failed"}`), so a redelivery can answer it again. A command that was
  heard and never answered is exactly the silent no-op this ticket exists
  to stop.

The label-write failures this PR did have - a refused write that was
recorded as if the sign were up - are fixed in
services/webhook/rerun.py: `_mark_reviewing_label` and
`_clear_reviewing_label` return False and log the refusal at ERROR level
with the coordinates.
