"""Agent instruction files must tell writers to use American English spelling.

A public comment used the British form "enrol" instead of "enroll" because
instruction files never said which spelling to use. This test keeps the
rule line in the repo-specific section of AGENTS.md.

The Elder review system prompt module is intentionally not listed: changing
that file requires re-recording the Elder evaluation baseline.
"""

from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_AGENTS = _REPO / "AGENTS.md"
_MARKER = "<!-- repo-specific below -->"

# The exact line the repo-specific section must carry.
AMERICAN_ENGLISH_RULE = (
    "Write American English spelling: enroll, color, behavior, canceled, "
    "organize, not enrol, colour, behaviour, cancelled, organise."
)


def _repo_specific_section() -> str:
    """Return the text of AGENTS.md after the repo-specific marker."""
    text = _AGENTS.read_text(encoding="utf-8")
    if _MARKER not in text:
        raise AssertionError("AGENTS.md is missing the repo-specific marker")
    return text.split(_MARKER, 1)[1]


def test_the_rule_is_in_the_repo_specific_agents_section():
    """The repo-specific section carries the American English spelling rule."""
    assert AMERICAN_ENGLISH_RULE in _repo_specific_section()
