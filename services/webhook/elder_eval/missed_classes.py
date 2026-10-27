"""Finding classes Elder missed on PR #751, as replayable fixtures (#752).

A src-a reviewer caught three real defects on PR #751 at the reviewed head
below; Elder's only finding there was the deterministic `high-complexity`
scanner's, so its LLM arm scored 0/3. Each class needs reasoning the
ledger replay (`corpus.py`) cannot score, because a ledger case is graded
per CLASS - any `correctness` finding anywhere on the PR would count as
catching a cross-hop dataflow bug. These classes are precision-critical
("your guard is insufficient" costs operator time when wrong), so a catch
here is LINE-ANCHORED: the finding must sit on the reference finding's
path, inside its defect line range, and name the defect in its message.

The fixture data under `fixtures/pr751/` is the real reviewed head: the
hunks of the two Python files carrying all three defects, and the head
snapshot of `services/api/installations.py` (production sends a changed
file's full content when it fits the #336 budget, and this one does).
The snapshot's non-ASCII punctuation is transliterated one-for-one, so
every line number still matches the hunk headers. The workflow and
dashboard hunks are omitted: no defect anchors there.

The pure core (fixtures, `caught`, `score`, `recall`) runs in CI. The live
replay (`replay`, `main`) makes backend calls and runs only on demand:

    python -m elder_eval.missed_classes            # print per-fixture result
    python -m elder_eval.missed_classes --record   # also record the run

The reachability decision per class (prompt/rule work vs a structural
change) is recorded on each fixture and argued in
docs/research/elder-missed-finding-classes-pr751.md.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
from llm_client import Hunk, _build_messages, _parse_review
from review_evidence import ElderEvidence, assemble_evidence
from sast_benchmark.backends import BenchBackend, configured_backends
from sast_benchmark.runner import _post

log = logging.getLogger("grug.elder_eval.missed_classes")

REPO = "quadseven/grug"
PR = 751
REVIEWED_HEAD = "61dce166be34e610caed52eac2c444cbd43da80c"
# Corpus attribution of the reference findings (runtime harvest config).
REFERENCE_SOURCE = "src-a"

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pr751"
BASELINE_PATH = Path(__file__).parent / "missed_classes_baseline.json"
# Measure the shipped prompt, as the sibling runners do.
_PROMPT_VARIANT = "v2"

ATTESTER_PATH = "infra/scripts/attest_persona_surface_coherence.py"
INSTALLATIONS_PATH = "services/api/installations.py"

# The data file holding each path's real hunks, and its head content
# (None = a new file, whose content is the hunks' added lines).
_DIFF_FILES: dict[str, tuple[str, str | None]] = {
    INSTALLATIONS_PATH: ("installations.diff", "installations.py.txt"),
    ATTESTER_PATH: ("attest_persona_surface_coherence.diff", None),
}

PROMPT_RULE = "prompt-rule"
STRUCTURAL = "structural"


class _FindingLike(Protocol):
    """What scoring reads from a finding: `llm_client.Finding`, or a
    recorded finding in the baseline."""

    @property
    def path(self) -> str: ...
    @property
    def line(self) -> int: ...
    @property
    def message(self) -> str: ...


@dataclass(frozen=True)
class MissedClassFixture:
    """One missed finding class, anchored on the real PR #751 head.

    `defect_lines` are inclusive new-side ranges on `path`; `signal_terms`
    are words a catching finding's message must contain at least one of
    (case-insensitive). `counterexample` is the concrete input that shows
    the defect. `reachability` is PROMPT_RULE or STRUCTURAL."""

    name: str
    finding_class: str
    path: str
    defect_lines: tuple[tuple[int, int], ...]
    signal_terms: tuple[str, ...]
    reference_finding: str
    counterexample: str
    reachability: str


_FIXTURES: tuple[MissedClassFixture, ...] = (
    MissedClassFixture(
        name="cross_hop_tpm_default_side_effect",
        finding_class="cross-hop-dataflow",
        path=INSTALLATIONS_PATH,
        # Hop 1, the payload default and the fields the diff adds beside
        # it; hops 2-3, the forwarding call through the enforcement toggle.
        defect_lines=((65, 76), (446, 475)),
        signal_terms=("tpm_enabled", "_toggle_enforcement", "paused"),
        reference_finding=(
            "Preserve `tpm_enabled` during sparse persona updates. A request "
            "containing only `sentinel_enabled` (or another new optional "
            "flag) still sends `tpm_enabled=True` from Line 65. This "
            "overwrites a previously paused repo and triggers enforcement "
            "because Line 471 reads the request default rather than the "
            "merged config."
        ),
        counterexample=(
            'PUT .../config with {"sentinel_enabled": false} on a repo stored '
            "with tpm_enabled=false: the payload default fills "
            "tpm_enabled=True, set_repo_config persists it, and "
            "now_enabled=True with was_enabled=False calls "
            "_toggle_enforcement(enable=True), creating a branch ruleset on "
            "the paused repo."
        ),
        reachability=STRUCTURAL,
    ),
    MissedClassFixture(
        name="weak_assertion_keyword_only_forwarding",
        finding_class="weak-assertion",
        path=ATTESTER_PATH,
        # `_set_repo_config_kwargs`, then the forwarding check in main().
        defect_lines=((119, 129), (168, 182)),
        signal_terms=("keyword", "body.", "constant"),
        reference_finding=(
            "Verify forwarded values match their payload fields. This records "
            "only keyword names. `set_repo_config(sentinel_enabled="
            "body.guard_enabled)` (or a constant) would pass CI even though "
            "Sentinel is not API-settable. Return each keyword's "
            "`body.<field>` attribute and assert it matches the flag name."
        ),
        counterexample="sentinel_enabled=body.guard_enabled",
        reachability=PROMPT_RULE,
    ),
    MissedClassFixture(
        name="reverse_direction_stale_ui_mapping",
        finding_class="reverse-direction-completeness",
        path=ATTESTER_PATH,
        # The reverse (dashboard -> registry) loop in main().
        defect_lines=((201, 207),),
        signal_terms=("stale", "orphan", "ghost", "no longer", "deleted", "removed"),
        reference_finding=(
            "Exclude stale mapping entries from reverse UI validation. The "
            "reverse check accepts any value in `REGISTRY_KEY_TO_UI_ID`, even "
            "if its key no longer exists in `registry_flags`. A stale entry "
            'such as `"removed_persona": "ghost"` lets a ghost dashboard '
            "persona pass. Build the allowed UI-ID set only from currently "
            "discovered registry keys."
        ),
        counterexample=(
            'REGISTRY_KEY_TO_UI_ID keeps "deleted_persona": "ghostcard" after '
            "its PersonaSpec is removed, and the dashboard still lists "
            "ghostcard: the reverse check passes."
        ),
        reachability=PROMPT_RULE,
    ),
)


def load_fixtures() -> tuple[MissedClassFixture, ...]:
    """The fixtures, validated: unique names (the baseline key), a known
    reachability, and a path the fixture data actually carries."""
    names = [f.name for f in _FIXTURES]
    if len(set(names)) != len(names):
        raise ValueError("missed-class fixture names must be unique (baseline key)")
    for f in _FIXTURES:
        if f.reachability not in (PROMPT_RULE, STRUCTURAL):
            raise ValueError(f"{f.name}: unknown reachability {f.reachability!r}")
        if f.path not in _DIFF_FILES:
            raise ValueError(f"{f.name}: no fixture diff for {f.path}")
    return _FIXTURES


def split_hunks(path: str, diff_text: str) -> list[Hunk]:
    """Split one file's hunk text (no `diff --git` header) at each `@@`."""
    bodies: list[list[str]] = []
    for line in diff_text.splitlines():
        if line.startswith("@@"):
            bodies.append([])
        if not bodies:
            raise ValueError(f"{path}: fixture diff does not start with a hunk header")
        bodies[-1].append(line)
    return [Hunk(path=path, body="\n".join(b)) for b in bodies]


