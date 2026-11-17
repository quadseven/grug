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
| ruff (`lint.py`) | Changed `.py` file text | Head | Landlock and seccomp, read-only copy, `--isolated`, no config on disk | Sandboxed, #1576 |
| opengrep analyzer (`analyzers.py`) | Changed JS and TS file text | Head | Landlock and seccomp, read-only copy, Grug's rules | Sandboxed, #1576 |
| opengrep scan (`sast.py`) | All changed file text | Head | Landlock and seccomp, read-only copy, Grug's rules, PR ignore files dropped | Sandboxed, #1576 |
| Smasher Trial (`smasher/`) | The repo's code and tests | Head | Separate Job, no credential, read-only checkout; deny-all egress needs a policy-enforcing CNI (ADR-0013) | Sandboxed; off by default |
| Trial dependency install | Requirements files | Head | Wheels only and no build backend. Option lines and direct references are dropped before pip reads the file, and pip runs isolated against the configured index only, so the file cannot choose the host the prep pod connects to, #1580 | Sandboxed; off by default |
| Trial tarball unpack | Archive member names | Head | Members outside the destination and links are skipped | Handled in code |
| Grug's own CI | This repo's PRs | Head | No `pull_request_target`; previews run for same-repo PRs only | Handled in config |

The runtime image launches each analyzer with a scrubbed environment and closes
inherited descriptors. Landlock allows reads of the copied files, trusted rules,
and immutable runtime files. Each child gets private writable scratch. The
pod's credential mounts and other processes' environments are outside that
boundary. Seccomp denies network socket creation and permits only private
connection-oriented Unix socket pairs for opengrep's event loop. This works
with the existing pod security context and is independent of network policy.
If the kernel cannot install either restriction, the analyzer is skipped.
Opengrep is unpacked at image build time so scans share immutable engine code.

## Instructions

| Source | Reaches | Read from | State |
| --- | --- | --- | --- |
| `.grug.yaml` | Skip rules, ignores, path instructions | Base commit, default branch head since #1578 for other targets | Base only |
| Ruff policy, `.semgrepignore` | Which findings survive | Base commit, default branch head since #1578 for other targets | Base only |
| Guideline files (`CLAUDE.md`, `AGENTS.md` and the like) | System prompt | Base commit since #1574, default branch head since #1578 for other targets | Base only |
| `CONTEXT.md`, `docs/adr/` | Review context, as the repo's source of truth | Base commit since #1574, default branch head since #1578 for other targets | Base only |
| Reply-mined learnings | System prompt | Stored, write access required | Gated by the author's access; learnings mutations require repository write access, and list hides teacher logins below write access |
| `/grug` commands | Reruns, `/grug fix`, removal acceptance | Comment | Gated by the commenter's access |
| Diff, changed files, PR title and body, linked issues | Prompt, labeled as untrusted data | Head | Data, and #1569 quarantines metadata on sensitive paths |
| CI check-run names | Prompt, labeled as data | Head | Data |
| Inline suppression comments | Which analyzer findings exist | Head | Open, #1579 |

"Base commit" is the PR's own base on every pass when the PR targets the
default branch, kept apart from a Living Hunt's diff start, which is a PR
commit. A PR into any other branch reads every steering file above from the
default branch head instead, resolved once per pass, because its base can be a
branch the PR's author may push to (#1578). A failed lookup falls back to the
default branch name, never to that base.

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
  attempts 0 for the stand-in and both real engines, configs obeyed 0, repository
  code run 0, and no secret in any child environment.
- Egress is what the tripwire saw, so it covers the configs that name it. Secret
  access is measured by the child's environment. A missing engine fails CI.
- That run covers the analyzers on a CI runner. The run through a live
  installation on a test repository, against a deployed build, is #1587.

### Kernel boundary proof for #1576

On 2026-11-17, `test_analyzer_child_cannot_access_parent_resources` failed on
the old launch path and passed with the launcher in an arm64 K3s
v1.37.2+k3s1 pod with Calico v3.33.1 and Linux 6.8. The pod used UID 10001,
`RuntimeDefault` seccomp, all capabilities dropped, and a read-only root
filesystem. A second pod's Calico deny-egress policy blocked an ordinary
TCP connection to the test Service. The reviewer parent could reach that
Service and a loopback listener and could read the synthetic credential mount
and its own environment. The analyzer was denied each operation by the
kernel. Real-engine checks in the same pod still reported the planted ruff,
SAST, and Elder findings.

In the same cluster, a 40-file fixture (20 Python, 20 JavaScript) ran Elder's
ruff and opengrep passes plus the standalone SAST pass. The warm median over
12 runs fell from 2.52 s on `dc55243e` to 1.99 s with the launcher and bundled
runtime, a 0.53 s reduction. The first run took 2.62 s before and 1.94 s after.
The timings cover the analyzer portion of a review and exclude GitHub and
model calls. Both runs used pinned opengrep 1.27.1 and the same ruff binary.
