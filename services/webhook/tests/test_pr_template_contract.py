"""The repo's own PR template must fail Chief until the author fills it.

`.github/pull_request_template.md` is what a person starts from in the
GitHub UI. If it passed the plan checks unfilled (a prefilled `Size: S`,
example bullets, or guidance prose that counts as content), Chief would wave
an empty PR through. It must also follow the org's link rule: `Refs #N` and
`Part of #N`, never a closing keyword, and plain ASCII (this repo is public).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from personas.tpm.dor_checks import _CLOSING_KEYWORD_PAT, run_all

_TEMPLATE = Path(__file__).resolve().parents[3] / ".github" / "pull_request_template.md"


@pytest.fixture(scope="module")
def template() -> str:
    assert _TEMPLATE.is_file(), _TEMPLATE
    return _TEMPLATE.read_text(encoding="utf-8")


@pytest.mark.parametrize("check", ["why", "acceptance", "estimate", "scope-fence"])
def test_unfilled_template_fails_blocking_check(template, check):
    results = {r.name: r for r in run_all(template)}
    assert not results[check].passed, results[check].detail


def test_template_has_no_closing_keyword(template):
    assert _CLOSING_KEYWORD_PAT.search(template) is None
    assert "closes #" not in template.lower()


def test_template_is_ascii(template):
    assert template.isascii(), [c for c in template if not c.isascii()]


def test_template_names_ticket_and_epic(template):
    assert "Refs #" in template
    assert "Part of #" in template
