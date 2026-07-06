"""Teller persona: pure module tests (#554, epic #522).

Mermaid diagram determinism + injection safety, effort heuristic +
model-override coercion, changed-files table, and the full comment
assembly. No LLM, no network.
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
    assert "Adds a guard clause." in body
    assert "x.py" in body and "tweak" in body
    assert "```mermaid" in body
    assert "quick" in body
    assert "aaaaaaaaaaaa" in body  # truncated head sha
    assert "quiet this pass" not in body  # not degraded


def test_walkthrough_body_no_diagram_omits_the_section():
    body = walkthrough_body(
        summary="s", files=[], diagram=None, effort="quick",
        head_sha="b" * 40, degraded=False,
    )
    assert "```mermaid" not in body


def test_walkthrough_body_degraded_says_so_honestly():
    body = walkthrough_body(
        summary="fallback summary", files=[], diagram=None, effort="quick",
        head_sha="c" * 40, degraded=True,
    )
    assert "quiet this pass" in body
