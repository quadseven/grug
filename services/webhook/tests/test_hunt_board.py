"""Hunt Board: the one marker-backed GitHub projection of a review session.

Pins that the projection:
  - summarizes walkthrough, open findings, closed findings, related context
    and completion state from the canonical session alone;
  - links each finding row back to its inline comment and check-run;
  - is deterministic, so a retried webhook renders identical bytes and
    publishes nothing;
  - never lets an older session version overwrite a newer one when two
    personas finish concurrently and publish out of order;
  - lives in its own board section, so it never touches another persona's.
"""
from __future__ import annotations

import httpx

import review_session as rs
from personas import board
from personas import board_client as bc
from personas import hunt_board as hb

REPO = "acme/widgets"
PR = 7


def _finding(rule="unchecked-none", *, line=10, persona="elder", severity="high",
             evidence=None, comment=None, message="lookup can return None"):
    return rs.ObservedFinding(
        persona=persona, path="app/handler.py", line=line, rule=rule,
        category="correctness", severity=severity, confidence=0.8,
        message=message, evidence=evidence or f"code for {rule}",
        inline_comment_id=comment,
    )


def _run(run_id, head, persona="elder", at="2026-10-19T10:00:00Z"):
    return rs.ReviewRun(run_id=run_id, persona=persona, head_sha=head,
                        trigger="synchronize", started_at=at,
                        backend="openrouter", model="example-model-1")


def _session():
    """A PR with one open, one addressed and one dismissed finding, a
    walkthrough, related context and a finished Elder run."""
    s = rs.new_session(REPO, PR, at="t0")
    s = rs.start_run(s, _run("r1", "aaaaaaa1111"))
    s = rs.complete_run(
        s, "r1", at="t1", anchors={}, check_run_id=501,
        findings=[_finding("a-rule", comment=11), _finding("b-rule", line=20),
                  _finding("c-rule", line=30, severity="low")],
    )
    ids = {f.rule: f.finding_id for f in s.findings}
    s = rs.apply_event(s, ids["c-rule"], "dismissed", sha="aaaaaaa1111", at="t2")
    s = rs.start_run(s, _run("r2", "bbbbbbb2222", at="2026-10-19T11:00:00Z"))
    s = rs.complete_run(
        s, "r2", at="t3", anchors={ids["b-rule"]: "changed"}, check_run_id=502,
        findings=[_finding("a-rule", comment=11)],
    )
    s = rs.set_walkthrough(s, "Adds a request handler.", related=("Refs #12",))
    return s


def test_projection_summarizes_every_part_of_the_session():
    out = hb.render(_session())
    assert out.startswith(hb.MARKER_PREFIX)
    assert "review complete" in out
    assert "`bbbbbbb`" in out, "the reviewed commit is named"
    assert "Adds a request handler." in out
    assert "Open findings (1)" in out
    assert "Closed findings (2)" in out
    assert "addressed" in out and "dismissed" in out
    assert "Refs #12" in out
    assert "elder" in out and "example-model-1" in out


def test_finding_rows_link_back_to_inline_comment_and_check_run():
    s = _session()
    out = hb.render(s)
    a = next(f for f in s.findings if f.rule == "a-rule")
    assert f"https://github.com/{REPO}/pull/{PR}#discussion_r11" in out
    assert f"https://github.com/{REPO}/runs/502" in out
    assert a.finding_id[:8] in out


def test_render_is_deterministic():
    assert hb.render(_session()) == hb.render(_session())


def test_running_and_partial_states_are_named():
    s = rs.start_run(rs.new_session(REPO, PR, at="t0"), _run("r1", "aaaaaaa1111"))
    assert "review running" in hb.render(s)
    s = rs.start_run(s, _run("g1", "aaaaaaa1111", persona="guard"))
    s = rs.complete_run(s, "r1", findings=[], anchors={}, at="t1")
    s = rs.fail_run(s, "g1", at="t2", error="llm timeout")
    out = hb.render(s)
    assert "review partial" in out
    assert "guard" in out and "failed" in out