def hunks_for(path: str) -> list[Hunk]:
    """The real reviewed-head hunks for `path`."""
    diff_name, _ = _DIFF_FILES[path]
    return split_hunks(path, (_FIXTURE_DIR / diff_name).read_text())


def head_content(path: str) -> str:
    """`path`'s content at the reviewed head: the snapshot, or for a new
    file the added lines of its single all-additions hunk."""
    _, content_name = _DIFF_FILES[path]
    if content_name is not None:
        return (_FIXTURE_DIR / content_name).read_text()
    body = hunks_for(path)[0].body.splitlines()[1:]
    return "".join(line[1:] + "\n" for line in body)


def pr751_evidence() -> ElderEvidence:
    """The evidence production assembles for the two defect-carrying files:
    their hunks plus their full head content."""
    return assemble_evidence(
        [h for path in _DIFF_FILES for h in hunks_for(path)],
        file_contents={path: head_content(path) for path in _DIFF_FILES},
    )


def caught(fixture: MissedClassFixture, findings: Iterable[_FindingLike]) -> bool:
    """True when some finding sits on the fixture's path, inside a defect
    range, and names the defect. Location alone is not a catch: an
    unrelated finding on the same lines must not score as the class."""
    terms = tuple(t.lower() for t in fixture.signal_terms)
    for f in findings:
        if f.path != fixture.path:
            continue
        if not any(lo <= f.line <= hi for lo, hi in fixture.defect_lines):
            continue
        if any(t in f.message.lower() for t in terms):
            return True
    return False


