"""Eval corpus for the Elder replay harness (#361 slice 2, #537).

Pure: groups slice-1 `LedgerRow`s into one `EvalCase` per (repo, pr) and
bridges the LEDGER class vocabulary (test-gap, security-scope, ...) onto
Elder's closed `_BUG_CLASSES` taxonomy. Ledger classes Elder cannot
express (doc-truth, iac-hygiene, ...) are surfaced as `out_of_taxonomy`
and EXCLUDED from scoring denominators - a class the reviewer has no way
to name must never score as a miss.

Rows arrive via the slice-1 corpus layer only: `rows_from_store` (the
ingested `pg_install_store` rows) or `ledger.parse_jsonl` on the
committed JSONL - never a third parser.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from typing import Iterable

# Deliberate private import: the eval must bridge onto Elder's REAL closed
# taxonomy, not a copy that drifts (same rationale as the SAST runner
# importing _build_messages - measuring Elder means using Elder's own parts).
from code_review_prompt import _BUG_CLASSES
from ledger import LedgerRow, parse_row

log = logging.getLogger("grug.elder_eval")

_NORM_RE = re.compile(r"[^a-z0-9]+")


def normalize_class(label: str) -> str:
    """Kebab-normalize a class label so Elder's display labels ("silent
    failure") and the ledger's kebab labels ("silent-failure") compare."""
    return _NORM_RE.sub("-", label.lower()).strip("-")


# Abbreviated-to-full git SHA range. Guards against the `commit` field's
# free-text history ('sed-sim-test', 'golang:1.26', '-', ...) - see #545 and
# the ledger row schema note in `ledger.py`. Real hex SHAs never collide with
# that junk (it always carries a char outside [0-9a-f] or the wrong length).
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


def _looks_like_sha(value: str | None) -> bool:
    """Best-effort git-sha shape check, not a GitHub round-trip - the real
    validation is the API call failing loudly (falls back to unanchored,
    #545)."""
    return bool(value) and bool(_SHA_RE.fullmatch(value.strip().lower()))


# Permanently unresolvable cases (#895): the PR itself (not just an
# anchor) is gone from GitHub, so `diff_for_case`'s own anchor-resolution
# fallback (fetch the PR's current merged diff) is doomed too - confirmed
# via `run_eval`'s existing 404/406 handling, which already logs "the
# ledger references a PR whose diff cannot be fetched; prune or annotate
# it" for exactly this case. Listing it here is that action: `build_cases`
# stamps the reason onto the case, the runner skips attempting a replay
# for it, and `scoring.score` reports it in its own stable bucket instead
# of `errored_cases` (reserved for NEW, possibly-transient breakage) -
# every eval run re-hitting the same known-dead PR is corpus rot, not a
# fresh signal.
_KNOWN_UNRESOLVABLE_CASES: dict[tuple[str, int], str] = {
    ("quadseven/zippie", 165): (
        "PR #165 and its recorded commit (e3a58c73) are both absent from "
        "quadseven/zippie's current history; the repo was recreated "
        "2026-08-28, after this finding was recorded (2026-08-12) - see #895."
    ),
}

ELDER_CLASSES: frozenset[str] = frozenset(normalize_class(c) for c in _BUG_CLASSES)

# Ledger vocabulary -> Elder vocabulary, where the words differ but the
# concept is the same. Keys and values are normalized. A ledger class not
# in this map and not itself an Elder class is OUT OF TAXONOMY.
_CLASS_ALIASES: dict[str, frozenset[str]] = {
    "test-gap": frozenset({"test-coverage", "test-fidelity"}),
    "security-scope": frozenset({"security"}),
    "simplification": frozenset({"maintainability"}),
    "upstream-semantics": frozenset({"correctness", "robustness"}),
}

# A rename/removal in _BUG_CLASSES must fail HERE at import, not silently
# turn an aliased ledger class uncatchable (same import-time-guard style as
# ReviewRule.__post_init__).
for _ledger_cls, _elder_set in _CLASS_ALIASES.items():
    _unknown = _elder_set - ELDER_CLASSES
    if _unknown:
        raise ValueError(
            f"_CLASS_ALIASES[{_ledger_cls!r}] maps to classes outside "
            f"Elder's taxonomy: {sorted(_unknown)}"
        )


