# ADR-0020 - Reply-mined learnings (slice 1: store and apply)

## Status

Proposed (2026-07-17). Implements slice 1 of issue #670. Part of epic #522
(LORE-class review intelligence). Composes with the reaction loop (#539) and
the review-findings ledger (#361).

## Context

A maintainer can reply to one of grug's inline findings with a preference, for
example "we prefer early returns here because our monitoring tracks error
codes." Today grug does nothing with that reply. GitHub sends it as a
`pull_request_review_comment` event, and grug has no handler for that event, so
it falls through to a no-op.

The competing review bot's one differentiating loop is that a reply durably
changes future reviews, with a visible acknowledgment that builds trust. Grug's
reaction loop (#539) already turns a thumbs reaction into a stored verdict and a
refreshed prompt cache, but there is no path from a natural-language correction
to changed future behavior.

Two corpora already feed the review prompt and stay distinct:

- practices (#527) and few-shot exemplars (#538) are outcome-taught: derived
  from the ledger of accepted or rejected findings.
- learnings (this ADR) are operator-taught: a maintainer states a preference in
  their own words, and grug applies it verbatim.

Keeping them separate matters. A practice is a statistical pattern grug
inferred; a learning is an instruction a person gave. Blurring them would let a
single inferred pattern read as a stated rule, and would make the acknowledgment
untraceable.

## Decision

Add the inbound seam and the store-and-apply loop. Defer the management commands
and org-wide scope to later slices.

### What slice 1 does

1. Receive the reply. Add a `pull_request_review_comment` case to the webhook
   dispatcher. A new handler gates on trust (the reply author is the PR author
   or a write-or-above collaborator, the same gate the reaction loop uses),
   joins the reply back to grug's finding through `comment.in_reply_to_id`, and
   enqueues a job. It never runs the language model inline, so the webhook still
   acknowledges within GitHub's timeout.

2. Classify the reply. The consumer runs a bounded, JSON-constrained model call
   that decides whether the reply is a durable team preference or a one-off, and
   if durable restates it as a short self-instructive rule. The classifier errs
   toward one-off: when unsure, it does not store.

3. Store a durable learning. Durable learnings go to a repo-scoped partition in
   the existing key-value store, keyed by content digest so a re-delivered reply
   heals in place rather than duplicating. Each row carries the learning text,
   the repo, an optional file-path glob, the source pull request and comment, the
   author, the created-at timestamp, and usage counters seeded at zero.

4. Acknowledge in the thread. The consumer posts a threaded reply under the
   finding. A stored learning gets a "Markings remembered" collapsible section
   that quotes the exact rule grug will apply. A one-off gets an explicit note
   that grug read the reply but did not store a durable rule.

5. Apply on the next review. A new best-effort block loads the repo's learnings
   and appends them to the review system prompt, alongside the existing practices
   and exemplars blocks. The same block feeds the `/grug ask` prompt.

### Why the language-model call runs in the consumer

The webhook awaits `dispatch` before returning 200, and GitHub times out a
delivery after about 10 seconds. A model call can take several seconds. The
established pattern for `/grug ask` (#528) enqueues the heavy call to the SQS
consumer and acknowledges fast. The learnings classifier follows the same path:
a new job kind on the existing queue, deduplicated on the reply comment id.

### Why learnings are a separate prompt block

Folding learnings into the practices corpus would ride the existing injection
path with no new code, but it would erase the operator-taught versus
outcome-taught distinction. A separate block with its own header keeps the two
sources legible to the reviewer and to the model, and keeps the acknowledgment
honest: grug can quote the exact stored learning.

### Trust and safety

- Only the PR author or a write-or-above collaborator can teach a learning. A
  random commenter on a public-listed app cannot poison the corpus.
- The learning text is untrusted repository data. It is redacted for
  secret-shaped values before it reaches a third-party backend, the same guard
  the practices and exemplars blocks use, and it is neutralized against prompt
  injection with the existing sanitizer.
- The learnings block is bounded in size, so a flood of learnings cannot crowd
  out the static rules.

## Scope

In slice 1:

- the `pull_request_review_comment` inbound handler and trust gate
- the classifier and its job on the consumer
- the repo-scoped learnings store
- the threaded acknowledgment
- the review-prompt and `/grug ask` injection

Deferred to later slices, tracked on #670:

- listing and deleting learnings with `/grug` commands
- an approval delay before a new learning becomes active (org config)
- org-wide scope (slice 1 is repo-scoped only)
- incrementing the usage counters when a learning is applied (the fields exist
  from slice 1; the increment is a later slice)
- a dashboard view, CSV export, and similarity search (out of scope on #670)

## Consequences

- Grug gains the memory loop that the competing bot's users value most: a reply
  changes future reviews, visibly.
- The learnings corpus is a new operator-facing surface with no management UI
  yet. Until the `/grug` commands land, a wrong learning can only be corrected
  by editing the store directly. The classifier's bias toward not storing limits
  the blast radius.
- The review prompt grows by a bounded block on repos that have learnings.
