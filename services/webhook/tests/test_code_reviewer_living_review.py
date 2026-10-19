"""Tests for personas/code_reviewer/living_review.py (#1187).

Elder's inline findings show their current state in place: a finding whose
anchored lines a later push changed reads "Addressed in <sha>", a resolved
thread reads "Resolved", a maintainer's thumbs-down reads "Dismissed". The
original finding text is kept verbatim under a struck-through header, and
every transition is idempotent per (finding, state, sha).
"""
from __future__ import annotations

import pytest

from personas.code_reviewer import living_review as lr
from personas.code_reviewer.dedup import parse_rule, rule_marker
from personas.code_reviewer.persona import Finding

HEAD = "abcdef1234567890abcdef1234567890abcdef12"
HEADER = "\U0001f525 high | _correctness_ | `null-deref`"
ORIGINAL = (
    f"{HEADER}\n\n**What Elder sees**\n\nx may be None\n\n"
    "**Fix** (one-click, line-exact):\n```suggestion\nif x:\n```\n\n"
    + rule_marker("null-deref")
)


def _finding(file="a.py", line=10, rule="null-deref") -> Finding:
    return Finding(file=file, line=line, severity="high", rule_name=rule,
                   message="m", suggestion=None)


def _comment(*, body=ORIGINAL, author="grug-tribe", db_id=1, line=10):
    return {
        "id": f"C{db_id}", "databaseId": db_id, "isMinimized": False,
        "author": {"login": author}, "path": "a.py", "line": None,
        "originalLine": line, "originalCommit": {"oid": "c0ffee"}, "body": body,
    }


def _thread(*, outdated=False, resolved=False, comments=None, tid="T1", **kw):
    nodes = comments if comments is not None else [_comment(**kw)]
    return {
        "id": tid, "isResolved": resolved, "isOutdated": outdated,
        "comments": {"totalCount": len(nodes), "nodes": nodes},
    }


def _review(*findings, trusted_push=True):
    return lr.ReviewEvidence(
        head_sha=HEAD, findings=tuple(findings), trusted_push=trusted_push,
    )


# --- rendering ---------------------------------------------------------------

@pytest.mark.parametrize("state", [
    lr.FindingState("addressed", HEAD[:7]),
    lr.FindingState("resolved"),
    lr.FindingState("dismissed"),
])
def test_render_keeps_the_original_text_under_a_struck_header(state):
    body = lr.render(ORIGINAL, state)
    assert lr.parse_state(body) == state
    assert lr.original_body(body) == ORIGINAL
    assert f"~~{HEADER}~~" in body
    # Everything after the header survives verbatim (fix block included).
    assert ORIGINAL.split("\n", 1)[1] in body
    # The rule marker is still the last one, so dedup still sees the finding.
    assert parse_rule(body) == "null-deref"
    assert body.rstrip().endswith(rule_marker("null-deref"))


def test_render_chips_name_the_state():
    assert f"Addressed in {HEAD[:7]}" in lr.render(
        ORIGINAL, lr.FindingState("addressed", HEAD[:7]))
    assert "Resolved" in lr.render(ORIGINAL, lr.FindingState("resolved"))
    assert "Dismissed" in lr.render(ORIGINAL, lr.FindingState("dismissed"))


def test_render_open_restores_the_original_and_is_ascii():
    body = lr.render(ORIGINAL, lr.FindingState("dismissed"))
    assert lr.render(lr.original_body(body), None) == ORIGINAL
    chip_part = body.split(lr.ORIGINAL_MARK, 1)[0]
    assert chip_part.isascii()


def test_plain_body_has_no_state():
    assert lr.parse_state(ORIGINAL) is None
    assert lr.original_body(ORIGINAL) == ORIGINAL
    assert lr.parse_state("") is None


# --- planning ----------------------------------------------------------------

def test_fixed_line_plans_addressed_in_the_review_head():
    [t] = lr.plan([_thread(outdated=True)], {}, _review())
    assert t.comment_id == 1
    assert t.target == lr.FindingState("addressed", HEAD[:7])
    assert lr.original_body(t.body) == ORIGINAL