def expected_elder_classes(ledger_class: str) -> frozenset[str]:
    """The Elder classes that would count as CATCHING a ledger finding of
    `ledger_class`. Empty set = out of taxonomy (excluded, not a miss)."""
    norm = normalize_class(ledger_class)
    if norm in _CLASS_ALIASES:
        return _CLASS_ALIASES[norm]
    if norm in ELDER_CLASSES:
        return frozenset({norm})
    return frozenset()


@dataclass(frozen=True)
class EvalCase:
    """One ledger PR as a replay unit.

    `expected_classes`: ledger class (normalized) -> the Elder classes that
    count as a catch. Only accepted (fixed/declined) in-taxonomy rows.
    `fp_only_classes`: ELDER-normalized classes known on this PR ONLY as
    false positives - a replay emission there is measured noise.
    `out_of_taxonomy`: ledger classes Elder cannot express (accepted OR
    false-positive rows), with row counts - reported, never scored.
    `unknown_verdicts`: rows whose verdict is neither accepted nor
    false-positive (a typo or a new verdict value) - excluded from
    scoring, but COUNTED so a mislabeled corpus cannot silently yield an
    empty eval.

    `anchor_head_sha` / `anchor_fix_commit` (#545): how to replay this case
    against the PRE-FIX snapshot instead of the PR's final merged diff -
    see `anchored`. At most one is set; `anchor_head_sha` wins when both a
    row's `head_sha` and another row's `commit` would qualify.

    `unresolvable_reason` (#895): set when this (repo, pr) is a KNOWN,
    permanently dead reference (see `_KNOWN_UNRESOLVABLE_CASES`) - the
    runner skips replaying it and `scoring.score` reports it separately
    from `errored_cases`, so a stable, already-diagnosed corpus problem
    stops being re-flagged as if it were new.
    """

    repo: str
    pr: int
    expected_classes: dict[str, frozenset[str]]
    fp_only_classes: frozenset[str]
    out_of_taxonomy: dict[str, int]
    unknown_verdicts: dict[str, int]
    anchor_head_sha: str | None = None
    anchor_fix_commit: str | None = None
    unresolvable_reason: str | None = None

    @property
    def case_id(self) -> str:
        return f"{self.repo}#{self.pr}"

    @property
    def scorable(self) -> bool:
        """Worth an LLM call: contributes to catch or noise denominators."""
        return bool(self.expected_classes or self.fp_only_classes)

    @property
    def anchored(self) -> bool:
        """True when a pre-fix snapshot is derivable for this case (#545):
        either a row recorded the reviewed head SHA directly, or a row's
        `commit` parses as a fix-commit SHA whose PARENT stands in for it.
        False means only the PR's final merged diff is available - the
        KNOWN METHODOLOGY BIAS documented in specs/DESIGN.md: a `fixed`
        row replayed there may look like a miss when Elder correctly saw
        nothing wrong in already-fixed code."""
        return bool(self.anchor_head_sha or self.anchor_fix_commit)


def _case_anchor(rows: list[LedgerRow]) -> tuple[str | None, str | None]:
    """(anchor_head_sha, anchor_fix_commit) for one (repo, pr) group (#545).

    First sha-shaped `head_sha` seen wins; only when NO row has one does a
    sha-shaped `commit` (historical rows: the FIX commit) stand in. Rows
    disagreeing on the value are logged, not raised - `elder_eval` must
    keep running on a messy corpus, it just cannot promise which of the
    disagreeing SHAs it picked."""
    head_sha: str | None = None
    fix_commit: str | None = None
    head_sha_conflict = False
    fix_commit_conflict = False
    for r in rows:
        if _looks_like_sha(r.head_sha):
            candidate = r.head_sha.strip().lower()
            if head_sha is None:
                head_sha = candidate
            elif head_sha != candidate:
                head_sha_conflict = True
        elif _looks_like_sha(r.commit):
            candidate = r.commit.strip().lower()
            if fix_commit is None:
                fix_commit = candidate
            elif fix_commit != candidate:
                fix_commit_conflict = True
    if head_sha_conflict:
        log.warning(
            "eval_case_head_sha_conflict repo=%s pr=%s using=%s",
            rows[0].repo, rows[0].pr, head_sha,
        )
    if fix_commit_conflict:
        log.warning(
            "eval_case_fix_commit_conflict repo=%s pr=%s using=%s",
            rows[0].repo, rows[0].pr, fix_commit,
        )
    # An explicit reviewed head always outranks a derived fix-commit, even
    # if some other row on the same PR only carries the latter.
    return head_sha, (fix_commit if head_sha is None else None)


