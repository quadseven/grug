"""Elder reviews an agent pull request with another model family (grug#1437)."""
from __future__ import annotations

import json
import threading
from unittest.mock import patch

import activity_log
import httpx
import llm_client as lc
import pytest
from llm_client import Backend, BackendConfig, Hunk, review_diff
from model_family import (
    ReviewFamilies,
    app_author_family,
    model_family,
    review_families,
)
from personas.code_reviewer import dispatch as cr_dispatch
from personas.code_reviewer import persona as cr_persona
from personas.tpm.dor_checks import loom_footer_model
from review_evidence import assemble_evidence

_CLAUDE = "claude-haiku-4.5"
_LAGUNA = "poolside/laguna-s-2.1"
_DEEPSEEK = "deepseek-v4.1-flash"


def _tier(backend: Backend, model: str) -> BackendConfig:
    return BackendConfig(
        backend=backend, url=f"http://{backend.value}.test/v1/chat/completions",
        model=model, key_loader=lambda: "test-key",
        retry_attempts=1, transport_retry_attempts=1,
    )


@pytest.fixture(autouse=True)
def _setup_env(monkeypatch):
    monkeypatch.setattr(lc, "_RETRY_SLEEP", lambda s: None)
    monkeypatch.setattr(lc, "_bench_state", {}, raising=False)
    monkeypatch.setattr(lc, "_slow_bench_state", {}, raising=False)
    monkeypatch.setenv("GRUG_REVIEW_BACKEND_PRIORITY", "cloud")
    monkeypatch.setenv("GRUG_REVIEW_DEPTH", "fast")
    monkeypatch.delenv("GRUG_CAVE_GATEWAY_URL", raising=False)
    monkeypatch.setattr(
        lc, "_cloud_chain_tiers",
        lambda: [
            _tier(Backend.OPENCODE_GO, _CLAUDE),
            _tier(Backend.POOLSIDE, _LAGUNA),
        ],
    )
    monkeypatch.setattr(lc, "_free_provider_fallback_configs", list)
    monkeypatch.setattr(lc, "_subscription_fallback_config", lambda: None)


def _hunk() -> Hunk:
    return Hunk(path="src/x.py", body="@@ -1 +1 @@\n-old\n+new")


def _pr(body: str, association: str = "OWNER", user_type: str = "User", login: str = "alice") -> dict:
    return {"body": body, "user": {"login": login, "type": user_type}, "author_association": association}


