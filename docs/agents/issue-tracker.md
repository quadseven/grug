# Issue tracker: GitHub

Issues and PRDs for this repo live as GitHub issues at `quadseven/grug`. Use the `gh` CLI for all operations.

## Conventions

- **Title prefix:** `[grug]` — kept for search consistency.
- **Body shape:** Why / What (or Slices for an epic) / Acceptance criteria — matches Grug's own DoR checker (`Grug - Chief`), which also requires a `Size: XS/S/M/L/XL` line and a plain-text `closes #<n>` (not markdown-linked) on the implementing PR.
- **Labels:** state-role labels currently live in this repo are `needs-triage`, `ready-for-agent`, and `wontfix` — the canonical five-role vocabulary (`needs-info`, `ready-for-human` included) has not been fully adopted here yet. Category labels include `bug`, `enhancement`, `epic-*` (per-initiative, e.g. `epic-security`, `epic-resiliency`), `prd`, `documentation`, and others — see `gh label list --repo quadseven/grug` for the live set.
- **Create an issue**: `gh issue create --repo quadseven/grug --title "[grug] ..." --body "..."`. Use a heredoc for multi-line bodies.
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
