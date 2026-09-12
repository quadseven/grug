#!/usr/bin/env python3
"""Block private infrastructure identifiers from reaching a PUBLIC repo.

WHY THIS EXISTS
---------------
This repo is public. On 2026-08-15 a working session that spanned this repo and
a PRIVATE infrastructure repo leaked, into public commits, issues and PR bodies:

  - cluster and worker-node names
  - private repo names, and cross-references to private ticket numbers
  - the operator's antivirus product, and repeated "the operator" phrasing

None of it was secret-SHAPED. No key, no token, no password - so a secret
scanner would not have looked at it twice, and none did. What leaked was
topology and naming, which is what makes a private network enumerable:
hostnames tell an attacker what to look for, and private repo names tell them
where.

The failure was not carelessness about secrets. It was writing TRUE, USEFUL
engineering context ("which nodes are arm64", "which repos have no coverage")
into the wrong repo while reasoning across both at once. Being careful does not
scale to that; a check does.

THE DESIGN TRAP
---------------
A deny-list naming the real hosts CANNOT live in this repo - that file would BE
the leak, published in the exact place it is meant to protect. So:

  1. The patterns below match generic SHAPES, never specific names. They are
     safe to read publicly, and they catch hosts nobody has thought to add yet.
  2. A specific deny-list (product names, people, anything with no pattern) is
     fetched at runtime from SSM via --deny-list-ssm and never stored here.

Layer 1 works offline with zero setup, which is why it is the default: a guard
that needs credentials to run is a guard that gets skipped. It is NOT
sufficient on its own - a person's name has no shape, so layer 2 is the half
that catches people. CI must always pass --deny-list-ssm; the local pre-commit
hook deliberately does not (it has no credentials).

Every run says which layers were loaded, because this guard spent its entire
life running with layer 2 switched off and reporting "clean" - the CI workflow
never passed --deny-list-ssm, so it was structurally incapable of matching the
operator's own first name while two audits certified the repo clean (#921).
"Clean" must never be printable without saying what was actually checked.

USAGE
    check_private_leaks.py --staged            # pre-commit: staged diff
    check_private_leaks.py --diff BASE..HEAD   # CI: a PR's diff
    check_private_leaks.py --text-file body.md # an issue/PR body before posting
    check_private_leaks.py --deny-list-ssm /path/to/param   # add specifics

Exit 0 clean, 1 on a hit, 2 on a usage/credential/empty-deny-list error.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys

# Generic shapes only. Every entry must be safe to read in a public repo, so
# describe the SHAPE of a private identifier, never an instance of one.
PATTERNS: list[tuple[str, str, str]] = [
    (
        "tailnet-host",
        r"\b[a-z0-9-]+\.ts\.[a-z0-9-]+\.[a-z]{2,}\b",
        "a tailnet hostname (private overlay network address)",
    ),
    (
        "tailscale-ip",
        r"\b100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.\d{1,3}\.\d{1,3}\b",
        "a CGNAT/Tailscale IP (the 100.64/10 range)",  # leak-guard-allow: RFC range, not a host
    ),
    (
        "rfc1918-ip",
        r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}"
        r"|172\.(?:1[6-9]|2[0-9]|3[01])\.\d{1,3}\.\d{1,3})\b",
        "a private-range IP address",
    ),
    (
        "server-hostname",
        r"\b(?:srv|usr)-[a-z0-9]+(?:-[a-z0-9]+)+\b",
        "an internal server hostname (srv-*/usr-* naming convention)",
    ),
    (
        "cluster-node",
        r"\bk8s-[a-z0-9]+-[a-z0-9-]*worker-\d+\b",
        "a Kubernetes worker-node name",
    ),
    (
        "private-issue-ref",
        # A `name` + `#` + number cross-repo ref. A bare issue number with no
        # prefix is this repo's own and always fine.
        # The final char before `#` must be alphanumeric: prose like "Post-#77"
        # or "pre-#354" is a hyphenated word followed by THIS repo's issue, not
        # a repo reference, and 91 of the first 99 hits on the existing tree
        # were exactly that. Requiring [a-z0-9] before the `#` removes them
        # while still catching real cross-repo refs.
        r"(?<![\w/-])(?!grug\b)[a-z][a-z0-9_]*(?:-[a-z0-9]+)*[a-z0-9]#\d+\b",
        "a cross-repo issue reference (may name a private repo)",
    ),
]

# Lines that are ABOUT the guard rather than a leak. Without this the file
# documenting the patterns trips the patterns.
ALLOW_MARKERS = (
    "check_private_leaks",
    "leak-guard-allow",
    "PATTERNS:",
)


def compiled() -> list[tuple[str, re.Pattern[str], str]]:
    return [(n, re.compile(p, re.I), why) for n, p, why in PATTERNS]


DENY = "deny-list"          # rule name for layer-2 hits; scan() redacts these


def _fatal(msg: str) -> SystemExit:
    print(f"FATAL: {msg}", file=sys.stderr)
    print("The deny-list layer is the half that catches PEOPLE. Refusing to "
          "run without it: a guard that degrades to 'clean' is worse than no "
          "guard, because it produces a passing check that means nothing.",
          file=sys.stderr)
    return SystemExit(2)


def parse_deny_list(raw: str) -> list[str]:
    """One term per line; blanks and #-comments dropped; case-insensitive dedup.

    Line-based, NOT whitespace-split: a full name is one term, and splitting it
    would silently turn "Ada Lovelace" into two unrelated single-word rules.
    """
    seen: set[str] = set()
    terms: list[str] = []
    for line in raw.splitlines():
        term = line.strip()
        if not term or term.startswith("#"):
            continue
        if term.lower() in seen:
            continue
        seen.add(term.lower())
        terms.append(term)
    return terms


def deny_rule(term: str) -> re.Pattern[str]:
    """A denied term, anchored on word-ish boundaries.

    Plain substring matching is unusable for people: a four-letter first name
    is a substring of ordinary English words (`ada` sits inside `canada`),
    and a guard that fires on prose gets bypassed within a day, at which point
    it protects nothing. The boundary is applied only where the term's own edge
    is alphanumeric, so `/Users/<name>` and `.example.net` still match mid-path
    and mid-hostname. Known gap: a term glued into camelCase (`<name>Handler`)
    is not matched - add the joined form as its own term if that ever matters.
    """
    lead = r"(?<![0-9A-Za-z_])" if (term[0].isalnum() or term[0] == "_") else ""
    trail = r"(?![0-9A-Za-z_])" if (term[-1].isalnum() or term[-1] == "_") else ""
    return re.compile(lead + re.escape(term) + trail, re.I)


def load_ssm_deny_list(param: str) -> list[tuple[str, re.Pattern[str], str]]:
    """Specific names that have no generic shape (products, people, projects).

    Fetched at runtime, never written to this repo. EVERY failure here is
    FATAL, including an empty parameter: silently degrading to "generic shapes
    only" would mean the guard quietly stops covering the exact terms someone
    deliberately added to it, while still printing "clean". That is precisely
    the failure this repo already shipped for months (#921).
    """
    argv = ["aws", "ssm", "get-parameter", "--name", param, "--with-decryption",
            "--query", "Parameter.Value", "--output", "text"]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, check=False)
    except OSError as exc:
        # No `aws` on PATH. Without this the interpreter dies with a traceback
        # and exit 1 - indistinguishable from "a leak was found".
        raise _fatal(f"cannot run the AWS CLI to read {param}: {exc}") from exc
    if out.returncode != 0:
        raise _fatal(f"could not read deny-list from SSM {param}: "
                     f"{out.stderr.strip()[:200]}")
    terms = parse_deny_list(out.stdout)
    if not terms:
        raise _fatal(f"deny-list {param} exists but holds no terms")
    return [(DENY, deny_rule(t), "an explicitly denied term") for t in terms]


def scan(text: str, rules, *, label: str, diff_mode: bool = False) -> list[str]:
    """Scan text for private identifiers.

    `diff_mode` is NOT cosmetic. In a diff, a line starting with `-` is a
    REMOVAL - i.e. someone scrubbing a leak - and blocking that would make the
    guard forbid its own remedy. In plain text (an issue or PR body) a leading
    `-` is a markdown bullet and must be scanned like any other line. The two
    cannot be told apart by looking, so the caller says which it has.
    """
    hits: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if diff_mode:
            if line.startswith(("---", "+++")):
                continue          # file headers carry paths, not content
            if line.startswith("-"):
                continue          # a removal is a scrub; never block it
            payload = line[1:] if line.startswith("+") else line
        else:
            payload = line
        if any(m in payload for m in ALLOW_MARKERS):
            continue
        for name, rx, why in rules:
            m = rx.search(payload)
            if m:
                # A layer-2 hit is NEVER echoed. This runs in a PUBLIC repo's
                # Actions log; printing the matched term would republish the
                # very string the deny-list exists to keep out, and label it as
                # protected. The line number is enough to find it - the author
                # is looking at their own diff.
                shown = (f"<redacted, {len(m.group(0))} chars>" if name == DENY
                         else repr(m.group(0)))
                hits.append(f"  {label}:{lineno}  [{name}] {shown} - {why}")
                break
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--staged", action="store_true", help="scan the staged diff")
    src.add_argument("--diff", help="scan `git diff <RANGE>`")
    src.add_argument("--text-file", help="scan a file (an issue/PR body)")
    ap.add_argument("--deny-list-ssm", help="SSM param holding extra terms")
    args = ap.parse_args()

    rules = compiled()
    n_shapes = len(rules)
    n_deny = 0
    if args.deny_list_ssm:
        deny = load_ssm_deny_list(args.deny_list_ssm)
        n_deny = len(deny)
        rules += deny
    # Say what is loaded on EVERY run, pass or fail. The whole point of #921 is
    # that "leak guard: clean" was printed for months by a guard whose people-
    # catching half was never switched on, and nothing in the output said so.
    coverage = (f"{n_shapes} shape patterns, {n_deny} deny-list terms"
                if args.deny_list_ssm else
                f"{n_shapes} shape patterns, deny-list NOT CONFIGURED "
                f"(layer 2 OFF - names and products are not being checked)")

    if args.staged:
        text = subprocess.run(["git", "diff", "--cached", "-U0"],
                              capture_output=True, text=True).stdout
        label = "staged"
    elif args.diff:
        text = subprocess.run(["git", "diff", "-U0", args.diff],
                              capture_output=True, text=True).stdout
        label = args.diff
    else:
        text = open(args.text_file, encoding="utf-8").read()
        label = args.text_file

    hits = scan(text, rules, label=label, diff_mode=args.staged or bool(args.diff))
    if not hits:
        print(f"leak guard: clean ({coverage})")
        return 0

    print(f"BLOCKED: private infrastructure identifiers found ({coverage})\n",
          file=sys.stderr)
    for h in hits:
        print(h, file=sys.stderr)
    print(
        "\nThis repo is PUBLIC. Hostnames, node names, private-range IPs and\n"
        "cross-repo issue refs identify infrastructure even when they contain no\n"
        "secret. Describe the shape instead: 'an arm64 node', 'the infrastructure\n"
        "repo', 'a tailnet host'.\n"
        "\nA [deny-list] hit is a term with no generic shape - a person, a\n"
        "product, a host codename - and its text is deliberately NOT echoed\n"
        "here. Open the line above in your own diff to see it.\n"
        "\nIf a hit is genuinely fine, add the marker 'leak-guard-allow' to that\n"
        "line - deliberately noisy, so the exemption is visible in review.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
