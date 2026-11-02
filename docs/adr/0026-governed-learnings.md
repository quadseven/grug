# ADR-0026 - Governed learnings: who can quiet Elder, for how long, and on what

## Status

Accepted (2026-11-01). Implements #1571. Builds on ADR-0020 (reply-mined
learnings) and ADR-0024 (Cave Drawings).

## Context

Grug's authors are mostly agents, and Elder learns from what happens to its
findings. An ungoverned loop is a training set the reviewed can write to.

Two paths could silence a finding before this ADR:

- A thumbs-down wrote a false-positive ledger row, and `derive_practices`
  turned it into an `AVOID FALSE POSITIVE` prompt rule. Any write-level
  reactor could do it, including an App, with no reason given. The rule had no
  expiry and no security exemption.
- A reply-mined learning told Elder not to re-flag something. It reached the
  review prompt and the judge prompt. The judge decides whether a SAST candidate
  publishes, so a learning could silence a SAST finding.

## Decision

1. One store. The Cave Drawings rows are the only source of rules that quiet
   Elder. They are versioned (`version`, `history`, `supersedes`) and export to
   a diffable document, which is the "repo file or equivalent". Practices and
   few-shot examples only ask Elder to find more, so they stay outside it.
2. No suppression from a reaction. `derive_practices` derives from accepted
   rows only, and cached `avoid` rows are dropped on read. A false-positive
   label stays as precision telemetry.
3. Human with a reason. A learning needs a reply of at least four words from a
   write collaborator. Reactions never teach, and even as telemetry they ignore
   Apps and anyone who wrote the PR; an unknown author or an unreadable commit
   list trusts nobody. App actors are refused by `type` and by the `[bot]`
   suffix, because an App can hold write access. The PR author and every commit
   author or committer on the PR are refused whatever their role, because an
   agent on a person's account looks human. If the commits cannot be read, the
   reply is not learned from.
4. Expiry. A rule stops firing 90 days after its last human affirmation:
   taught, re-taught, approved or edited. `effective_status` derives `expired`
   at read time, so no job has to run. The 180-day row TTL stays, so an expired
   rule remains visible and re-affirmable before it is purged. Approving an
   active rule re-affirms it. A row with no usable date reads as expired.
5. Security and SAST. A rule taught on a security or SAST finding, or whose
   text names that territory, is refused at teach time and dropped again in
   `select_for_prompt`, which covers older rows, edits and imports. No learning
   rides a judge batch that holds such a finding. The review prompt says
   learnings never reduce security or SAST reporting. The class list
   (`SAST_CLASSES`) is pinned to the shipped rules by a test. A protected
   class is never muted, by class (`muted_classes`, including any class with a
   security-category row) or per finding at publication (`finding_is_muted`).
6. Audit. `record_learning_usage` records the rule ids per review, and
   `GET /api/v1/admin/learnings/fired` returns them. The list view gives origin,
   reason, expiry and fire counts.

## Consequences

- A person cannot teach from a PR they wrote or committed to. Elder's false
  positives on a person's own PR are taught by a second maintainer, or not at
  all.
- The vocabulary match is broad on purpose. A false match loses one preference;
  a miss could silence a security finding.
- Rows older than 90 days at deploy stop firing until a person re-affirms them.
  That is the rule applied to existing data, not a migration fault.
- An agent running under a person's account, replying on a PR it did not touch,
  still reads as that person. GitHub exposes nothing that separates them, so
  this rests on agents holding their own App identity.
- `muted_classes` (#1436) still demotes a class by measured precision, which
  reactions feed. It stays: it is a measured demotion to the board, needs 20
  labels, and now excludes protected classes, Apps and the PR's writers.
- A thumbs-down from a second maintainer still dismisses that one finding on
  that one PR. That is a person's decision about a finding, not a learned rule.
- Thread resolves are not governed here: any resolver hides a finding from
  re-review on that PR. That sits in the review pipeline, tracked separately.
- The main review prompt's protection for a free-text rule rests on the filter
  and the prompt sentence. The hard guarantees are the SAST and judge paths.
