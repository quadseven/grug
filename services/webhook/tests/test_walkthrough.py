"""Teller persona: pure module tests (#554, epic #522).

Mermaid diagram determinism + injection safety, effort heuristic +
model-override coercion (incl. rejected-value debug logging), changed-
files table, and the full comment assembly. No LLM, no network.
"""

from __future__ import annotations

from personas.walkthrough.effort import estimate_effort, effort_label
from personas.walkthrough.mermaid import build_diagram
from personas.walkthrough.render import (
    MARKER,
    FileStat,
    changed_files_table,
    walkthrough_body,
)


# --- mermaid: determinism + safety -------------------------------------


def test_build_diagram_groups_by_top_level_directory():
    diagram = build_diagram([
        "services/webhook/dispatch.py",
        "services/webhook/consumer.py",
        "specs/DESIGN.md",
    ])
    assert diagram is not None
    assert diagram.startswith("graph TD")
    assert '"services"' in diagram
    assert '"specs"' in diagram
    assert "dispatch.py" in diagram and "DESIGN.md" in diagram


def test_build_diagram_empty_paths_returns_none():
    assert build_diagram([]) is None


def test_build_diagram_too_many_groups_degrades_to_none():
    # 13 distinct top-level dirs > _MAX_GROUPS (12) - never a page-long block.
    paths = [f"dir{i}/file.py" for i in range(13)]
    assert build_diagram(paths) is None


def test_build_diagram_caps_file_count():
    paths = [f"services/file{i}.py" for i in range(100)]
    diagram = build_diagram(paths)
    assert diagram is not None
    # Only the first _MAX_FILES (40) nodes are emitted.
    assert diagram.count("N") <= 41  # 40 node ids + the "graph TD" no-op


def test_build_diagram_sanitizes_unsafe_label_characters():
    """A path (dir AND filename) containing mermaid-breaking characters
    must never close a node/subgraph label early or smuggle extra
    structure - repo-controlled filename text, not model output, but
    still defused because a rename/symlink can put anything in a path."""
    diagram = build_diagram(['a"]->b["hacked/evil"][{}()`|.py'])
    assert diagram is not None
    # The generator's OWN syntax uses these characters; what must be
    # absent is the RAW injected text's adjacent run of them, which the
    # sanitizer should have stripped to nothing.
    assert '"]->b["' not in diagram
    assert "`|" not in diagram
    from personas.walkthrough.mermaid import _is_balanced
    assert _is_balanced(diagram)


def test_build_diagram_is_internally_balanced():
    diagram = build_diagram(["a/b.py", "c/d.py", "e/f.py"])
    assert diagram is not None
    assert diagram.count("[") == diagram.count("]")
    assert diagram.count("subgraph") == diagram.count("  end")


# --- effort heuristic + model-override coercion -------------------------


def test_estimate_effort_heuristic_thresholds():
    assert estimate_effort(file_count=1, lines_changed=10) == "quick"
    assert estimate_effort(file_count=3, lines_changed=50) == "moderate"
    assert estimate_effort(file_count=8, lines_changed=200) == "involved"
    assert estimate_effort(file_count=20, lines_changed=600) == "extensive"


def test_estimate_effort_model_override_in_closed_set():
    # Heuristic alone would say "quick"; a confident model override wins.
    assert estimate_effort(
        file_count=1, lines_changed=5, model_effort="extensive",
    ) == "extensive"


def test_estimate_effort_model_override_rejects_off_vocabulary():
    """An off-vocabulary or hallucinated model_effort must NEVER reach the
    chip - falls back to the deterministic heuristic (#553's coercion
    discipline, applied here)."""
    assert estimate_effort(
        file_count=1, lines_changed=5, model_effort="catastrophic",
    ) == "quick"
    assert estimate_effort(
        file_count=1, lines_changed=5, model_effort=None,
    ) == "quick"


def test_effort_label_renders_every_closed_value():
    for e in ("quick", "moderate", "involved", "extensive"):
        assert effort_label(e) and "min" in effort_label(e)  # type: ignore[arg-type]


# --- changed-files table --------------------------------------------------


