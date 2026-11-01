# Reviewer attack surface

Grug reviews code written by people it does not trust, so its own tooling is
part of what an attacker can reach. This page lists every place Grug executes
or obeys content a pull request controls. It is the result of the #1574 audit
(2026-11-01) and describes classes of issue, not exploits.

`test_reviewer_hardening.py` holds the analyzer and base-commit rows below.

## Rule

Nothing a PR controls may steer the reviewer. Text that steers a review comes
from the PR's base commit. Text from the head reaches the model as labeled
data. No repository code runs in a pod that holds a credential.

## Execution

| Surface | PR-controlled input | Read from | Boundary | State |
| --- | --- | --- | --- | --- |
| ruff (`lint.py`) | Changed `.py` file text | Head | Scrubbed environment, read-only copy, `--isolated`, no config on disk | Convention only, #1576 |
| opengrep analyzer (`analyzers.py`) | Changed JS and TS file text | Head | Scrubbed environment, read-only copy, Grug's rules | Convention only, #1576 |
| opengrep scan (`sast.py`) | All changed file text | Head | Same as above since #1574; a PR's own ignore files are dropped | Convention only, #1576 |
| Smasher Trial (`smasher/`) | The repo's code and tests | Head | Separate Job, no credential, read-only checkout; deny-all egress needs a policy-enforcing CNI (ADR-0013) | Sandboxed; off by default |
| Trial dependency install | Requirements files | Head | Wheels only and no build backend, but the prep pod's egress can be steered by the file, #1580 | Partly sandboxed; off by default |
| Trial tarball unpack | Archive member names | Head | Members outside the destination and links are skipped | Handled in code |
| Grug's own CI | This repo's PRs | Head | No `pull_request_target`; previews run for same-repo PRs only | Handled in config |

"Convention only" means the child process shares the pod's user id, network and
filesystem view. Scrubbing keeps secrets out of its environment, and no analyzer
is asked to reach the network, but a flaw in the engine's own parser would not
meet an operating-system boundary.

## Instructions

| Source | Reaches | Read from | State |
| --- | --- | --- | --- |
| `.grug.yaml` | Skip rules, ignores, path instructions | Base commit | Base only |
| Ruff policy, `.semgrepignore` | Which findings survive | Base commit | Base only |
| Guideline files (`CLAUDE.md`, `AGENTS.md` and the like) | System prompt | Base commit since #1574 | Base only |
| `CONTEXT.md`, `docs/adr/` | Review context, as the repo's source of truth | Base commit since #1574 | Base only |
| Reply-mined learnings | System prompt | Stored, write access required | Gated by the author's access |
| `/grug` commands | Reruns, `/grug fix`, removal acceptance | Comment | Gated by the commenter's access |
| Diff, changed files, PR title and body, linked issues | Prompt, labeled as untrusted data | Head | Data, and #1569 quarantines metadata on sensitive paths |
| CI check-run names | Prompt, labeled as data | Head | Data |
| Inline suppression comments | Which analyzer findings exist | Head | Open, #1579 |

"Base commit" is the PR's own base on every pass, kept apart from a Living
Hunt's diff start, which is a PR commit. It is not always the default branch: see
#1578.

Review models have no tools. They read text and return structured findings, and
the only effect of an instruction hidden in a diff is on what a review says.
Model text is neutralized before it posts, and `/grug fix` commits only on a
maintainer's command, never to a fork, and cannot change workflow files because
the App holds no `workflows` permission.

## GitHub App permissions

Read from the App's public record on 2026-11-01.

| Permission | Held | Least privilege | Excess |
| --- | --- | --- | --- |
| `metadata` | `read` | `read` | None |
| `checks` | `write` | `write` | None |
| `issues` | `write` | `write` for comments and labels | None |
| `pull_requests` | `write` | `write` | None |
| `contents` | `write` | `read` for review, `write` only for `/grug fix` and thread resolution | Shared with every review token |
| `administration` | `write` | `read` for detection, `write` only for ruleset enforcement | Shared with every review token, undocumented until this audit |

Every installation token carries the whole grant, so a token used to post a
comment can also push and change rulesets. #1577 narrows tokens by purpose in
code. Separating the commenting identity from the pushing identity needs a
second App, which a person registers, and #1500 depends on that decision.

## Verification

- CI runs the real engines against a hostile tree, and a stand-in engine that
  obeys every file it can see and reports every secret in its environment to a
  local tripwire. The bar is zero tripwire connections, zero config files
  obeyed, and no repository code run.
- Recorded result, CI on 2026-11-01 with opengrep 1.27.1 and ruff 0.16: egress
  attempts 0, secret access attempts 0, configs obeyed 0, repository code run 0.
- That run covers the analyzers on a CI runner. The run through a live
  installation on a test repository, against a deployed build, is #1587.
