"""The review-payload attester accepts the multi-line comment shape and
rejects every way it could send GitHub a null or drifted anchor (#1759)."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "infra/scripts/attest_github_reviews_client.py"
CLIENT = REPO / "services/_shared/github_reviews_client.py"


def _attester():
    spec = importlib.util.spec_from_file_location("attest_reviews", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _failures(tmp_path: Path, text: str) -> list[str]:
    target = tmp_path / "github_reviews_client.py"
    target.write_text(text, encoding="utf-8")
    return _attester()._check(target)


def test_real_client_passes():
    assert _failures(CLIENT.parent, CLIENT.read_text(encoding="utf-8")) == [] \
        if False else _attester()._check(CLIENT) == []


def _mutated(old: str, new: str) -> str:
    text = CLIENT.read_text(encoding="utf-8")
    assert old in text
    return text.replace(old, new, 1)


def test_optional_start_line_is_rejected(tmp_path):
    out = _failures(tmp_path, _mutated(
        "    start_line: int\n", "    start_line: int | None = None\n",
    ))
    assert any("start_line must be a required" in f for f in out)


def test_nullable_start_side_is_rejected(tmp_path):
    out = _failures(tmp_path, _mutated(
        '    start_side: Literal["RIGHT"] = "RIGHT"\n',
        "    start_side: str | None = None\n",
    ))
    assert any("start_side must never be None" in f for f in out)


def test_extra_span_field_is_rejected(tmp_path):
    out = _failures(tmp_path, _mutated(
        '    start_side: Literal["RIGHT"] = "RIGHT"\n',
        '    start_side: Literal["RIGHT"] = "RIGHT"\n    extra: int = 0\n',
    ))
    assert any("must add exactly" in f for f in out)


def test_start_fields_on_the_one_line_class_are_rejected(tmp_path):
    out = _failures(tmp_path, _mutated(
        '    side: Literal["LEFT", "RIGHT"] = "RIGHT"\n',
        '    side: Literal["LEFT", "RIGHT"] = "RIGHT"\n    start_line: int | None = None\n',
    ))
    assert any("InlineComment fields must equal" in f for f in out)


def test_manual_marshalling_is_still_rejected(tmp_path):
    out = _failures(tmp_path, _mutated("json=asdict(result),", "json=dict(result),"))
    assert any("json=asdict(result)" in f for f in out)


def test_missing_span_comment_is_rejected(tmp_path):
    text = re.sub(r"class SpanComment", "class Other", CLIENT.read_text(encoding="utf-8"))
    out = _failures(tmp_path, text)
    assert any("SpanComment is not a frozen dataclass" in f for f in out)
