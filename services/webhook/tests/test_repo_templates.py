"""The issue template and the issue-tracker doc must steer filers into a body
Chief passes, under an epic that is still open.

Two drifts this pins (#1335):

- Both told authors to link with a closing keyword. On a ticket's PR that
  auto-closes the ticket at merge, before its acceptance boxes are ticked
  from evidence, and Chief's blocking `linked-issue-completeness` check fails
  a PR that closes a ticket with an open box. The org rule is `Refs #N` /
  `Part of #N`; the ticket is closed by hand once its boxes are ticked.
- The issue template hard-coded a "Live epics" list. It went stale (a closed
  epic listed, a dozen open ones missing), so a filer following it parented
  work under a closed epic. The template now points at the `gh` query that
  lists live epics instead of enumerating them.

The PR template has its own contract test (test_pr_template_contract.py).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from personas.tpm.dor_checks import _CLOSING_KEYWORD_PAT, _PURE_CHECKS

REPO = Path(__file__).resolve().parents[3]
_ISSUE_TEMPLATES = sorted((REPO / ".github" / "ISSUE_TEMPLATE").glob("*.md"))
_TRACKER_DOC = REPO / "docs" / "agents" / "issue-tracker.md"

# Chief's own closing-keyword pattern, widened from `#<digits>` to `#`
# followed by anything: guidance text writes the keyword with a placeholder
# (`closes #N`, `closes #<n>`, or a bare `closes #` for the author to finish),
# and every one of those teaches the author to type a closing keyword.
_CLOSING_KEYWORD_ANY = re.compile(
    _CLOSING_KEYWORD_PAT.pattern.replace(r"#(\d+)", "#"),
    _CLOSING_KEYWORD_PAT.flags,
)

# A line that starts with an issue number: the shape of an enumerated epic
# list (`  #707 review quality - ...`).
_ENUMERATED_ISSUE_LINE = re.compile(r"^\s*#\d+\b", re.MULTILINE)

_FRONT_MATTER = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def _ids(paths: list[Path]) -> list[str]:
    return [str(p.relative_to(REPO)) for p in paths]


def _fill_as_directed(template: str) -> str:
    """Fill an issue template the way a filer following it would: drop the
    front matter (GitHub does not put it in the body) and the guidance
    comments, write a Why, fill every empty bullet, and link the epic in
    Dependencies as the template asks."""
    body = _HTML_COMMENT.sub("", _FRONT_MATTER.sub("", template))
    body = body.replace(
        "## Why\n",
        "## Why\n\nThe nightly sweep misses tickets filed after midnight, so they wait a day.\n",
        1,
    )
    head, sep, tail = body.partition("## Dependencies")
    if sep:
        tail = re.sub(r"^- *$", "- Part of #823", tail, count=1, flags=re.MULTILINE)
        body = head + sep + tail
    body = re.sub(r"^- \[ \] *$", "- [ ] the sweep picks up a late ticket", body, flags=re.MULTILINE)
    return re.sub(r"^- *$", "- the sweep schedule itself", body, flags=re.MULTILINE)


def test_issue_templates_exist():
    assert _ISSUE_TEMPLATES, REPO / ".github" / "ISSUE_TEMPLATE"


@pytest.mark.parametrize("path", _ISSUE_TEMPLATES, ids=_ids(_ISSUE_TEMPLATES))
def test_issue_template_filled_as_directed_passes_chief(path):
    body = _fill_as_directed(path.read_text(encoding="utf-8"))
    failed = [r for r in (check(body) for check in _PURE_CHECKS) if not r.passed]
    assert not failed, failed


@pytest.mark.parametrize(
    "path", [*_ISSUE_TEMPLATES, _TRACKER_DOC], ids=_ids([*_ISSUE_TEMPLATES, _TRACKER_DOC])
)
def test_no_closing_keyword_advice(path):
    text = path.read_text(encoding="utf-8")
    hits = [m.group(0) for m in _CLOSING_KEYWORD_ANY.finditer(text)]
    assert not hits, f"{path.name} teaches a closing keyword: {hits}"


@pytest.mark.parametrize("path", _ISSUE_TEMPLATES, ids=_ids(_ISSUE_TEMPLATES))
def test_issue_template_does_not_enumerate_epics(path):
    text = path.read_text(encoding="utf-8")
    listed = _ENUMERATED_ISSUE_LINE.findall(text)
    assert not listed, f"{path.name} hard-codes issue numbers: {listed}"
    assert "Epic in:title" in text, "point filers at the live epic query instead"
