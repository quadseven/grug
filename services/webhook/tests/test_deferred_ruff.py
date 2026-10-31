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


def _review(ticket, head, *, body="Closes #1313", diff=_OTHER_DIFF, thread=_THREAD):
    """One review pass, with GitHub faked at the reads. `head` is the text of
    the named file at the head; README.md is the only file in the diff."""
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
        return thread

    def read_files(_token, _owner, _repo, paths, ref, **_kwargs):
        file_reads.append((paths, ref))
        return {p: head for p in paths} if head is not None else {}

    with (
        patch.object(dispatch, "build_issue_details_fetcher",
                     return_value=lambda _n: {"title": "Settle", "body": ticket}),
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
    body = "Refs acme/widgets#1313\n\n---\nLoom ticket acme.widgets-1313, lane x, model m.\n"
    merged, _, _ = _review(_TICKET, _FIRING, body=body)
    assert [f.rule_name for f in merged.findings] == ["unmet-criterion"]


def test_a_person_pr_that_only_refs_the_ticket_is_not():
    merged, comment_reads, _ = _review(_TICKET, _FIRING, body="Refs acme/widgets#1313")
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


def test_a_thread_github_no_longer_has_names_nothing():
    request = httpx.Request("GET", "https://api.github.com/x")
    gone = httpx.HTTPStatusError(
        "gone", request=request, response=httpx.Response(404, request=request),
    )
    merged, _, _ = _review(_TICKET, _FIRING, thread=gone)
    assert merged == _clean()


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