def score(
    fixtures: Iterable[MissedClassFixture], findings: Iterable[_FindingLike],
) -> dict[str, bool]:
    """{fixture name: caught} for one review's findings."""
    found = list(findings)
    return {f.name: caught(f, found) for f in fixtures}


def recall(caught_by_name: dict[str, bool]) -> float:
    """Caught fixtures / fixtures; 0.0 for an empty result."""
    if not caught_by_name:
        return 0.0
    return sum(caught_by_name.values()) / len(caught_by_name)


@dataclass(frozen=True)
class Replay:
    """One live replay. `errored` means the call or the parse failed, so
    the empty `findings` are not Elder's answer (honest-zero rule)."""

    findings: tuple[_FindingLike, ...]
    errored: bool


def replay(
    backend: BenchBackend,
    post: Callable[[BenchBackend, list[dict[str, str]]], httpx.Response] = _post,
) -> Replay:
    """Review the PR #751 evidence once with Elder's exact prompt and parser."""
    messages = _build_messages(pr751_evidence(), _PROMPT_VARIANT)
    try:
        resp = post(backend, messages)
    except (httpx.RequestError, httpx.TimeoutException) as e:
        log.warning(
            "missed_classes_replay_failed backend=%s kind=%s",
            backend.name, type(e).__name__,
        )
        return Replay(findings=(), errored=True)
    parsed = _parse_review(resp)
    if parsed.error and not parsed.findings:
        log.warning(
            "missed_classes_parse_failed backend=%s err=%s", backend.name, parsed.error,
        )
        return Replay(findings=(), errored=True)
    return Replay(findings=tuple(parsed.findings), errored=False)


def _record(name: str, model: str, result: dict[str, bool]) -> None:
    """Add or replace `name`'s measurement, keeping every other entry
    (the production measurement above all)."""
    baseline = json.loads(BASELINE_PATH.read_text())
    baseline["measurements"][name] = {
        "source": f"live replay via elder_eval.missed_classes, model {model}",
        "caught": result,
        "recall": recall(result),
    }
    BASELINE_PATH.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n")


def _print_result(name: str, result: dict[str, bool], found: Iterable[_FindingLike]) -> None:
    print(f"\n=== backend: {name} === recall {recall(result):.2f}")
    for fixture_name, hit in result.items():
        print(f"  {fixture_name:42s} {'CAUGHT' if hit else 'missed'}")
    for f in found:
        print(f"    finding {f.path}:{f.line} {f.message[:120]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="elder_eval.missed_classes")
    parser.add_argument("--record", action="store_true", help="record the run")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    backends = configured_backends()
    if not backends:
        print("No backends configured (see sast_benchmark.backends).", file=sys.stderr)
        return 2
    fixtures = load_fixtures()
    status = 0
    for backend in backends:
        run = replay(backend)
        if run.errored:
            print(f"\n!! backend {backend.name}: replay errored - not a result",
                  file=sys.stderr)
            status = 2
            continue
        result = score(fixtures, run.findings)
        _print_result(backend.name, result, run.findings)
        if args.record:
            _record(backend.name, backend.model, result)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
