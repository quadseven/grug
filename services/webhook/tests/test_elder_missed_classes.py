"""Tests for the PR #751 missed-finding-class fixtures (#752).

No LLM, no network. Three kinds of check:

- the fixtures are the real reviewed head (hunks agree with the snapshot);
- each counterexample really is a defect: the PRE-FIX attester, run from
  the fixture's own diff, passes it, and the shipped attester rejects it;
- the recorded production measurement re-scores to what it claims.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import types
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from elder_eval import missed_classes as mc
from sast_benchmark.backends import BenchBackend

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BACKEND = BenchBackend(name="fake", url="http://bench.invalid", model="m", api_key="")


@dataclass(frozen=True)
class _F:
    path: str
    line: int
    message: str


def _fixture(name: str) -> mc.MissedClassFixture:
    return next(f for f in mc.load_fixtures() if f.name == name)


# --- the fixtures are the real reviewed head --------------------------------


def test_three_classes_attributed_to_pr751_head():
    fixtures = mc.load_fixtures()
    assert {f.finding_class for f in fixtures} == {
        "cross-hop-dataflow", "weak-assertion", "reverse-direction-completeness",
    }
    assert (mc.REPO, mc.PR, mc.REFERENCE_SOURCE) == ("quadseven/grug", 751, "src-a")
    assert mc.REVIEWED_HEAD.startswith("61dce166be")


def test_installations_hunks_match_the_head_snapshot():
    """Every context and added line of each hunk sits at its new-side line
    number in the snapshot - the diff and the snapshot are one head."""
    snapshot = mc.head_content(mc.INSTALLATIONS_PATH).splitlines()
    for hunk in mc.hunks_for(mc.INSTALLATIONS_PATH):
        header, *lines = hunk.body.splitlines()
        n = int(header.split("+")[1].split(",")[0])
        for line in lines:
            if line.startswith("-"):
                continue
            assert snapshot[n - 1] == line[1:], (n, line)
            n += 1


def test_fixture_data_is_ascii():
    for path in mc._FIXTURE_DIR.iterdir():
        assert path.read_text().isascii(), path.name


def test_attester_head_content_is_the_new_file():
    content = mc.head_content(mc.ATTESTER_PATH)
    assert len(content.splitlines()) == 223
    ast.parse(content)


# --- class 1: cross-hop dataflow --------------------------------------------


def _changed(path: str) -> tuple[str, str]:
    """(added lines, context lines) across `path`'s hunks."""
    lines = [ln for h in mc.hunks_for(path) for ln in h.body.splitlines()[1:]]
    added = "\n".join(ln for ln in lines if ln.startswith("+"))
    context = "\n".join(ln for ln in lines if ln.startswith(" "))
    return added, context


def test_cross_hop_defect_lives_outside_the_added_lines():
    """Hop 1 (the default) is only diff context, and hops 2-3 (the read and
    the side effect it gates) are in no hunk at all. What the diff adds is
    the new optional fields that make a sparse payload natural."""
    added, context = _changed(mc.INSTALLATIONS_PATH)
    assert "tpm_enabled: bool = Field(default=True)" in context
    assert "tpm_enabled: bool = Field(default=True)" not in added
    for hop in ("now_enabled = body.tpm_enabled", "_toggle_enforcement("):
        assert hop not in added + context
    assert "sentinel_enabled: bool | None = Field(default=None)" in added


def test_cross_hop_evidence_reaches_the_prompt_only_as_context():
    """The file fits the full-file budget, so the distant read is in the
    prompt, framed as context with an instruction to flag only additions."""
    text = mc.pr751_evidence().reviewer_view().text
    assert "471:     now_enabled = body.tpm_enabled" in text
    assert "flag only diff additions" in text


