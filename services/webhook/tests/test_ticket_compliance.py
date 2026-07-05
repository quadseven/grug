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
    """#535 Qodo: a CHECKED box is the author asserting done - never a
    candidate for 'unaddressed', so it can't false-positive."""
    body = "## Acceptance criteria\n- [x] add the nist ghsa merged feed\n- [ ] emit dogstatsd gauge\n"
    assert acceptance_criteria(body) == ["emit dogstatsd gauge"]
