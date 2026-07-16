# Reviewer pattern harvest (org-wide)

Internal notes from sampling recent PR review surfaces across githumps
repos. Product codenames for what we build next stay pure Grug (Elder,
Markings Board, Lore, Omen, Swift Hunt, Living Hunt). This file does not
name third-party products in product copy - only as data sources for the
harvest.

## Signal frequency (sample window)

| Pattern | What strong reviewers do | Grug today | Apex / next |
|---|---|---|---|
| Severity + category tags | Every inline finding labeled (correctness / security / maintainability) | severity + rule_name | Keep; add category chip on Markings |
| Effort chips | quick-win vs heavy-lift on findings and docs | effort vocabulary exists | Surfaced in Markings Board table |
| File:line anchors | Always pin to path + line | yes | yes |
| Committable suggestions | One-click fix when single-line safe | yes (#553) | Keep; never multi-line corrupt |
| AI agent prompt block | Copy-paste repair brief per finding + consolidated | yes | yes |
| HTML details / collapsible | Analysis chain, prompts, long evidence hidden | agent prompts in details | Match for long Lore |
| Walkthrough + sequence diagram | PR open: what changed, mermaid, effort | Teller | Keep default-on |
| Analysis / verification chain | Show how the finding was checked | judge + cave provenance | Log backend/model on check summary |
| Confidence / false-positive | Measured precision, not vibes | Lore ledger + judge | Precedent chip already |
| Cross-file / callers | Findings that need symbols outside the diff | 1-hop cross_file | Deepen Living Hunt |
| Commit-range / delta review | Re-review only files since last pass | full re-review each time | #557 Living Hunt |
| Dedup prior findings | Do not re-spam fixed lines | dedup markers | Living Markings #556 |
| Fingerprint / stable id | Comment identity across pushes | grug-rule marker | Keep |
| Checklist / test plan | Actionable verify steps | Chief DoR | Keep |
| Runtime / production link | Tie code risk to live signal | Omen | Expand service map |
| Autofix checkbox UX | One-click "push a fix commit" | agent prompt only | Optional later |

## Harvest by reviewer class (behavior, not branding)

### Dense inline analyst
- Severity | effort | category header on every thread
- Analysis chain in collapsible (scripts / evidence)
- Autofix prompt for agent tools
- Fingerprint HTML comments for dedup
- Walkthrough issue-comment + change stack

**Take for Elder:** Markings Board already mirrors severity/effort/fix/agent
prompt. Next: optional category dimension; never reopen a completed check on
deduped enqueue (fixed this PR).

### Ticket-aware summary bot
- Issue-comment PR summary with effort estimate and mermaid
- Alternative-approach tradeoff table
- Cross-file and confidence language
- Links related tickets

**Take for Teller + Chief:** Teller already does walkthrough + mermaid +
effort. Chief ticket compliance should keep linking closes #N. Elder Lore
cites prior PR numbers - keep expanding ledger ingest.

### Workflow / secret surface hunter
- High-severity "missing environment / secret not loaded" findings
- Concrete suggested YAML patches
- Short AI agent remediation block

**Take for Guard + Warder:** Keep secret/IaC/SAST on Guard. Warder should
own release/workflow shape. Do not fold into Elder Markings.

## What we shipped in Apex from this harvest

1. Pending required check on enqueue (visibility like "review started")
2. Do not reopen completed/pending checks on FIFO-deduped re-send
3. Structured Markings table + What/Where/Fix/Lore
4. Table/code-span sanitization for model-controlled fields
5. Swift Hunt adaptive settle (small PRs feel instant)
6. Stable snapshot ignoring HTML footers (less thrash)

## Next slices (tracked)

- Living Hunt: commit-range scoped re-review (#557)
- Living Markings: edit findings Resolved/Dismissed/Addressed (#556)
- Call of the Elder: @grug interactive (#617)
- Stronger Omen fusion when service map present (#346 pillar 2)
