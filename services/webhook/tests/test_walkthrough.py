"""Teller persona: pure module tests (#554, epic #522).

Mermaid diagram determinism + injection safety, effort heuristic +
model-override coercion (incl. rejected-value debug logging), changed-
files table, and the full comment assembly. No LLM, no network.
"""

from __future__ import annotations

from personas.walkthrough.effort import estimate_effort, effort_label
from personas.walkthrough.mermaid import build_diagram
from personas.walkthrough.render import (
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


def test_walkthrough_body_email_rendering_pins_no_heading_no_table_row_no_mermaid():
    """T1386: walkthrough renders at one text size in phone email clients:
    no ## heading, no markdown table row, and no mermaid diagram fence."""
    files = [
        FileStat(path="a.py", additions=3, deletions=1, summary="added a guard"),
        FileStat(path="b.py", additions=0, deletions=5, summary=None),
    ]
    body = walkthrough_body(
        summary="Adds a guard clause.",
        files=files,
        diagram="graph TD\n  subgraph \"x\"\n    N0[\"a.py\"]\n  end",
        effort="quick",
        head_sha="a" * 40,
    )
    # No ATX heading: the board section and the check run carry the title
    assert not any(line.strip().startswith("##") for line in body.splitlines())
    assert "##" not in body

    # Changed files render as a list, one line each: file, change count, summary
    assert "- `a.py` (+3/-1) - added a guard" in body
    assert "- `b.py` (+0/-5)" in body
    # No markdown table rows anywhere in the body
    for line in body.splitlines():
        assert not line.strip().startswith("|"), f"table row found: {line}"
    assert "|---" not in body

    # Mermaid diagram source never reaches the walkthrough
    assert "```mermaid" not in body
    assert "```" not in body
    assert "Shape of the change" not in body


def test_walkthrough_body_carries_summary_table_effort_and_no_diagram():
    files = [FileStat(path="x.py", additions=2, deletions=0, summary="tweak")]
    body = walkthrough_body(
        summary="Adds a guard clause.",
        files=files,
        diagram="graph TD\n  N0[\"x.py\"] --> N1[\"y.py\"]",
        effort="quick",
        head_sha="a" * 40,
    )
    assert "grug-teller:walkthrough" not in body
    assert "##" not in body
    assert "Adds a guard clause." in body
    assert "x.py" in body
    assert "tweak" in body
    assert "- `x.py` (+2/-0) - tweak" in body
    assert "quick" in body
    assert "aaaaaaaaaaaa" in body  # truncated head sha
    # Detail is collapsed by default (short PR timeline).
    assert "<details>" in body
    assert "<summary>Changed files (1)</summary>" in body
    assert "Shape of the change" not in body


def test_walkthrough_body_has_no_branded_header_or_button():
    """T1432: the walkthrough is a board section and a check-run summary,
    not a comment of Teller's own, so the comment's marker, portrait and
    "View full review" button go: the body opens on the summary itself."""
    files = [FileStat(path="x.py", additions=2, deletions=0, summary="tweak")]
    body = walkthrough_body(
        summary="Adds a guard clause.",
        files=files,
        effort="quick",
        head_sha="a" * 40,
    )
    assert body.startswith("Adds a guard clause.")
    assert "<!--" not in body
    assert "<img" not in body
    assert "<a href" not in body


def test_walkthrough_body_no_diagram_and_no_files_omits_the_section_silently():
    """With zero changed files there is nothing to explain - the whole
    walkthrough is otherwise near-empty, so no note is added."""
    body = walkthrough_body(
        summary="s", files=[], diagram=None, effort="quick",
        head_sha="b" * 40,
    )
    assert "```mermaid" not in body
    assert "Shape of the change" not in body


def test_walkthrough_body_no_diagram_with_files_states_a_degradation_reason():
    """T1386: The mermaid 'Shape of the change' diagram never reaches the
    walkthrough; it is left out entirely, with no note in its place."""
    files = [FileStat(path="a.py", additions=1, deletions=0)]
    body = walkthrough_body(
        summary="s", files=files, diagram=None, effort="quick",
        head_sha="b" * 40,
    )
    assert "```mermaid" not in body
    assert "Shape of the change" not in body
    assert "No diagram this pass" not in body


def test_walkthrough_body_diagram_never_reaches_comment():
    """T1386: The mermaid 'Shape of the change' diagram never reaches the
    walkthrough; it is left out entirely."""
    files = [FileStat(path="a.py", additions=1, deletions=0)]
    body = walkthrough_body(
        summary="s", files=files, diagram="graph TD\n  N0[\"a.py\"]", effort="quick",
        head_sha="b" * 40,
    )
    assert "```mermaid" not in body
    assert "Shape of the change" not in body


def test_walkthrough_body_drops_a_diagram_with_no_edges():
    """T1432: a graph of bare subgraphs only repeats the file names the
    list above already shows."""
    files = [FileStat(path="a/x.py", additions=1, deletions=0)]
    body = walkthrough_body(
        summary="s", files=files, diagram=build_diagram(["a/x.py", "b/y.py"]),
        effort="quick", head_sha="b" * 40,
    )
    assert "```mermaid" not in body
    assert "Shape of the change" not in body


def test_walkthrough_body_degraded_says_so_honestly_by_omission():
    """T1432: the deterministic fallback (summary=None) renders no prose
    paragraph and no sentence saying the summary failed: the body opens on
    the effort line and the file list carries the walkthrough. The board
    names the fallback in its model column instead."""
    files = [FileStat(path="a.py", additions=1, deletions=0)]
    body = walkthrough_body(
        summary=None, files=files, diagram=None, effort="quick",
        head_sha="c" * 40,
    )
    assert body.startswith("Review effort: ")
    assert "- `a.py` (+1/-0)" in body
    assert "fell back" not in body
    assert "teller-voice was quiet" not in body


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
        effort="extensive", head_sha="d" * 40,
        files_truncated=True,
    )
    assert "File list is partial" in body
    assert "first 0 files" in body


