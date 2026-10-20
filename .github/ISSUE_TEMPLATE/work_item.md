---
name: Work item
about: Standard work item - bug, idea, tech-debt. Must pass Grug DoR.
title: 'feat(grug): '
labels: ''
assignees: ''
---

<!--
Grug Definition of Ready (5 checks). Fill every section before this
ticket leaves Triage. The PR that closes it must mirror the same shape.
-->

## Why

<!--
One paragraph. Why this work exists. What problem it solves, who feels
it, and what changes once it ships. >=1 sentence.
-->

## Acceptance criteria

<!--
Observable behavior at completion. >=3 bullets. Each bullet is a check
the reviewer can verify without reading code.
-->

- [ ] 
- [ ] 
- [ ] 

## Size

<!--
Effort estimate. XS = <=30min, S = <=2h, M = <=1d, L = <=3d.
XL items must be split into >=2 sub-issues before queueing.
-->

**Size:** S

## Dependencies

<!--
Blocking issues / sibling repos / external prereqs. Use `Blocked by #N`,
`Refs #N` or `Part of #N` so Grug's issue-link check passes and the
graph is navigable. Never a closing keyword: the PR that mirrors this
ticket links it with `Refs #N`, and the ticket is closed by hand once
its acceptance boxes are ticked from evidence. A closing keyword closes
it at merge, and Chief's blocking completeness check fails a PR that
closes a ticket with an open box.

EPIC LINK REQUIRED, no exceptions. Every work item belongs to an epic.
Write `Part of #N` naming a live (open) epic, AND set the native
sub-issue parent after filing (the epic's "Add sub-issue" button). The
text is what a reader sees; the sub-issue link is what tooling counts.

`Part of` beats `Refs` - `Refs` means "related" and is not parentage.
There is no orphan-ok escape hatch; if nothing fits, the epic set is
wrong and needs a new umbrella.

List the live epics rather than trusting a copy here (a hard-coded list
went stale and sent work to a closed epic):
  gh issue list --repo quadseven/grug --state open --search "Epic in:title"
-->

- 

## Out of scope

<!--
Adjacent work that intentionally won't be done in this item. Prevents
scope creep + tells the reviewer what to NOT review for.
-->

- 
