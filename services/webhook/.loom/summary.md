run_command now logs a failed slash command (verb and exception kind) before flipping the reaction to thumbs-down and re-raising, so the fault is no longer silent.

## Acceptance criteria
- [x] Teller walkthrough or /grug ask shows a placeholder, then updates in place
- [ ] Slash-command trigger gets an eyes reaction, flipping to thumbs-up or thumbs-down on completion
- [x] Regression test: placeholder posts before the LLM call and the same comment ID is PATCHed
- [x] Re-running updates the same comment and keeps a collapsed record of previous findings with resolved items marked
- [x] A failure or degrade path resolves to a real message, verified by a regression test

## Operator steps before merge
none
