# Repo config: `.grug.yaml`

An optional `.grug.yaml` at the repository root tunes Elder reviews. It lives next to the code, so changes to it go through review like any other change.

## Where it is read from

Elder reads the file from the pull request's **base** commit, never from the PR head. A PR that edits `.grug.yaml` is reviewed under the existing file, so a PR cannot loosen its own review. The edited file takes effect once it is merged.

A missing or empty file changes nothing.

## Keys

All keys are optional.

| Key | Type | Effect |
| --- | --- | --- |
| `ignore` | list of path globs | Matching files are never reviewed and never reach the model. The summary names them. |
| `path_instructions` | list of `{path, instructions}` | `instructions` is added to the review prompt only when the diff touches a file matching `path`. |
| `min_inline_severity` | `low`, `medium`, `high`, or `critical` | Findings below this level appear in the check summary but get no inline comment. |
| `skip_prs` | `{authors, labels, title_patterns}`, each a list of strings | A matching pull request is not reviewed. See below. |
| `security_review_paths` | list of path globs | Replaces the default sensitive-path list of the code-only security pass. An empty list turns the pass off for this repo. See below. |
| `elder_blocking_cap` | integer, default `5` | Blocking model findings per PR that get an inline comment; `0` means no cap. The rest appear ranked on the board and still fail the check. See below. |
| `elder_drip_feed` | boolean, default `true` | On a second or later round, hold a new model finding below critical on an unchanged line. See below. |
| `elder_mute_min_labeled` | integer, default `20` | Labeled ledger rows a finding class needs before it can be muted. |
| `elder_mute_below_precision` | number 0 to 1, default `0.2` | A class with at least `elder_mute_min_labeled` labeled rows and a lower fixed-rate is muted: no inline comment, only the board and check summary, marked "muted: fixed in N of M past findings". A high or critical finding still posts inline and blocks. |
| `elder_mute_locked_classes` | list of class names | These classes keep their inline comments whatever their precision. |

### Skipping a whole pull request

`skip_prs` opts a pull request out of the Elder review and the Teller walkthrough, for example a dependency-bump app's pull requests or ones carrying a label that marks them as generated:

```yaml
skip_prs:
  authors: ["dependabot[bot]"]
  labels: ["generated"]
  title_patterns: ["chore(deps)*"]
```

- `authors` and `labels` match exactly, ignoring case.
- A title pattern matches the whole title, ignoring case. Only `*` (any text) and `?` (one character) are special, so `[deps] *` matches a literal `[deps]` prefix.
- A matching pull request gets a `success` Elder check whose summary reads `skipped by .grug.yaml skip_prs (<which rule>)`, and no review, no inline comment, no board comment and no walkthrough.
- Chief and Guard never read `skip_prs`: the definition-of-ready and secret checks run on every pull request.
- Like every key, it is read from the base commit, so a pull request that adds itself to `skip_prs` is still reviewed.

### Glob rules

- `*` matches within one path segment; `**` crosses segments; `?` matches one character.
- A pattern with no `/` (for example `*.lock`) matches the file name at any depth.
- Patterns match the full repo-relative path (`vendor/**`, `docs/*.md`).

### Limits

At most 20 `path_instructions` entries are used, each truncated to 2000 characters.

### Precedence

Elder also reads the repo's agent-guideline files (`CLAUDE.md`, `AGENTS.md`, `.cursorrules`, `.github/copilot-instructions.md`, `.windsurfrules`) and rules the team has taught it. When they conflict, the order, highest first, is:

1. `path_instructions`, for the files they name.
2. The agent-guideline files.
3. Taught rules (learnings).

Path instructions come first because they name specific files. `.grug.yaml` and the guideline files are all read from the base commit, so a PR cannot use its own `CLAUDE.md` to steer its own review; a change to one takes effect once it merges. Both prompt blocks state this order to the model.

## Code-only security pass

When `GRUG_QUARANTINED_SECURITY_PASS` is set on the deployment, a pull request touching a sensitive path gets a second security review of those paths. That pass, its judge, and its refute gate read the diff and code context only: no title, body, linked issue, repo docs, or agent guideline files. Its findings merge with the normal review by file, line, and rule at the stricter severity, and it can never remove a finding. The default list is auth, crypto, payment, secret, and credential paths plus `.github/workflows/`, `.github/actions/`, and `.grug.yaml`.

## Comment budget

The budget applies only when `GRUG_COMMENT_BUDGET` is set on the deployment. Deterministic scanner findings are never capped or dropped. A held finding has no inline comment, is listed on the board by severity and judge confidence, and still counts toward the check. A non-blocking finding the judge called not real, or graded below 0.7, is held too. When a blocking finding is held for the cap, the next push reviews that ground again.

## Example

```yaml
ignore:
  - vendor/**
  - "*.lock"
  - "**/generated/**"

path_instructions:
  - path: "k8s/**"
    instructions: Check every container sets resource requests and limits.
  - path: "migrations/**"
    instructions: Flag any migration that is not safe to run while the old code is live.

min_inline_severity: medium
```

## Mistakes

A malformed file, an unknown key, or a bad value never stops the review. Elder ignores the bad part, applies the rest, and adds one advisory paragraph to the check summary that begins with `.grug.yaml problem`.

## Not covered

Dashboard settings stay in the dashboard. Per-path model or persona selection is not supported.
