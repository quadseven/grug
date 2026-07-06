"""Elder replay eval harness (#361 slice 2, #537).

Replays Elder over the PRs recorded in the review-findings ledger corpus
(#361 slice 1) and scores CATCH-RATE vs NOISE per finding class - the
measured answer to "what do rival reviewers catch that Elder misses".

Webhook-only tool (NOT mirrored, NOT on the request path), sibling of
`sast_benchmark/` and reusing its backend transport. The pure corpus +
scoring core runs in CI with no LLM; the live runner records the baseline
on demand. `baseline.json` carries `prompt_sha` - the per-PR CI suite
fails when `code_review_prompt.py` changes without a re-recorded
baseline (the #537 CI gate on prompt changes).
"""
