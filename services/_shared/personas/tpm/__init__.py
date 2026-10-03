"""TPM persona — Hunt Plan (Definition-of-Ready) PR check. Canonical name: Chief.

`evaluate_pull_request(pr_body)` in `persona.py` runs the 7 static Hunt Plan
checks (`why`, `acceptance`, `estimate`, `scope-fence`, `issue-link`,
`linked-issue-completeness`, `linked-issue-epic`; see `dor_checks.py`) and
rolls them into a `TpmEvaluation`. The GitHub check-run POST lives in
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
