"""Tests for the sage voice pack feature (#288)."""
import pytest

class TestSummaryMarkdownVoice:
    """Test that voice selection is respected in check-run output."""

    def test_summary_markdown_caveman_degraded(self) -> None:
        from personas.code_reviewer.dispatch import _summary_markdown
        from personas.code_reviewer.persona import CodeReviewEvaluation
        
        evaluation = CodeReviewEvaluation(
            degraded_reason="test error",
            findings=(),
            dropped_hallucinations=0,
            conclusion="neutral",
        )
        
        title, summary = _summary_markdown(evaluation, voice="caveman")
        
        # Caveman uses "Grug Elder" as subject
        assert "Grug Elder" in title or "Grug Elder" in summary

    def test_summary_markdown_sage_degraded(self) -> None:
        from personas.code_reviewer.dispatch import _summary_markdown
        from personas.code_reviewer.persona import CodeReviewEvaluation
        
        evaluation = CodeReviewEvaluation(
            degraded_reason="test error",
            findings=(),
            dropped_hallucinations=0,
            conclusion="neutral",
        )
        
        title, summary = _summary_markdown(evaluation, voice="sage")
        
        # Sage has distinct inverted cadence with Hmm/Yes
        assert "Hmm" in summary or "yes" in summary.lower() or "Yes" in summary

    def test_summary_markdown_caveman_no_findings(self) -> None:
        from personas.code_reviewer.dispatch import _summary_markdown
        from personas.code_reviewer.persona import CodeReviewEvaluation
        
        evaluation = CodeReviewEvaluation(
            degraded_reason=None,
            findings=(),
            dropped_hallucinations=0,
            conclusion="success",
        )
        
        title, summary = _summary_markdown(evaluation, voice="caveman")
        
        assert "Grug" in title or "Grug" in summary

    def test_summary_markdown_sage_no_findings(self) -> None:
        from personas.code_reviewer.dispatch import _summary_markdown
        from personas.code_reviewer.persona import CodeReviewEvaluation
        
        evaluation = CodeReviewEvaluation(
            degraded_reason=None,
            findings=(),
            dropped_hallucinations=0,
            conclusion="success",
        )
        
        title, summary = _summary_markdown(evaluation, voice="sage")
        
        # Sage cadence
        assert "Nothing" in title or "nothing" in title.lower()
