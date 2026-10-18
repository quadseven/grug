"""#529: Chief ticket-compliance heuristic - conservative, few false positives."""

from __future__ import annotations

from personas.tpm.ticket_compliance import (
    acceptance_criteria,
    advisory_markdown,
    closes_refs,
    diff_signals,
    unaddressed_criteria,
)


def test_closes_refs_only_closing_verbs():
    body = "closes #12\nrefs #34\nPart of #56\nfixes #78\nblocked by #90"
    assert closes_refs(body) == [12, 78]


def test_closes_refs_dedup_order():
    assert closes_refs("fixes #5 and closes #5 then resolves #9") == [5, 9]


def test_acceptance_criteria_extracts_boxes():
    body = """## Why
words
## Acceptance criteria
- [ ] add the emit_gauge helper
- [x] wire the poller pass
- not a box
* [ ] star-bullet box too
"""
    # only UNCHECKED boxes are candidates; the checked "wire the poller
    # pass" is excluded (author asserts it done).
    assert acceptance_criteria(body) == [
        "add the emit_gauge helper", "star-bullet box too",
    ]


def test_addressed_when_tokens_overlap():
    criteria = ["add the emit_gauge helper to observability"]
    signals = diff_signals(["services/_shared/observability.py"], "add emit_gauge")
    assert unaddressed_criteria(criteria, signals) == []


def test_unaddressed_when_no_overlap():
    criteria = ["DD monitor for queue-age routed to Discord"]
    signals = diff_signals(["services/webhook/poller_handler.py"], "poller pass")
    # 'monitor', 'queue', 'age', 'discord' don't appear -> flagged
    assert unaddressed_criteria(criteria, signals) == criteria


def test_criterion_with_only_stopwords_never_flagged():
    criteria = ["it should be done"]  # all stopwords/noise
    assert unaddressed_criteria(criteria, signals=set()) == []


def test_camelcase_and_path_tokenization():
    # a criterion naming a symbol matches a file that defines it
    criteria = ["preview_mode gate lives in _shared"]
    signals = diff_signals(["services/_shared/preview_mode.py"])
    assert unaddressed_criteria(criteria, signals) == []


def test_advisory_none_when_all_addressed():
    assert advisory_markdown(42, []) is None


def test_advisory_lists_unaddressed_with_marker():
    md = advisory_markdown(42, ["thing one", "thing two"])
    assert md is not None
    assert "grug-chief:ticket-compliance" in md
    assert "#42" in md and "thing one" in md and "thing two" in md
    assert "Advisory only" in md
    assert "- [ ] thing one\n- [ ] thing two" in md


def test_multi_criteria_mixed():
    criteria = [
        "emit grug.sqs.messages_visible per queue",   # addressed
        "add a nist ghsa merged feed",                # not addressed
    ]
    signals = diff_signals(
        ["services/webhook/consumer.py"], "emit grug.sqs.messages_visible per queue via dogstatsd",
    )
    assert unaddressed_criteria(criteria, signals) == ["add a nist ghsa merged feed"]


def test_checked_box_not_flagged():
    """#535 LORE: a CHECKED box is the author asserting done - never a
    candidate for 'unaddressed', so it can't false-positive."""
    body = "## Acceptance criteria\n- [x] add the nist ghsa merged feed\n- [ ] emit dogstatsd gauge\n"
    assert acceptance_criteria(body) == ["emit dogstatsd gauge"]


def test_regression_grug730_retry_criterion_flagged():
    """Live audit finding (2026-07-24): grug#730's 6 acceptance criteria
    against the real PR #734 diff. #734 genuinely shipped capture + stack
    refresh (criteria 1-3, 6) and deliberately deferred idempotent retry +
    failure-recoverability to a follow-up issue (#738) - but Chief never
    posted an advisory despite the gap, for two compounding reasons:

    1. A stopword gap ("but"/"after" weren't in _STOP) let pure connector
       words count as "overlap" - fixed by completing the stopword list.
    2. A single shared content word was enough to call a criterion
       "addressed" - fixed by requiring >=2 overlapping tokens (see
       _MIN_MATCH_TOKENS).

    Both fixes together now correctly flag criterion 4 (retry/idempotency
    - its only real overlap was the single generic word "findings", which
    every criterion in this issue happens to share). Criterion 5 (failure
    recovery) is a KNOWN RESIDUAL GAP this heuristic still misses: the
    real PR body's unrelated "capture failure" sentence coincidentally
    supplies two of criterion 5's words ("failure", "publication") for a
    reason that has nothing to do with the criterion. A deterministic
    bag-of-words heuristic can't reliably tell "these words co-occur
    because the behavior was built" from "these words co-occur because
    the feature area shares vocabulary" - that's exactly why this advisory
    is a nudge, never a gate, and why the real backstop for this class of
    miss is Elder's own structured, severity-tagged findings (see the
    Living-Hunt cumulative-verdict fix), not smarter prose-similarity."""
    criteria = [
        "A successful asynchronous deep finding is stored with its GitHub "
        "comment ID and stable finding identity.",
        "The PR stack/walkthrough projection is refreshed after deep "
        "findings publish.",
        "Reactions and trusted replies on a deep finding enter the same "
        "verdict and learning pipelines as synchronous findings.",
        "Retries are idempotent and cannot create duplicate stored "
        "findings or duplicate comments.",
        "Failure after model completion but before publication is "
        "observable and recoverable without losing the candidate finding.",
        "Regression tests cover publish, capture, retry, reaction, reply, "
        "and stack-refresh behavior.",
    ]
    signals = diff_signals(
        [
            "infra/scripts/attest_code_reviewer_dispatch.py",
            "services/_shared/personas/code_reviewer/dispatch.py",
            "services/webhook/tests/test_code_reviewer_dispatch.py",
        ],
        "feat(elder): capture async deep findings in durable review "
        "lifecycle (#730)\n\n"
        "Routes the async deep-review publication through the same "
        "durable finding-capture contract as the synchronous path: "
        "captures inline-comment IDs and refreshes the PR-timeline stack "
        "comment. Best-effort: capture/stack failures are swallowed and "
        "never change the deep review outcome. 3 regression tests: deep "
        "finding capture with span context, capture failure does not "
        "change result, stack refresh after deep publish. Out of scope: "
        "full decomposition of dispatch_code_review, deep review evals "
        "submission, Tier-1 synchronous capture path unchanged.",
    )
    unaddressed = unaddressed_criteria(criteria, signals)
    assert unaddressed == [criteria[3]]