def test_reraised_nearby_or_unchanged_line_stays_open():
    assert lr.plan([_thread(outdated=True)], {}, _review(_finding(line=13))) == []
    assert lr.plan([_thread(outdated=False)], {}, _review()) == []


def test_other_rule_on_same_line_is_not_a_reraise():
    assert len(lr.plan([_thread(outdated=True)], {}, _review(_finding(rule="x")))) == 1


def test_addressed_needs_a_review_so_the_poll_lane_never_guesses():
    assert lr.plan([_thread(outdated=True)], {}, None) == []


def test_threads_that_are_not_elders_findings_are_ignored():
    assert lr.plan([_thread(outdated=True, author="alice")], {}, _review()) == []
    no_marker = _comment(body="plain text from grug")
    assert lr.plan([_thread(outdated=True, comments=[no_marker])], {}, _review()) == []


def test_already_addressed_is_idempotent_and_keeps_the_first_sha():
    body = lr.render(ORIGINAL, lr.FindingState("addressed", "1111111"))
    assert lr.plan([_thread(outdated=True, body=body)], {}, _review()) == []


def test_resolved_thread_plans_resolved_and_reverts_when_reopened():
    [t] = lr.plan([_thread(resolved=True)], {}, None)
    assert t.target == lr.FindingState("resolved")
    reopened = _thread(resolved=False, body=t.body)
    [back] = lr.plan([reopened], {}, None)
    assert back.target is None and back.body == ORIGINAL


def test_thumbs_down_dismisses_even_an_addressed_finding():
    body = lr.render(ORIGINAL, lr.FindingState("addressed", "1111111"))
    [t] = lr.plan([_thread(outdated=True, body=body)], {1: "false_positive"}, None)
    assert t.target == lr.FindingState("dismissed")
    assert lr.original_body(t.body) == ORIGINAL


def test_flipped_reaction_restores_a_dismissed_finding():
    body = lr.render(ORIGINAL, lr.FindingState("dismissed"))
    [t] = lr.plan([_thread(body=body)], {1: "confirmed"}, None)
    assert t.target is None and t.body == ORIGINAL


def test_dismissed_stays_dismissed_while_the_verdict_holds():
    body = lr.render(ORIGINAL, lr.FindingState("dismissed"))
    assert lr.plan([_thread(outdated=True, body=body)], {1: "false_positive"}, _review()) == []


def test_count_addressed_feeds_the_check_title():
    threads = [_thread(outdated=True), _thread(tid="T2", outdated=False, db_id=2)]
    assert lr.count_addressed(threads, _review()) == 1
    assert lr.note_on_title("Elder: 1 finding", 0, HEAD) == "Elder: 1 finding"
    assert lr.note_on_title("Elder: 1 finding", 2, HEAD) == (
        f"Elder: 1 finding; 2 earlier finding(s) addressed in {HEAD[:7]}"
    )


# --- applying (I/O) ----------------------------------------------------------

