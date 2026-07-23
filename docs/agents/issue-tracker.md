# Issue tracker: GitHub

Issues and PRDs for this repo live as GitHub issues at `quadseven/grug`. Use the `gh` CLI for all operations.

## Conventions

- **Title prefix:** `feat(grug): ` — the enforced default from `.github/ISSUE_TEMPLATE/work_item.md`. Older issues titled `[grug] ...` predate that template; new issues should use `feat(grug): `.
- **Body shape (from the work-item template, mirrors Grug's own DoR checker `Grug - Chief`):**
  - `## Why` — one paragraph, why this work exists.
  - `## Acceptance criteria` — >=3 checkbox bullets, each independently verifiable.
  - `## Size` — `**Size:** XS|S|M|L|XL` (`XS` <=30min, `S` <=2h, `M` <=1d, `L` <=3d; split `XL` into sub-issues first).
  - `## Dependencies` — `Blocked by #N`, `Refs #N`, or `closes #N` (plain text, not markdown-linked) so Grug's issue-link check passes.
  - `## Out of scope` — adjacent work intentionally not covered.
  - The implementing PR mirrors this shape and must carry a plain-text `closes #<n>` for Grug's `Grug - Chief` DoR gate to pass.
- **Labels (full live set, verify with `gh label list --repo quadseven/grug` before relying on this list — it drifts):**
  - State-role: `needs-triage`, `ready-for-agent`, `wontfix`. The canonical five-role vocabulary's other two roles (`needs-info`, `ready-for-human`) are not yet defined as labels in this repo.
  - Category/other: `arch-review`, `bug`, `dependencies`, `documentation`, `duplicate`, `enhancement`, `epic-arch-hygiene`, `epic-deploy-reliability`, `epic-enforcement`, `epic-grug-saas`, `epic-resiliency`, `epic-security`, `feature`, `gh-app-best-practice`, `good first issue`, `grug-pulse`, `help wanted`, `invalid`, `javascript`, `likely-resolved`, `nightly-bot`, `prd`, `preview`, `question`, `stale`.
- **Create an issue**: `gh issue create --repo quadseven/grug --title "feat(grug): ..." --body "..."`. Use a heredoc for multi-line bodies; follow the template's five-section shape above.
- **Read an issue**: `gh issue view <number> --repo quadseven/grug --comments`.
- **List issues**: `gh issue list --repo quadseven/grug --state open --json number,title,body,labels,comments`.
- **Comment on an issue**: `gh issue comment <number> --repo quadseven/grug --body "..."`
- **Apply / remove labels**: `gh issue edit <number> --repo quadseven/grug --add-label "..."` / `--remove-label "..."`
- **Close**: `gh issue close <number> --repo quadseven/grug --comment "..."`

Infer the repo from `git remote -v` when run inside a clone — `gh` does this automatically and the `--repo` flag above is only needed from outside one.

## Pull requests as a triage surface

**PRs as a request surface: no.** Set to `yes` if this repo should treat external PRs as feature requests; `/triage` reads this flag. Flip it here if that changes.

## When a skill says "publish to the issue tracker"

Create a GitHub issue: `gh issue create --repo quadseven/grug`.

## When a skill says "fetch the relevant ticket"

Run `gh issue view <number> --repo quadseven/grug --comments`.
