Not valid against this code: the check-run fetch already has a timeout, so the
delivery path cannot hang on it.

`interactive._elder_review_state` calls `review_label.elder_check_state`,
which calls `github_checks_client.list_check_runs_for_ref`, whose `httpx.get`
passes `timeout=10` (services/_shared/github_checks_client.py:177). The other
GitHub calls in review_label.py (the label listing and the PR head fetch)
pass `timeout=_TIMEOUT` (10.0). A hung request therefore fails, and the state
reads as "unknown" - which the command path treats as "do not queue a second
review", not as "idle".

The webhook path also has no unbounded call in front of it: `token_fn` is
`with_install_token_retry`, which refreshes a stale token once and retries a
5xx with bounded backoff and jitter.