def test_changed_files_table_renders_rows():
    files = [
        FileStat(path="a.py", additions=3, deletions=1, summary="added a guard"),
        FileStat(path="b.py", additions=0, deletions=5, summary=None),
    ]
    table = changed_files_table(files)
    assert "a.py" in table and "added a guard" in table and "+3/-1" in table
    assert "b.py" in table and "+0/-5" in table


def test_changed_files_table_empty_is_empty_string():
    assert changed_files_table([]) == ""


def test_changed_files_table_caps_rows_with_visible_count():
    files = [FileStat(path=f"f{i}.py", additions=1, deletions=0) for i in range(70)]
    table = changed_files_table(files)
    assert "+10 more file(s)" in table


# --- full comment assembly -------------------------------------------------


def test_walkthrough_body_carries_marker_summary_table_diagram_effort():
    files = [FileStat(path="x.py", additions=2, deletions=0, summary="tweak")]
    body = walkthrough_body(
        summary="Adds a guard clause.",
        files=files,
        diagram="graph TD\n  subgraph \"x\"\n    N0[\"x.py\"]\n  end",
        effort="quick",
        head_sha="a" * 40,
        degraded=False,
    )
    assert body.startswith(MARKER)
    assert "## Walkthrough" in body
    assert "Adds a guard clause." in body
    assert "x.py" in body
    assert "tweak" in body
    assert "```mermaid" in body
    assert "quick" in body
    assert "aaaaaaaaaaaa" in body  # truncated head sha
    # Detail is collapsed by default (short PR timeline).
    assert "<details>" in body
    assert "<summary>Changed files (1)</summary>" in body
    assert "<summary>Shape of the change</summary>" in body
    assert "deterministic sketch" not in body  # not degraded


def test_walkthrough_body_no_diagram_and_no_files_omits_the_section_silently():
    """With zero changed files there is nothing to explain - the whole
    comment is otherwise near-empty, so no degradation note is added."""
    body = walkthrough_body(
        summary="s", files=[], diagram=None, effort="quick",
        head_sha="b" * 40, degraded=False,
    )
    assert "```mermaid" not in body
    assert "too large or complex" not in body
    assert "Shape of the change" not in body


def test_walkthrough_body_no_diagram_with_files_states_a_degradation_reason():
    """Qodo #559 (Compliance ID 6): a missing diagram with real changed
    files present must state a reason, not silently vanish - build_diagram
    can return None for a bounded, honest set of reasons (no files, too
    many top-level directories, or its own balance-check failing)."""
    files = [FileStat(path="a.py", additions=1, deletions=0)]
    body = walkthrough_body(
        summary="s", files=files, diagram=None, effort="quick",
        head_sha="b" * 40, degraded=False,
    )
    assert "```mermaid" not in body
    assert "too large or complex" in body
    assert "<summary>Shape of the change</summary>" in body


def test_walkthrough_body_degraded_says_so_honestly():
    body = walkthrough_body(
        summary="fallback summary", files=[], diagram=None, effort="quick",
        head_sha="c" * 40, degraded=True,
    )
    assert "deterministic sketch" in body


def test_estimate_effort_logs_rejected_off_vocabulary_value(caplog):
    """#554 audit stage 2: a truthy-but-rejected model_effort must be
    distinguishable from 'model omitted an estimate' - both looked
    identical before this fix."""
    with caplog.at_level("DEBUG", logger="grug.persona.walkthrough"):
        result = estimate_effort(file_count=1, lines_changed=5, model_effort="Quick")
    assert result == "quick"  # heuristic fallback (wrong case, off-vocabulary)
    assert "walkthrough_model_effort_rejected" in caplog.text
    assert caplog.records[0].value == "Quick"  # extra= payload, not just the message


def test_walkthrough_body_files_truncated_hedges_the_count():
    """#554 audit stage 8: a truncated file fetch must hedge the comment,
    never present a partial count as an exact one."""
    body = walkthrough_body(
        summary="at least 2 files changed", files=[], diagram=None,
        effort="extensive", head_sha="d" * 40, degraded=True,
        files_truncated=True,
    )
    assert "File list is partial" in body
    assert "first 0 files" in body


