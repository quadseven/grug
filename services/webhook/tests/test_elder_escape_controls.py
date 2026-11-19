"""Focused coverage for real-PR clean controls in the Elder replay corpus."""

from __future__ import annotations

import pytest
from elder_eval import __main__ as cli
from elder_eval.corpus import build_cases, parse_clean_controls
from elder_eval.scoring import CaseReplay, score
from ledger import LedgerRow

_REPO = "example/project"


def test_clean_controls_are_replayable_and_all_emissions_count_as_noise():
    controls = parse_clean_controls('{"repo":"example/project","pr":17}\n')
    (case,) = build_cases([], clean_controls=controls)

    assert (case.repo, case.pr) == (_REPO, 17)
    assert case.scorable
    assert case.clean

    report = score(
        [case],
        {
            case.case_id: CaseReplay(
                case_id=case.case_id,
                emitted={"correctness": 2},
                errored=False,
            )
        },
    )

    assert report.cases_scored == 1
    assert report.overall_catch == 0.0
    assert report.noise_rate == 1.0


def test_clean_control_input_rejects_malformed_rows():
    with pytest.raises(ValueError, match="line 1"):
        parse_clean_controls('{"repo":"example/project","pr":"bad"}\n')


def test_replay_cli_loads_clean_control_jsonl(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("")
    controls = tmp_path / "controls.jsonl"
    controls.write_text('{"repo":"example/project","pr":17}\n')
    _, args = cli._parse_args([
        "--jsonl", str(ledger), "--clean-controls-jsonl", str(controls),
    ])

    _, all_cases, cases = cli._load_corpus(args)

    assert [(case.repo, case.pr) for case in all_cases] == [(_REPO, 17)]
    assert cases == all_cases


def test_clean_control_with_known_findings_only_counts_unexpected_classes_as_noise():
    finding = LedgerRow(
        repo=_REPO,
        pr=17,
        reviewer="fixture-reviewer",
        severity="HIGH",
        finding_class="correctness",
        finding="synthetic finding",
        verdict="fixed",
    )
    (case,) = build_cases([finding], clean_controls=[(_REPO, 17)])

    assert case.clean_control
    report = score(
        [case],
        {
            case.case_id: CaseReplay(
                case_id=case.case_id,
                emitted={"correctness": 2, "security": 1},
                errored=False,
            )
        },
    )

    assert report.overall_catch == 1.0
    assert report.noise_rate == pytest.approx(1 / 3)


def test_clean_control_argument_is_optional():
    assert build_cases([]) == ()
