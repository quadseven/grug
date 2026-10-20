"""Run Grug - Chief's Hunt Plan checks on a PR body before the PR exists.

    uv run --with httpx --with 'psycopg[binary,pool]' python scripts/chief_check.py \\
        --repo OWNER/NAME (--body-file FILE | --body-file - | --pr N) [--base REF]

Agents open PRs with `gh pr create --body`, which skips the repo's PR
template, so the first time they learn a body fails Chief is the red check
after the PR is up. This runs the same evaluator Chief runs
(`personas.tpm.persona.evaluate_pull_request`), so the verdict, the advisory
set and the agent-marker skip rule cannot drift from the gate. Only the
fetchers differ: Chief reads GitHub with its install token, this reads it
with the caller's `gh` login. The facts built here mirror
`personas.tpm.issue_fetcher.issue_facts` field for field.

The author is treated as a trusted operator: this is the check an operator
runs on their own body, and a trusted operator's agent-marked PR is checked
rather than skipped (grug#1251).

Exit codes: 0 pass or neutral (a skip rule applied), 1 any blocking check
failed, 2 usage error (bad flags, unreadable body file, no PR body).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "_shared"))

from personas.tpm import persona  # noqa: E402
from personas.tpm.dor_checks import IssueFacts  # noqa: E402

# argv -> stdout. Raises on a non-zero exit. Injected so the tests drive the
# whole CLI without gh, git or the network.
Runner = Callable[[list[str]], str]


class GhError(RuntimeError):
    """A gh or git command exited non-zero; the message is its stderr."""


def _subprocess_runner(argv: list[str]) -> str:
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise GhError(proc.stderr.strip() or f"{argv[0]} exited {proc.returncode}")
    return proc.stdout


def build_fetchers(run: Runner, repo: str, pr: int | None, base: str):
    """The three fetchers `evaluate_pull_request` takes, backed by `run`.

    Each raises on a failed read, and Chief's checks turn that into a
    fail-open `skipped` row, so a gh blip here reads the same as one there.
    """

    def issue(number: int) -> dict:
        return json.loads(run(["gh", "api", f"repos/{repo}/issues/{int(number)}"]) or "{}")

    def fetch_issue(number: int) -> str:
        return issue(number).get("body") or ""

    def fetch_issue_facts(number: int) -> IssueFacts:
        data = issue(number)
        try:
            raw = run(["gh", "api", f"repos/{repo}/issues/{int(number)}/parent"])
            parent = (json.loads(raw or "{}") or {}).get("number")
        except GhError as exc:
            # 404 is GitHub saying "no parent"; anything else is a failed read.
            if "HTTP 404" not in str(exc):
                raise
            parent = None
        return IssueFacts(
            number=int(number),
            title=data.get("title") or "",
            body=data.get("body") or "",
            labels=tuple(
                (label.get("name") or "") if isinstance(label, dict) else str(label)
                for label in data.get("labels") or []
            ),
            sub_issue_count=int((data.get("sub_issues_summary") or {}).get("total") or 0),
            parent_number=parent,
            is_pull_request=bool(data.get("pull_request")),
        )

    def fetch_changed_files() -> list[str]:
        if pr is not None:
            out = run(["gh", "pr", "diff", str(pr), "-R", repo, "--name-only"])
        else:
            out = run(["git", "diff", "--name-only", f"{base}...HEAD"])
        return [line for line in out.splitlines() if line.strip()]

    return fetch_issue, fetch_issue_facts, fetch_changed_files


def epic_hint(run: Runner, repo: str) -> list[str]:
    """Lines naming the repo's open epics and the two ways to join one.

    Chief's row says the ticket belongs to no epic but not which epics exist;
    that lookup is the step agents kept doing by hand. Best effort: a failed
    search still prints the remedies.
    """
    lines = ["", "which epic: open epics in this repo:"]
    try:
        raw = run([
            "gh", "issue", "list", "-R", repo, "--state", "open",
            "--search", "Epic: in:title", "--json", "number,title", "--limit", "50",
        ])
        epics = [
            e for e in json.loads(raw or "[]")
            if (e.get("title") or "").startswith("Epic:")
        ]
    except (GhError, ValueError) as exc:
        epics = []
        lines.append(f"  (could not list epics: {exc})")
    for e in epics:
        lines.append(f"  #{e['number']} {e['title']}")
    if not epics:
        lines.append("  (none found)")
    lines += [
        "Fix the ticket, not the PR body. Either make it a sub-issue:",
        (
            f"  gh api -X POST repos/{repo}/issues/<epic>/sub_issues "
            f"-F sub_issue_id=$(gh api repos/{repo}/issues/<ticket> --jq .id)"
        ),
        "or add a line `Part of #<epic>` to the ticket's body.",
    ]
    return lines


def _read_body(args: argparse.Namespace, run: Runner) -> str:
    if args.pr is not None:
        return run(["gh", "pr", "view", str(args.pr), "-R", args.repo,
                    "--json", "body", "--jq", ".body"])
    if args.body_file == "-":
        return sys.stdin.read()
    return Path(args.body_file).read_text()


def main(argv: list[str] | None = None, *, run: Runner = _subprocess_runner) -> int:
    parser = argparse.ArgumentParser(
        prog="chief_check",
        description="Run Grug - Chief's Hunt Plan checks on a PR body locally.",
    )
    parser.add_argument("--repo", required=True, help="OWNER/NAME the PR targets")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--body-file", help="PR body file, or - for stdin")
    src.add_argument("--pr", type=int, help="check an open PR's current body and diff")
    parser.add_argument(
        "--base", default="origin/main",
        help="diff base for the in-scope check with --body-file (default origin/main)",
    )
    args = parser.parse_args(argv)
    if args.repo.count("/") != 1 or not all(args.repo.split("/")):
        print(f"chief_check: --repo must be OWNER/NAME, got {args.repo!r}", file=sys.stderr)
        return 2
    try:
        body = _read_body(args, run)
    except (OSError, GhError) as exc:
        print(f"chief_check: cannot read the PR body: {exc}", file=sys.stderr)
        return 2

    fetch_issue, fetch_issue_facts, fetch_changed_files = build_fetchers(
        run, args.repo, args.pr, args.base,
    )
    evaluation = persona.evaluate_pull_request(
        body,
        author_trusted=True,
        fetch_issue=fetch_issue,
        fetch_issue_facts=fetch_issue_facts,
        fetch_changed_files=fetch_changed_files,
    )
    title, summary = persona._summary(list(evaluation.results))
    print(title)
    print()
    print(summary)
    if any(
        r.name == "linked-issue-epic" and not r.passed and not r.skipped
        for r in evaluation.results
    ):
        print("\n".join(epic_hint(run, args.repo)))
    return 0 if evaluation.passed else 1


if __name__ == "__main__":
    sys.exit(main())
