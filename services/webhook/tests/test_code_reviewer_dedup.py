"""Tests for personas/code_reviewer/dedup.py.

On a `synchronize` push, Grug must not re-post a finding it already
commented on an unchanged line. Findings key on (file, line, rule);
prior Grug comments are identified by a hidden rule-marker in the
comment body. A moved/changed line yields a different key → posts as
new (the desired behavior)."""
from __future__ import annotations

from personas.code_reviewer import dedup
from personas.code_reviewer.persona import Finding


def _finding(file="x.py", line=2, rule="null-deref", sev="high") -> Finding:
    return Finding(
        file=file, line=line, severity=sev, rule_name=rule,
        message="m", suggestion=None,
    )


# --- key + marker ---

def test_finding_key_combines_rule_file_line():
    k = dedup.finding_key("src/x.py", 10, "null-deref")
    assert "null-deref" in k and "src/x.py" in k and "10" in k


def test_finding_key_differs_on_line():
    assert dedup.finding_key("x.py", 10, "r") != dedup.finding_key("x.py", 11, "r")


def test_finding_key_differs_on_rule():
    assert dedup.finding_key("x.py", 10, "a") != dedup.finding_key("x.py", 10, "b")


def test_rule_marker_round_trips():
    body = "some comment\n\n" + dedup.rule_marker("silent-exception-swallow")
    assert dedup.parse_rule(body) == "silent-exception-swallow"


def test_parse_rule_returns_none_without_marker():
    assert dedup.parse_rule("a human comment with no marker") is None


def test_parse_rule_handles_none_body():
    """A comment dict with `body: None` must not blow up the regex."""
    assert dedup.parse_rule(None) is None  # type: ignore[arg-type]


# --- prior_keys_from_comments ---

def test_prior_keys_extracts_grug_findings():
    comments = [
        {"path": "x.py", "line": 2, "body": "review\n" + dedup.rule_marker("null-deref")},
        {"path": "y.py", "line": 9, "body": "review\n" + dedup.rule_marker("race-condition")},
    ]
    keys = dedup.prior_keys_from_comments(comments)
    assert dedup.finding_key("x.py", 2, "null-deref") in keys
    assert dedup.finding_key("y.py", 9, "race-condition") in keys


def test_prior_keys_skips_non_grug_comments():
    """A human review comment (no marker) contributes no key — we only
    dedup against our OWN prior findings."""
    comments = [
        {"path": "x.py", "line": 2, "body": "looks good to me"},
        {"path": "x.py", "line": 3, "body": "m\n" + dedup.rule_marker("dead-code")},
    ]
    keys = dedup.prior_keys_from_comments(comments)
    assert keys == {dedup.finding_key("x.py", 3, "dead-code")}


def test_parse_rule_takes_last_marker():
    """A message quoting a literal marker, then the real trailing one →
    the real (last) marker wins, not the embedded fake."""
    body = (
        "Found a dupe of <!-- grug-rule:fake-embedded -->\n\n"
        + dedup.rule_marker("real-rule")
    )
    assert dedup.parse_rule(body) == "real-rule"


def test_prior_keys_skips_left_side_comment():
    """A LEFT-side comment can't be Grug's (we post RIGHT-side) — even
    with a coincidental marker it must not contribute a key."""
    comments = [{
        "path": "x.py", "line": 2, "side": "LEFT",
        "body": dedup.rule_marker("null-deref"),
    }]
    assert dedup.prior_keys_from_comments(comments) == set()


def test_prior_keys_keeps_right_side_and_absent_side():
    """RIGHT-side (and side-absent, defaulting RIGHT) comments count."""
    comments = [
        {"path": "x.py", "line": 2, "side": "RIGHT", "body": dedup.rule_marker("a")},
        {"path": "y.py", "line": 3, "body": dedup.rule_marker("b")},  # no side
    ]
    keys = dedup.prior_keys_from_comments(comments)
    assert keys == {
        dedup.finding_key("x.py", 2, "a"), dedup.finding_key("y.py", 3, "b"),
    }


def test_prior_keys_skips_non_numeric_line():
    """A malformed `line` must be skipped, not raise out of best-effort
    dedup (the caller only catches httpx errors)."""
    comments = [{"path": "x.py", "line": "not-a-number",
                 "body": dedup.rule_marker("r")}]
    assert dedup.prior_keys_from_comments(comments) == set()


def test_prior_keys_tolerates_missing_line():
    """A comment with no `line` (e.g. a file-level or outdated comment)
    can't form a (file,line,rule) key — skip it, don't crash."""
    comments = [{"path": "x.py", "line": None, "body": dedup.rule_marker("r")}]
    assert dedup.prior_keys_from_comments(comments) == set()


# --- dedup_findings ---

def test_dedup_skips_finding_already_commented_same_line():
    findings = (_finding(line=2, rule="null-deref"),)
    prior = {dedup.finding_key("x.py", 2, "null-deref")}
    assert dedup.dedup_findings(findings, prior) == ()


def test_dedup_keeps_finding_on_moved_line():
    """Same file+rule but the line shifted (surrounding edit) → different
    key → treated as NEW, re-posted. AC: moved-line detection."""
    findings = (_finding(line=5, rule="null-deref"),)
    prior = {dedup.finding_key("x.py", 2, "null-deref")}  # was line 2
    assert dedup.dedup_findings(findings, prior) == findings


def test_dedup_keeps_new_rule_on_same_line():
    """A different rule firing on a line that already has a Grug comment
    IS posted — two distinct findings can share a line."""
    findings = (_finding(line=2, rule="race-condition"),)
    prior = {dedup.finding_key("x.py", 2, "null-deref")}
    assert dedup.dedup_findings(findings, prior) == findings


def test_dedup_mixed_keep_and_skip():
    keep = _finding(line=5, rule="null-deref")
    skip = _finding(line=2, rule="dead-code")
    prior = {dedup.finding_key("x.py", 2, "dead-code")}
    out = dedup.dedup_findings((keep, skip), prior)
    assert out == (keep,)


def test_dedup_empty_prior_keeps_all():
    findings = (_finding(line=2), _finding(line=3))
    assert dedup.dedup_findings(findings, set()) == findings


def test_prior_keys_skips_comment_without_path():
    """A comment missing `path` (malformed payload) is skipped, not a
    KeyError — uniform with the line-less guard."""
    comments = [{"line": 2, "body": dedup.rule_marker("r")}]
    assert dedup.prior_keys_from_comments(comments) == set()


def test_rule_name_charset_enforced_at_source():
    """code_review_prompt.ReviewRule rejects a rule name outside the
    marker charset — guarantees every real rule round-trips the dedup
    marker so finding-side and prior-side keys can't diverge."""
    import code_review_prompt as crp
    import pytest as _pytest
    with _pytest.raises(ValueError, match=r"\[A-Za-z0-9_-\]"):
        crp.ReviewRule(
            name="weird@rule", bug_class="correctness",
            description="long enough desc", bad_example="b",
            good_example="g", severity="low",
        )


def test_dedup_hallucinated_rule_fails_safe_posts():
    """A finding with an out-of-charset rule (only possible from a
    hallucinated LLM rule, since real rules are charset-enforced) can't
    match a marker-parsed prior key → it POSTS (safe direction: a
    duplicate, never a skipped real finding)."""
    weird = _finding(line=2, rule="weird@rule")
    # Even if a prior key existed for the truncated form, the
    # finding-side key uses the full raw rule → no match → kept.
    prior = {dedup.finding_key("x.py", 2, "weird")}
    assert dedup.dedup_findings((weird,), prior) == (weird,)
