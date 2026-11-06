"""Tests for iac_scan's manifest-break detectors (#1480).

Three Kubernetes manifest changes that break production show in the diff
alone: a pod (anti-)affinity term that selects another workload's pods with
no namespaces, a singleton Deployment switched from Recreate to
RollingUpdate, and an imagePullSecrets block removed with no re-add. Each
detector fires on its anonymized corpus fixture and stays quiet on the clean
controls beside it, and the corpus recall and false-positive rate recorded in
`fixtures/manifest_breaks/corpus.json` are what the scanner produces now.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from personas.code_reviewer.diff_parser import parse_diff
from personas.code_reviewer.iac_scan import (
    AFFINITY_NO_NAMESPACE,
    IAC_MISCONFIG,
    MANIFEST_BREAK_KINDS,
    PULL_SECRET_DROPPED,
    SINGLETON_ROLLING_UPDATE,
    scan_iac,
)

_CORPUS_DIR = (
    Path(__file__).resolve().parents[1] / "elder_eval" / "fixtures" / "manifest_breaks"
)
_CORPUS = json.loads((_CORPUS_DIR / "corpus.json").read_text())


def _fixture(name: str) -> str:
    return (_CORPUS_DIR / name).read_text()


def _break_kinds(diff: str) -> list[str]:
    """The manifest-break kinds scan_iac raises on a diff, in order."""
    out = []
    for cand in scan_iac(parse_diff(diff)):
        assert cand.vuln_class == IAC_MISCONFIG
        kind = cand.snippet.split(": ", 1)[0]
        if kind in MANIFEST_BREAK_KINDS:
            out.append(kind)
    return out


def _anchor(diff: str, kind: str):
    hunks = parse_diff(diff)
    cand = next(c for c in scan_iac(hunks) if c.snippet.startswith(kind))
    added = {(h.file_path, n) for h in hunks for n in h.new_lines}
    return cand, (cand.file, cand.line) in added


# --- each detector fires on its fixture -------------------------------------


def test_affinity_without_namespaces_fires_on_pr_a():
    diff = _fixture("damaging_pr_a_affinity_no_namespace.diff")
    assert _break_kinds(diff) == [AFFINITY_NO_NAMESPACE]
    cand, on_added_line = _anchor(diff, AFFINITY_NO_NAMESPACE)
    assert on_added_line
    assert "selects egress-proxy;" in cand.snippet


def test_singleton_rolling_update_and_dropped_pull_secret_fire_on_pr_d():
    diff = _fixture("damaging_pr_d_singleton_and_pull_secret.diff")
    assert sorted(_break_kinds(diff)) == sorted(
        [SINGLETON_ROLLING_UPDATE, PULL_SECRET_DROPPED]
    )
    cand, on_added_line = _anchor(diff, SINGLETON_ROLLING_UPDATE)
    assert on_added_line and "RollingUpdate" in cand.snippet
    cand, on_added_line = _anchor(diff, PULL_SECRET_DROPPED)
    assert on_added_line and cand.file == "manifests/home/light-controller.yaml"


# --- and stays quiet on the controls ----------------------------------------


@pytest.mark.parametrize(
    "fixture",
    [c["fixture"] for c in _CORPUS["cases"] if c["label"] == "clean"],
)
def test_clean_control_raises_no_manifest_break(fixture):
    assert _break_kinds(_fixture(fixture)) == []


def test_self_anti_affinity_in_damaging_pr_d_is_not_flagged():
    # PR D adds a podAntiAffinity term selecting its own app label; that one
    # is correct and must not count as a cross-namespace term.
    diff = _fixture("damaging_pr_d_singleton_and_pull_secret.diff")
    assert AFFINITY_NO_NAMESPACE not in _break_kinds(diff)


def test_live_state_only_pr_c_raises_nothing():
    # Two replicas with maxSurge 0: not a singleton, and the self
    # anti-affinity is correct. Its break needs live cluster state.
    assert _break_kinds(_fixture("damaging_pr_c_live_state_only.diff")) == []


def test_detectors_only_read_yaml():
    diff = _fixture("damaging_pr_a_affinity_no_namespace.diff").replace(
        "deployment.yaml", "deployment.txt"
    )
    assert _break_kinds(diff) == []


def test_removed_affinity_term_is_not_flagged():
    # A term that only appears on removed lines is not introduced by the PR.
    diff = (
        "diff --git a/m/a.yaml b/m/a.yaml\n--- a/m/a.yaml\n+++ b/m/a.yaml\n"
        "@@ -1,7 +1,1 @@\n"
        " spec:\n"
        "-  affinity:\n"
        "-    podAffinity:\n"
        "-      requiredDuringSchedulingIgnoredDuringExecution:\n"
        "-        - labelSelector:\n"
        "-            matchLabels: {app: other}\n"
        "-          topologyKey: kubernetes.io/hostname\n"
    )
    assert _break_kinds(diff) == []


# --- the recorded corpus measurement ----------------------------------------


def _measure() -> tuple[str, str]:
    expected_total = caught = 0
    clean_total = clean_flagged = 0
    for case in _CORPUS["cases"]:
        kinds = _break_kinds(_fixture(case["fixture"]))
        expected = list(case["expected"])
        expected_total += len(expected)
        caught += sum(1 for k in expected if k in kinds)
        if case["label"] == "clean":
            clean_total += 1
            clean_flagged += 1 if kinds else 0
        else:
            # A damaging case raising a kind it should not is still noise.
            assert set(kinds) <= set(expected), (case["fixture"], kinds)
    return f"{caught}/{expected_total}", f"{clean_flagged}/{clean_total}"


def test_corpus_measurement_matches_the_recorded_after_numbers():
    recall, fp_rate = _measure()
    after = _CORPUS["measurements"]["after"]
    assert (recall, fp_rate) == (after["recall"], after["false_positive_rate"])


def test_every_llm_only_class_has_a_written_reason():
    reasons = _CORPUS["llm_only_reasons"]
    named = {k for c in _CORPUS["cases"] for k in c.get("llm_only", [])}
    assert named and named <= set(reasons)
    assert all(len(reasons[k].split()) >= 20 for k in named)


def test_expected_kinds_are_real_detector_kinds():
    for case in _CORPUS["cases"]:
        assert set(case["expected"]) <= set(MANIFEST_BREAK_KINDS)
