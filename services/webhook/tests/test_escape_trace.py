"""#1568 slice 1: tracing a fix or revert PR back to the PR it corrects."""

from __future__ import annotations

from escape_trace import (
    REGRESSION_BODY,
    REGRESSION_TRAILER,
    REVERT_BODY,
    REVERT_TRAILER,
    FindingRef,
    PullRef,
    TraceSources,
    trace_escape,
)

REPO = "acme/widgets"
OTHER_REPO = "elsewhere/gadgets"
SHA = "a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0"


def _sources(pulls, commits=None, findings=None):
    by_num = {p.number: p for p in pulls}
    return TraceSources(
        get_pull=by_num.get,
        pull_for_commit=lambda sha: (commits or {}).get(sha),
        findings_for_pull=lambda n: (findings or {}).get(n, []),
    )


def _pr(n, **kw):
    kw.setdefault("merged", True)
    return PullRef(number=n, **kw)


def test_revert_body_link():
    old = _pr(10, files=("a.py",))
    fix = _pr(20, title='Revert "x"', body="Reverts #10", files=("a.py",))
    t = trace_escape(fix, REPO, _sources([old, fix]))
    assert (t.escaped_pr, t.link, t.chain, t.reverts_root) == (10, REVERT_BODY, (10,), True)
    assert t.traced and t.root_pr == 10


def test_revert_body_accepts_repo_qualified_and_url_refs():
    old = _pr(10)
    for body in (f"Reverts {REPO}#10", f"Reverts https://github.com/{REPO}/pull/10"):
        t = trace_escape(_pr(20, body=body), REPO, _sources([old]))
        assert (t.escaped_pr, t.link) == (10, REVERT_BODY), body


def test_regression_body_link():
    old = _pr(10)
    fix = _pr(21, body="Fixes regression from #10 in the parser")
    t = trace_escape(fix, REPO, _sources([old, fix]))
    assert (t.escaped_pr, t.link, t.reverts_root) == (10, REGRESSION_BODY, None)


def test_revert_trailer_link_resolves_sha_to_pr():
    old = _pr(10)
    fix = _pr(22, commit_messages=(f"Undo it\n\nThis reverts commit {SHA}.\n",))
    t = trace_escape(fix, REPO, _sources([old, fix], commits={SHA: 10}))
    assert (t.escaped_pr, t.link, t.reverts_root) == (10, REVERT_TRAILER, True)


def test_regression_trailer_link():
    old = _pr(10)
    fix = _pr(23, commit_messages=("Fix crash\n\nRegression-of: #10",))
    t = trace_escape(fix, REPO, _sources([old, fix]))
    assert (t.escaped_pr, t.link) == (10, REGRESSION_TRAILER)


def test_body_link_beats_trailer_link():
    fix = _pr(24, body="Reverts #10", commit_messages=("Regression-of: #11",))
    t = trace_escape(fix, REPO, _sources([_pr(10), _pr(11), fix]))
    assert (t.escaped_pr, t.link) == (10, REVERT_BODY)


def test_unresolvable_first_candidate_falls_through_to_the_next():
    fix = _pr(25, body="Reverts #99", commit_messages=("Regression-of: #11",))
    t = trace_escape(fix, REPO, _sources([_pr(11), fix]))
    assert (t.escaped_pr, t.link) == (11, REGRESSION_TRAILER)


def test_correcting_pr_with_no_link_traces_to_nothing():
    fix = _pr(30, title="Fix typo", body="Small cleanup, nothing else.")
    t = trace_escape(fix, REPO, _sources([fix]))
    assert not t.traced
    assert (t.escaped_pr, t.link, t.chain, t.touching_findings) == (None, None, (), ())
    assert t.root_pr is None and "no link" in t.reason


def test_ref_to_another_repo_is_ignored():
    fix = _pr(31, body=f"Reverts {OTHER_REPO}#10")
    t = trace_escape(fix, REPO, _sources([_pr(10), fix]))
    assert not t.traced


def test_unmerged_target_and_self_reference_do_not_link():
    fix = _pr(32, body="Reverts #10\nReverts #32")
    t = trace_escape(fix, REPO, _sources([_pr(10, merged=False), fix]))
    assert not t.traced


def test_unmerged_correcting_pr_is_not_traced():
    fix = _pr(33, body="Reverts #10", merged=False)
    t = trace_escape(fix, REPO, _sources([_pr(10), fix]))
    assert not t.traced and "not merged" in t.reason


def test_revert_of_a_revert_follows_the_chain_and_reapplies():
    a = _pr(10)
    b = _pr(20, body="Reverts #10")
    c = _pr(30, body="Reverts #20")
    t = trace_escape(c, REPO, _sources([a, b, c]))
    assert (t.escaped_pr, t.chain, t.root_pr) == (20, (20, 10), 10)
    assert t.reverts_root is False


def test_revert_chain_of_three_removes_the_root_again():
    pulls = [_pr(10), _pr(20, body="Reverts #10"), _pr(30, body="Reverts #20"),
             _pr(40, body="Reverts #30")]
    t = trace_escape(pulls[3], REPO, _sources(pulls))
    assert t.chain == (30, 20, 10) and t.reverts_root is True


def test_revert_cycle_terminates():
    b = _pr(20, body="Reverts #30")
    c = _pr(30, body="Reverts #20")
    t = trace_escape(c, REPO, _sources([b, c]))
    assert t.chain == (20,)


def test_chain_depth_is_capped():
    pulls = [_pr(1)] + [_pr(n, body=f"Reverts #{n - 1}") for n in range(2, 12)]
    t = trace_escape(pulls[-1], REPO, _sources(pulls), max_depth=3)
    assert len(t.chain) == 3


def test_regression_link_does_not_follow_a_chain():
    a = _pr(10)
    b = _pr(20, body="Reverts #10")
    fix = _pr(30, body="Fixes regression from #20")
    t = trace_escape(fix, REPO, _sources([a, b, fix]))
    assert (t.escaped_pr, t.chain) == (20, (20,))


def test_findings_on_shared_files_are_attached():
    old = _pr(10, files=("a.py", "b.py"))
    fix = _pr(20, body="Reverts #10", files=("b.py", "c.py"))
    hit = FindingRef(pr=10, path="b.py", finding_class="null-deref", verdict="declined")
    miss = FindingRef(pr=10, path="a.py", finding_class="style", verdict="fixed")
    t = trace_escape(fix, REPO, _sources([old, fix], findings={10: [hit, miss]}))
    assert t.touching_findings == (hit,)
    assert t.shared_files == ("b.py",)


def test_no_shared_files_means_no_findings():
    old = _pr(10, files=("a.py",))
    fix = _pr(20, body="Reverts #10", files=("z.py",))
    f = FindingRef(pr=10, path="a.py")
    t = trace_escape(fix, REPO, _sources([old, fix], findings={10: [f]}))
    assert t.traced and t.touching_findings == () and t.shared_files == ()


def test_ordinary_prose_without_a_pull_reference_does_not_link():
    body = "This reverts the cache change. A regression in latency, see the dashboard. Fixes #"
    fix = _pr(34, body=body, commit_messages=("Tidy up\n\nRegression-of: the parser",))
    t = trace_escape(fix, REPO, _sources([_pr(10), fix]))
    assert not t.traced and t.link is None