def test_cross_hop_counterexample_sparse_payload_carries_tpm_true():
    """Hop 1 to hop 2, run on the real head class: a payload naming only
    `sentinel_enabled` still validates to tpm_enabled=True."""
    pydantic = pytest.importorskip("pydantic")
    tree = ast.parse(mc.head_content(mc.INSTALLATIONS_PATH))
    cls = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "RepoConfigPayload"
    )
    namespace = {
        "BaseModel": pydantic.BaseModel,
        "ConfigDict": pydantic.ConfigDict,
        "Field": pydantic.Field,
    }
    exec(compile(ast.Module(body=[cls], type_ignores=[]), "<head>", "exec"), namespace)
    payload = namespace["RepoConfigPayload"](sentinel_enabled=False)
    assert payload.tpm_enabled is True


# --- classes 2 and 3: the attester, before and after the fix -----------------


def _prefix_attester() -> types.ModuleType:
    """The attester exactly as reviewed, built from the fixture diff."""
    mod = types.ModuleType("prefix_attester")
    mod.__file__ = str(_REPO_ROOT / mc.ATTESTER_PATH)
    source = mc.head_content(mc.ATTESTER_PATH)
    # The fixture's own committed source, not external input.
    exec(compile(source, mod.__file__, "exec"), mod.__dict__)
    return mod


def _shipped_attester() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "shipped_attester", _REPO_ROOT / mc.ATTESTER_PATH,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _tree(
    tmp_path: Path, *, sentinel_source: str = "body.sentinel_enabled",
    ghost_card: bool = False,
) -> dict[str, Path]:
    """A minimal repo the attester can read: two personas, one extra flag."""
    files = {
        "REGISTRY": (
            'PersonaSpec(key="tpm", enabled_flag="tpm_enabled")\n'
            'PersonaSpec(key="sentinel", enabled_flag="sentinel_enabled")\n'
        ),
        "STORE": '_EXTRA_REPO_FLAGS = frozenset({"dep_watch_enabled"})\n',
        "API": (
            "class RepoConfigPayload(BaseModel):\n"
            "    tpm_enabled: bool | None = None\n"
            "    sentinel_enabled: bool | None = None\n"
            "    dep_watch_enabled: bool | None = None\n"
            "\n"
            "def update_repo_config(body):\n"
            "    set_repo_config(\n"
            "        tpm_enabled=body.tpm_enabled,\n"
            f"        sentinel_enabled={sentinel_source},\n"
            "        dep_watch_enabled=body.dep_watch_enabled,\n"
            "    )\n"
        ),
        "DASHBOARD": (
            '{ id: "chief", code: "F-01" },\n{ id: "sentinel", code: "F-08" },\n'
            + ('{ id: "ghostcard", code: "F-09" },\n' if ghost_card else "")
        ),
    }
    paths = {}
    for name, text in files.items():
        paths[name] = tmp_path / f"{name.lower()}.src"
        paths[name].write_text(text)
    return paths


def _run(mod: types.ModuleType, tmp_path: Path, paths: dict[str, Path],
         mapping: dict[str, str]) -> int:
    vars(mod).update(paths, REPO_ROOT=tmp_path, REGISTRY_KEY_TO_UI_ID=mapping)
    return mod.main()


_LIVE_MAPPING = {"tpm": "chief", "sentinel": "sentinel"}
_STALE_MAPPING = {**_LIVE_MAPPING, "deleted_persona": "ghostcard"}


@pytest.mark.parametrize("loader", [_prefix_attester, _shipped_attester])
def test_both_attesters_pass_a_coherent_tree(tmp_path, loader):
    """The synthetic tree is valid, so a 0 below is the guard being fooled,
    not the tree failing to parse."""
    assert _run(loader(), tmp_path, _tree(tmp_path), _LIVE_MAPPING) == 0


@pytest.mark.parametrize(
    "sentinel_source", ["body.guard_enabled", "True"], ids=["wrong-field", "constant"],
)
def test_weak_assertion_counterexample_fools_the_reviewed_guard(tmp_path, sentinel_source):
    paths = _tree(tmp_path, sentinel_source=sentinel_source)
    assert _run(_prefix_attester(), tmp_path, paths, _LIVE_MAPPING) == 0
    assert _run(_shipped_attester(), tmp_path, paths, _LIVE_MAPPING) == 1


