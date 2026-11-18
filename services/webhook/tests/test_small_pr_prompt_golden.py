"""Golden test: a small PR produces the same review prompt (#1747 box 3).

Each case is a small unified diff under `fixtures/small_pr_prompt/`. The
test drives it through the production path - `parse_diff`, `plan_review`
with the live budgets, `render_review_map`, `assemble_evidence`,
`_build_messages` - and compares the user message, byte for byte, with the
stored `<case>.prompt.txt`.

It guards work on large diffs (compression, hunk splitting): whatever that
work does to an oversized diff, a small diff must stay one unstaged cohort
with no review map and an unchanged prompt.

The system prompt is not stored: its rule text changes often and does not
depend on the diff. It is pinned to what the prompt builder gives for an
empty review map, so a small PR can never pick up staging text.

To accept an intended prompt change, regenerate the goldens with
`UPDATE_SMALL_PR_PROMPT_GOLDEN=1` set and review the diff of the files.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import llm_client as lc
from llm_client import Hunk
from personas.code_reviewer.diff_parser import parse_diff
from review_evidence import assemble_evidence
from review_pipeline import ReviewPlan, plan_review, render_review_map

_FIXTURES = Path(__file__).parent / "fixtures" / "small_pr_prompt"
_CASES = ("single_file", "multi_file", "rename_and_delete")
_UPDATE_ENV = "UPDATE_SMALL_PR_PROMPT_GOLDEN"
_VARIANT = "v2"


@pytest.fixture(autouse=True)
def _default_budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "GRUG_REVIEW_COHORT_CHARS", "GRUG_REVIEW_COHORT_FILES",
        "GRUG_REVIEW_MAX_COHORTS",
    ):
        monkeypatch.delenv(name, raising=False)


def _hunks(case: str) -> list[Hunk]:
    diff = (_FIXTURES / f"{case}.diff").read_text(encoding="utf-8")
    return [Hunk(path=h.file_path, body=h.body) for h in parse_diff(diff)]


def _plan(hunks: list[Hunk]) -> ReviewPlan:
    """The plan production makes (`review_is_staged` uses the same call)."""
    return plan_review(
        hunks,
        max_cohort_chars=lc._review_cohort_chars(),
        max_cohort_paths=lc._review_cohort_paths(),
        max_cohorts=lc._review_max_cohorts(),
    )


def _messages(hunks: list[Hunk], plan: ReviewPlan) -> list[dict[str, str]]:
    cohort_hunks = [hunks[index] for index in plan.cohorts[0].hunk_indexes]
    return lc._build_messages(
        assemble_evidence(cohort_hunks), _VARIANT,
        review_map=render_review_map(plan),
    )


@pytest.mark.parametrize("case", _CASES)
def test_small_pr_is_one_unstaged_cohort(case: str) -> None:
    hunks = _hunks(case)
    plan = _plan(hunks)

    assert hunks, "fixture diff parsed to no hunks"
    assert not plan.staged
    assert not plan.truncated
    assert plan.concerns == ()
    assert len(plan.cohorts) == 1
    # Every hunk, in diff order: nothing dropped, split or reordered.
    assert plan.cohorts[0].hunk_indexes == tuple(range(len(hunks)))
    assert render_review_map(plan) == ""


@pytest.mark.parametrize("case", _CASES)
def test_small_pr_user_prompt_matches_golden(case: str) -> None:
    hunks = _hunks(case)
    user = _messages(hunks, _plan(hunks))[1]["content"]
    golden = _FIXTURES / f"{case}.prompt.txt"

    if os.environ.get(_UPDATE_ENV) == "1":
        golden.write_text(user, encoding="utf-8")

    assert user == golden.read_text(encoding="utf-8"), (
        f"the review prompt for small diff {case!r} changed; if that is "
        f"intended, rerun with {_UPDATE_ENV}=1 and commit the new golden"
    )


@pytest.mark.parametrize("case", _CASES)
def test_small_pr_system_prompt_has_no_staging_text(case: str) -> None:
    hunks = _hunks(case)
    messages = _messages(hunks, _plan(hunks))

    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[0]["content"] == lc._review_system_prompt(
        _VARIANT, voice="caveman", has_intent=False, review_map="",
        team_practices="", few_shot_examples="", learnings="",
    )


def test_golden_files_are_the_whole_fixture_set() -> None:
    """A diff with no golden (or the reverse) would be a case never run."""
    diffs = {p.name[: -len(".diff")] for p in _FIXTURES.glob("*.diff")}
    goldens = {p.name[: -len(".prompt.txt")] for p in _FIXTURES.glob("*.prompt.txt")}
    assert diffs == goldens == set(_CASES)