class _Gh:
    """Fake GitHub: GraphQL thread listing plus REST comment PATCH."""

    def __init__(self, threads, *, patch_status=200):
        self.threads = threads
        self.patches: list[tuple[str, str]] = []
        self.patch_status = patch_status

    def post(self, url, json=None, headers=None, timeout=None):
        assert "reviewThreads" in json["query"]
        return _Resp({"data": {"repository": {"pullRequest": {"reviewThreads": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": self.threads}}}}})

    def patch(self, url, json=None, headers=None, timeout=None):
        self.patches.append((url, json["body"]))
        return _Resp({}, self.patch_status)


class _Resp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError(
                "boom", request=httpx.Request("PATCH", "https://x"),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        return self._p


def _learning_record(comment_id=1, last_verdict=None):
    return {
        "comment_id": comment_id, "repo": "o/r", "pr_number": 5,
        "review_span_context": None,
        "finding_tags": {"rule_name": "null-deref", "file": "a.py",
                         "line": "10", "severity": "high"},
        "finding_text": "x may be None", "head_sha": "c0ffee",
        "trust_reactors": True, "last_verdict": last_verdict,
    }


@pytest.fixture
def wired(monkeypatch):
    """Wire fake GitHub and a fake store; return the captured side effects."""
    from adapters import install_store

    state = {"records": [_learning_record()], "ledger": [], "resolutions": []}

    def _wire(gh):
        monkeypatch.setattr(lr.httpx, "post", gh.post)
        monkeypatch.setattr(lr.httpx, "patch", gh.patch)
        monkeypatch.setattr(lr, "with_install_token_retry", lambda iid, fn: fn("tok"))
        monkeypatch.setattr(
            lr, "list_comment_records_for_pr",
            lambda iid, repo, pr: list(state["records"]),
        )
        monkeypatch.setattr(
            lr, "update_comment_record_resolution",
            lambda **kw: state["resolutions"].append(kw),
        )
        monkeypatch.setattr(install_store, "put_ledger_row", state["ledger"].append)
        monkeypatch.setattr(install_store, "list_ledger_rows", lambda repo: list(state["ledger"]))
        monkeypatch.setattr(install_store, "put_repo_practices", lambda repo, data: None)
        monkeypatch.setattr(install_store, "put_repo_exemplars", lambda repo, data: None)
        return state
    return _wire


def test_fixed_finding_visibly_changes_state_without_human_action(wired):
    """Acceptance: a later commit fixed the line; the review lane edits the
    comment in place, records the resolution, and gives the ledger the
    evidence, with no person touching the thread."""
    gh = _Gh([_thread(outdated=True)])
    state = wired(gh)
    assert lr.sync_pr(1, "o", "r", 5, review=_review()) == 1

    [(url, body)] = gh.patches
    assert url.endswith("/repos/o/r/pulls/comments/1")
    assert lr.parse_state(body) == lr.FindingState("addressed", HEAD[:7])
    assert lr.original_body(body) == ORIGINAL
    assert state["resolutions"] == [{
        "install_id": 1, "comment_id": 1, "state": "addressed", "sha": HEAD[:7],
    }]
    [row] = state["ledger"]
    assert row["verdict"] == "fixed"
    assert row["resolution"] == "addressed"
    assert row["resolution_sha"] == HEAD[:7]
    assert row["evidence"] == "github-review-comment:1"


def test_second_pass_on_the_same_sha_edits_nothing(wired):
    gh = _Gh([_thread(outdated=True)])
    wired(gh)
    lr.sync_pr(1, "o", "r", 5, review=_review())
    gh.threads = [_thread(outdated=True, body=gh.patches[0][1])]
    assert lr.sync_pr(1, "o", "r", 5, review=_review()) == 0
    assert len(gh.patches) == 1


def test_resolved_without_a_reaction_records_state_but_no_ledger_label(wired):
    """A resolve alone does not say whether the finding was real, so the
    ledger gets no verdict from it."""
    gh = _Gh([_thread(resolved=True)])
    state = wired(gh)
    assert lr.sync_pr(1, "o", "r", 5) == 1
    assert state["ledger"] == []
    assert state["resolutions"][0]["state"] == "resolved"


def test_dismissed_from_the_stored_verdict_writes_false_positive_evidence(wired):
    gh = _Gh([_thread()])
    state = wired(gh)
    state["records"] = [_learning_record(last_verdict="false_positive")]
    assert lr.sync_pr(1, "o", "r", 5) == 1
    assert lr.parse_state(gh.patches[0][1]) == lr.FindingState("dismissed")
    [row] = state["ledger"]
    assert (row["verdict"], row["resolution"]) == ("false-positive", "dismissed")


def test_ledger_label_follows_the_transition_not_the_stale_stored_state(wired):
    """The record is listed before its resolution is updated, so it still
    holds the previous state. The ledger row must take this transition's
    label (addressed = fixed), not one derived from that stale state."""
    gh = _Gh([_thread(outdated=True)])
    state = wired(gh)
    record = _learning_record()
    record["resolution"] = {"state": "resolved", "sha": ""}
    state["records"] = [record]
    assert lr.sync_pr(1, "o", "r", 5, review=_review()) == 1
    [row] = state["ledger"]
    assert (row["verdict"], row["resolution"]) == ("fixed", "addressed")


def test_given_threads_are_reused_without_a_second_listing(wired, monkeypatch):
    gh = _Gh([])
    wired(gh)
    monkeypatch.setattr(lr.httpx, "post", lambda *a, **kw: pytest.fail("listed twice"))
    assert lr.sync_pr(1, "o", "r", 5, threads=[_thread(outdated=True)], review=_review()) == 1


def test_edit_failure_is_logged_and_never_raises(wired, caplog):
    gh = _Gh([_thread(outdated=True), _thread(tid="T2", outdated=True, db_id=2)],
             patch_status=500)
    wired(gh)
    with caplog.at_level("WARNING"):
        assert lr.sync_pr(1, "o", "r", 5, review=_review()) == 0
    assert len(gh.patches) == 2  # one bad edit does not stop the rest
    assert any(r.message == "elder_living_review_edit_failed" for r in caplog.records)


def test_listing_failure_is_logged_and_never_raises(wired, monkeypatch, caplog):
    wired(_Gh([]))

    def boom(*a, **kw):
        raise RuntimeError("down")
    monkeypatch.setattr(lr.httpx, "post", boom)
    with caplog.at_level("WARNING"):
        assert lr.sync_pr(1, "o", "r", 5) == 0
    assert any(r.message == "elder_living_review_list_failed" for r in caplog.records)


def test_poll_lane_syncs_each_due_pr_once_and_caps_the_count(monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(lr, "sync_pr", lambda iid, o, r, pr, **kw: seen.append((iid, o, r, pr)) or 1)
    records = [
        {"comment_id": 1, "repo": "o/r", "pr_number": 5},
        {"comment_id": 2, "repo": "o/r", "pr_number": 5},
        {"comment_id": 3, "repo": "o/r", "pr_number": 6},
        {"comment_id": 4, "repo": "malformed", "pr_number": 7},
    ]
    assert lr.sync_due_prs(records, install_id=9) == 2
    assert seen == [(9, "o", "r", 5), (9, "o", "r", 6)]

    seen.clear()
    many = [{"comment_id": i, "repo": "o/r", "pr_number": i}
            for i in range(lr.MAX_PRS_PER_POLL + 3)]
    lr.sync_due_prs(many, install_id=9)
    assert len(seen) == lr.MAX_PRS_PER_POLL


def test_untrusted_reactor_verdict_never_dismisses(wired):
    """A legacy record (no `trust_reactors`) keeps every reactor's verdict;
    a thumbs-down from someone without write access must not dismiss the
    finding, which would also drop it from the blocking carry-forward."""
    gh = _Gh([_thread()])
    state = wired(gh)
    record = _learning_record(last_verdict="false_positive")
    del record["trust_reactors"]
    state["records"] = [record]
    assert lr.sync_pr(1, "o", "r", 5) == 0
    assert gh.patches == []


def test_fork_push_shows_addressed_but_teaches_nothing_on_its_own(wired):
    """Pushing to a fork needs no write access, so a fork push alone must
    not label Elder's finding `fixed` in the learning corpus. The chip and
    the stored state still show what happened."""
    gh = _Gh([_thread(outdated=True)])
    state = wired(gh)
    assert lr.sync_pr(1, "o", "r", 5, review=_review(trusted_push=False)) == 1
    assert lr.parse_state(gh.patches[0][1]) == lr.FindingState("addressed", HEAD[:7])
    assert state["resolutions"][0]["state"] == "addressed"
    assert state["ledger"] == []


def test_fork_push_with_a_maintainer_thumbs_up_is_fixed(wired):
    gh = _Gh([_thread(outdated=True)])
    state = wired(gh)
    state["records"] = [_learning_record(last_verdict="confirmed")]
    lr.sync_pr(1, "o", "r", 5, review=_review(trusted_push=False))
    [row] = state["ledger"]
    assert (row["verdict"], row["resolution"]) == ("fixed", "addressed")