def test_weak_assertion_fixture_counterexample_is_the_one_proven():
    fixture = _fixture("weak_assertion_keyword_only_forwarding")
    assert fixture.counterexample == "sentinel_enabled=body.guard_enabled"


def test_stale_mapping_entry_blesses_an_orphaned_card(tmp_path):
    paths = _tree(tmp_path, ghost_card=True)
    assert _run(_prefix_attester(), tmp_path, paths, _STALE_MAPPING) == 0
    assert _run(_shipped_attester(), tmp_path, paths, _STALE_MAPPING) == 1


def test_orphaned_card_without_a_stale_entry_fails_both(tmp_path):
    """Without the stale entry the pre-fix reverse check does work: the
    stale intermediary is the whole defect."""
    paths = _tree(tmp_path, ghost_card=True)
    assert _run(_prefix_attester(), tmp_path, paths, _LIVE_MAPPING) == 1


# --- scoring ------------------------------------------------------------------


def test_catch_needs_path_line_and_signal():
    fixture = _fixture("reverse_direction_stale_ui_mapping")
    hit = _F(mc.ATTESTER_PATH, 203, "a stale REGISTRY_KEY_TO_UI_ID entry passes")
    assert mc.caught(fixture, [hit])
    assert not mc.caught(fixture, [_F(mc.ATTESTER_PATH, 203, "use list.extend")])
    assert not mc.caught(fixture, [_F(mc.ATTESTER_PATH, 150, hit.message)])
    assert not mc.caught(fixture, [_F(mc.INSTALLATIONS_PATH, 203, hit.message)])


def test_recall():
    assert mc.recall({"a": True, "b": False, "c": False}) == pytest.approx(1 / 3)
    assert mc.recall({}) == 0.0


def test_production_baseline_rescores_to_zero_of_three():
    baseline = json.loads(mc.BASELINE_PATH.read_text())
    production = baseline["measurements"]["production"]
    findings = [_F(f["path"], f["line"], f["message"]) for f in production["findings"]]
    result = mc.score(mc.load_fixtures(), findings)
    assert result == production["caught"]
    assert set(result) == {f.name for f in mc.load_fixtures()}
    assert production["recall"] == mc.recall(result) == 0.0


def test_reachability_decided_per_class():
    by_class = {f.finding_class: f.reachability for f in mc.load_fixtures()}
    assert by_class == {
        "cross-hop-dataflow": mc.STRUCTURAL,
        "weak-assertion": mc.PROMPT_RULE,
        "reverse-direction-completeness": mc.PROMPT_RULE,
    }


# --- live replay, with the transport faked ----------------------------------


def _response(findings: list[dict]) -> httpx.Response:
    content = json.dumps({"findings": findings})
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_replay_sends_full_file_evidence_and_scores_the_answer():
    sent: list[list[dict[str, str]]] = []

    def post(backend, messages):
        sent.append(messages)
        return _response([{
            "path": mc.ATTESTER_PATH, "line": 125, "rule": "test-fidelity",
            "severity": "medium",
            "message": "records only keyword names; body.guard_enabled would pass",
        }])

    run = mc.replay(_BACKEND, post=post)
    assert not run.errored
    prompt = "\n".join(m["content"] for m in sent[0])
    assert "471:     now_enabled = body.tpm_enabled" in prompt
    result = mc.score(mc.load_fixtures(), run.findings)
    assert result["weak_assertion_keyword_only_forwarding"] is True
    assert mc.recall(result) == pytest.approx(1 / 3)


def test_replay_transport_failure_is_errored_not_zero():
    def post(backend, messages):
        raise httpx.ConnectError("down")

    assert mc.replay(_BACKEND, post=post).errored


def test_replay_unparseable_answer_is_errored():
    def post(backend, messages):
        return httpx.Response(200, text="<html>gateway</html>")

    assert mc.replay(_BACKEND, post=post).errored
