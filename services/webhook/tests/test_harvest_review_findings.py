"""Tests for the review-findings corpus parsers + recall join (#595).

Pure-layer only (parsers + recall_report); the network `harvest`/`main` are
deliberately untested here, mirroring sast_benchmark's posture. Fixture
bodies are trimmed captures of the comment formats seen on our PRs; reviewer
account logins never appear (runtime config).
"""

from __future__ import annotations

from elder_eval.harvest_review_findings import (
    parse_inline_header_comment,
    parse_grug_comment,
    parse_summary_block_comment,
    recall_report,
)

_INLINE_BODY = (
    "_📐 Maintainability & Code Quality_ | _🟡 Minor_ | _⚡ Quick win_\n\n"
    "**Update the rerun contract test/documentation to five receives.**\n\n"
    "`services/webhook/tests/test_consumer.py:50-120` still states ...\n"
)

_GRUG_BODY = (
    "**HIGH · `caller-not-updated`** · heavy lift\n\n"
    "Grug see call site not walk the new path. `review_backends` now equals ...\n"
    "So speaks Grug.\n"
)

_SUMMARY_BODY = (
    "<h3>Code Review by X</h3>\n"
    "<details><summary>  1.  Missing success assertion <code>🐞 Bug</code>"
    " <code>⚙ Maintainability</code></summary>\n"
    "> `⭐⭐⭐ High`\n"
    ">[grugthink/tests/test_x.py[R39-54]](https://github.com/x/pull/598/files#diff-abc)\n"
    "</details>\n"
    "<details><summary>  2.  Lock-held probe can stall <code>🐞 Bug</code></summary>\n"
    "> `⭐⭐ Medium`\n"
    ">[services/webhook/poller.py[R211]](https://github.com/x/pull/598/files#diff-def)\n"
    "</details>\n"
)


def _inline_raw(**over):
    raw = {
        "body": _INLINE_BODY,
        "path": "services/webhook/tests/test_consumer.py",
        "line": 73,
        "original_line": 73,
        "pull_request_url": "https://api.github.com/repos/githumps/grug/pulls/607",
        "created_at": "2026-07-12T06:00:00Z",
        "html_url": "https://github.com/githumps/grug/pull/607#discussion_r1",
        "user": {"login": "reviewer-a[bot]"},
    }
    raw.update(over)
    return raw


class TestInlineHeaderParser:
    def test_finding_comment_parses(self):
        f = parse_inline_header_comment("githumps/grug", _inline_raw())
        assert f is not None
        assert f.source == "src-a"
        assert f.pr == 607
        assert f.path.endswith("test_consumer.py")
        assert f.line == 73
        assert f.severity == "medium"          # Minor -> medium
        assert f.category == "maintainability-code-quality"
        assert f.finding.startswith("Update the rerun contract")
        assert f.outdated is False

    def test_reply_without_header_is_skipped(self):
        raw = _inline_raw(body="`@githumps` Thanks for confirming — resolved.")
        assert parse_inline_header_comment("githumps/grug", raw) is None

    def test_outdated_position_flagged(self):
        f = parse_inline_header_comment("githumps/grug", _inline_raw(line=None, original_line=61))
        assert f is not None
        assert f.line == 61
        assert f.outdated is True

    def test_preamble_bold_text_does_not_corrupt_title(self):
        """Regression: title extraction is anchored AFTER the header, so a
        bold string in a preamble (quoted text, prior-section chatter) can
        never masquerade as the finding title."""
        body = (
            "> quoting **some earlier bold text** here\n\n"
            "_🎯 Functional Correctness_ | _🟠 Major_\n\n"
            "**The real finding title.**\n\ndetail...\n"
        )
        f = parse_inline_header_comment("o/r", _inline_raw(body=body))
        assert f is not None
        assert f.finding == "The real finding title."

    def test_bold_in_detail_text_not_grabbed_as_title(self):
        """The title is the first line after the header; a bold span deep in
        the detail text must not be grabbed when the title line is plain."""
        body = (
            "_🎯 Functional Correctness_ | _🟠 Major_\n\n"
            "Plain title line without bold.\n\n"
            "Detail with **bold emphasis** later on.\n"
        )
        f = parse_inline_header_comment("o/r", _inline_raw(body=body))
        assert f is not None
        assert f.finding == "Plain title line without bold."

    def test_severity_scale(self):
        for label, expect in (
            ("Critical", "critical"), ("Major", "high"),
            ("Minor", "medium"), ("Trivial", "low"),
        ):
            body = f"_🎯 Functional Correctness_ | _🟠 {label}_\n\n**T.**\n"
            f = parse_inline_header_comment("o/r", _inline_raw(body=body))
            assert f is not None and f.severity == expect, label


class TestGrugParser:
    def test_finding_comment_parses(self):
        raw = _inline_raw(body=_GRUG_BODY, user={"login": "grug-tribe[bot]"})
        f = parse_grug_comment("githumps/grug", raw)
        assert f is not None
        assert f.source == "grug"
        assert f.severity == "high"
        assert f.category == "caller-not-updated"
        # The finding text is the DESCRIPTIVE line after the header, never a
        # duplicate of the severity/category header itself.
        assert f.finding.startswith("Grug see call site")
        assert "HIGH" not in f.finding

    def test_walkthrough_chatter_skipped(self):
        raw = _inline_raw(body="<!-- grug-teller:walkthrough -->\nGrug walk the diff...")
        assert parse_grug_comment("githumps/grug", raw) is None


class TestSummaryBlockParser:
    def test_blocks_parse_with_stars_and_anchor(self):
        out = parse_summary_block_comment(
            "githumps/grug", 598, _SUMMARY_BODY, "2026-07-12T06:00:00Z", "u",
        )
        assert len(out) == 2
        first, second = out
        assert first.source == "src-b"
        assert first.severity == "high"        # 3 stars
        assert first.path == "grugthink/tests/test_x.py"
        assert first.line == 39
        assert second.severity == "medium"     # 2 stars
        assert second.line == 211

    def test_non_review_comment_yields_nothing(self):
        assert parse_summary_block_comment("o/r", 1, "<h3>PR Summary</h3>", "t", "u") == []


class TestRecallReport:
    def _bot(self, path="a.py", line=10, source="src-a", severity="high"):
        return {"source": source, "repo": "o/r", "pr": 1, "path": path,
                "line": line, "severity": severity}

    def _grug(self, path="a.py", line=10):
        return {"source": "grug", "repo": "o/r", "pr": 1, "path": path, "line": line}

    def test_exact_and_slack_matches(self):
        report = recall_report(
            [self._bot(line=10), self._bot(line=14, severity="low")],
            [self._grug(line=12)],
        )
        # line 10 within +/-3 of 12 -> hit; line 14 is not (delta 2? no: |14-12|=2 -> hit)
        assert report["denominator"] == 2
        assert report["matched"] == 2
        assert report["recall"] == 1.0

    def test_miss_and_per_source_split(self):
        report = recall_report(
            [self._bot(line=10), self._bot(path="b.py", line=5, source="src-b")],
            [self._grug(line=10)],
        )
        assert report["matched"] == 1
        assert report["by_source"]["src-a"]["recall"] == 1.0
        assert report["by_source"]["src-b"]["recall"] == 0.0

    def test_unanchored_bot_rows_excluded_from_denominator(self):
        report = recall_report([self._bot(path="")], [])
        assert report["denominator"] == 0
        assert report["unanchored_excluded"] == 1

    def test_lineless_bot_row_matches_on_path(self):
        report = recall_report([self._bot(line=None)], [self._grug(line=99)])
        assert report["matched"] == 1
