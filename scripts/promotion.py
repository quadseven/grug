"""Promotion decision for the merge deploy (#498, ADR-0016).

Pure logic, no I/O: the workflow gathers the facts (merged-PR lookup, git
trees, registry manifest existence) and this module decides PROMOTE vs
REBUILD. Keeping the decision here gives it a unit-test seam the workflow
YAML cannot have; the workflow is a thin gatherer.

The rule (ADR-0016): promote the PR-tested digest ONLY when
  1. the pushed sha resolves to exactly ONE merged PR,
  2. the PR head's git TREE equals the merge commit's tree (if main moved
     under the PR, the tested image is NOT the merged code), and
  3. every service's pr-<n> manifest exists in the registry.
Anything else - direct push, workflow_dispatch, fork-sourced PR whose gate
could not push, ambiguity of any kind - REBUILDS. Promotion is an
optimization, never a requirement.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Decision:
    promote: bool
    reason: str


def decide(
    *,
    pr_numbers: list[int],
    pr_head_tree: str | None,
    merge_tree: str | None,
    manifests_present: dict[str, bool],
) -> Decision:
    """Decide PROMOTE vs REBUILD from gathered facts.

    pr_numbers: PRs associated with the pushed commit (merged only).
    pr_head_tree / merge_tree: `git rev-parse <sha>^{tree}` outputs.
    manifests_present: service -> does grug-<svc>:pr-<n> exist in the
    registry (the workflow probes with `docker manifest inspect`).
    """
    if len(pr_numbers) != 1:
        return Decision(False, f"rebuild: {len(pr_numbers)} PRs resolve to this sha (need exactly 1)")
    if not pr_head_tree or not merge_tree:
        return Decision(False, "rebuild: missing tree hash (shallow clone or lookup failure)")
    if pr_head_tree != merge_tree:
        return Decision(
            False,
            "rebuild: merge tree differs from PR head tree (main moved under the PR - "
            "the tested image is not the merged code)",
        )
    if not manifests_present:
        return Decision(False, "rebuild: no services enumerated")
    missing = sorted(svc for svc, ok in manifests_present.items() if not ok)
    if missing:
        return Decision(False, f"rebuild: registry missing pr-{pr_numbers[0]} manifest for: {', '.join(missing)}")
    return Decision(True, f"promote: pr-{pr_numbers[0]} digest is tree-identical and fully present")


def _main() -> int:
    """CLI for the deploy workflow: facts arrive as JSON on stdin, the
    decision leaves as `promote=<bool> reason=<text>` lines on stdout
    (GITHUB_OUTPUT-friendly). Exit 0 always - a gather/parse failure is a
    REBUILD, never a failed deploy."""
    import json
    import sys

    try:
        facts = json.load(sys.stdin)
        d = decide(
            pr_numbers=[int(n) for n in facts.get("pr_numbers", [])],
            pr_head_tree=facts.get("pr_head_tree"),
            merge_tree=facts.get("merge_tree"),
            manifests_present={
                str(k): bool(v)
                for k, v in facts.get("manifests_present", {}).items()
            },
        )
    except Exception as e:  # noqa: BLE001 - ambiguity rebuilds, never breaks the deploy
        d = Decision(False, f"rebuild: facts unparseable ({type(e).__name__})")
    print(f"promote={'true' if d.promote else 'false'}")
    print(f"reason={d.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