def _answer() -> httpx.Response:
    body = {
        "choices": [{
            "message": {"content": json.dumps({"findings": []}), "role": "assistant"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 500, "total_tokens": 510},
    }
    return httpx.Response(200, text=json.dumps(body))


def _review(body: str, *, down: frozenset[str] = frozenset()):
    """Run one review; returns (response, models called in order)."""
    called: list[str] = []

    def post(url, **kwargs):
        model = kwargs["json"]["model"]
        called.append(model)
        if model in down:
            raise httpx.ConnectTimeout("arm down")
        return _answer()

    context = {"repo": "o/r", "pr_number": 1, "head_sha": "a" * 40, "body": body}
    family = cr_dispatch._author_family(_pr(body))
    if family:
        context["author_family"] = family
    evidence = assemble_evidence([_hunk()], pr_context=context)
    with patch.object(httpx, "post", side_effect=post):
        return review_diff(evidence, installation_id=1), called


def test_same_family_first_arm_gives_way_to_another_family() -> None:
    response, called = _review("Why\n\nLoom ticket #7, lane fix, model claude-opus-5-5.\n")

    assert called == [_LAGUNA]
    assert response.kind == "reviewed"
    assert response.families == ReviewFamilies(author="claude", reviewers=("laguna",))


def test_other_arm_failing_walks_back_to_the_same_family_with_a_note() -> None:
    response, called = _review(
        "Loom ticket #7, lane fix, model opus\n", down=frozenset({_LAGUNA}),
    )

    assert called == [_LAGUNA, _CLAUDE]
    assert response.kind == "reviewed"
    assert response.families is not None and response.families.same_family
    evaluation = cr_persona.evaluate_diff((), response)
    _, summary = cr_dispatch._summary_markdown(evaluation)
    assert "same-family review" in summary
    assert "Author family: `claude`" in summary
    assert "Reviewer family: `claude`" in summary


def test_no_footer_keeps_todays_arm_order() -> None:
    response, called = _review("A person wrote this.\n")

    assert called == [_CLAUDE]
    assert response.families is None
    evaluation = cr_persona.evaluate_diff((), response)
    _, summary = cr_dispatch._summary_markdown(evaluation)
    assert "family" not in summary.lower()


def test_unknown_footer_model_keeps_todays_arm_order() -> None:
    _, called = _review("Loom ticket #7, lane fix, model mystery-9\n")

    assert called == [_CLAUDE]


def test_different_family_first_arm_keeps_its_place_with_no_note() -> None:
    response, called = _review("Loom ticket #7, lane fix, model deepseek-v4\n")

    assert called == [_CLAUDE]
    evaluation = cr_persona.evaluate_diff((), response)
    _, summary = cr_dispatch._summary_markdown(evaluation)
    assert "Author family: `deepseek`" in summary
    assert "Reviewer family: `claude`" in summary
    assert "same-family review" not in summary


def test_untrusted_authors_footer_is_ignored() -> None:
    body = "Loom ticket #7, lane fix, model claude-opus-5-5.\n"

    assert cr_dispatch._author_family(_pr(body)) == "claude"
    assert cr_dispatch._author_family(_pr(body, "NONE")) is None
    assert cr_dispatch._author_family(_pr(body, "CONTRIBUTOR")) is None
    assert cr_dispatch._author_family({"body": body}) is None


def test_cancelled_review_never_walks_back_to_the_same_family() -> None:
    cancel = threading.Event()
    called: list[str] = []

    def post(url, **kwargs):
        called.append(kwargs["json"]["model"])
        cancel.set()
        raise httpx.ConnectTimeout("arm down")

    evidence = assemble_evidence(
        [_hunk()],
        pr_context={"repo": "o/r", "pr_number": 1, "author_family": "claude"},
    )
    with patch.object(httpx, "post", side_effect=post):
        response = review_diff(evidence, installation_id=1, cancel_event=cancel)

    assert response.kind != "reviewed"
    assert _CLAUDE not in called


def test_all_arms_failing_keeps_the_first_walk_failure() -> None:
    response, called = _review(
        "Loom ticket #7, lane fix, model opus\n",
        down=frozenset({_LAGUNA, _CLAUDE}),
    )

    assert response.kind == "all_failed"
    assert response.families is None
    assert _LAGUNA in called and _CLAUDE in called


def test_families_survive_the_evaluation_rewrites() -> None:
    families = ReviewFamilies(author="claude", reviewers=("laguna",))
    response = lc.LlmReviewResponse(kind="reviewed", families=families)
    evaluation = cr_persona.evaluate_diff((), response)
    assert evaluation.families == families
    assert cr_persona.with_findings(evaluation, ()).families == families
    degraded = cr_persona.CodeReviewEvaluation(
        findings=(), conclusion="neutral", degraded_reason="all_failed",
    )
    assert cr_persona.with_degradation(evaluation, degraded).families == families


def test_app_author_family_uses_trusted_source() -> None:
    """Test that App authors use the trusted source for author family (grug#1553)."""
    # Mock the app_author_family function to test the integration
    with patch.object(cr_dispatch, 'app_author_family') as mock_app_family:
        mock_app_family.return_value = "claude"

        # Create a PR with a Bot user (App author)
        pr = _pr("Some PR body", user_type="Bot", login="my-app[bot]")

        from personas.code_reviewer.dispatch import _author_family
        result = _author_family(pr)

        assert result == "claude"
        mock_app_family.assert_called_once_with("my-app[bot]")


def test_app_author_family_not_listed_gets_none() -> None:
    """Test that unlisted App authors get None from the trusted source (grug#1553)."""
    # Mock the app_author_family function to test the integration
    with patch.object(cr_dispatch, 'app_author_family') as mock_app_family:
        mock_app_family.return_value = None

        # Create a PR with a Bot user (App author)
        pr = _pr("Some PR body", user_type="Bot", login="unknown-app[bot]")

        from personas.code_reviewer.dispatch import _author_family
        result = _author_family(pr)

        assert result is None
        mock_app_family.assert_called_once_with("unknown-app[bot]")


def test_app_author_family_case_insensitive() -> None:
    """Test that App author family lookup is case insensitive (grug#1553)."""
    # Mock the app_author_family function to test the integration
    with patch.object(cr_dispatch, 'app_author_family') as mock_app_family:
        mock_app_family.return_value = "gpt"

        # Create a PR with a Bot user (App author) with different case
        pr = _pr("Some PR body", user_type="Bot", login="My-App[Bot]")

        from personas.code_reviewer.dispatch import _author_family
        result = _author_family(pr)

        assert result == "gpt"
        mock_app_family.assert_called_once_with("My-App[Bot]")


def test_app_author_family_not_used_for_human_authors() -> None:
    """Test that App author family is not used for human authors (grug#1553)."""
    # Mock the app_author_family function to verify it's not called
    with patch.object(cr_dispatch, 'app_author_family') as mock_app_family:
        # Create a PR with a User (human) author
        pr = _pr("Loom ticket #7, lane fix, model claude-opus-5-5.", user_type="User")

        from personas.code_reviewer.dispatch import _author_family
        result = _author_family(pr)

        assert result == "claude"
        mock_app_family.assert_not_called()


def test_app_author_family_overrides_trusted_operator_check() -> None:
    """Test that App authors bypass the trusted_operator check (grug#1553)."""
    # Mock the app_author_family function to test the integration
    with patch.object(cr_dispatch, 'app_author_family') as mock_app_family:
        mock_app_family.return_value = "gpt"

        # Create a PR with a Bot user (App author)
        pr = _pr("Some PR body", user_type="Bot", login="lane[bot]")

        from personas.code_reviewer.dispatch import _author_family
        result = _author_family(pr)

        assert result == "gpt"
        mock_app_family.assert_called_once_with("lane[bot]")


def test_app_author_family_authorization_association_ignored() -> None:
    """Test that App author family lookup ignores author_association (grug#1553)."""
    # Mock the app_author_family function to test the integration
    with patch.object(cr_dispatch, 'app_author_family') as mock_app_family:
        mock_app_family.return_value = "gemini"

        # Create a PR with a Bot user (App author) and untrusted association
        pr = _pr("Some PR body", user_type="Bot", login="lane[bot]", association="NONE")

        from personas.code_reviewer.dispatch import _author_family
        result = _author_family(pr)

        assert result == "gemini"
        mock_app_family.assert_called_once_with("lane[bot]")


def test_activity_log_hands_both_families_to_the_store(monkeypatch) -> None:
    stored: dict = {}
    monkeypatch.setattr(activity_log, "put_check_verdict", lambda **kw: stored.update(kw))

    activity_log.record_check_verdict(
        install_id=1, persona_key="code_reviewer", repo="o/r", pr_number=1,
        head_sha="a" * 40, conclusion="success", summary="s", findings_count=0,
        blocking=False, author_family="claude", reviewer_family="laguna",
    )

    assert stored["author_family"] == "claude"
    assert stored["reviewer_family"] == "laguna"


@pytest.mark.parametrize(
    ("model", "family"),
    [
        ("claude-opus-5-5", "claude"),
        ("opus", "claude"),
        ("anthropic/claude-haiku-4.5", "claude"),
        ("claude-code", "claude"),
        ("gpt-5.6-luna", "gpt"),
        ("codex", "gpt"),
        ("qwen3-coder-next:q8_0", "qwen"),
        ("nvidia/nemotron-3-super-120b-a12b:free", "nemotron"),
        ("spark:warm-any", None),
        ("openrouter/free", None),
        ("", None),
        (None, None),
    ],
)
def test_model_family(model: str | None, family: str | None) -> None:
    assert model_family(model) == family


def test_reviewer_with_no_known_family_reads_unknown() -> None:
    families = review_families("claude", ("spark:warm-any",))
    assert families == ReviewFamilies(author="claude", reviewers=("unknown",))
    assert families is not None and not families.same_family
    assert review_families(None, (_CLAUDE,)) is None


def test_mixed_cohorts_with_a_same_family_arm_count_as_same_family() -> None:
    families = review_families("claude", (_LAGUNA, _CLAUDE))
    assert families is not None
    assert families.same_family
    assert families.reviewer == "claude, laguna"


@pytest.mark.parametrize(
    ("body", "model"),
    [
        ("Loom ticket #5, lane spark-3, model qwen3-coder.\n", "qwen3-coder"),
        ("x\nLoom ticket #7, lane fix, model opus\n", "opus"),
        ("Loom ticket #7, lane fix\n", None),
        ("model opus\n", None),
        ("", None),
    ],
)
def test_loom_footer_model(body: str, model: str | None) -> None:
    assert loom_footer_model(body) == model


@pytest.mark.parametrize(
    ("model", "family"),
    [("glm-4.6", "glm"), ("z-ai/glm-5", "glm"),
     ("longcat-flash-chat", "longcat"), ("meituan/longcat-2", "longcat")],
)
def test_glm_and_longcat_families_still_resolve(model: str, family: str) -> None:
    assert model_family(model) == family


def test_app_author_family_reads_the_env_table(monkeypatch) -> None:
    monkeypatch.setenv(
        "GRUG_APP_AUTHOR_FAMILIES", "lane-app[bot]:claude;\nOther-App[bot]:GPT\n",
    )
    assert app_author_family("lane-app[bot]") == "claude"
    assert app_author_family("LANE-APP[BOT]") == "claude"
    assert app_author_family("other-app[bot]") == "gpt"


def test_app_author_family_matches_only_exact_logins(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_APP_AUTHOR_FAMILIES", "lane-app[bot]:claude")
    assert app_author_family("lane-app") is None
    assert app_author_family("my-lane-app[bot]") is None
    assert app_author_family("claude[bot]") is None
    assert app_author_family("") is None
    assert app_author_family(None) is None


def test_app_author_family_ignores_unknown_families_and_bad_pairs(monkeypatch) -> None:
    monkeypatch.setenv(
        "GRUG_APP_AUTHOR_FAMILIES", "a[bot]:not-a-family;b[bot];:claude;c[bot]:",
    )
    for login in ("a[bot]", "b[bot]", "c[bot]", ""):
        assert app_author_family(login) is None


def test_app_author_family_unset_gives_no_family(monkeypatch) -> None:
    monkeypatch.delenv("GRUG_APP_AUTHOR_FAMILIES", raising=False)
    assert app_author_family("lane-app[bot]") is None


def test_bot_author_family_flows_from_the_env_table(monkeypatch) -> None:
    monkeypatch.setenv("GRUG_APP_AUTHOR_FAMILIES", "lane-app[bot]:gpt")
    pr = _pr("Some PR body", user_type="Bot", login="lane-app[bot]", association="NONE")
    assert cr_dispatch._author_family(pr) == "gpt"
    human = _pr("Some PR body", user_type="User", login="lane-app[bot]", association="NONE")
    assert cr_dispatch._author_family(human) is None