def build_cases(rows: Iterable[LedgerRow]) -> tuple[EvalCase, ...]:
    """Group ledger rows into per-(repo, pr) EvalCases, (repo, pr)-sorted.

    Rows with NO pr number are dropped here, and that is a real distinction
    rather than a tidy-up. `LedgerRow.pr` became optional in #764 so that
    consensus findings written outside a PR context stop being silently
    deleted from the corpus - they carry a real verdict and MUST count
    toward class precision. But this eval REPLAYS a case by fetching its
    PR diff, and a row with no PR has no diff to replay: it is unscorable
    by construction, not an error.

    Without this filter those rows reached the runner, which fetched
    `/pulls/None`, took a 404, and marked the case `errored` - and
    `--record` refuses to write a baseline when ANY case errors, so six
    recovered rows blocked every future baseline re-record.

    It also removes a latent crash. The old `sorted(grouped)` compares
    `(repo, pr)` tuples, so one repo holding both a `None` and an int pr
    raises `TypeError: '<' not supported between 'int' and 'NoneType'` and
    kills the entire eval. That is masked today only because the PR-less
    rows say `quadseven/grug` while the older rows say `githumps/grug`
    (the pre-rename name) - a coincidence, not a guarantee.
    """
    grouped: dict[tuple[str, int], list[LedgerRow]] = {}
    dropped = 0
    for r in rows:
        if r.pr is None:
            dropped += 1
            continue
        grouped.setdefault((r.repo, r.pr), []).append(r)
    if dropped:
        # Logged, never silent: a shrinking denominator that nobody
        # announced is how corpus rot hides (same reasoning as
        # `eval_corpus_pr_unfetchable`).
        log.info(
            "eval_corpus_rows_without_pr_skipped rows=%d - not replayable "
            "(no diff to fetch); still counted by class_precision",
            dropped,
        )

    cases: list[EvalCase] = []
    for repo, pr in sorted(grouped):
        expected: dict[str, frozenset[str]] = {}
        out_of_taxonomy: Counter[str] = Counter()
        unknown_verdicts: Counter[str] = Counter()
        fp_elder: set[str] = set()
        accepted_elder: set[str] = set()
        for r in grouped[(repo, pr)]:
            norm = normalize_class(r.finding_class)
            elder = expected_elder_classes(norm)
            if r.accepted:
                if not elder:
                    out_of_taxonomy[norm] += 1
                    continue
                expected[norm] = elder
                accepted_elder |= elder
            elif r.false_positive:
                if not elder:
                    out_of_taxonomy[norm] += 1
                    continue
                fp_elder |= elder
            else:
                # Neither accepted nor FP: a typo'd or novel verdict. It
                # must not vanish - a mislabeled corpus that yields zero
                # expected cells needs to say WHY.
                unknown_verdicts[r.verdict] += 1
        anchor_head_sha, anchor_fix_commit = _case_anchor(grouped[(repo, pr)])
        cases.append(
            EvalCase(
                repo=repo,
                pr=pr,
                expected_classes=expected,
                # A class both accepted AND FP'd on the same PR is not
                # fp-only - emitting it there is a legitimate catch.
                fp_only_classes=frozenset(fp_elder - accepted_elder),
                anchor_head_sha=anchor_head_sha,
                anchor_fix_commit=anchor_fix_commit,
                out_of_taxonomy=dict(out_of_taxonomy),
                unknown_verdicts=dict(unknown_verdicts),
                unresolvable_reason=_KNOWN_UNRESOLVABLE_CASES.get((repo, pr)),
            )
        )
    return tuple(cases)


def rows_from_store(repo: str) -> list[LedgerRow]:
    """The INGESTED corpus (#361 slice 1): store rows -> LedgerRows via the
    slice-1 parser. Live-only (needs the DB env); imported lazily so the
    pure paths never touch the adapter."""
    from adapters.pg_install_store import list_ledger_rows  # type: ignore

    out: list[LedgerRow] = []
    malformed = 0
    for d in list_ledger_rows(repo):
        row = parse_row(d)
        if row is None:
            malformed += 1
            continue
        out.append(row)
    if malformed:
        # A corrupted ingest must not silently shrink the corpus.
        log.warning(
            "eval_store_rows_malformed repo=%s skipped=%d", repo, malformed
        )
    return out
