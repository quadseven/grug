"""Elder re-checks the ruff findings a settle-findings ticket names (#1477).

Recorded shape: grug PR 1470 settled ticket 1313, whose two lines name
`lint-bug` in a file and link Elder's earlier threads. The ruff rule is only in
those threads. Both findings were still in the code when Elder passed the PR,
on lines its diff did not touch.

These tests run the real ruff binary over the same seam dispatch uses.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from personas import review_publish
from personas.code_reviewer import carry_forward, deferred_ruff, dispatch, thread_resolver
from personas.code_reviewer.diff_parser import parse_diff
from personas.code_reviewer.persona import CodeReviewEvaluation

_PATH = "services/api/installations.py"
_LINK = "https://github.com/acme/widgets/pull/1312#discussion_r4324290468"
_TICKET = (
    "## What\n\n"
    f"1. Medium | _general_ | `lint-bug` in `{_PATH}`: {_LINK}\n"
    "\n## Original issue\n\n"
    f"> - medium | _general_ | `lint-bug` in `{_PATH}`: {_LINK}\n"
)
_THREAD = (
    "\U0001f7e0 medium | _general_ | `lint-bug`\n\n**What Elder sees**\n\n"
    "ruff `B904`: Within an `except` clause, raise exceptions with `raise ... from err`.\n"
    "\n<!-- grug-rule:lint-bug -->"
)
_FIRING = (
    "def load(raw):\n"
    "    try:\n"
    "        return int(raw)\n"
    "    except ValueError:\n"
    "        raise KeyError(raw)\n"
)
_FIXED = _FIRING.replace("raise KeyError(raw)", "raise KeyError(raw) from None")
_OTHER_DIFF = (
    "diff --git a/README.md b/README.md\n"
    "--- a/README.md\n"
    "+++ b/README.md\n"
    "@@ -1,1 +1,2 @@\n"
    " # title\n"
    "+more\n"
)


def _clean() -> CodeReviewEvaluation:
    return CodeReviewEvaluation(findings=(), conclusion="success")


_BOT = "grug-tribe[bot]"
_REF = "acme/widgets" "#1313"  # joined at parse time: keeps a literal cross-repo ref out of the source


def _review(ticket, head, *, body="Closes #1313", diff=_OTHER_DIFF, thread=_THREAD, author=_BOT):
    """One review pass, with GitHub faked at the reads. `head` is the text of
    the named file at the head; README.md is the only file in the diff.
    `ticket` is one body, or a callable from issue number to body."""
    context = {
        "repo": "acme/widgets", "pr_number": 1470, "installation_id": 7,
        "head_sha": "abc123", "title": "Settle lint", "body": body,
    }
    comment_reads = []
    file_reads = []

    def read_comment(_token, _owner, _repo, comment_id):
        comment_reads.append(comment_id)
        if isinstance(thread, Exception):
            raise thread
        return author, thread

    def read_issue(number):
        return {"title": "Settle", "body": ticket(number) if callable(ticket) else ticket}

    def read_files(_token, _owner, _repo, paths, ref, **_kwargs):
        file_reads.append((paths, ref))
        return {p: head for p in paths} if head is not None else {}

    with (
        patch.object(dispatch, "build_issue_details_fetcher",
                     return_value=read_issue),
        patch.object(dispatch, "with_install_token_retry", lambda _i, fn: fn("tok")),
        patch.object(dispatch, "fetch_review_comment_body", read_comment),
        patch.object(dispatch, "fetch_file_contents", read_files),
    ):
        dispatch._attach_linked_issue_context(
            context, installation_id=7, owner="acme", repo_name="widgets",
        )
        merged = dispatch._merge_deferred_ruff(_clean(), parse_diff(diff), {}, context)
    return merged, comment_reads, file_reads


def test_a_named_rule_still_firing_gets_an_unmet_criterion_thread():
    merged, comment_reads, file_reads = _review(_TICKET, _FIRING)
    assert [f.rule_name for f in merged.findings] == ["unmet-criterion"]
    finding = merged.findings[0]
    assert "`B904`" in finding.message
    assert _PATH in finding.message
    assert "Line(s) 5." in finding.message
    assert finding.check_output.startswith(f"{_PATH}:5: B904 ")
    # A real thread: anchored on a diff line, though the file is outside it.
    assert (finding.file, finding.outside_diff_reason) == ("README.md", None)
    assert finding.line in parse_diff(_OTHER_DIFF)[0].new_lines
    assert finding.severity == "high"
    assert merged.conclusion == "failure"
    # Read at the head, once per thread, though the ticket repeats the line.
    assert comment_reads == [4324290468]
    assert file_reads == [((_PATH,), "abc123")]


def test_the_fixed_finding_gets_no_thread():
    merged, _, file_reads = _review(_TICKET, _FIXED)
    assert merged.findings == ()
    assert merged.conclusion == "success"
    assert file_reads == [((_PATH,), "abc123")]


def test_a_ticket_with_no_deferred_lines_changes_nothing():
    # A model-judged finding is a deferred line too, with nothing to measure.
    ticket = (
        "## Acceptance criteria\n- [ ] A thing\n\n"
        f"- medium | _maintainability_ | `dead-code` | quick win in `{_PATH}`: {_LINK}\n"
    )
    merged, comment_reads, file_reads = _review(ticket, _FIRING)
    assert merged == _clean()
    assert (comment_reads, file_reads) == ([], [])


def test_a_lane_pr_that_only_refs_its_ticket_is_held_to_it():
    body = f"Refs {_REF}\n\n---\nLoom ticket acme.widgets-1313, lane x, model m.\n"
    merged, _, _ = _review(_TICKET, _FIRING, body=body)
    assert [f.rule_name for f in merged.findings] == ["unmet-criterion"]


def test_a_person_pr_that_only_refs_the_ticket_is_not():
    merged, comment_reads, _ = _review(_TICKET, _FIRING, body=f"Refs {_REF}")
    assert merged == _clean()
    assert comment_reads == []


def test_a_firing_line_in_the_diff_is_the_anchor():
    diff = (
        f"diff --git a/{_PATH} b/{_PATH}\n--- a/{_PATH}\n+++ b/{_PATH}\n"
        "@@ -3,2 +3,3 @@\n"
        "         return int(raw)\n"
        "     except ValueError:\n"
        "+        raise KeyError(raw)\n"
    )
    merged, _, _ = _review(_TICKET, _FIRING, diff=diff)
    finding = merged.findings[0]
    assert (finding.file, finding.line) == (_PATH, 5)
    assert "only because GitHub" not in finding.message


def test_with_no_diff_line_left_the_finding_stays_in_the_summary():
    merged, _, _ = _review(_TICKET, _FIRING, diff="")
    finding = merged.findings[0]
    assert (finding.file, finding.line) == (_PATH, 5)
    assert finding.outside_diff_reason
    assert merged.conclusion == "failure"


def test_two_rules_in_one_file_do_not_share_an_anchor():
    ref = deferred_ruff.DeferredLint(1313, _PATH, 1)
    hits = [
        deferred_ruff.RuffHit(_PATH, 5, "B904", "one"),
        deferred_ruff.RuffHit(_PATH, 9, "B023", "two"),
        deferred_ruff.RuffHit(_PATH, 12, "B904", "one"),
    ]
    found = deferred_ruff.unmet_findings(
        [(ref, "B904"), (ref, "B023")], hits, parse_diff(_OTHER_DIFF),
    )
    assert len({(f.file, f.line) for f in found}) == 2
    assert "Line(s) 5, 12." in found[0].message


def test_a_rule_firing_only_in_another_file_is_not_this_finding():
    ref = deferred_ruff.DeferredLint(1313, _PATH, 1)
    hits = [deferred_ruff.RuffHit("elsewhere.py", 5, "B904", "one")]
    assert deferred_ruff.unmet_findings([(ref, "B904")], hits, ()) == ()


def test_a_file_gone_at_the_head_has_nothing_firing():
    merged, _, _ = _review(_TICKET, None)
    assert merged == _clean()


def _http_error(status):
    request = httpx.Request("GET", "https://api.github.com/x")
    return httpx.HTTPStatusError(
        "failed", request=request, response=httpx.Response(status, request=request),
    )


def test_a_thread_github_no_longer_has_degrades_the_review():
    merged, _, _ = _review(_TICKET, _FIRING, thread=_http_error(404))
    assert merged.findings == ()
    assert merged.degraded_reason == "partial_review"


def test_a_resolved_thread_never_hides_a_rule_measured_at_this_head():
    # Finding 1: the (rule, file, line) match would drop it.
    merged, _, _ = _review(_TICKET, _FIRING)
    finding = merged.findings[0]
    thread = _thread()
    thread["isResolved"] = True
    thread["comments"]["nodes"][0].update(path=finding.file, line=finding.line)
    assert carry_forward.filter_findings_against_resolved_threads(
        merged.findings, 7, "acme", "widgets", 1470, threads=[thread],
        pr_author="author-login",
    ) == merged.findings


_FOUR = "Closes #1\nCloses #2\nCloses #3\nCloses #1313\n"


def test_a_fourth_settled_ticket_is_read():
    # Finding 2: the criteria block reads only the first three links.
    merged, _, _ = _review(lambda n: _TICKET if n == 1313 else "", _FIRING, body=_FOUR)
    assert [f.rule_name for f in merged.findings] == ["unmet-criterion"]
    assert merged.degraded_reason is None


def test_tickets_over_the_bound_degrade_and_what_was_read_still_counts():
    body = "".join(f"Closes #{n}\n" for n in (1313, 2, 3, 4, 5, 6))
    merged, _, _ = _review(lambda n: _TICKET if n == 1313 else "", _FIRING, body=body)
    assert [f.rule_name for f in merged.findings] == ["unmet-criterion"]
    assert merged.degraded_reason == "partial_review"


def test_a_ticket_read_that_fails_degrades():
    def unreadable(_number):
        raise _http_error(502)

    merged, _, _ = _review(unreadable, _FIRING)
    assert merged.degraded_reason == "partial_review"


def test_a_fetcher_that_cannot_be_built_degrades_a_pr_that_settles_a_ticket():
    context = {"repo": "acme/widgets", "pr_number": 1, "body": "Closes #1313"}
    with patch.object(dispatch, "build_issue_details_fetcher", side_effect=RuntimeError("no")):
        dispatch._attach_linked_issue_context(
            context, installation_id=7, owner="acme", repo_name="widgets",
        )
    merged = dispatch._merge_deferred_ruff(_clean(), (), {}, context)
    assert merged.degraded_reason == "partial_review"


def test_a_ticket_github_does_not_have_defers_nothing():
    def absent(_number):
        raise _http_error(404)

    merged, _, _ = _review(absent, _FIRING)
    assert merged == _clean()


def test_a_degraded_recheck_leaves_the_threads_to_the_next_pass():
    # Finding 2: an unmeasured finding is not closed as fixed.
    merged, _, _ = _review(_TICKET, _FIRING, thread=httpx.ConnectError("down"))
    with patch.object(dispatch, "resolve_fixed_threads") as resolve:
        dispatch._tidy_fixed_threads(
            action="synchronize", review_published=True, evaluation=merged,
            installation_id=7, owner="acme", repo_name="widgets", pull_number=1470,
            head_sha="abc123",
        )
    resolve.assert_not_called()


def test_a_lane_work_failure_does_not_skip_the_recheck():
    # Finding 3.
    context = {
        "repo": "acme/widgets", "pr_number": 1470, "installation_id": 7, "head_sha": "abc123",
        "deferred_lint": (deferred_ruff.DeferredLint(1313, _PATH, 4324290468),),
    }
    inputs = dispatch._LaneWorkInputs(_OTHER_DIFF, parse_diff(_OTHER_DIFF), {_PATH: _FIRING}, {})
    with (
        patch.object(dispatch, "_merge_lane_work", side_effect=ValueError("bad")),
        patch.object(dispatch, "with_install_token_retry", lambda _i, fn: fn("tok")),
        patch.object(dispatch, "fetch_review_comment_body", lambda *_a: (_BOT, _THREAD)),
    ):
        merged = dispatch._merge_lane_work_for_pass(
            _clean(), inputs, context, living_range="", base_sha="b0", correctness_only=False,
        )
    assert [f.rule_name for f in merged.findings] == ["unmet-criterion"]


def test_any_failure_in_the_recheck_degrades():
    # Finding 3: not only the failures the re-check expects.
    with patch.object(dispatch, "ruff_hits", side_effect=ValueError("bad")):
        merged, _, _ = _review(_TICKET, _FIRING)
    assert merged.degraded_reason == "partial_review"


def test_more_findings_than_the_bound_degrade_and_the_rest_still_count():
    # Finding 4.
    lines = "".join(
        f"- medium | _general_ | `lint-bug` in `{_PATH}`: {_LINK[:-1]}{n}\n"
        for n in range(deferred_ruff.MAX_DEFERRED + 1)
    )
    merged, comment_reads, _ = _review(lines, _FIRING)
    assert len(comment_reads) == deferred_ruff.MAX_DEFERRED
    assert [f.rule_name for f in merged.findings] == ["unmet-criterion"]
    assert merged.degraded_reason == "partial_review"


def test_more_files_than_one_read_takes_raises_and_the_review_degrades():
    # Finding 4: a path past the cap is not "gone from the head".
    many = tuple(f"f{n}.py" for n in range(21))
    with pytest.raises(review_publish.ContextFilesTruncated):
        review_publish.fetch_file_contents("tok", "acme", "widgets", many, "abc", only_404_skips=True)
    lines = "".join(
        f"- medium | _general_ | `lint-bug` in `f{n}.py`: {_LINK}{n}\n" for n in range(21)
    )
    context = {
        "repo": "acme/widgets", "pr_number": 1, "installation_id": 7, "head_sha": "abc",
        "deferred_lint": deferred_ruff.parse_deferred_lint(
            lines, issue=1, owner="acme", repo="widgets",
        ),
    }
    with (
        patch.object(dispatch, "with_install_token_retry", lambda _i, fn: fn("tok")),
        patch.object(dispatch, "fetch_review_comment_body", lambda *_a: (_BOT, _THREAD)),
    ):
        merged = dispatch._merge_deferred_ruff(_clean(), (), {}, context)
    assert merged.degraded_reason == "partial_review"


@pytest.mark.parametrize("author,thread", [
    ("a-person", _THREAD),
    (_BOT, _THREAD.replace("<!-- grug-rule:lint-bug -->", "")),
    (_BOT, _THREAD.replace("grug-rule:lint-bug", "grug-rule:dead-code")),
])
def test_a_code_counts_only_from_elders_own_lint_comment(author, thread):
    # Finding 5: the head fires B904, yet this thread cannot name it.
    merged, _, _ = _review(_TICKET, _FIRING, author=author, thread=thread)
    assert merged.findings == ()
    assert merged.degraded_reason == "partial_review"


def test_the_comment_read_reports_its_author():
    response = httpx.Response(
        200, json={"user": {"login": _BOT}, "body": "text"},
        request=httpx.Request("GET", "https://api.github.com/x"),
    )
    with patch.object(review_publish.httpx, "get", return_value=response):
        assert review_publish.fetch_review_comment_body("tok", "o", "r", 5) == (_BOT, "text")


def test_a_thread_read_that_fails_degrades_instead_of_passing():
    merged, _, _ = _review(_TICKET, _FIRING, thread=httpx.ConnectError("down"))
    assert merged.findings == ()
    assert merged.degraded_reason == "partial_review"
    assert merged.conclusion != "success"


def test_a_ruff_run_that_fails_degrades_instead_of_passing():
    with patch("personas.code_reviewer.lint.subprocess.run", side_effect=FileNotFoundError):
        merged, _, _ = _review(_TICKET, _FIRING)
    assert merged.degraded_reason == "partial_review"


@pytest.mark.parametrize("path", ["../secret.py", "/etc/x.py"])
def test_a_path_leaving_the_repository_is_not_read(path):
    ticket = f"- medium | _general_ | `lint-bug` in `{path}`: {_LINK}\n"
    assert deferred_ruff.parse_deferred_lint(
        ticket, issue=1, owner="acme", repo="widgets",
    ) == ()


def test_a_thread_of_another_repository_is_not_read():
    assert deferred_ruff.parse_deferred_lint(
        _TICKET, issue=1, owner="acme", repo="other",
    ) == ()


def _thread(*, outdated=False):
    body = "\U0001f534 high | _requirements_ | `unmet-criterion`\n\n<!-- grug-rule:unmet-criterion -->"
    return {
        "id": "T1", "isResolved": False, "isOutdated": outdated,
        "comments": {"totalCount": 1, "nodes": [{
            "author": {"login": "grug-tribe"}, "body": body, "path": "README.md",
            "line": 2, "originalLine": 2, "originalCommit": {"oid": "abc"},
        }]},
    }


def test_a_fixed_finding_releases_a_thread_on_a_line_the_fix_never_touched():
    assert carry_forward.select_carried([_thread()], ()) == ()
    assert thread_resolver.select_fixed_threads([_thread()], ())[0] == ["T1"]
