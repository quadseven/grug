Not valid against this code: there is no `run_async` in this repo
(`grep -rn run_async .` finds nothing), and nothing at
services/webhook/interactive.py:64 is an except block - that line is part of
the `_elder_review_state` docstring.

The broad `except Exception` blocks that do exist in this file are the
pause-store calls in `_pause_after_wake` (around `resume(...)` and
`is_paused(...)`), and they are deliberate: the Elder review is already
enqueued when they run, the pause lift is a side effect on top of it, and
the reply tells the reader the pause may still hold. Narrowing them to a
transport class would let a programmer error 500 the webhook, and GitHub does
not redeliver on 5xx, so the command would be lost - the same reasoning the
`/grug recheck` final guard carries.

What this PR changed is the code it added: every new catch in
interactive.py is narrowed to the expected GitHub transport errors
(`httpx.HTTPStatusError`, `httpx.RequestError`), so a NameError or a
TypeError is no longer masked behind a swallowed traceback there. The
`_elder_review_state` catch now answers "unknown" (do not queue) and says so
in the log; the `_reply` catch now answers False, and the caller keeps the
command unclaimed when the answer did not land, so it can be answered again.
