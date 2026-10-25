"""Deterministic reviewability assessment produced before expensive review.

Promotes the existing cohort analysis (review_pipeline.plan_review) into a
stable, model-agnostic contract: an evidence-backed, multi-dimensional
assessment of how reviewable a PR is, plus a concrete decomposition plan
(suggested cohorts) for poorly bounded changes.

Pure, deterministic, no IO, no model calls. Every score is 0.0 (poor) to
1.0 (good) and carries a human-readable evidence string, so the assessment
reports dimensions rather than a single opaque score.

Generated, vendored, lockfile and mechanical changes are separated from
semantic changes before any dimension is computed, so a lockfile bump or a
vendored tree cannot drown out a real author change.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal, Protocol, Sequence

from review_pipeline import (
    DEFAULT_MAX_COHORT_CHARS,
    DEFAULT_MAX_COHORT_PATHS,
    ReviewCohort,
    ReviewPlan,
    plan_review,
)

ReviewabilityPolicy = Literal["warn", "block", "ignore"]
ReviewabilityVerdict = Literal["pass", "warn", "block"]

# Score bands. A dimension below POOR is a concrete reviewability defect.
_POOR = 0.40
_GOOD = 0.70


class _HunkLike(Protocol):
    """Minimum shape accepted by the assessment: a path and a diff body."""

    @property
    def path(self) -> str: ...

    @property
    def body(self) -> str: ...


# ── noise classification: generated / vendored / lockfile / mechanical ─────

_LOCKFILE_NAMES = {
    "poetry.lock",
    "package-lock.json",
    "yarn.lock",
    "cargo.lock",
    "Pipfile.lock",
    "uv.lock",
    "Gemfile.lock",
    "composer.lock",
    "go.sum",
}
_VENDOR_DIRS = frozenset({"vendor", "third_party", "node_modules", ".git"})
_GENERATED_DIRS = frozenset({"generated", "gen", "dist", "build", "out"})
_GENERATED_SUFFIXES = (
    ".pb.go",
    "_generated.py",
    ".gen.py",
    "-generated.",
    ".min.js",
    ".min.css",
)


def _path_parts(path: str) -> tuple[str, ...]:
    return PurePosixPath(path).parts


def _is_lockfile(path: str) -> bool:
    name = PurePosixPath(path).name
    low = name.lower()
    return name in _LOCKFILE_NAMES or low.endswith(".lock") or low == "go.sum"


def _is_vendored(path: str) -> bool:
    return any(part in _VENDOR_DIRS for part in _path_parts(path))


def _is_generated(path: str) -> bool:
    parts = _path_parts(path)
    name = parts[-1] if parts else ""
    low = name.lower()
    if any(part in _GENERATED_DIRS for part in parts):
        return True
    if any(low.endswith(suffix) for suffix in _GENERATED_SUFFIXES):
        return True
    return "generated" in low or low.endswith(".generated")


def _is_minified(body: str) -> bool:
    lines = body.splitlines()
    if not lines:
        return False
    longest = max(len(ln) for ln in lines)
    return longest > 500 and len(lines) < 10


def classify_hunk(path: str, body: str) -> tuple[bool, str]:
    """Return (is_noise, reason).

    A hunk is noise when it is generated, vendored, a lockfile, or
    mechanical (minified / bulk auto-format). The reason names the class so
    the operator can see exactly what was separated out.
    """
    if _is_lockfile(path):
        return True, "lockfile"
    if _is_vendored(path):
        return True, "vendored"
    if _is_generated(path):
        return True, "generated"
    if _is_minified(body):
        return True, "mechanical"
    return False, ""


# ── symbol / dependency extraction (best-effort, diff bodies) ──────────────

_SYMBOL_RE = re.compile(
    r"^\+\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)", re.MULTILINE
)
_IMPORT_RE = re.compile(
    r"^\+\s*(?:from\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s+import|import\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*))",
    re.MULTILINE,
)


def _extract_symbols(body: str) -> tuple[str, ...]:
    """Class/function names introduced by added lines, best-effort.

    Tries the AST first (added lines can still be a parseable fragment when
    the hunk is small) and falls back to a line scan of `+` lines.
    """
    names: list[str] = []
    try:
        tree = ast.parse(body)
    except (SyntaxError, ValueError):
        tree = None
    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(node.name)
        return tuple(dict.fromkeys(names))
    for match in _SYMBOL_RE.finditer(body):
        names.append(match.group(1))
    return tuple(dict.fromkeys(names))


def _extract_dependencies(body: str) -> tuple[str, ...]:
    """Module names imported by added lines, best-effort."""
    modules: list[str] = []
    for match in _IMPORT_RE.finditer(body):
        module = match.group(1) or match.group(2)
        if module:
            modules.append(module.split(".")[0])
    return tuple(dict.fromkeys(modules))


# ── dimensions ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ReviewabilityDimension:
    """One evidence-backed reviewability dimension.

    `score` is 0.0 (poor) to 1.0 (good). `evidence` is the human-readable
    justification. `paths` are the changed paths the dimension is about.
    """

    name: str
    score: float
    evidence: str
    paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SuggestedCohort:
    """One concrete cohort boundary for a poorly bounded change.

    `risk_reduced` explains what review risk the split removes, so the
    author understands *why* to cut here rather than where.
    """

    label: str
    paths: tuple[str, ...]
    symbols: tuple[str, ...]
    dependencies: tuple[str, ...]
    risk_reduced: str


@dataclass(frozen=True, slots=True)
class ReviewabilityAssessment:
    """The deterministic reviewability assessment for one PR.

    Computed before any model review. `reviewable` is the boolean gate:
    True when the change is bounded enough to review reliably. `decision`
    is the repository policy applied to `reviewable` (warn / block / pass
    / ignore) - it never refuses to review, only surfaces a verdict.
    """

    policy: ReviewabilityPolicy
    reviewable: bool
    decision: ReviewabilityVerdict
    dimensions: tuple[ReviewabilityDimension, ...]
    semantic_paths: tuple[str, ...]
    noise_paths: tuple[str, ...]
    suggested_cohorts: tuple[SuggestedCohort, ...]
    partitionable: bool
    overall: str


def _size_dimension(
    hunks: Sequence[_HunkLike], max_chars: int, max_paths: int
) -> ReviewabilityDimension:
    total_chars = sum(len(hunk.body) for hunk in hunks)
    total_paths = len({hunk.path for hunk in hunks})
    largest = max((len(hunk.body) for hunk in hunks), default=0)
    fit = total_chars <= max_chars and total_paths <= max_paths
    if fit:
        score = 1.0
    elif total_chars > max_chars * 4 or total_paths > max_paths * 8:
        score = 0.1
    else:
        score = 0.5
    evidence = (
        f"{total_paths} changed paths, {total_chars} diff chars, largest "
        f"hunk {largest} chars (budget {max_chars} chars / {max_paths} "
        f"paths per cohort). "
    )
    if not fit:
        evidence += "Exceeds one bounded review unit."
    return ReviewabilityDimension(
        name="size",
        score=score,
        evidence=evidence,
        paths=tuple(dict.fromkeys(hunk.path for hunk in hunks)),
    )


def _cohesion_dimension(hunks: Sequence[_HunkLike]) -> ReviewabilityDimension:
    """How tightly the changed files belong to one semantic unit.

    Scores by the largest semantic stem's concentration (do the changes
    cluster around one feature?) and test-to-implementation pairing. A
    coherent change clusters around one stem; a bundled change scatters
    unrelated stems. Top-level area is deliberately NOT the primary signal,
    since one feature legitimately spans schema, implementation, test and
    docs directories.
    """
    paths = [hunk.path for hunk in hunks]
    if not paths:
        return ReviewabilityDimension(
            name="cohesion",
            score=1.0,
            evidence="No changed paths.",
            paths=(),
        )

    stems: dict[str, list[str]] = {}
    for hunk in hunks:
        stems.setdefault(_semantic_stem(hunk.path), []).append(hunk.path)
    largest_stem = max(len(ps) for ps in stems.values())
    stem_concentration = largest_stem / len(paths)

    impl_stems = {
        stem
        for stem, ps in stems.items()
        if any(_layer(path) == "implementation" for path in ps)
    }
    paired = sum(1 for stem in impl_stems if _has_verification(hunks, stem))
    alignment = (paired / len(impl_stems)) if impl_stems else 1.0

    score = 0.6 * stem_concentration + 0.4 * alignment
    score = max(0.0, min(1.0, score))
    evidence = (
        f"changes span {len(stems)} semantic stem(s); the largest carries "
        f"{largest_stem} of {len(paths)} path(s); {paired} of "
        f"{len(impl_stems)} implementation stem(s) have a paired test. "
    )
    if score < _POOR:
        evidence += "Unrelated stems bundled together - low cohesion."
    elif score < _GOOD:
        evidence += "Partially cohesive."
    else:
        evidence += "Changes cluster around one semantic unit."
    return ReviewabilityDimension(
        name="cohesion",
        score=score,
        evidence=evidence,
        paths=tuple(paths),
    )


def _has_verification(hunks: Sequence[_HunkLike], stem: str) -> bool:
    for hunk in hunks:
        if _semantic_stem(hunk.path) == stem and _layer(hunk.path) == "verification":
            return True
    return False


def _cross_boundary_dimension(
    plan: ReviewPlan,
) -> ReviewabilityDimension:
    """Coupling across review boundaries: modules split across cohorts,
    proof units split across cohorts, and truncated plans."""
    paths = tuple(
        dict.fromkeys(path for cohort in plan.cohorts for path in cohort.paths)
    )
    kinds = {concern.kind for concern in plan.concerns}
    penalty = 0.0
    details: list[str] = []
    if "cross-cohort-module" in kinds:
        penalty += 0.35
        details.append("a module spans multiple cohorts")
    if "cross-cohort-proof" in kinds:
        penalty += 0.35
        details.append("implementation and verification split across cohorts")
    if plan.truncated:
        penalty += 0.30
        details.append(
            f"{plan.total_cohorts_planned - len(plan.cohorts)} cohorts unreachable"
        )
    score = max(0.0, 1.0 - penalty)
    if not details:
        evidence = "No module or proof unit is split across review boundaries."
    else:
        evidence = "; ".join(details) + "."
    return ReviewabilityDimension(
        name="cross_boundary_coupling",
        score=score,
        evidence=evidence,
        paths=paths,
    )


def _noise_dimension(
    semantic_paths: tuple[str, ...],
    noise_paths: tuple[str, ...],
) -> ReviewabilityDimension:
    total = len(semantic_paths) + len(noise_paths)
    if total == 0:
        return ReviewabilityDimension(
            name="generated_vendor_noise",
            score=1.0,
            evidence="No changed paths.",
        )
    noise_fraction = len(noise_paths) / total
    score = max(0.0, 1.0 - noise_fraction)
    if noise_fraction == 0:
        evidence = "All changes are semantic; no generated/vendor/lockfile noise."
    elif noise_fraction < 0.25:
        evidence = f"{len(noise_paths)} of {total} hunks are generated/vendor/lockfile/mechanical."
    else:
        evidence = (
            f"{len(noise_paths)} of {total} hunks are generated/vendor/lockfile/"
            f"mechanical ({noise_fraction:.0%} noise) - separated from semantic changes."
        )
    return ReviewabilityDimension(
        name="generated_vendor_noise",
        score=score,
        evidence=evidence,
        paths=noise_paths,
    )


def _test_alignment_dimension(hunks: Sequence[_HunkLike]) -> ReviewabilityDimension:
    """Whether every implementation change is accompanied by its test."""
    impl_stems: set[str] = set()
    verified_stems: set[str] = set()
    for hunk in hunks:
        stem = _semantic_stem(hunk.path)
        layer = _layer(hunk.path)
        if layer == "implementation":
            impl_stems.add(stem)
        elif layer == "verification":
            verified_stems.add(stem)
    if not impl_stems:
        return ReviewabilityDimension(
            name="test_to_change_alignment",
            score=1.0,
            evidence="No implementation changes.",
        )
    aligned = len(impl_stems & verified_stems)
    score = aligned / len(impl_stems)
    evidence = (
        f"{aligned} of {len(impl_stems)} implementation stem(s) have a paired test. "
    )
    if score == 1.0:
        evidence += "Every implementation change carries its verification."
    elif score < _POOR:
        evidence += "Most implementation changes lack a paired test."
    else:
        evidence += "Partially covered by tests."
    return ReviewabilityDimension(
        name="test_to_change_alignment",
        score=score,
        evidence=evidence,
        paths=tuple(
            sorted(
                {hunk.path for hunk in hunks if _layer(hunk.path) == "implementation"}
            )
        ),
    )


def _partitionability_dimension(plan: ReviewPlan) -> ReviewabilityDimension:
    """Whether the diff partitions into bounded review cohorts."""
    paths = tuple(
        dict.fromkeys(path for cohort in plan.cohorts for path in cohort.paths)
    )
    if not plan.cohorts:
        return ReviewabilityDimension(
            name="partitionability",
            score=1.0,
            evidence="Empty diff - trivially partitionable.",
        )
    if plan.truncated:
        score = 0.2
        evidence = (
            f"Planned {plan.total_cohorts_planned} cohorts but only "
            f"{len(plan.cohorts)} are reachable in one pass - too large to "
            f"review in full."
        )
    elif any(cohort.oversized for cohort in plan.cohorts):
        score = 0.35
        evidence = "At least one hunk is indivisible and exceeds a bounded review unit."
    else:
        score = 1.0
        evidence = f"Partitions into {len(plan.cohorts)} bounded review cohort(s)."
    return ReviewabilityDimension(
        name="partitionability",
        score=score,
        evidence=evidence,
        paths=paths,
    )


def _layer(path: str) -> str:
    lowered = path.lower()
    parts = PurePosixPath(lowered).parts
    name = parts[-1] if parts else lowered
    if any(
        part in {"schema", "schemas", "migration", "migrations", "types", "interfaces"}
        for part in parts
    ) or any(
        token in name for token in ("schema", "model", "types", "contract", "interface")
    ):
        return "contract"
    if any(
        part in {"test", "tests", "spec", "specs"} for part in parts
    ) or name.startswith(("test_", "spec_")):
        return "verification"
    if any(part in {"doc", "docs"} for part in parts) or name.endswith((".md", ".rst")):
        return "documentation"
    return "implementation"


def _semantic_stem(path: str) -> str:
    stem = PurePosixPath(path.lower()).stem
    for prefix in ("test_", "spec_"):
        if stem.startswith(prefix):
            stem = stem[len(prefix) :]
    for suffix in ("_test", "_spec", ".test", ".spec"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    return stem


# ── suggested cohorts ──────────────────────────────────────────────────────


def _cohort_risk_label(cohort: ReviewCohort, concerns: tuple) -> str:
    """One-line explanation of the risk reduced by reviewing this cohort alone."""
    layers = set(cohort.layers)
    if {"implementation", "verification"}.issubset(layers):
        return (
            "Keeping implementation and its verification in one bounded "
            "unit so behavior and its evidence are reviewed together."
        )
    if "contract" in layers and len(layers) > 1:
        return (
            "Isolating the contract change so consumers are reviewed "
            "separately from the implementations that depend on them."
        )
    if "verification" in layers and "implementation" not in layers:
        return (
            "Reviewing the verification change on its own so a test-only "
            "edit cannot mask a missing implementation change."
        )
    return "Reviewing this bounded slice in isolation keeps intent and context tight."


def _build_suggested_cohorts(
    plan: ReviewPlan,
    all_hunks: Sequence[_HunkLike],
) -> tuple[SuggestedCohort, ...]:
    """Turn the planner's cohorts into suggested, risk-ordered cohorts.

    Ordered high-risk first: cohorts that split implementation from
    verification, or couple multiple modules, lead so they receive context
    and budget before the low-risk remainder.
    """

    def risk_key(cohort: ReviewCohort) -> tuple[int, int, str]:
        layers = set(cohort.layers)
        high_risk = {"implementation", "verification"}.issubset(
            layers
        ) or "contract" in layers
        return (0 if high_risk else 1, -cohort.diff_chars, cohort.label)

    ordered = sorted(plan.cohorts, key=risk_key)
    cohorts: list[SuggestedCohort] = []
    for cohort in ordered:
        symbols: list[str] = []
        dependencies: list[str] = []
        for hunk in all_hunks:
            if hunk.path in cohort.paths:
                symbols.extend(_extract_symbols(hunk.body))
                dependencies.extend(_extract_dependencies(hunk.body))
        cohorts.append(
            SuggestedCohort(
                label=cohort.label,
                paths=cohort.paths,
                symbols=tuple(dict.fromkeys(symbols)),
                dependencies=tuple(dict.fromkeys(dependencies)),
                risk_reduced=_cohort_risk_label(cohort, plan.concerns),
            )
        )
    return tuple(cohorts)


def _overall_score(dimensions: tuple[ReviewabilityDimension, ...]) -> float:
    if not dimensions:
        return 1.0
    return sum(dim.score for dim in dimensions) / len(dimensions)


def _verdict_from_policy(
    reviewable: bool, policy: ReviewabilityPolicy
) -> ReviewabilityVerdict:
    if policy == "ignore":
        return "pass"
    if reviewable:
        return "pass"
    return "block" if policy == "block" else "warn"


def assess_reviewability(
    hunks: Sequence[_HunkLike],
    *,
    max_cohort_chars: int = DEFAULT_MAX_COHORT_CHARS,
    max_cohort_paths: int = DEFAULT_MAX_COHORT_PATHS,
    max_cohorts: int | None = None,
    policy: ReviewabilityPolicy = "warn",
) -> ReviewabilityAssessment:
    """Assess how reviewable one diff is, before any model review.

    Separates generated/vendor/lockfile/mechanical hunks from semantic ones,
    scores six evidence-backed dimensions, and proposes concrete cohort
    boundaries (high-risk first) for poorly bounded changes. Pure and
    deterministic.
    """
    semantic: list[_HunkLike] = []
    noise: list[_HunkLike] = []
    for hunk in hunks:
        is_noise, _reason = classify_hunk(hunk.path, hunk.body)
        (noise if is_noise else semantic).append(hunk)

    semantic_paths = tuple(dict.fromkeys(hunk.path for hunk in semantic))
    noise_paths = tuple(dict.fromkeys(hunk.path for hunk in noise))

    plan = plan_review(
        semantic,
        max_cohort_chars=max_cohort_chars,
        max_cohort_paths=max_cohort_paths,
        max_cohorts=max_cohorts,
    )

    dimensions = (
        _size_dimension(semantic, max_cohort_chars, max_cohort_paths),
        _cohesion_dimension(semantic),
        _cross_boundary_dimension(plan),
        _noise_dimension(semantic_paths, noise_paths),
        _test_alignment_dimension(semantic),
        _partitionability_dimension(plan),
    )

    partitionable = not plan.truncated and not any(
        cohort.oversized for cohort in plan.cohorts
    )
    cohesion_score = next(
        (dim.score for dim in dimensions if dim.name == "cohesion"), 1.0
    )
    reviewable = partitionable and cohesion_score >= _POOR

    decision = _verdict_from_policy(reviewable, policy)
    suggested = _build_suggested_cohorts(plan, semantic)
    overall = (
        "reviewable" if reviewable else "poorly bounded"
    ) + f" (overall {_overall_score(dimensions):.2f})"

    return ReviewabilityAssessment(
        policy=policy,
        reviewable=reviewable,
        decision=decision,
        dimensions=dimensions,
        semantic_paths=semantic_paths,
        noise_paths=noise_paths,
        suggested_cohorts=suggested,
        partitionable=partitionable,
        overall=overall,
    )


def dimension_score(assessment: ReviewabilityAssessment, name: str) -> float:
    """Read one dimension's score by name, or 1.0 when absent."""
    for dim in assessment.dimensions:
        if dim.name == name:
            return dim.score
    return 1.0
