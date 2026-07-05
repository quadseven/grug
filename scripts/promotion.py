"""Promotion decision for the merge deploy (#498, ADR-0016).

Pure logic, no I/O: the workflow gathers the facts (merged-PR lookup, git
trees via the commits API, registry manifest presence, baked image env)
and this module decides PROMOTE vs REBUILD. Keeping the decision here
gives it a unit-test seam the workflow YAML cannot have; the workflow is
a thin gatherer.

The rule (ADR-0016): promote the PR-tested digest ONLY when
  1. the pushed sha resolves to exactly ONE merged PR,
  2. the PR head's git TREE equals the merge commit's tree - both
     VALIDATED as real 40-hex tree hashes (a failed `gh api --jq` lookup
     emits the literal string "null"; equal garbage must never promote),
  3. every expected service's pr-<n> manifest exists in the registry, and
  4. (verified by the workflow via baked_sha_matches) the image's baked
     DD_GIT_COMMIT_SHA equals the PR head sha - closing the stale-tag
     vectors tree identity cannot see: an out-of-order gate run, a
     path-filter dropout, or a merge-ref build overwriting pr-<n> with
     bytes that are not the head's.
Anything else - direct push, workflow_dispatch, fork-sourced PR whose
gate could not push, ambiguity of any kind - REBUILDS. Promotion is an
optimization, never a requirement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_TREE_HASH = re.compile(r"^[0-9a-f]{40}$")

# Categories drive the workflow's alerting tone: an EXPECTED_REBUILD is
# routine (dispatch, direct push, fork PR); ARTIFACT_MISSING means
# promotion SHOULD have been possible (single PR, trees identical) but
# the gate's artifact is absent - the silent-promotion-death case that
# must surface as a ::warning, not scroll past in a green summary.
CATEGORY_PROMOTE = "promote"
CATEGORY_EXPECTED_REBUILD = "expected-rebuild"
CATEGORY_ARTIFACT_MISSING = "artifact-missing"
CATEGORY_LOOKUP_FAILURE = "lookup-failure"


def _one_line(text: str) -> str:
    """GITHUB_OUTPUT is line-oriented key=value; a newline smuggled into a
    fact string must never become an injected output line."""
    return " ".join(str(text).split())


@dataclass(frozen=True)
class Decision:
    promote: bool
    reason: str
    category: str
    pr_number: int | None = field(default=None)

    def __post_init__(self) -> None:
        if self.promote and self.pr_number is None:
            raise ValueError("promote=True requires pr_number")
        object.__setattr__(self, "reason", _one_line(self.reason))

    @classmethod
    def rebuild(cls, detail: str, *, category: str = CATEGORY_EXPECTED_REBUILD) -> "Decision":
        return cls(False, detail, category)

    @classmethod
    def promoted(cls, pr_number: int) -> "Decision":
        return cls(
            True,
            f"pr-{pr_number} digest is tree-identical and fully present",
            CATEGORY_PROMOTE,
            pr_number,
        )


def _valid_tree(value: str | None) -> bool:
    return bool(value) and bool(_TREE_HASH.match(value))


def decide(
    *,
    pr_numbers: list[int],
    pr_head_tree: str | None,
    merge_tree: str | None,
    manifests_present: dict[str, bool],
    expected_services: list[str],
) -> Decision:
    """Decide PROMOTE vs REBUILD from gathered facts.

    pr_numbers: ALL merged PRs the pushed commit resolves to (the
    workflow passes the full list; the exactly-one rule lives HERE).
    manifests_present must enumerate EXACTLY expected_services - a
    gatherer/matrix drift (missing or unexpected service) rebuilds.
    """
    if len(pr_numbers) != 1:
        return Decision.rebuild(
            f"{len(pr_numbers)} merged PRs resolve to this sha (need exactly 1)",
        )
    pr = pr_numbers[0]
    if not _valid_tree(pr_head_tree) or not _valid_tree(merge_tree):
        return Decision.rebuild(
            "tree hash missing or malformed (API lookup failure - equal "
            "garbage must never promote)",
            category=CATEGORY_LOOKUP_FAILURE,
        )
    if pr_head_tree != merge_tree:
        return Decision.rebuild(
            "merge tree differs from PR head tree (main moved under the "
            "PR - the tested image is not the merged code)",
        )
    if set(manifests_present) != set(expected_services):
        return Decision.rebuild(
            f"gatherer enumerated {sorted(manifests_present)} but expected "
            f"{sorted(expected_services)} (matrix/gather drift)",
            category=CATEGORY_LOOKUP_FAILURE,
        )
    missing = sorted(svc for svc, ok in manifests_present.items() if not ok)
    if missing:
        return Decision.rebuild(
            f"registry missing pr-{pr} manifest for: {', '.join(missing)} "
            "(gate did not push: fork PR, unseeded CI secrets, push "
            "failure, or expired tag)",
            category=CATEGORY_ARTIFACT_MISSING,
        )
    return Decision.promoted(pr)


def baked_sha_matches(env_entries: list[str], expected_sha: str) -> tuple[bool, str]:
    """Verify the image's baked DD_GIT_COMMIT_SHA equals the PR head sha.

    env_entries is the image config's Env array (docker buildx imagetools
    inspect --format '{{json .Image.Config.Env}}'). Docker env semantics:
    last assignment wins; the key must match EXACTLY (a
    DD_GIT_COMMIT_SHA_ORIG prefix-cousin must not satisfy the check).
    Fails closed on absence or empty value.
    """
    baked = None
    for entry in env_entries:
        key, sep, value = str(entry).partition("=")
        if sep and key == "DD_GIT_COMMIT_SHA":
            baked = value
    if not baked:
        return False, "image lacks a baked DD_GIT_COMMIT_SHA"
    if baked != expected_sha:
        return False, f"baked sha {baked[:12]} != PR head {str(expected_sha)[:12]}"
    return True, "baked sha matches PR head"


def _main() -> int:
    """CLI for the deploy workflow. Facts arrive as flags; the decision
    leaves as single-line key=value pairs on stdout (GITHUB_OUTPUT
    format). Exit 0 always - a gather/parse failure is a REBUILD, never a
    failed deploy. Subcommand --verify-baked-sha flips to the image-env
    check (same output contract: verified=true/false, reason=...)."""
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser()
    parser.add_argument("--pr-numbers-json", default="[]")
    parser.add_argument("--head-tree", default="")
    parser.add_argument("--merge-tree", default="")
    parser.add_argument("--manifest", action="append", default=[],
                        help="svc=true|false, repeatable")
    parser.add_argument("--expected-services", default="webhook,api")
    parser.add_argument("--verify-baked-sha", nargs=2, metavar=("ENV_JSON", "SHA"))
    try:
        args = parser.parse_args()
        if args.verify_baked_sha:
            env_json, sha = args.verify_baked_sha
            ok, why = baked_sha_matches(json.loads(env_json), sha)
            print(f"verified={'true' if ok else 'false'}")
            print(f"reason={_one_line(why)}")
            return 0
        manifests = {}
        for item in args.manifest:
            svc, sep, val = item.partition("=")
            if sep:
                manifests[svc] = val == "true"
        d = decide(
            pr_numbers=[int(n) for n in json.loads(args.pr_numbers_json)],
            pr_head_tree=args.head_tree or None,
            merge_tree=args.merge_tree or None,
            manifests_present=manifests,
            expected_services=[s for s in args.expected_services.split(",") if s],
        )
    except SystemExit:
        # argparse's own exit (bad flags) - even that must not fail the deploy
        d = Decision.rebuild("facts unparseable (bad CLI invocation)",
                             category=CATEGORY_LOOKUP_FAILURE)
    except Exception as e:  # noqa: BLE001 - ambiguity rebuilds, never breaks the deploy
        d = Decision.rebuild(f"facts unparseable ({type(e).__name__})",
                             category=CATEGORY_LOOKUP_FAILURE)
    print(f"promote={'true' if d.promote else 'false'}")
    print(f"reason={d.reason}")
    print(f"category={d.category}")
    print(f"pr_num={d.pr_number if d.pr_number is not None else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
