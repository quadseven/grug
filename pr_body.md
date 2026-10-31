Refs quadseven/grug#1467

## Why
When the local inference gateway encounters an outage (connection refused, 503 spark_gateway_queue_full, 504 spark_gateway_queue_wait_exceeded, or a read timeout), Elder should retry the review once on a configured backend that does not route through the gateway, rather than returning a generic all_failed or http_503 error. If no such backend is configured, it should conclude with an explicit failure naming the gateway outage, ensuring it never just hangs or fails obscurely.

## Summary
Updated _is_gateway_outage_error to accurately match error signatures of the local inference gateway outages.
Added logic to _review_diff_dispatch_cave_primary to intercept gateway outages and properly delegate to a fallback backend exactly once if configured, bypassing backends that route through the local gateway (such as the free-pool).
Changed the final return logic in _review_diff_dispatch_cave_primary to emit an explicit failure naming the gateway outage if all fallbacks fail or none are configured.
Modified _ladder_failure_reason and _call_llm_attempt to capture and preserve gateway outage errors natively so they don't get swallowed by normal HTTP exception handling.
Added tests in test_gateway_outage_fallback.py to verify fallback logic.

## Acceptance
- fallback completes and posts a conclusion
- no fallback configured concludes within the absolute deadline with a summary naming the outage
- one arm succeeding makes no fallback call

## Test plan
- Add tests to ensure fallback triggers successfully upon a gateway outage
- Add tests to ensure explicitly failing with an outage message when no fallback is configured
- Add tests to verify no fallback calls are made when an arm succeeds

## Size
Size: M

## Out of scope
No changes were made to the gateway, its queue bounds, persona prompts, or which conclusion counts as passing.