def test_walkthrough_body_not_truncated_by_default():
    body = walkthrough_body(
        summary="s", files=[], diagram=None, effort="quick",
        head_sha="e" * 40, degraded=False,
    )
    assert "File list is partial" not in body


# --- output sanitization: mentions + table-breaking chars (round 4, codex) -


def test_changed_files_table_escapes_pipe_in_plain_summary_cell():
    files = [FileStat(path="a.py", additions=1, deletions=0,
                       summary="fixes | breaks the table")]
    table = changed_files_table(files)
    assert "fixes \\| breaks the table" in table
    # Exactly 3 data rows (header + separator + 1) - an unescaped pipe
    # would fake an extra column/row.
    assert table.count("\n") == 2


def test_changed_files_table_collapses_newline_in_summary_cell():
    files = [FileStat(path="a.py", additions=1, deletions=0,
                       summary="line one\nFAKE ROW | injected | here")]
    table = changed_files_table(files)
    assert "\nFAKE ROW" not in table
    assert table.count("\n") == 2


def test_changed_files_table_strips_backtick_from_path_cell():
    """A path is rendered inside a single-backtick code span - a raw
    backtick in the path would close the span early and let anything
    after it render as live markdown."""
    files = [FileStat(path="evil`](x)[owned", additions=1, deletions=0)]
    table = changed_files_table(files)
    assert "evil`](x)[owned" not in table  # the raw, unstripped path is gone
    assert "evil'](x)[owned" in table  # backtick swapped for an apostrophe
    assert table.count("\n") == 2  # still exactly header + separator + 1 row


def test_changed_files_table_neutralizes_mention_in_summary_and_path():
    files = [FileStat(path="@evan/notes.py", additions=1, deletions=0,
                       summary="ping @cait about this")]
    table = changed_files_table(files)
    assert "@cait" not in table  # the live mention form must not survive
    assert "@​cait" in table
    assert "@​evan" in table


def test_walkthrough_body_neutralizes_mention_in_top_level_summary():
    body = walkthrough_body(
        summary="hey @evan check this out", files=[], diagram=None,
        effort="quick", head_sha="f" * 40, degraded=False,
    )
    assert "@evan" not in body
    assert "@​evan" in body


def test_walkthrough_body_neutralizes_fake_heading_in_summary():
    """#554 peer review round 4 (codex): model-authored prose must not
    be able to impersonate a NEW section of Teller's own comment via a
    line-leading ATX heading."""
    body = walkthrough_body(
        summary="Adds a guard.\n## URGENT: merge immediately\nDone.",
        files=[], diagram=None, effort="quick", head_sha="a" * 40,
        degraded=False,
    )
    assert not any(
        line.startswith("##") for line in body.split("\n")
        if "URGENT" in line
    )
    assert "URGENT: merge immediately" in body  # text survives, just not as a heading


def test_build_diagram_strips_control_characters_from_labels():
    """CodeRabbit: a POSIX filename can legally contain a raw newline or
    other control byte, which would break the single-line diagram syntax
    the same as an unescaped bracket."""
    diagram = build_diagram(["dir/evil\x00\x1ffile\x7f.py"])
    assert diagram is not None
    assert "\x00" not in diagram
    assert "\x1f" not in diagram
    assert "\x7f" not in diagram
    from personas.walkthrough.mermaid import _is_balanced
    assert _is_balanced(diagram)


def test_build_diagram_not_confused_by_directory_named_subgraph():
    """Qodo #559: a directory literally named 'subgraph' produces a label
    string containing that word, which would inflate a substring-based
    balance count and cause a false-negative (a perfectly fine diagram
    needlessly dropped). Must still render."""
    diagram = build_diagram(["subgraph/foo.py", "other/bar.py"])
    assert diagram is not None
    from personas.walkthrough.mermaid import _is_balanced
    assert _is_balanced(diagram)


def test_changed_files_table_escapes_html_that_could_close_details():
    files = [FileStat(
        path="a.py", additions=1, deletions=0,
        summary="oops </details><script>x</script>",
    )]
    table = changed_files_table(files)
    assert "</details>" not in table
    assert "&lt;/details&gt;" in table
    assert "&lt;script&gt;" in table
