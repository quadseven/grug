Not valid against this code: nothing in this repo builds a shell command out
of a commenter's text. There is no `./bin/grug-ask` and no `--arg` flag
anywhere (`grep -rn "grug-ask" .` finds nothing), and the `/grug` command
path never calls a shell.

What `/grug ask <question>` actually does: `interactive.run_command` hands
the text to `rerun.enqueue_ask`, which puts it in a JSON job body
(`{"kind": "ask", ..., "question": question}`) on the rerun queue. The
consumer redacts it (`llm_client._redact_secrets`), sends it to the model as
prompt text, and posts the answer back as a PR comment. A comment of
`/grug ask anything; rm -rf /` is therefore a question, not a command: the
semicolon is never interpreted by anything.

The only subprocess calls in the tree are the review tools themselves
(`ruff`, `opengrep`), and they already use list-form argv with no shell
(services/_shared/personas/code_reviewer/lint.py:296 and
analyzers.py:164), so no commenter's text can reach an argument list either.
