"""Unified review board: one comment per PR, one email per PR.

The properties worth pinning are the ones that make the board SAFE to have
several personas writing into the same comment concurrently:
  - a persona rewrites only the bytes between its own delimiters
  - order is fixed, so the comment does not reshuffle between passes
  - an unknown section from a newer/older grug is preserved, never dropped
"""
from __future__ import annotations

from personas import board


def test_new_board_starts_with_the_marker():
    """The comment-finder matches on the marker alone, so it must be first
    and must not depend on parsing anything else."""
    b = board.new_board()
    assert b.startswith(board.BOARD_MARKER)
    assert board.is_board(b)


def test_upsert_inserts_then_replaces_in_place():
    b = board.new_board()
    b = board.upsert_section(b, "elder", "first")
    assert "first" in b
    b2 = board.upsert_section(b, "elder", "second")
    assert "second" in b2 and "first" not in b2
    assert b2.count("<!-- grug-sec:elder -->") == 1


def test_a_persona_never_touches_another_persona_section():
    """The concurrency guarantee. Two personas writing the same comment can
    only clobber their OWN region."""
    b = board.new_board()
    b = board.upsert_section(b, "elder", "ELDER-BODY")
    b = board.upsert_section(b, "teller", "TELLER-BODY")
    b = board.upsert_section(b, "elder", "ELDER-REWRITTEN")
    assert "TELLER-BODY" in b
    assert "ELDER-REWRITTEN" in b and "ELDER-BODY" not in b


def test_sections_render_in_fixed_order_regardless_of_arrival():
    """A board whose layout moves between passes is one nobody learns to
    skim. Arrival order must not decide render order."""
    b = board.new_board()
    for key in ("sentinel", "elder", "chief"):     # deliberately reversed
        b = board.upsert_section(b, key, f"{key}-body")
    assert board.section_keys(b) == ["chief", "elder", "sentinel"]
    assert b.index("chief-body") < b.index("elder-body") < b.index("sentinel-body")


def test_unknown_section_is_preserved_not_dropped():
    """An older or newer grug may have written a section this version does
    not know about. Dropping it would silently delete a persona's output."""
    b = board.new_board()
    b = board.upsert_section(b, "elder", "e")
    b = board.upsert_section(b, "from-the-future", "keep me")
    b = board.upsert_section(b, "elder", "e2")
    assert "keep me" in b
    assert "from-the-future" in board.section_keys(b)


def test_section_body_containing_another_marker_cannot_run_past_its_close():
    """Non-greedy match + escaped delimiters: a section quoting another
    section's marker text must not swallow the rest of the board."""
    b = board.new_board()
    b = board.upsert_section(b, "elder", "quoting <!-- grug-sec:teller --> inline")
    b = board.upsert_section(b, "guard", "GUARD-BODY")
    b = board.upsert_section(b, "elder", "clean")
    assert "GUARD-BODY" in b
    assert "clean" in b


def test_collapse_keeps_blank_lines_so_github_renders_markdown():
    """Without the blank lines GitHub does not render markdown inside
    <details> - the section degrades to literal asterisks and pipes. This is
    the most common way a folded section looks broken."""
    out = board.collapse("Summary", "| a | b |\n|---|---|")
    assert "<summary>Summary</summary>\n\n" in out
    assert out.rstrip().endswith("\n\n</details>")


def test_collapse_can_default_open():
    assert "<details open>" in board.collapse("s", "b", open_by_default=True)
    assert "<details>" in board.collapse("s", "b")


def test_empty_body_upsert_is_safe():
    """Defensive: a board comment whose body was manually emptied must not
    raise - it rebuilds rather than crashing the review."""
    out = board.upsert_section("", "elder", "x")
    assert "x" in out and "<!-- grug-sec:elder -->" in out


def test_is_board_rejects_a_plain_comment():
    assert not board.is_board("just a human comment")
    assert not board.is_board("")


# --- the verdict header IS the email ----------------------------------------
# GitHub notifies on comment CREATION only, so the mail a human receives is
# exactly the body at first post. These pin that it leads with a conclusion
# and that evidence stays folded.

def test_clean_review_is_a_single_sentence():
    """Nothing wrong -> the whole email is one line. A clean review that
    costs a scroll is a clean review nobody reads next time."""
    h = board.render_header("#1 Bump pytest", 0, 0)
    assert "Trail clear" in h
    assert "block" not in h and "advise" not in h
    assert len([l for l in h.splitlines() if l.strip()]) == 2  # verdict + title