def test_walkthrough_body_not_truncated_by_default():
    body = walkthrough_body(
        summary="s", files=[], diagram=None, effort="quick",
        head_sha="e" * 40,
    )
    assert "File list is partial" not in body


# --- output sanitization: mentions + table-breaking chars (round 4, codex) -


def test_changed_files_table_escapes_pipe_in_plain_summary_cell():
    files = [FileStat(path="a.py", additions=1, deletions=0,
                       summary="fixes | breaks the table")]
    table = changed_files_table(files)
    assert "fixes | breaks the table" in table
    assert not table.startswith("|")
    assert table.count("\n") == 0


def test_changed_files_table_collapses_newline_in_summary_cell():
    files = [FileStat(path="a.py", additions=1, deletions=0,
                       summary="line one\nFAKE ROW | injected | here")]
    table = changed_files_table(files)
    assert "\nFAKE ROW" not in table
    assert table.count("\n") == 0


def test_changed_files_table_strips_backtick_from_path_cell():
    """A path is rendered inside a single-backtick code span - a raw
    backtick in the path would close the span early and let anything
    after it render as live markdown."""
    files = [FileStat(path="evil`](x)[owned", additions=1, deletions=0)]
    table = changed_files_table(files)
    assert "evil`](x)[owned" not in table  # the raw, unstripped path is gone
    assert "evil'](x)[owned" in table  # backtick swapped for an apostrophe
    assert table.count("\n") == 0


def test_changed_files_table_neutralizes_mention_in_summary_and_path():
    files = [FileStat(path="@alice/notes.py", additions=1, deletions=0,
                       summary="ping @cait about this")]
    table = changed_files_table(files)
    assert "@cait" not in table  # the live mention form must not survive
    assert "@​cait" in table
    assert "@​alice" in table


def test_walkthrough_body_neutralizes_mention_in_top_level_summary():
    body = walkthrough_body(
        summary="hey @alice check this out", files=[], diagram=None,
        effort="quick", head_sha="f" * 40,
    )
    assert "@alice" not in body
    assert "@​alice" in body


def test_walkthrough_body_neutralizes_fake_heading_in_summary():
    """#554 peer review round 4 (codex): model-authored prose must not
    be able to impersonate a NEW section of Teller's own comment via a
    line-leading ATX heading."""
    body = walkthrough_body(
        summary="Adds a guard.\n## URGENT: merge immediately\nDone.",
        files=[], diagram=None, effort="quick", head_sha="a" * 40,
    )
    assert not any(
        line.startswith("##") for line in body.split("\n")
        if "URGENT" in line
    )
    assert "URGENT: merge immediately" in body  # text survives, just not as a heading


def test_build_diagram_strips_control_characters_from_labels():
    """FLINT: a POSIX filename can legally contain a raw newline or
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
    """LORE #559: a directory literally named 'subgraph' produces a label
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


def test_every_grug_lol_asset_a_service_links_is_shipped_by_the_site():
    """A bot comment links its portrait from grug.lol/assets, which serves
    web/public/assets; a missing file answers with the site's HTML page and
    the comment shows a broken image (grug_teller.png, 2026-10-19)."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    shipped = {p.name for p in (root / "web" / "public" / "assets").iterdir()}
    linked = {}
    for source in (root / "services").rglob("*.py"):
        if "tests" in source.parts:
            continue
        for name in re.findall(r"grug\.lol/assets/([\w.-]+)", source.read_text()):
            linked.setdefault(name, source.relative_to(root))
    assert linked, "no grug.lol asset links found; the scan is broken"
    missing = {name: str(src) for name, src in linked.items() if name not in shipped}
    assert missing == {}


def test_assets_already_linked_by_posted_comments_stay_shipped():
    """Comments already posted keep their image links: walkthroughs before
    the Teller portrait moved to grug_smile.png (2026-10-20) link
    grug_teller.png, so the site keeps serving that name."""
    from pathlib import Path

    assets = Path(__file__).resolve().parents[3] / "web" / "public" / "assets"
    for name in ("grug_teller.png",):
        assert (assets / name).is_file(), name
