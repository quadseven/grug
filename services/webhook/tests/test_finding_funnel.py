"""Every Elder pass publishes through one finding funnel (#1586).

The tier-1 path used to be the only caller of the correctness-only,
unreachable-defensive and checked-shell filters. These tests pin the same
filters on the deep pass, the quarantined pass and the eval replay, and pin
the falsification verifier (#1567) inside the funnel."""
from __future__ import annotations

import time
from unittest.mock import MagicMock

from llm_client import (
    Backend,
    FindingJudgement,
    LlmReviewResponse,
)
from llm_client import (
    Finding as LlmFinding,
)
from personas.code_reviewer import dispatch as cr_dispatch
from personas.code_reviewer.judge import RefuteOutcome
from personas.code_reviewer.persona import (
    CodeReviewEvaluation,
    Finding,
)
from review_evidence import assemble_evidence

_SHELL_PATH = "auth/rotate.sh"
_PY_PATH = "auth/client.py"
_SHELL_TEXT = (
    "#!/usr/bin/env bash\n"
    "if ! curl -fsS https://example.com/health; then\n"
    "  exit 1\n"
    "fi\n"
)
_PY_TEXT = "import httpx\nresp = httpx.get(url)\nvalue = resp.json()['x']\n"
_DIFF = (
    f"diff --git a/{_SHELL_PATH} b/{_SHELL_PATH}\n"
    f"--- a/{_SHELL_PATH}\n+++ b/{_SHELL_PATH}\n"
    "@@ -1,1 +1,4 @@\n"
    " #!/usr/bin/env bash\n"
    "+if ! curl -fsS https://example.com/health; then\n"
    "+  exit 1\n"
    "+fi\n"
    f"diff --git a/{_PY_PATH} b/{_PY_PATH}\n"
    f"--- a/{_PY_PATH}\n+++ b/{_PY_PATH}\n"
    "@@ -1,1 +1,3 @@\n"
    " import httpx\n"
    "+resp = httpx.get(url)\n"
    "+value = resp.json()['x']\n"
)
_CONTENTS = {_SHELL_PATH: _SHELL_TEXT, _PY_PATH: _PY_TEXT}


def _llm_findings() -> tuple[LlmFinding, ...]:
    return (
        # Checked by `if !`: the checked-shell filter drops it (#771).
        LlmFinding(
            path=_SHELL_PATH, line=2, rule="missing-error-handling",
            severity="medium", message="curl exit status is not checked",
            suggestion="check it", effort="quick-win",
        ),
        # A defensive rule naming no reachable caller or input: dropped.
        LlmFinding(
            path=_PY_PATH, line=2, rule="missing-timeout",
            severity="medium", message="httpx.get has no timeout",
            suggestion="add timeout=10", effort="quick-win",
        ),
        # The same defensive class naming a reachable caller: kept.
        LlmFinding(
            path=_PY_PATH, line=3, rule="null-deref",
            severity="medium",
            message="caller `sync` sends a body without x, so this raises",
            suggestion="use .get", effort="quick-win",
        ),
    )


def _response() -> LlmReviewResponse:
    return LlmReviewResponse(
        kind="reviewed", findings=_llm_findings(),
        backend_used=Backend.POOLSIDE, model_name="laguna",
    )


def _evidence(**pr_context):
    hunks = tuple(cr_dispatch.parse_diff(_DIFF))
    evidence = assemble_evidence(
        cr_dispatch._to_llm_hunks(hunks), pr_context=pr_context or None,
        file_contents=_CONTENTS,
    )
    return hunks, evidence


def _no_judge(monkeypatch):
    monkeypatch.setattr(cr_dispatch, "grade_findings", lambda *a, **kw: ())


def _rules(findings) -> list[str]:
    return sorted(f.rule_name for f in findings)


def test_deep_pass_drops_checked_shell_and_unreachable_defensive(monkeypatch):
    _no_judge(monkeypatch)
    hunks, evidence = _evidence()
    deep_eval, _, _, _ = cr_dispatch._grade_deep_response(
        _response(), hunks, evidence, 11,
        pr_context={}, owner="o", repo_name="r", pull_number=1,
    )

    assert _rules(deep_eval.findings) == ["null-deref"]