def test_blocking_verdict_leads_and_counts():
    h = board.render_header("#2 thing", 2, 5)
    first = h.splitlines()[0]
    assert "WAIT" in first          # conclusion is the FIRST thing rendered
    assert "**2 block**" in h and "5 advise" in h


def test_advisory_only_does_not_say_wait():
    """Advisory findings never block, so telling the author to WAIT would be
    a lie that trains them to ignore the verdict line."""
    h = board.render_header("#3", 0, 4)
    assert "WAIT" not in h and "go" in h


def test_degraded_says_so_rather_than_claiming_clear():
    """A degraded pass must never render as 'Trail clear' - that is grug
    asserting an all-clear it did not actually establish."""
    h = board.render_header("#4", 0, 0, degraded=True)
    assert "could not review" in h and "Trail clear" not in h


def test_the_blackout_verdict_is_the_checks_voice():
    """#818: the board verdict and the check say "no review happened" in the
    same words, and both say an operator was alerted."""
    from personas.code_reviewer.degraded_gate import COULD_NOT_REVIEW_VERDICT

    h = board.render_header("#4", 0, 0, degraded=True)
    assert COULD_NOT_REVIEW_VERDICT in h
    assert "operator has been alerted" in h.lower()
    assert "cloudy" not in h and "read for self" not in h


def test_partial_coverage_does_not_claim_clear():
    """Elder walked most of the trail but not all of it. Saying 'Trail clear'
    would assert an all-clear over ground it never covered."""
    h = board.render_header("#7", 0, 0, partial=True)
    assert "Trail clear" not in h
    assert "not walked" in h


def test_partial_coverage_is_not_the_blackout_message():
    """'Grug could not review this pass' means Elder saw NOTHING. Using it
    for a pass that reviewed most cohorts and published real findings tells
    the author to ignore work that was actually done."""
    partial = board.render_header("#8", 0, 0, partial=True)
    blackout = board.render_header("#8", 0, 0, degraded=True)
    assert "could not review" in blackout
    assert "could not review" not in partial
    assert partial != blackout


def test_blocking_findings_outrank_partial_coverage():
    """A blocking finding is the most actionable fact on the board - partial
    coverage must not bury it behind a caveat."""
    h = board.render_header("#9", 2, 0, partial=True)
    assert "WAIT" in h.splitlines()[0]
    assert "**2 block**" in h


def test_total_blackout_still_outranks_partial():
    h = board.render_header("#10", 0, 0, degraded=True, partial=True)
    assert "could not review" in h


def test_header_is_replaced_not_duplicated_across_passes():
    """Regenerated every pass as personas report in; the email keeps the
    creation-time copy, which is why creation must be verdict-shaped."""
    b = board.set_header(board.new_board(), board.render_header("#5", 0, 0))
    b = board.upsert_section(b, "elder", "E")
    b2 = board.set_header(b, board.render_header("#5", 3, 1))
    assert b2.count(board.BOARD_MARKER) == 1
    assert "Trail clear" not in b2 and "WAIT" in b2
    assert "E" in b2                      # sections survive a header rewrite


def test_header_rewrite_preserves_every_section():
    b = board.set_header(board.new_board(), board.render_header("#6", 0, 0))
    for k in ("teller", "elder", "sentinel"):
        b = board.upsert_section(b, k, f"{k}-body")
    b = board.set_header(b, board.render_header("#6", 1, 0))
    for k in ("teller", "elder", "sentinel"):
        assert f"{k}-body" in b
    assert board.section_keys(b) == ["teller", "elder", "sentinel"]


def test_evidence_is_folded_so_the_email_stays_short():
    """The complaint that started this: a file table and a diagram rendered
    inline made the email a diff dump."""
    sec = board.collapse("What changed - 6 files", "| f | +/- |\n|---|---|\n| a.py | +6/-1 |")
    assert sec.startswith("<details>")     # collapsed by default
    assert "<summary>" in sec


# --- #673 item 2: every run discloses what it read -------------------------


def test_review_scope_line_names_a_range_not_just_a_commit():
    """The author pastes this into `git diff`, so a single sha is not an
    answer - `base..head` is."""
    assert board.review_scope_line(living_range="aaa..bbb") == "`aaa..bbb`"
    full = board.review_scope_line(base_sha="base1234567", head_sha="head1234567")
    assert "base123..head123" in full


def test_review_scope_line_degrades_to_head_when_base_is_unknown():
    assert "head123" in board.review_scope_line(head_sha="head1234567")


def test_review_scope_line_is_empty_when_it_knows_nothing():
    """Better to say nothing than to emit a dangling 'Looked at:'."""
    assert board.review_scope_line() == ""