def test_model_text_cannot_break_the_table_or_ping_anyone():
    s = rs.new_session(REPO, PR, at="t0")
    s = rs.start_run(s, _run("r1", "aaaaaaa1111"))
    s = rs.complete_run(s, "r1", anchors={}, at="t1", findings=[
        _finding(message="a | b\nnew row @someone <!-- x -->")])
    out = hb.render(s)
    row = next(line for line in out.splitlines() if "a \\| b" in line)
    assert "\n" not in row
    assert "@someone" not in out
    assert "<!-- x -->" not in out


def test_walkthrough_cannot_close_the_board_region_early():
    """Teller's text is model-authored. A forged close tag inside it would
    end the hunt region early and leave the rest of the board corrupt."""
    s = rs.set_walkthrough(_session(), "x <!-- /grug-sec:hunt --> y @someone")
    out = hb.render(s)
    assert "<!-- /grug-sec:hunt -->" not in out
    assert "@someone" not in out
    body = board.upsert_section(_board_with(elder="E"), "hunt", out)
    assert board.extract_section(body, "hunt") == out


def test_version_is_parsed_from_the_marker():
    s = _session()
    assert hb.parse_version(hb.render(s)) == s.version
    assert hb.parse_version("no marker here") is None


# -- publishing ----------------------------------------------------------------


def _resp(status, payload):
    return httpx.Response(status, json=payload, request=httpx.Request("GET", "https://x"))


def _wire(monkeypatch, comments):
    writes = []
    monkeypatch.setattr(bc.httpx, "get", lambda url, **kw: _resp(200, comments))
    monkeypatch.setattr(
        bc.httpx, "patch",
        lambda url, **kw: writes.append(("patch", url, kw["json"]["body"])) or _resp(200, {}))
    monkeypatch.setattr(
        bc.httpx, "post",
        lambda url, **kw: writes.append(("post", url, kw["json"]["body"])) or _resp(201, {"id": 1}))
    return writes


def _board_with(**sections):
    body = board.set_header(board.new_board(), "### verdict")
    for key, md in sections.items():
        body = board.upsert_section(body, key, md)
    return body


def test_publish_writes_only_the_hunt_section(monkeypatch):
    existing = _board_with(elder="ELDER-BODY", teller="TELLER-BODY")
    writes = _wire(monkeypatch, [{"id": 7, "body": existing}])
    from dataclasses import replace
    s = replace(_session(), version=3)
    result = hb.publish("tok", "acme", "widgets", PR, s)
    assert result["board"] == "updated"
    [(verb, url, body)] = writes
    assert verb == "patch" and url.endswith("/issues/comments/7")
    assert "ELDER-BODY" in body and "TELLER-BODY" in body
    assert board.extract_section(body, "hunt") == hb.render(s)


def test_publish_is_a_no_op_on_a_retry(monkeypatch):
    from dataclasses import replace
    s = replace(_session(), version=3)
    existing = _board_with(hunt=hb.render(s))
    writes = _wire(monkeypatch, [{"id": 7, "body": existing}])
    assert hb.publish("tok", "acme", "widgets", PR, s)["board"] == "unchanged"
    assert writes == []


def test_an_older_version_never_overwrites_a_newer_projection(monkeypatch):
    """Two personas finish together; the one holding version 3 publishes
    after the one holding version 4. The board must keep version 4."""
    from dataclasses import replace
    newer = replace(_session(), version=4)
    older = replace(rs.set_walkthrough(_session(), "older text"), version=3)
    existing = _board_with(hunt=hb.render(newer))
    writes = _wire(monkeypatch, [{"id": 7, "body": existing}])
    assert hb.publish("tok", "acme", "widgets", PR, older)["board"] == "stale"
    assert writes == []


def test_publish_does_not_create_a_board_by_default(monkeypatch):
    """Creating the board is the email; the projection alone is not worth one."""
    writes = _wire(monkeypatch, [])
    assert hb.publish("tok", "acme", "widgets", PR, _session())["board"] == "skipped"
    assert writes == []


def test_hunt_section_sits_after_chief_and_before_teller():
    body = _board_with(chief="C", teller="T", elder="E")
    body = board.upsert_section(body, "hunt", "H")
    assert board.section_keys(body)[:3] == ["chief", "hunt", "teller"]