def test_quarantined_pass_drops_checked_shell_and_unreachable_defensive(
    monkeypatch,
):
    _no_judge(monkeypatch)
    monkeypatch.setenv("GRUG_QUARANTINED_SECURITY_PASS", "1")
    monkeypatch.setattr(cr_dispatch, "falsification_verifier_enabled", lambda: False)
    monkeypatch.setattr(
        cr_dispatch, "refute_with_outcome",
        lambda findings, *a, **kw: RefuteOutcome((), frozenset()),
    )
    # A high finding is blocking, so the merge would keep it: only the
    # checked-shell filter can stop it.
    findings = tuple(
        f if f.rule != "missing-error-handling"
        else LlmFinding(
            path=f.path, line=f.line, rule=f.rule, severity="high",
            message=f.message, suggestion=f.suggestion, effort=f.effort,
        )
        for f in _llm_findings()
    )
    response = LlmReviewResponse(
        kind="reviewed", findings=findings,
        backend_used=Backend.POOLSIDE, model_name="laguna",
    )
    captured = []
    monkeypatch.setattr(
        cr_dispatch, "review_diff",
        lambda evidence, **kw: captured.append(evidence) or response,
    )
    monkeypatch.setattr(cr_dispatch, "submit_evals", lambda *a, **kw: None)
    hunks, evidence = _evidence()
    primary = CodeReviewEvaluation(findings=(), conclusion="success")

    merged, note = cr_dispatch._run_quarantined_pass(
        primary, hunks, evidence, 11,
        repo_config=cr_dispatch.RepoConfig(), started=time.monotonic(),
        voice=MagicMock(), cancel_event=None, owner="o", repo_name="r",
        pull_number=1, head_sha="abc", skip=False,
    )

    assert captured, "the quarantined pass never ran"
    # The merge keeps only security or blocking second-pass findings, so
    # nothing a filter should have dropped may survive into the review.
    assert "missing-error-handling" not in _rules(merged.findings)
    assert "missing-timeout" not in _rules(merged.findings)
    assert note == ""


def test_quarantined_grading_runs_the_same_filters(monkeypatch):
    _no_judge(monkeypatch)
    hunks, evidence = _evidence()
    graded, _, _, _ = cr_dispatch._grade_deep_response(
        _response(), hunks, evidence, 11,
        pr_context=cr_dispatch.quarantined_evidence(
            evidence, (_SHELL_PATH, _PY_PATH),
        ).pr_context or {},
        owner="o", repo_name="r", pull_number=1, arm="quarantined",
    )

    assert _rules(graded.findings) == ["null-deref"]


def test_deep_pass_drops_non_correctness_on_a_correctness_only_review(
    monkeypatch,
):
    _no_judge(monkeypatch)
    hunks, evidence = _evidence()
    response = LlmReviewResponse(
        kind="reviewed",
        findings=_llm_findings() + (LlmFinding(
            path=_PY_PATH, line=3, rule="dead-code", severity="low",
            message="value is unused", suggestion="drop it",
            effort="quick-win",
        ),),
        backend_used=Backend.POOLSIDE, model_name="laguna",
    )
    deep_eval, _, _, _ = cr_dispatch._grade_deep_response(
        response, hunks, evidence, 11,
        pr_context={"review_scope": cr_dispatch.CORRECTNESS_ONLY_SCOPE},
        owner="o", repo_name="r", pull_number=1,
    )

    assert _rules(deep_eval.findings) == ["null-deref"]


def test_eval_publishes_the_same_set_as_dispatch(monkeypatch):
    """One fixture through the deep grading and the eval replay: the same
    findings publish, and the eval reads the fixture's file contents."""
    from elder_eval.runner import _published_findings

    _no_judge(monkeypatch)
    hunks, evidence = _evidence()
    deep_eval, _, _, _ = cr_dispatch._grade_deep_response(
        _response(), hunks, evidence, 11,
        pr_context=evidence.pr_context or {}, owner="o", repo_name="r",
        pull_number=1,
    )
    published = _published_findings(
        _response(), hunks, evidence,
        grade=lambda *a, **kw: (), refute=lambda *a, **kw: (),
    )

    def key(findings):
        return sorted((f.file, f.line, f.rule_name) for f in findings)

    assert key(published) == key(deep_eval.findings)
    assert _rules(published) == ["null-deref"]


def test_falsification_verifier_runs_inside_the_funnel():
    from personas.code_reviewer.finding_funnel import publish_funnel

    high = Finding(
        file=_PY_PATH, line=3, rule_name="null-deref", severity="high",
        message="json() may lack key x", suggestion=None,
    )
    evaluation = CodeReviewEvaluation(findings=(high,), conclusion="failure")
    calls = []

    def falsify(findings, plain_findings, plain_verdicts):
        calls.append((findings, plain_findings, plain_verdicts))
        return ()

    verdicts = (FindingJudgement(0, True, "real", 0.9),)
    result = publish_funnel(
        evaluation, _CONTENTS, {},
        grade=lambda e: verdicts,
        verify=lambda e: e,
        falsify=falsify,
    )

    assert calls == [((high,), (high,), verdicts)]
    assert result.evaluation.findings == ()


def test_funnel_filters_run_before_the_judge():
    """The judge never grades a finding a deterministic filter drops."""
    from personas.code_reviewer.finding_funnel import publish_funnel

    defensive = Finding(
        file=_PY_PATH, line=2, rule_name="missing-timeout", severity="medium",
        message="httpx.get has no timeout", suggestion=None,
    )
    graded = []

    def grade(evaluation):
        graded.append(evaluation.findings)
        return ()

    result = publish_funnel(
        CodeReviewEvaluation(findings=(defensive,), conclusion="success"),
        _CONTENTS, {}, grade=grade, verify=lambda e: e,
        falsify=lambda findings, *_: findings,
    )

    assert graded == [()]
    assert result.defensive_dropped == 1
    assert result.evaluation.findings == ()
