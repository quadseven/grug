"""TPM persona — Hunt Plan (Definition-of-Ready) PR check. Canonical name: Chief.

`evaluate_pull_request(pr_body)` in `persona.py` runs the 9 Hunt Plan checks
in `dor_checks.py` and rolls them into a `TpmEvaluation`: 5 static checks on
the PR body (`why`, `acceptance`, `estimate`, `scope-fence`, `issue-link`)
and 3 that fetch the ticket and the PR diff the body names
(`linked-issue-completeness`, `linked-issue-epic`, `in-scope`). Only the 5
static ones are on the purity attester's
list (`infra/scripts/attest_persona_purity.py`), because the other 3 do IO. The GitHub check-run POST lives in
`publish_tpm_evaluation(...)`, which publishes through the shared
`personas.publish_check` seam.

A PR body carrying an agent-authored marker (a `claude.ai/code/session_`
link, Claude Code's `Generated with [Claude Code]` attribution footer, or
`<!-- grug:agent-authored -->`) skips the Hunt Plan and concludes NEUTRAL —
the format gate applies to PRs written by people; Elder + Guard still review
the code.

The LLM "Scope review" companion half is roadmap-only. An earlier version of
this docstring claimed it was wired through `poolside_client.py` — that
module was never built and does not exist in the repo.
"""
