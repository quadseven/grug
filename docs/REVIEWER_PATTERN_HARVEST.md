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

## Markings v2 (review-stack shell)

Shipped after the dense-inline-analyst harvest comparison:

1. Category chip on every inline marking (RULES `bug_class`)
2. **Why it matters** impact one-liner (taxonomy map, no extra LLM)
3. CR-style agent meta preamble (verify / skip-if-fixed / minimal / validate)
4. PR-timeline **Elder review stack** issue comment (upsert by marker):
   actionable count, severity breakdown, markings table, consolidated agent prompt
5. Autofix push still out of scope (comment-only + suggestion + agent prompt)

## Caveman chrome chips (CR density, tribe glyphs)

Closed alphabet for Markings / Guard scan surfaces (identifiers stay ASCII):

| Signal | Chip |
|---|---|
| critical | skull critical |
| high | fire high |
| medium | orange medium |
| low | eye low |
| quick-win | bolt quick win |
| heavy-lift | rock heavy lift |

Header shape on inline markings (CR-dense):
`{sev chip} | _{category}_ | \`rule\` | {effort chip}`

## Smarter Elder: docs/code claim floor

Qodo/CodeRabbit caught settle-comment and exclusive-bound drift that pure
LLM review missed on speed PRs (#664 class). Shipped:

1. Deterministic `claim_check.scan_claim_checks` - compares ADDED comment/doc
   claims (Steady medium settle cap, deep-diff exclusive vs inclusive) to
   implementation facts at head; advisory MEDIUM, no judge.
2. Dispatch enrich: when the diff mentions settle/deep policy, also fetch
   known policy sources (`snapshot.py`, `llm_client.py`) if missing from
   changed paths so comment-only PRs still get a floor.
3. LLM rule `doc-code-claim-drift` for the general numeric claim class.

## Next slices (tracked)

- Living Hunt: commit-range scoped re-review (#557)
- Living Markings: edit findings Resolved/Dismissed/Addressed (#556)
- Call of the Elder: @grug interactive (#617)
- Stronger Omen fusion when service map present (#346 pillar 2)


## Harvest addendum (PR 643 bot rounds)

- Treat any `status=completed` check as terminal when deciding whether to
  post pending (not only success/failure allowlists).
- Defuse both backtick and tilde fences in finding prose.
- When putting model text inside HTML `<details>`, escape `<`/`>`/`&`.
- Prefer collapsible detail blocks over always-open walls of table/mermaid.
