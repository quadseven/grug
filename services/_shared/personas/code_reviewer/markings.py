"""Markings projection: every Elder PR surface rendered from plain data (#1591).

The check-run summary, the inline review (body and comments), and the
PR-timeline stack comment are all bodies computed here, with no GitHub call.
`dispatch.py` gathers the evidence, decides the verdict, and publishes; this
module only turns a finished pass into text.

Tier-1, the deep append and the ADR-0025 redelivery all reach the review and
the stack through `project(FinishedPass)`. Every input both surfaces read is
a required field of `FinishedPass`, so a path that forgets one fails as a
type error instead of rendering a quieter body. The deep path stores the
projected bodies in its outbox row, so a redelivery applies the same text.

The bodies carry the markers other code reads back: the board marker and
section delimiters the stack upsert merges by, the hidden `grug-rule` tag
the dedup keys on, and the hidden persona tag Elder withdraws its own
reviews by (#1581). A wording change here is a change to every one of them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Literal

from model_family import ReviewFamilies
from code_review_prompt import RULES
from github_checks_client import CheckConclusion
from github_reviews_client import InlineComment, ReviewEvent, ReviewResult
from llm_client import PrContext
from review_types import EFFORTS, SEVERITIES
from personas.review_publish import (
    PUBLISHABLE_DEGRADATIONS, is_blackout, is_low_confidence,
    is_partial_coverage, is_real_degradation,
)
from personas.code_reviewer.dedup import (
    dedup_findings, finding_key, rule_marker,
)
from personas.code_reviewer.precedent import (
    ClassPrecision, finding_is_muted, mute_note,
)
from personas import board
from personas.code_reviewer.suggestion import checked_suggestion
from personas.code_reviewer.docstrings import find_docstring_targets
from personas.code_reviewer.line_refs import repair_where_refs
from markdown_safety import neutralize_mentions
from personas.code_reviewer.repo_config import (
    RepoConfig,
    meets_inline_floor,
)
from personas.code_reviewer.linked_issue import (
    RULE_CATEGORIES as _LINKED_ISSUE_RULE_CATEGORIES,
)
from personas.code_reviewer.pinned_review import pin_line, pin_review
from personas.code_reviewer.persona import CodeReviewEvaluation, Finding
from personas.code_reviewer.degraded_gate import (
    gate_sentence, lead_with_degraded_line,
)

# Grug's face on the review comment. The code-reviewer persona IS Elder,
# so it leads with the Elder portrait — hosted at grug.lol/assets and
# rendered via an <img> (GitHub markdown allows width/align) so it's a little
# face, not a giant banner.
_PERSONA = "Elder"
_PERSONA_PORTRAIT = "https://grug.lol/assets/grug_elder.png"
# Hidden tag naming the persona that posted a review. Guard and Smasher build
# their reviews with `build_review_result` too, so the visible header cannot
# tell them apart; Elder withdraws only reviews carrying its own tag.
_REVIEW_PERSONA_TAG = "<!-- grug:review persona={} -->"


def _path_tail(paths: tuple, limit: int = 10) -> str:
    """The `(+N more)` tail for a truncated path list, or empty.

    Extracted because THREE transparency blocks - excluded, oversized and
    duplicate - each rebuilt this same ternary inline, and each one is a branch
    the complexity cap counts. #813's third block pushed _review_transparency to
    cyclomatic 17 against this repo's own cap of 15; folding the repeated shape
    into one place is the fix Elder's deterministic rule asked for.
    """
    return f" (+{len(paths) - limit} more)" if len(paths) > limit else ""


def _coverage_lines(coverage) -> str:
    """The coverage sentence plus any reviewability concerns.

    Split out of _review_transparency for the same cap: the concern loop and
    its heading marker are self-contained and were the densest branch cluster
    in a function that had five other independent blocks.

    "failed" and "never attempted" are reported separately (grug#939). They
    used to share one list, so a cohort the scheduler ran out of budget before
    reaching was announced to the author as a cohort that had been reviewed and
    broken. The two ask for opposite things - a re-run may fix the first and
    can never fix the second - and Elder must not imply it looked at ground it
    never opened.
    """
    unattempted = coverage.unattempted_cohorts
    ran_and_failed = tuple(
        index for index in coverage.failed_cohorts if index not in unattempted
    )
    parts = ""
    if ran_and_failed:
        parts += f"; failed: {', '.join(str(i) for i in ran_and_failed)}"
    if unattempted:
        parts += f"; never attempted: {', '.join(str(i) for i in unattempted)}"
    # Completed but cut off at the token cap: without this a capped review read
    # "N/N cohorts completed" and still failed as partial with no cause shown.
    capped = coverage.capped_cohorts
    if capped:
        parts += f"; hit the token cap: {', '.join(str(i) for i in capped)}"
    lines = (
        f"\n\nCoverage: {coverage.completed_cohorts}/{coverage.total_cohorts} "
        f"cohorts completed{parts}."
    ) + _coverage_file_lines(coverage)
    for concern in coverage.concerns:
        paths = ", ".join(f"`{_md_code_span(path)}`" for path in concern.paths[:6])
        lines += (
            f"\n- `{_md_code_span(concern.kind)}`: "
            f"{_defused(concern.message)} Paths: {paths}"
        )
    if coverage.concerns:
        marker = lines.index("\n- ", lines.index("Coverage:"))
        lines = f"{lines[:marker]}\n\n**Reviewability**{lines[marker:]}"
    return lines


_COVERAGE_PATHS_SHOWN = 10


def _path_list(paths: tuple[str, ...]) -> str:
    shown = ", ".join(f"`{_md_code_span(path)}`" for path in paths[:_COVERAGE_PATHS_SHOWN])
    return shown + _path_tail(paths, _COVERAGE_PATHS_SHOWN)


def _coverage_file_lines(coverage) -> str:
    """Which files were reviewed and which were not (grug#1196).

    "2/3 cohorts completed" told the author how much, never where. A file is
    reviewed only when every chunk holding part of it completed."""
    reviewed = tuple(coverage.reviewed_paths)
    unwalked = tuple(coverage.unwalked_paths)
    if not reviewed and not unwalked:
        return ""
    lines = f"\n\nFiles reviewed ({len(reviewed)}): " + (
        _path_list(reviewed) if reviewed else "none"
    ) + "."
    if unwalked:
        lines += f"\nFiles not reviewed ({len(unwalked)}): {_path_list(unwalked)}."
    return lines


_TITLE_MODELS_SHOWN = 4


def _tried_models_title(coverage) -> str:
    """": tried m1 (reason), m2 (reason)" for a neutral title, or empty.

    grug#1196: "eyes clouded (all_failed)" named no model and no cause, so a
    reader could not tell a rate limit from a dead host from a model that
    answered in prose."""
    models = tuple(coverage.failed_models) if coverage is not None else ()
    if not models:
        return ""
    shown = ", ".join(models[:_TITLE_MODELS_SHOWN])
    extra = len(models) - _TITLE_MODELS_SHOWN
    return f": tried {shown}" + (f" (+{extra} more)" if extra > 0 else "")


def _tried_models_note(coverage) -> str:
    """Every model tried and why it failed, for the neutral summary."""
    models = tuple(coverage.failed_models) if coverage is not None else ()
    if not models:
        return ""
    shown = ", ".join(f"`{_md_code_span(model)}`" for model in models[:12])
    return f"\n\nModels tried: {shown}{_path_tail(models, 12)}."


def _move_note(move_paths: tuple[tuple[str, str], ...]) -> str:
    """The one summary line naming relocated files (#772), or empty.

    Names each as `source -> destination` so the claim "unchanged" can be
    checked; a subtree source says `subtree <sha>:<path>`, the commit tree
    it came from, because that path does not exist in this repo. A moved
    file's edits are not here: they stay in review."""
    if not move_paths:
        return ""
    shown = ", ".join(
        f"`{src.replace(chr(96), '')}` -> `{dst.replace(chr(96), '')}`"
        for src, dst in move_paths[:10]
    )
    return (
        f"\n\nGrug did not review {len(move_paths)} moved file(s) as new "
        f"code - same content as the source, proven by git blob id: "
        f"{shown}{_path_tail(move_paths)}. Lines edited in a moved file are "
        f"still reviewed."
    )


def _review_transparency(
    evaluation: CodeReviewEvaluation,
    suppressed_count: int,
    excluded_paths: tuple[str, ...],
    oversized_paths: tuple[str, ...] = (),
    duplicate_paths: tuple[tuple[str, str], ...] = (),
    move_paths: tuple[tuple[str, str], ...] = (),
) -> str:
    lines = (
        f"\n\nGrug held back {suppressed_count} weak finding(s) his judge doubted."
        if suppressed_count
        else ""
    )
    if evaluation.dropped_withdrawn:
        # grug#1397: say how many candidates were dropped because the
        # reviewer's own text ruled them out, so the drop is never silent.
        lines += (
            f"\n\nGrug dropped {evaluation.dropped_withdrawn} candidate(s) "
            "the reviewer itself withdrew (its own verdict or text ruled them out)."
        )
    coverage = evaluation.coverage
    if coverage is not None:
        lines += _coverage_lines(coverage)
    if excluded_paths:
        shown = ", ".join(
            f"`{path.replace(chr(96), '')}`" for path in excluded_paths[:10]
        )
        more = _path_tail(excluded_paths)
        lines += (
            f"\n\nGrug not read {len(excluded_paths)} data/generated "
            f"file(s) - no meat for review there: {shown}{more}."
        )
    if oversized_paths:
        # Distinct sentence from the data/generated line above: these ARE
        # reviewable in principle, they just arrived as a single hunk too
        # big for one bounded cohort. Saying "no meat for review there"
        # would be a lie about a real code change.
        shown = ", ".join(
            f"`{path.replace(chr(96), '')}`" for path in oversized_paths[:10]
        )
        more = _path_tail(oversized_paths)
        lines += (
            f"\n\nGrug not read {len(oversized_paths)} file(s) whose change "
            f"landed as one hunk too big to hold in a single look: "
            f"{shown}{more}. Split the change to get eyes on it."
        )
    if duplicate_paths:
        # Named as PAIRS (#813 acceptance): the exclusion must say WHICH
        # path it duplicates, not just that it was skipped, or "byte-
        # identical" is an unverifiable claim.
        shown = ", ".join(
            f"`{path.replace(chr(96), '')}` (= `{original.replace(chr(96), '')}`)"
            for path, original in duplicate_paths[:10]
        )
        more = _path_tail(duplicate_paths)
        lines += (
            f"\n\nGrug not spend budget re-reading {len(duplicate_paths)} "
            f"file(s) byte-identical to another file in this diff or "
            f"already in the repo at this head: {shown}{more}. Any change "
            f"this diff makes to the original is still reviewed."
        )
    return lines + _move_note(move_paths)


def _clean_review_scope(
    living_range: str,
    excluded_paths: tuple[str, ...],
    oversized_paths: tuple[str, ...] = (),
    duplicate_paths: tuple[tuple[str, str], ...] = (),
    move_paths: tuple[tuple[str, str], ...] = (),
) -> str:
    if living_range:
        return (
            "Elder walked the delta diff (full file + cross-file + Omen "
            "runtime signal when mapped). No markings survived the judge. "
            "Code walk steady."
        )
    if excluded_paths or oversized_paths or duplicate_paths or move_paths:
        return (
            "Elder walked the reviewable diff (full file + cross-file + "
            "Omen when mapped), skipping data/generated paths listed "
            "below. No markings survived the judge on the reviewed paths. "
            "Code walk steady."
        )
    return (
        "Elder walked the whole diff (full file + cross-file + Omen "
        "runtime signal when mapped). No markings survived the judge. "
        "Code walk steady."
    )


def _review_phase_line(review_phase: Literal["tier1", "deep", "dual"]) -> str:
    if review_phase == "tier1":
        return (
            "Tier-1 coder-arm review on the Cave (reasoner may append later "
            "if escalated), graded by the judge, grounded in Lore when prior "
            "tribe history exists."
        )
    if review_phase == "deep":
        return (
            "Deep reasoner arm appended after Tier-1 completed, graded by "
            "the judge, grounded in Lore when prior tribe history exists."
        )
    return (
        "Dual-arm deep review (coder + reasoner on the Cave), graded by "
        "the judge, grounded in Lore when prior tribe history exists."
    )


def _findings_table(evaluation: CodeReviewEvaluation) -> str:
    rows = [
        "| Severity | Effort | File | Line | Rule | Marking |",
        "|---|---|---|---|---|---|",
    ]
    rows.extend(
        f"| {severity_chip(f.severity)} | {_effort_chip(f.effort)} | "
        f"`{_md_code_span(f.file)}` | {f.line} | "
        f"`{_md_code_span(f.rule_name)}` | {_md_table_cell(f.message)} |"
        for f in evaluation.findings if not _is_complexity_finding(f)
    )
    return "\n".join(rows)


def _is_complexity_finding(finding: Finding) -> bool:
    return finding.rule_name in {"high-complexity", "complexity-hotspot"}


def _maintainability_notes(evaluation: CodeReviewEvaluation) -> str:
    notes = [
        f"- `{_md_code_span(f.file)}:{f.line}` {_md_table_cell(f.message)}"
        for f in evaluation.findings if _is_complexity_finding(f)
    ]
    return "\n\n## Maintainability notes\n\n" + "\n".join(notes) if notes else ""


def _defensive_drop_note(count: int) -> str:
    if not count:
        return ""
    return f"\n\n{count} defensive findings dropped: no reachable caller named"


def _tier_served_text(tier_served: str, models_used: tuple[str, ...]) -> str:
    """The tier, then the models that answered when a reply named them."""
    models = ", ".join(f"`{_md_code_span(model)}`" for model in models_used)
    return f"`{tier_served}` ({models})" if models else f"`{tier_served}`"


def _tier_served_note(
    tier_served: str | None, models_used: tuple[str, ...] = (),
) -> str:
    if not tier_served:
        return ""
    return f"\n\nTier served: {_tier_served_text(tier_served, models_used)}."


_SAME_FAMILY_NOTE = (
    "same-family review: no arm of another model family answered, so this "
    "pull request was read by a model family that shares its author's blind spots."
)


def _family_line(families: ReviewFamilies) -> str:
    return (
        f"Author family: `{families.author}`. "
        f"Reviewer family: `{families.reviewer}`."
    )


def _family_note(families: ReviewFamilies | None) -> str:
    """Who wrote and who reviewed this pull request (grug#1437). Both names
    come from the closed family table, never from the pull request body."""
    if families is None:
        return ""
    note = f"\n\n{_family_line(families)}"
    return f"{note}\n\n{_SAME_FAMILY_NOTE}" if families.same_family else note


def _activity_families(families: ReviewFamilies | None) -> dict[str, str]:
    """The two family columns of an activity row; none without an author."""
    if families is None:
        return {}
    return {
        "author_family": families.author,
        "reviewer_family": families.reviewer,
    }


def _hunt_title(title: str, living_range: str) -> str:
    return f"Living Hunt {living_range} - {title}" if living_range else title


def _hunt_section(
    living_range: str, living_baseline_rejected: str = "",
    living_delta_empty: str = "",
) -> str:
    hunt = (
        (
            "\n\nLiving Hunt: reviewing `"
            + living_range
            + "` (files changed since the last completed Elder pass)."
        )
        if living_range
        else ""
    )
    if living_baseline_rejected:
        # #845: the stored baseline was rejected as a non-ancestor - the
        # range above is empty because no delta was reviewed, but the
        # fallback must be visible in the check-run, not just the logs.
        hunt += (
            "\n\nLiving Hunt: the stored baseline `"
            + living_baseline_rejected
            + "` was not an ancestor of the current head, or the range held a "
            "merge commit (history was rewritten or merged into) - Elder reviewed the full PR diff instead of the "
            "delta since the last pass."
        )
    if living_delta_empty:
        # grug#1644: an empty delta reviewed as the whole PR, not no_diff.
        hunt += (
            "\n\nLiving Hunt: no file changed in `"
            + living_delta_empty
            + "` since the last completed Elder pass, so Elder reviewed the "
            "full PR diff on this head instead of an empty delta."
        )
    return hunt


def _summary_markdown(
    evaluation: CodeReviewEvaluation,
    *,
    suppressed_count: int = 0,
    excluded_paths: tuple[str, ...] = (),
    oversized_paths: tuple[str, ...] = (),
    duplicate_paths: tuple[tuple[str, str], ...] = (),
    move_paths: tuple[tuple[str, str], ...] = (),
    living_range: str = "",
    living_baseline_rejected: str = "",
    living_delta_empty: str = "",
    review_phase: Literal["tier1", "deep", "dual"] = "dual",
    defensive_dropped_count: int = 0,
    blocking: bool = False,
    muted: dict[str, ClassPrecision] | None = None,
) -> tuple[str, str]:
    """Render a (title, summary) pair for the check-run output.

    Title is a one-liner status; summary is a Markdown table of findings
    by severity. Operators read this when triaging in GH's Checks tab.
    `suppressed_count` (#467) is how many weak findings the judge held back
    from publication - surfaced as a transparency line so a suppressed
    finding is never a silent gap.
    `living_range` (#557) when set is the prior..head delta Elder reviewed
    (Living Hunt) instead of the full PR base..head.
    `living_baseline_rejected` (#845) when set is the stored baseline SHA
    that turned out not to be an ancestor of head (rewritten history) -
    the delta was rejected and the full PR diff reviewed instead. Surfaced
    as a summary note so the fallback is visible, not silent.
    `living_delta_empty` (#1644) when set is a delta range that held no
    change, so the full PR diff was reviewed instead of publishing no_diff.
    `review_phase` (#646): tier1 = coder-only legend; deep = append legend;
    dual = both arms before publish (deep depth / rollback).
    `blocking` (grug#1212) is the repo's Elder mode. It only changes what a
    degraded summary says about the merge: in BLOCK mode a degraded review
    fails closed, so "merge not blocked" would be false there.
    """
    held = _review_transparency(
        evaluation, suppressed_count, excluded_paths, oversized_paths,
        duplicate_paths, move_paths,
    )
    defensive_note = _defensive_drop_note(defensive_dropped_count)
    tier_note = _tier_served_note(
        getattr(evaluation, "tier_served", None),
        getattr(evaluation, "models_used", ()),
    ) + _family_note(getattr(evaluation, "families", None))
    hunt = _hunt_section(
        living_range, living_baseline_rejected, living_delta_empty,
    )

    if is_partial_coverage(evaluation.degraded_reason):
        # Reached the PR again only after grug#806: this branch existed but
        # rerun.py's fail-open re-posted "Elder skipped - partial_review" over
        # it, so nobody ever saw it. Same event, same words as the board now.
        title = "WARN Elder review coverage partial"
        return _hunt_title(title, living_range), (
            "Grug walked part of the diff - some ground did not fit one look, "
            "or a bounded cohort returned nothing usable. Validated markings "
            "from the ground Grug walked are published below. "
            + gate_sentence(blocking)
            + " Coverage detail is in the table above."
        ) + held + hunt + defensive_note + tier_note
    if is_low_confidence(evaluation.degraded_reason):
        title = "WARN Elder review low confidence"
        return _hunt_title(title, living_range), (
            "Part of the diff got only a very short \"no findings\" answer "
            "that no other model confirmed, because every other model failed "
            "this pass. Grug keep the answer but not trust it. Validated "
            "markings from the rest are published below. "
            + gate_sentence(blocking)
        ) + held + hunt + defensive_note + tier_note
    if review_phase == "deep" and is_blackout(evaluation.degraded_reason):
        # grug#848: a blackout in the deep phase is the REASONER arm's -
        # `_async_deep_decision` never starts the deep append over a
        # blacked-out Tier-1 - so the generic "could not see the diff" below
        # would be false here: Tier-1 did see it and its verdict stands. The
        # news is that the second look Tier-1 promised ("reasoner may append
        # later if escalated") never happened, and this neutral is not the
        # reasoner agreeing.
        title = f"WARN Elder deep arm did not run ({evaluation.degraded_reason})"
        return _hunt_title(title, living_range), (
            "Tier-1 coder-arm review completed and its markings stand. The "
            "deep reasoner arm promised on escalation did not run this pass "
            f"- the mist: `{evaluation.degraded_reason}`. Grug not say the "
            "deeper look agreed; it never looked. "
            + gate_sentence(blocking)
        ) + held + hunt + defensive_note + tier_note
    if evaluation.degraded_reason:
        # grug#985: when some cohorts were never opened (starved of budget),
        # the headline must not read like every cohort ran and broke.
        starved = (
            evaluation.coverage is not None
            and bool(evaluation.coverage.unattempted_cohorts)
        )
        reason = evaluation.degraded_reason + _tried_models_title(evaluation.coverage)
        title = (
            f"WARN Grug eyes clouded ({reason}; some cohorts never attempted)"
            if starved
            else f"WARN Grug eyes clouded ({reason})"
        )
        starved_note = (
            "\n\nSome cohorts were never opened - the review budget ran out "
            "first - so part of this is not a broken read."
            if starved
            else ""
        )
        return _hunt_title(title, living_range), (
            "Grug Elder could not see the diff this pass. The mist: "
            f"`{evaluation.degraded_reason}`. "
            + gate_sentence(blocking)
        ) + _tried_models_note(evaluation.coverage) + starved_note + held + hunt + defensive_note + tier_note
    if not evaluation.findings:
        title = (
            "Elder clear - no markings"
            if not suppressed_count
            else "Elder clear - weak markings held back"
        )
        scope = _clean_review_scope(
            living_range, excluded_paths, oversized_paths, duplicate_paths,
            move_paths,
        )
        return _hunt_title(title, living_range), ("## Markings Board\n\n" + scope) + held + hunt + defensive_note + tier_note

    blocking = sum(1 for f in evaluation.findings if f.severity in ("high", "critical"))
    title = f"Elder markings - {blocking} blocking, {len(evaluation.findings)} total"
    title = _hunt_title(title, living_range)
    table = _findings_table(evaluation)
    phase_line = _review_phase_line(review_phase)
    legend = (
        "## Markings Board\n\n"
        f"{phase_line} "
        "Inline comments carry Fix + agent prompt on each marking.\n\n"
    )
    muted_note = _muted_lines(evaluation.findings, muted)
    if muted_note:
        muted_note = f"\n\n{muted_note}"
    summary = f"{legend}{table}{_maintainability_notes(evaluation)}{muted_note}{held}{hunt}{defensive_note}{tier_note}"
    agent = _consolidated_agent_prompt(evaluation)
    if agent:
        summary = f"{summary}\n\n{agent}"
    return title, summary


def _intent_paragraph(pr_context: PrContext) -> str:
    """Place the stated change and linked-issue comparison first."""
    title = re.sub(r"\s+", " ", str(pr_context.get("title") or "")).strip()
    body = re.sub(r"\s+", " ", str(pr_context.get("body") or "")).strip()
    body_sentence = re.split(r"(?<=[.!?])\s+", body, maxsplit=1)[0][:240]
    if body_sentence and body_sentence[-1] not in ".!?":
        body_sentence += "."
    intent = ". ".join(part.rstrip(".!?") for part in (title, body_sentence) if part)
    if intent:
        intent += ". "
    else:
        intent = "Change intent was not provided. "
    sensitive = bool(pr_context.get("sensitive_review"))
    if pr_context.get("linked_issue_context"):
        assessment = "Linked acceptance criteria were checked; mismatches are reported as high intent-mismatch findings"
    else:
        assessment = "No linked issue acceptance criteria were available for comparison"
    if sensitive:
        assessment += "; this PR is class:sensitive and received full review depth"
    return f"## Intent\n\n{intent}{assessment}.\n\n"


# GitHub caps check-run summaries at 65536 chars; the findings table is
# unbounded (message-length x count), so the consolidated prompt gets a
# fixed budget well under the cap and truncates by WHOLE findings.
_CONSOLIDATED_PROMPT_BUDGET = 8000


def _consolidated_agent_prompt(evaluation: CodeReviewEvaluation) -> str:
    """One copy-paste prompt covering the findings (#553), deterministic
    and bounded. Truncates by whole findings and SAYS how many were cut -
    a silently-partial prompt would read as the complete work list.

    Returns empty string when there are no findings — never emit a hollow
    "Address each finding below" shell with nothing under it.
    """
    findings = tuple(f for f in evaluation.findings if not _is_complexity_finding(f))
    if not findings:
        return ""

    header = [
        _AGENT_META_PREAMBLE,
        "",
        "Address each finding below.",
    ]

    body: list[str] = []
    used = sum(len(x) + 1 for x in header)
    included = 0
    for f in findings:
        cat = _category_for_finding(f)
        entry = (
            f"- {_md_code_span(f.file)}:{f.line} "
            f"[{severity_chip(f.severity)} | {cat} | "
            f"{_md_code_span(f.rule_name)}] {f.message}"
        )
        if f.suggestion:
            entry += f"\n  Suggested fix: {f.suggestion}"
        if used + len(entry) + 1 > _CONSOLIDATED_PROMPT_BUDGET:
            break
        body.append(entry)
        used += len(entry) + 1
        included += 1
    cut = len(findings) - included
    if cut:
        body.append(
            f"(+{cut} more finding(s) - see the findings table above)"
        )
    block = _details_block(
        "Prompt for AI agents (all findings)", "\n".join(header + body)
    )
    # Hard deterministic ceiling: fence growth (backtick-run + 1, twice)
    # and the wrapper are not in the per-entry budget, so cap the WHOLE
    # block - an oversized prompt must degrade loudly, never 422 the
    # check-run publish.
    if len(block) > 2 * _CONSOLIDATED_PROMPT_BUDGET:
        return "(Prompt for AI agents omitted - findings too large; see the table above)"
    return block


# Closed caveman-inspired chrome chips (FLINT-density scanability).
# Identifiers and check *names* stay plain ASCII; Markings surface uses these.
#
# These two are hand-written, so they can drift from the shared vocabulary.
# `_chip_vocabulary_drift()` is what stops that, and it is asserted in tests
# against SEVERITIES/EFFORTS - a level added to review_types but not given a
# chip here would otherwise fall through `.get()` to the bare identifier, and
# nothing would say so.
_SEVERITY_CHIP: dict[str, str] = {
    "critical": "💀 critical",
    "high": "🔥 high",
    "medium": "🟠 medium",
    "low": "👁 low",
}


_EFFORT_CHIP: dict[str, str] = {
    "quick-win": "⚡ quick win",
    "heavy-lift": "🪨 heavy lift",
}


def _chip_vocabulary_drift() -> dict[str, frozenset[str]]:
    """Vocabulary levels with no chip. Empty dict means no drift.

    Returned rather than asserted at import so a drifted chip table degrades
    to the bare identifier in production (ugly) instead of refusing to import
    dispatch (fatal). The test is where it must fail loudly.
    """
    return {
        k: v for k, v in {
            "severity": SEVERITIES - set(_SEVERITY_CHIP),
            "effort": EFFORTS - set(_EFFORT_CHIP),
        }.items() if v
    }


def severity_chip(severity: str) -> str:
    return _SEVERITY_CHIP.get(severity, severity)


def _effort_chip(effort: str | None) -> str:
    if not effort:
        return "-"
    return _EFFORT_CHIP.get(effort, effort.replace("-", " "))


# Markings v2: rule_name -> ReviewRule for category (bug_class) chips.
_RULES_BY_NAME = {r.name: r for r in RULES}


# Impact one-liners by bug_class (closed taxonomy in code_review_prompt).
_WHY_IT_MATTERS: dict[str, str] = {
    "silent failure": (
        "Errors swallowed hide real failures and make outages hard to debug."
    ),
    "correctness": (
        "Logic bugs ship wrong behavior to users and are expensive to reverse."
    ),
    "async blocker": (
        "Blocking work on async paths freezes event loops and stalls requests."
    ),
    "concurrency": (
        "Race conditions are intermittent, hard to reproduce, and production-only."
    ),
    "test fidelity": (
        "Tests that do not match production behavior give false confidence."
    ),
    "robustness": (
        "Missing guards turn edge cases into crashes under real load."
    ),
    "security": (
        "Security findings can be exploited; treat high/critical before merge."
    ),
    "type design": (
        "Weak types let invalid states compile and fail later at runtime."
    ),
    "maintainability": (
        "Hard-to-follow code slows every future change and hides more bugs."
    ),
    "test coverage": (
        "Unguarded paths regress silently; coverage gaps become merge risk."
    ),
    "performance": (
        "Hot-path waste compounds under concurrency and burns latency budget."
    ),
    "requirements": (
        "A PR that misses or contradicts its own issue's criteria ships the "
        "wrong thing even when the code is clean."
    ),
}


# CR-style agent contract: deterministic, no extra LLM call.
_AGENT_META_PREAMBLE = (
    "Verify each finding against the current code. Fix only if still valid; "
    "skip with a brief reason if already fixed or not applicable. Keep every "
    "change minimal and scoped to the named file/line; do not refactor beyond "
    "the finding. Validate after applying (tests or a focused check)."
)


# Upsert-by-marker issue comment for the Elder review stack (PR timeline).
# The comment Elder finds/upserts is now the shared BOARD (#791), not an
# Elder-private one. Locating by the BOARD marker is what lets Chief and the
# others edit the SAME comment instead of each posting their own - which is
# what turned one review into three emails.
#
# Legacy marker kept for LOCATION only: PRs reviewed before this change carry
# an elder-stack comment, and a fresh board next to it would be the duplicate
# this exists to prevent. Found -> rewritten in place as a board.
_STACK_MARKER = board.BOARD_MARKER


_LEGACY_STACK_MARKER = "<!-- grug-elder-stack -->"


def _category_for_rule(rule_name: str) -> str:
    """Display category from the RULES table; unknown rules stay general."""
    rule = _RULES_BY_NAME.get(rule_name)
    if rule is not None:
        return rule.bug_class
    return _LINKED_ISSUE_RULE_CATEGORIES.get(rule_name, "general")


def _category_for_finding(f: Finding) -> str:
    """The finding's taxonomy category (#683) - the same value the ledger
    row and the run store carry. An unset one is "general" (criterion 4),
    never the rule-derived class, so every surface agrees."""
    return f.category


def _why_it_matters(rule_name: str) -> str:
    cat = _category_for_rule(rule_name)
    return _WHY_IT_MATTERS.get(
        cat,
        "Left unfixed, this can become user-visible breakage or review debt.",
    )


def _details_block(summary: str, content: str) -> str:
    """The one <details> scaffold for agent prompts - the blank lines
    around the fence are load-bearing for GitHub rendering, so both
    surfaces share this instead of hand-building drift-prone copies."""
    return "\n".join(
        ["<details>", f"<summary>{summary}</summary>", "", _fenced(content), "", "</details>"]
    )


def _defused(prose: str) -> str:
    """Neutralize fence-capable runs and mentions in PROSE surfaces
    (comment head, table cells): an unterminated ``` or ~~~ in a model
    message would open a fence that swallows the rest of the body -
    including the dedup marker and the suggestion block. Inline code
    spans (1-2 backticks) render untouched.

    Mentions are broken too (#709): a marking posts with the app's own
    authority, so a live `@user` in model-authored prose would notify a
    real account. Fenced payloads (the suggestion block, the agent prompt)
    do not pass through here: a fence renders no mention, and the
    suggestion must stay byte-exact to commit cleanly."""
    out = re.sub(r"`{3,}", "``", prose)
    return neutralize_mentions(re.sub(r"~{3,}", "~~", out))


def _md_code_span(text: str) -> str:
    """Sanitize text for a single backtick-wrapped inline code span.

    Paths and rule names are model-controlled: strip backticks and collapse
    newlines so they cannot terminate the span or inject a second line.
    """
    cleaned = (text or "").replace("`", "")
    cleaned = cleaned.replace('\r', " ").replace('\n', " ")
    cleaned = re.sub(r" +", " ", cleaned).strip()
    return cleaned or "?"


def _md_table_cell(text: str) -> str:
    """Escape review-controlled prose for a GitHub Markdown table cell.

    Pipes break column structure; newlines break the row. Also run the
    prose defuser so an unterminated fence in a finding message cannot
    swallow the rest of the Markings Board.
    """
    cleaned = _defused(text or "")
    cleaned = cleaned.replace("|", '\\|')
    cleaned = cleaned.replace('\r', " ").replace('\n', " ")
    cleaned = re.sub(r" +", " ", cleaned).strip()
    return cleaned


def _fenced(text: str) -> str:
    """Wrap text in a code fence GUARANTEED to contain it: the fence is one
    backtick longer than the longest backtick run inside (CommonMark).
    Model-supplied text with ``` must never break out of the block and
    render live markdown (links, @-mentions that ping) inside an agent
    prompt or the check-run summary."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"


def _agent_prompt_block(f: Finding) -> str:
    """The copy-paste remediation prompt (#553), assembled DETERMINISTICALLY
    from finding fields - no extra LLM call, so it can never hallucinate
    beyond what the finding already claims. Markings v2 adds a CR-style
    verify/skip/minimal meta contract shared with the consolidated prompt."""
    cat = _category_for_finding(f)
    content = [
        _AGENT_META_PREAMBLE,
        "",
        f"In `{f.file}:{f.line}` address this {f.severity} / {cat} finding "
        f"(`{f.rule_name}`):",
        f.message,
        f"Why it matters: {_why_it_matters(f.rule_name)}",
        "Fix focus: change only what the finding names; keep the fix "
        "minimal and line-exact; do not refactor beyond it.",
    ]
    if f.suggestion:
        content += ["Suggested fix:", f.suggestion]
    return _details_block("Prompt for AI agents", "\n".join(content))


def _provenance_block(f: Finding) -> str:
    """Render bounded, immutable discovery scope for a finding."""
    if not f.origins:
        return ""
    lines: list[str] = []
    seen: set[tuple] = set()
    for origin in f.origins:
        key = (
            origin.backend,
            origin.model,
            origin.cohort_index,
            origin.cohort_count,
            origin.evidence_paths,
            origin.head_sha,
        )
        if key in seen:
            continue
        seen.add(key)
        scope = ""
        if origin.cohort_index is not None and origin.cohort_count is not None:
            scope = f"; cohort {origin.cohort_index}/{origin.cohort_count}"
        sha = f"; head `{_md_code_span(origin.head_sha[:12])}`" if origin.head_sha else ""
        paths = ", ".join(
            f"`{_md_code_span(path)}`" for path in origin.evidence_paths[:8]
        )
        path_note = f"; evidence paths: {paths}" if paths else ""
        lines.append(
            f"- `{_md_code_span(origin.model)}` ({origin.backend.value}){scope}{sha}{path_note}"
        )
    return _details_block("Evidence and provenance", "\n".join(lines))


def _inline_comment_body(
    f: Finding, precedent_note: str = "", *, committable: bool = True,
) -> str:
    """Format one finding as a structured Marking (#553 / #617 / Markings v2).

    Shape (Markings Board):
      - severity · category · rule · effort chip
      - What Elder sees (the finding)
      - Why it matters (taxonomy impact one-liner)
      - Where (file:line, always)
      - Fix (committable suggestion when safe, else fenced prose)
      - Lore (precedent + measured confidence when the ledger has history)
      - Prompt for AI agents (CR-style verify + copy-paste repair brief)

    Appends a hidden `grug-rule` marker (rendered invisibly by GitHub)
    so a later `synchronize` push can recognise this comment as a Grug
    finding for dedup (#189) — see dedup.parse_rule. The marker stays
    LAST (dedup.parse_rule reads the last marker in the body).

    `committable=False` renders any suggestion as fenced prose: dispatch
    passes it when `checked_suggestion` (#1149) could not verify the fix."""
    cat = _category_for_finding(f)
    # CR-dense header: severity chip | category | rule | effort chip
    chip = (
        f"{severity_chip(f.severity)} | _{_md_code_span(cat)}_ | "
        f"`{_md_code_span(f.rule_name)}`"
    )
    if f.effort:
        chip += f" | {_effort_chip(f.effort)}"
    head = (
        f"{chip}\n\n"
        # grug#1398 publish-time check: a Where the model wrote into the
        # message names the anchor, the same line the footer renders.
        f"**What Elder sees**\n\n"
        f"{_defused(repair_where_refs(f.message, f.file, f.line))}\n\n"
        f"**Why it matters**\n\n{_defused(_why_it_matters(f.rule_name))}\n\n"
        f"**Where:** `{_md_code_span(f.file)}:{f.line}`"
    )
    if precedent_note:
        # #555: ledger-grounded citation + measured-confidence chip, as a
        # blockquote under the message. _defused() neutralizes any user text
        # that reached the note via file paths; the note itself is our own
        # rendered string.
        head += f"\n\n**Lore**\n\n> {_defused(precedent_note)}"
    # strip wrapping NEWLINES only (not spaces): GitHub commits the block
    # verbatim as the full replacement line, so leading indentation must
    # survive, but a bare "\n\n\n" suggestion must not slip through as a
    # blank-line commit (FLINT finding on #558).
    stripped_suggestion = f.suggestion.strip("\n\r") if f.suggestion else ""
    if (
        committable
        and f.suggestion
        and "```" not in f.suggestion
        and "\n" not in f.suggestion.strip()
        and "\r" not in stripped_suggestion
        and stripped_suggestion
    ):
        # GitHub-native committable block - one click REPLACES the single
        # anchored line. Committable ONLY when the suggestion is itself
        # single-line and fence-safe: the comment anchors one line, so a
        # multi-line suggestion applied there duplicates the following
        # original lines - confident-looking one-click corruption. A bare
        # CR is a line ending to CommonMark, so it counts as multi-line
        # (#709), the same rule as suggestion._single_line.
        body = (
            f"{head}\n\n**Fix** (one-click, line-exact):\n"
            f"```suggestion\n{stripped_suggestion}\n```"
        )
    elif f.suggestion:
        # Multi-line or fence-bearing: fenced prose with an explicit scope
        # label. _fenced() contains ANY payload (a suggestion containing
        # ```suggestion would otherwise render as a live committable block
        # - the sanitizer must not route the payload around itself).
        body = (
            f"{head}\n\n**Fix** "
            f"(anchored at line {f.line} - verify scope before applying):\n"
            f"{_fenced(f.suggestion)}"
        )
    else:
        body = head
    provenance = _provenance_block(f)
    if provenance:
        body += f"\n\n{provenance}"
    function_marker = (
        f"\n<!-- grug-complexity-function:{f.complexity_function} -->"
        if f.complexity_function
        and re.fullmatch(r"[A-Za-z0-9_-]+", f.complexity_function)
        else ""
    )
    return f"{body}\n\n{_agent_prompt_block(f)}\n\n{rule_marker(f.rule_name)}{function_marker}"


# Rows of the findings table carried in the board body. Ten, because the body
# IS the email and <details> does not fold in Gmail (see _review_stack_body).
_STACK_TABLE_ROWS = 10


# Degradations the durable lane re-drives (rerun's `_RETRYABLE_SKIP_REASONS`
# imports this). Defined HERE because the email decision needs it and
# `_shared` is the lower layer - rerun already imports from this module.
RETRIED_DEGRADATIONS = frozenset({
    "all_failed", "parse_failed", "fetch_or_parse_failed",
})


def worth_an_email(evaluation: CodeReviewEvaluation) -> bool:
    """Should a review that found THIS be allowed to create the board?

    Creating a comment mails the author; editing one does not. So this is the
    question "is there news?", and a clean review is not news - the green
    check-run already says so, in the place people look for it.

    A REAL degradation is news in the other direction: "Grug could not review
    this" is precisely the case where silence would be misread as approval.
    `no_diff` is not that - nothing to review is not a failure to review.

    But a degradation the durable lane will RETRY is not news yet, whatever
    else this pass happened to collect. Measured on a PR in the infrastructure
    repo: the LLM half was cancelled mid-flight (`all_failed`), the deterministic
    complexity scanner still produced one finding, and that finding was enough
    to create the board - mailing "Grug eyes cloudy this pass - read for self"
    plus a cyclomatic nitpick on a PR fixing a packet-mode bootstrap deadlock.
    The retry six minutes later came back clean from the Cave, but GitHub does
    not mail an EDIT, so that inbox keeps the degraded verdict permanently.

    Deterministic findings are not lost by waiting: the retry re-runs the same
    scanners and republishes them alongside whatever the LLM finds.

    The trade is explicit. If every retry also fails, the author gets no board
    at all - but the check-run still carries the degraded verdict (rerun
    fail-opens it to neutral rather than leaving it in_progress), so the
    signal survives in the place a required check is read. A false "Grug could
    not see" is worse than a quiet one, because it trains the author to
    discount the surface entirely.
    """
    if evaluation.degraded_reason in RETRIED_DEGRADATIONS:
        return False
    return bool(evaluation.findings) or is_real_degradation(
        evaluation.degraded_reason,
    )


def _stack_detail_lines(
    review_phase: str, living_range: str, suppressed_count: int,
    head_sha: str = "", base_sha: str = "",
    tier_served: str | None = None,
    families: ReviewFamilies | None = None,
    models_used: tuple[str, ...] = (),
) -> str:
    """The two facts a skeptical reader asks for: what was looked at, and what
    was withheld. Everything else that used to live here was cut as noise.

    #673: "looked at" used to appear ONLY when a Living Hunt delta existed, so
    a full base..head review - the common path, and every first review of a
    PR - answered "what did you read" with nothing. Both cases now disclose.
    """
    lines = []
    if review_phase == "tier1":
        # Not jargon for its own sake: a clear tier-1 pass is PROVISIONAL, and
        # a reader who does not know that will read it as final.
        lines.append("- Fast look only - deep look may add more.")
    # ONE implementation of "what did you read", in board.review_scope_line.
    # This branch used to re-derive it with its own wording and its own
    # `head_sha[:7]`, so the same fact appeared in two vocabularies.
    scope = board.review_scope_line(
        living_range=living_range, base_sha=base_sha, head_sha=head_sha,
    )
    if scope:
        lines.append(f"- Looked at: {scope}")
    if tier_served:
        lines.append(f"- Tier served: {_tier_served_text(tier_served, models_used)}")
    if families is not None:
        lines.append(f"- {_family_line(families)}")
        if families.same_family:
            lines.append(f"- {_SAME_FAMILY_NOTE}")
    if suppressed_count:
        lines.append(f"- Grug swallow {suppressed_count} weak thought(s).")
    return "\n".join(lines)


def _stack_status_line(
    evaluation: CodeReviewEvaluation, n: int, sev_bits: str,
    conclusion: str = "neutral",
) -> str:
    """The board section's one-line status.

    Extracted from `_review_stack_body` when adding the partial-coverage
    branch pushed that function to cyclomatic 17 against a cap of 15 (caught
    by Elder on grug#807). The coverage-vs-findings decision is a self
    contained question, so it reads better named than inlined."""
    if is_partial_coverage(evaluation.degraded_reason):
        return (
            f"Partial coverage - {n} marking(s) from the ground Grug walked"
            if n
            else "Partial coverage - no markings on the ground Grug walked"
        )
    if is_low_confidence(evaluation.degraded_reason):
        return (
            f"Low confidence - {n} marking(s); some ground got only a short empty answer"
            if n
            else "Low confidence - only a short empty answer, no other model confirmed it"
        )
    if is_real_degradation(evaluation.degraded_reason):
        # grug#1212: in BLOCK mode the degraded check fails closed, so
        # "advisory only" would tell the author the opposite of the gate.
        gate = (
            "check fails until a full review lands"
            if conclusion == "failure"
            else "advisory only"
        )
        return f"Degraded (`{evaluation.degraded_reason}`) - {gate}"
    if n == 0:
        return "Clear - no markings published"
    return f"**{n} actionable marking(s)** ({sev_bits})"


_UNWALKED_PATHS_SHOWN = 8


def _unwalked_ground_note(evaluation: CodeReviewEvaluation) -> str:
    """Name the files a partial pass did not finish. "Some ground not
    walked" with no location gave the reader nothing to act on - they could
    neither check those files themselves nor tell whether it mattered."""
    paths = evaluation.coverage.unwalked_paths if evaluation.coverage else ()
    if not paths:
        return (
            "Some ground not walked this pass - part of the diff did not fit "
            "one look. What Grug did walk is above. Grug not say trail safe "
            "for ground Grug not walk."
        )
    shown = ", ".join(f"`{p}`" for p in paths[:_UNWALKED_PATHS_SHOWN])
    extra = len(paths) - _UNWALKED_PATHS_SHOWN
    more = f" (+{extra} more)" if extra > 0 else ""
    return (
        f"Grug did not finish walking {shown}{more}. Markings above cover "
        "the rest. Grug not say trail safe in those files - comment "
        "`/grug improve` to walk them again."
    )


def _stack_closing_note(
    evaluation: CodeReviewEvaluation, *, has_committable: bool,
) -> list[str]:
    """The board section's tail: what to do next, or why there is nothing.

    Exactly one of these applies, and a clean non-degraded pass gets NOTHING -
    it used to get "No agent prompt - nothing to remediate.", the fifth
    separate way one body said "no findings".

    `has_committable` means a finding's own suggestion passed
    `checked_suggestion`, the same check `/grug fix` runs, so the command is
    named only when it can commit."""
    agent = _consolidated_agent_prompt(evaluation)
    if agent:
        # Only when there is something to fix - empty "Address each finding"
        # shells are noise (and look broken).
        return [
            "",
            agent,
            "",
            "---",
            "",
            "Inline comments carry Fix + agent prompt on each marking. "
            + (
                "Comment `/grug fix` to commit the open suggestions, or hand "
                "the agent prompt to your coding agent."
                if has_committable
                else "Hand the agent prompt to your coding agent."
            ),
            "",
        ]
    if is_partial_coverage(evaluation.degraded_reason):
        # Elder DID review - just not all of it. "Grug could not see" would
        # discard real work and read as a tool failure.
        return ["", "---", "", _unwalked_ground_note(evaluation), ""]
    if is_low_confidence(evaluation.degraded_reason):
        return [
            "",
            "---",
            "",
            (
                "Some ground got only a very short \"no findings\" answer and "
                "no other model answered. Grug not say trail safe there."
            ),
            "",
        ]
    if is_real_degradation(evaluation.degraded_reason):
        # Degraded with empty findings is not a clean review, and the one thing
        # that must never happen is a reader taking it for one.
        return [
            "",
            "---",
            "",
            "Review degraded - no usable findings were produced. "
            "Grug not say trail safe. Grug say Grug could not see.",
            "",
        ]
    return []


def _docstring_offer(
    diff_text: str, file_contents: dict[str, str] | None,
) -> dict[str, tuple[Any, ...]]:
    """`_review_stack_body` keywords for the docstring offer. Every build of
    the stack goes through here: each one replaces the whole comment, so a
    build without them would erase the offer and its skip reasons."""
    targets, skips = find_docstring_targets(diff_text, file_contents or {})
    return {
        "docstring_targets": tuple(f"{t.path}:{t.line}" for t in targets),
        "docstring_skips": tuple(skips),
    }


def _docstring_offer_lines(
    targets: tuple[str, ...], skips: tuple[tuple[str, str], ...],
) -> list[str]:
    """The stack's docstring offer and every candidate skipped with its reason."""
    lines: list[str] = []
    if targets:
        lines.append(
            f"**Finishing touch:** {len(targets)} changed public "
            "function(s) lack docstrings. Run `/grug docstrings` to generate them."
        )
    if skips:
        skipped = "; ".join(
            f"`{_md_code_span(path)}`: {reason}" for path, reason in skips
        )
        lines.append(f"**Docstring candidates skipped:** {skipped}.")
    return lines


def _review_stack_body(
    evaluation: CodeReviewEvaluation,
    *,
    conclusion: CheckConclusion,
    living_range: str = "",
    suppressed_count: int = 0,
    review_phase: Literal["tier1", "deep", "dual"] = "dual",
    pr_title: str = "",
    head_sha: str = "",
    base_sha: str = "",
    file_contents: dict[str, str] | None = None,
    docstring_targets: tuple[str, ...] = (),
    docstring_skips: tuple[tuple[str, str], ...] = (),
    linked_issues: tuple[int, ...] = (),
    muted: dict[str, ClassPrecision] | None = None,
    held_note: str = "",
) -> str:
    """PR-timeline review stack comment (Markings v2 / FLINT-style shell).

    Deterministic markdown only — no extra LLM. Upserted by marker so
    synchronize edits in place rather than spamming the PR.

    grug#1429: Elder's section is framed by `pin_review` - it opens with the
    exact head judged and the linked issue, and ends with the numbered fixes
    (each with its target) and an explicit closure sentence.
    """
    findings = evaluation.findings
    n = len(findings)
    by_sev: dict[str, int] = {}
    for f in findings:
        by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
    sev_bits = ", ".join(
        f"{k}={by_sev[k]}" for k in ("critical", "high", "medium", "low") if k in by_sev
    ) or "none"
    status_line = _stack_status_line(evaluation, n, sev_bits, conclusion)

    # Ten, not twenty-five. <details> does NOT fold in Gmail - the raw
    # notification HTML for grug#799 carried a real <details>, and Gmail
    # rendered every child of it flat. So the fold buys nothing in the surface
    # a human actually reads, and the only thing that keeps the mail short is
    # the content being short. Each row is also one line, not a diff: the
    # evidence lives on the inline comment anchored to the actual code.
    rows = [
        "| Severity | File | Line | Rule |",
        "|---|---|---|---|",
    ]
    for f in findings[:_STACK_TABLE_ROWS]:
        rows.append(
            f"| {f.severity} | `{_md_code_span(f.file)}` | {f.line} | "
            f"`{_md_code_span(f.rule_name)}` |"
        )
    if n > _STACK_TABLE_ROWS:
        rows.append(f"| +{n - _STACK_TABLE_ROWS} more | | | |")
    table = "\n".join(rows) if findings else ""

    # `/grug fix` reads the stored `suggestion` field only, so a fix promoted
    # from the message's prose is offered inline and never counted here.
    committable = [
        f for f in findings
        if f.suggestion
        and checked_suggestion(f, f.line, (file_contents or {}).get(f.file, ""))
        is not None
    ]

    # The BOARD (#791). What a human receives by email is exactly this body at
    # CREATION time - GitHub never mails an edit - so the verdict leads and all
    # evidence is folded. Previously the first thing posted was a file table
    # and a diagram, which made the email a diff dump with the conclusion
    # buried under it.
    blocking = sum(1 for f in findings if f.severity in ("high", "critical"))
    advisory = len(findings) - blocking
    parts: list[str] = []
    if committable:
        parts.append(
            f"**Finishing touch:** {len(committable)} unresolved suggestion(s) "
            "can be applied together with `/grug fix`."
        )
    parts.extend(_docstring_offer_lines(docstring_targets, docstring_skips))
    # Phase, Status and Check-run used to live in the fold. All three were cut:
    # Status restated the summary line verbatim, Phase ("Tier-1 coder arm") is
    # internal vocabulary that means nothing to the author, and the check-run
    # name is already a row in the PR's own check list. Scope and judge
    # suppression stay - they are the two questions a skeptical reader asks:
    # what did you look at, and what did you decide not to tell me.
    #
    # BLANK LINE between table and bullets is required: a list starting on the
    # line after a table row is swallowed into the table.
    detail = "\n\n".join(p for p in [
        table,
        held_note,
        _muted_lines(findings, muted),
        _stack_detail_lines(
            review_phase, living_range, suppressed_count, head_sha, base_sha,
            tier_served=getattr(evaluation, "tier_served", None),
            families=getattr(evaluation, "families", None),
            models_used=getattr(evaluation, "models_used", ()),
        ),
    ] if p)
    if detail:
        parts.extend([
            "",
            board.collapse(
                f"Grug Elder - {status_line.replace('**','')} - check `{conclusion}`",
                detail,
                # Open when something needs doing, folded when it does not: a
                # clean review should cost no clicks and no scrolling.
                open_by_default=bool(blocking),
            ),
        ])
    # No detail at all (a clean pass with no scope or suppression note) gets no
    # fold. An empty <details> renders as a disclosure triangle hiding nothing,
    # which reads as a bug in the tool.
    parts.extend(_stack_closing_note(evaluation, has_committable=bool(committable)))

    # Assemble a REAL board: marker, a delimited header, and Elder's content
    # inside its own `grug-sec:elder` region.
    #
    # This used to be a flat `marker + header + content` string. It looked
    # right and was wrong: with no region delimiters,
    # `board.extract_section(body, "elder")` returned None, so
    # `_upsert_review_stack_comment` skipped the merge path entirely and
    # PATCHed the COMPLETE body - deleting Chief's section on every pass.
    # Verified against the live boards on #797, #798 and #799: all three carry
    # `grug-board` and not one `grug-sec:` region. The board_client unit tests
    # never caught it because they call the client directly; nothing asserted
    # that what Elder actually produces is something the client can merge.
    body = board.set_header(
        board.new_board(),
        board.render_header(
            pr_title, blocking, advisory,
            degraded=is_blackout(evaluation.degraded_reason),
            partial=evaluation.degraded_reason in PUBLISHABLE_DEGRADATIONS,
        ),
    )
    section = pin_review(
        "\n".join(parts), evaluation,
        head_sha=head_sha, linked_issues=linked_issues,
    )
    return board.upsert_section(body, "elder", section)


def _lead_if_degraded(summary: str, evaluation: CodeReviewEvaluation) -> str:
    """grug#1212: a degraded summary's first line says so and how to re-run,
    ahead of everything else (intent paragraph, deep preamble). A clean or
    `no_diff` summary is returned unchanged."""
    if not is_real_degradation(evaluation.degraded_reason):
        return summary
    return lead_with_degraded_line(summary, str(evaluation.degraded_reason))


def _muted_lines(
    findings: "tuple[Finding, ...] | list[Finding]",
    muted: dict[str, ClassPrecision] | None,
) -> str:
    """Board/summary list of the muted classes this review hit, or ''."""
    classes = sorted({
        f.rule_name for f in findings
        if muted and finding_is_muted(f.rule_name, f.category, muted)
    })
    if not classes:
        return ""
    bullets = "\n".join(
        f"- `{_md_code_span(c)}`: {mute_note(muted[c])}" for c in classes
    )
    return (
        "**Muted classes on this review** (no inline comment unless high or critical):"
        f"\n\n{bullets}"
    )


def _checked_comment_body(f: Finding, file_text: str, precedent_note: str) -> str:
    """Inline body whose committable block, if any, passed `checked_suggestion`.
    A fix that fails the check still renders, as prose under "Fix"."""
    checked = checked_suggestion(f, f.line, file_text)
    if checked is None:
        return _inline_comment_body(f, precedent_note, committable=False)
    return _inline_comment_body(replace(f, suggestion=checked), precedent_note)


def build_review_result(
    evaluation: CodeReviewEvaluation, *, head_sha: str, event: ReviewEvent,
    prior_keys: frozenset[str] = frozenset(),
    precedent_notes: dict[str, str] | None = None,
    repo_config: RepoConfig | None = None,
    file_contents: dict[str, str] | None = None,
    persona: str = "elder",
    muted: dict[str, ClassPrecision] | None = None,
    held_back: frozenset[str] = frozenset(),
) -> tuple[ReviewResult | None, tuple[Finding, ...]]:
    """Build the ReviewResult, or (None, ()) if nothing NEW to post.

    Skips entirely on fully degraded responses. A partial staged review still
    publishes its validated findings, but stays advisory. `prior_keys`
    (non-empty only on a synchronize/reopened push) dedups findings already commented
    on unchanged lines (#189) — so a re-review doesn't flood the PR with
    duplicate inline comments. If every finding was already posted,
    returns (None, ()) (nothing new). NOTE: dedup affects only the inline
    REVIEW; the check-run summary/conclusion still reflect ALL current
    findings (the bugs are still there).

    The second return value is `new_findings` itself, in the exact order
    used to build `comments` below - one `InlineComment` per finding, same
    order. Callers MUST thread it into `_capture_review_comments` as
    `findings=`: that function pairs GitHub's freshly-fetched comments with
    this tuple by POSITION (grug#967), so an order mismatch here would
    mis-attribute captured comments.

    `file_contents` is the head text dispatch already loaded. Every fix is
    offered as a committable block only after `checked_suggestion` (#1149)
    verifies it against that text; a file with no loaded text gets prose."""
    if evaluation.degraded_reason not in (None, "", *PUBLISHABLE_DEGRADATIONS):
        return None, ()
    new_findings = dedup_findings(evaluation.findings, prior_keys)
    # GitHub inline review comments must anchor to the PR diff. Outside-diff
    # findings remain visible in the check summary, where their advisory label
    # and file:line are retained, but are not submitted as invalid inline rows.
    new_findings = tuple(f for f in new_findings if f.outside_diff_reason is None)
    # Complexity is summary advice by default. An inline thread requires a
    # changed-line suggestion the reviewer can apply in this PR; heavy-lift
    # findings never create reply-required threads.
    new_findings = tuple(
        f for f in new_findings
        if not _is_complexity_finding(f)
        or (f.suggestion and f.effort != "heavy-lift" and f.severity in ("low", "medium"))
    )
    if repo_config is not None:
        # `.grug.yaml` min_inline_severity: lower findings stay in the check
        # summary (built from the full evaluation) but get no inline comment.
        new_findings = tuple(
            f for f in new_findings if meets_inline_floor(f.severity, repo_config)
        )
    if muted:
        # #1436: a muted class stays on the board and in the check summary.
        # A blocking finding (high, critical) keeps its inline thread: the
        # thread is what carries the block across later pushes.
        new_findings = tuple(
            f for f in new_findings
            if f.severity in ("high", "critical")
            or not finding_is_muted(f.rule_name, f.category, muted)
        )
    if held_back:
        # #1570: over-budget findings stay in the summary and on the board.
        new_findings = tuple(
            f for f in new_findings
            if finding_key(f.file, f.line, f.rule_name) not in held_back
        )
    if not new_findings:
        return None, ()
    notes = precedent_notes or {}
    texts = file_contents or {}
    comments = tuple(
        InlineComment(
            path=f.file, line=f.line,
            body=_checked_comment_body(
                f, texts.get(f.file, ""),
                notes.get(finding_key(f.file, f.line, f.rule_name), ""),
            ),
        )
        for f in new_findings
    )
    return ReviewResult(
        commit_id=head_sha,
        event=event,
        body=(
            f'<img src="{_PERSONA_PORTRAIT}" width="46" align="left" alt="Grug {_PERSONA}" />'
            f"\n\n**Grug {_PERSONA}** gaze upon your PR · {len(comments)} finding(s)"
            + (f"\n\n{pin_line(head_sha)}" if head_sha else "")
            + f"\n\n{_REVIEW_PERSONA_TAG.format(persona)}"
        ),
        comments=comments,
    ), new_findings


@dataclass(frozen=True, kw_only=True)
class FinishedPass:
    """Everything one finished Elder pass hands the projection (#1591).

    Tier-1 and the deep append both build this, and no field has a default:
    a path that leaves an input out (as the deep stack once left out file
    contents and linked issues, #1474, and precedent notes, #1246) fails to
    construct instead of rendering a quieter body.

    `board_evaluation` is every finding standing on this head (Tier-1 plus
    any deep append): the stack comment and the email decision read it.
    `review_evaluation` is the finding set this pass posts inline: Tier-1's
    own, or only the deep arm's novel findings.
    """

    board_evaluation: CodeReviewEvaluation
    review_evaluation: CodeReviewEvaluation
    conclusion: CheckConclusion
    event: ReviewEvent
    review_phase: Literal["tier1", "deep", "dual"]
    head_sha: str
    base_sha: str
    pr_title: str
    living_range: str
    suppressed_count: int
    file_contents: dict[str, str]
    diff_text: str
    linked_issues: tuple[int, ...]
    precedent_notes: dict[str, str]
    muted: dict[str, ClassPrecision]
    prior_keys: frozenset[str]
    held_back: frozenset[str]
    held_note: str
    repo_config: RepoConfig | None


@dataclass(frozen=True)
class Markings:
    """The bodies one pass publishes, before any GitHub call.

    `review` is None when nothing new goes inline. `posted_findings` is in
    the exact order of `review.comments`, which comment capture pairs by
    position (grug#967). `stack_body` is None when the stack could not be
    rendered (`stack_error` names the exception kind): the stack is cosmetic
    UX, so its failure never costs the check or the review.
    """

    review: ReviewResult | None
    posted_findings: tuple[Finding, ...]
    stack_body: str | None
    stack_create_if_absent: bool
    stack_error: str = ""


def project(finished: FinishedPass) -> Markings:
    """Render the inline review and the stack comment of one finished pass.

    Pure: no GitHub call, no store read. The publisher applies the bodies.
    """
    review, posted = build_review_result(
        finished.review_evaluation,
        head_sha=finished.head_sha,
        event=finished.event,
        prior_keys=finished.prior_keys,
        precedent_notes=finished.precedent_notes,
        repo_config=finished.repo_config,
        file_contents=finished.file_contents,
        muted=finished.muted,
        held_back=finished.held_back,
    )
    stack_body: str | None = None
    stack_error = ""
    try:
        stack_body = _review_stack_body(
            finished.board_evaluation,
            conclusion=finished.conclusion,
            living_range=finished.living_range,
            suppressed_count=finished.suppressed_count,
            review_phase=finished.review_phase,
            pr_title=finished.pr_title,
            head_sha=finished.head_sha,
            base_sha=finished.base_sha,
            file_contents=finished.file_contents,
            **_docstring_offer(finished.diff_text, finished.file_contents),
            linked_issues=finished.linked_issues,
            muted=finished.muted,
            held_note=finished.held_note,
        )
    except Exception as error:  # noqa: BLE001 - stack is cosmetic UX
        stack_error = type(error).__name__
    return Markings(
        review=review,
        posted_findings=posted,
        stack_body=stack_body,
        stack_create_if_absent=worth_an_email(finished.board_evaluation),
        stack_error=stack_error,
    )
