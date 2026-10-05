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

### Glob rules

- `*` matches within one path segment; `**` crosses segments; `?` matches one character.
- A pattern with no `/` (for example `*.lock`) matches the file name at any depth.
- Patterns match the full repo-relative path (`vendor/**`, `docs/*.md`).

### Limits

At most 20 `path_instructions` entries are used, each truncated to 2000 characters.

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
