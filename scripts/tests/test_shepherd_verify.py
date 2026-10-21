"""Post-deploy verdicts use the PR's stated, observable claims."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import shepherd_verify  # noqa: E402
from shepherd_verify import main, parse_observables, verify_claims  # noqa: E402


def test_only_acceptance_criteria_supply_observables():
    body = """Metric `outside.section` appears.
## Acceptance criteria
- [x] Metric `grug.review.completed` emits after a review
- [ ] Log event `review accepted` appears
- [ ] GET `https://example.com/livez` returns 200
## Out of scope
- Metric `outside.again` appears
"""
    assert [(item.source, item.target) for item in parse_observables(body)] == [
        ("metric", "grug.review.completed"),
        ("log", "review accepted"),
        ("endpoint", "https://example.com/livez"),
    ]


def test_no_observable_is_unverifiable_and_queries_nothing():
    def query(*args):
        raise AssertionError("query should not run")

    result = verify_claims(
        "## Acceptance criteria\n- [x] Make review faster", query, baselines={}
    )
    assert result.status == "unverifiable"
    assert "no observable" in result.evidence


def test_first_empty_metric_query_is_never_missing():
    calls = []

    def query(source, name):
        calls.append((source, name))
        return 1 if name == "grug.live" else 0

    result = verify_claims(
        "## Acceptance criteria\n- [x] Metric `grug.review.completed` appears",
        query,
        baselines={"metric": "grug.live"},
        attempts=1,
    )
    assert result.status == "unverifiable"
    assert calls == [("metric", "grug.review.completed"), ("metric", "grug.live")]
    assert "attempt 1" in result.evidence


def test_lagging_source_is_unverifiable_even_after_retry():
    def query(source, name):
        return 0

    result = verify_claims(
        "## Acceptance criteria\n- [x] Metric `grug.review.completed` appears",
        query,
        baselines={"metric": "grug.live"},
        attempts=2,
        pause=lambda seconds: None,
    )
    assert result.status == "unverifiable"
    assert "baseline" in result.evidence
    assert "attempt 2" in result.evidence
    assert "ingestion" in result.evidence


def test_live_source_and_repeated_absence_report_missing_with_evidence():
    calls = []

    def query(source, name):
        calls.append((source, name))
        return 3 if name == "grug.live" else 0

    result = verify_claims(
        "## Acceptance criteria\n- [x] Metric `grug.review.completed` appears",
        query,
        baselines={"metric": "grug.live"},
        attempts=2,
        pause=lambda seconds: None,
    )
    assert result.status == "missing"
    assert calls == [("metric", "grug.review.completed"), ("metric", "grug.live")] * 2
    assert "grug.review.completed" in result.evidence
    assert "grug.live" in result.evidence
    assert "attempt 1" in result.evidence and "attempt 2" in result.evidence


def test_late_target_is_observed_after_retry():
    target_reads = iter([0, 2])

    def query(source, name):
        return 1 if name == "grug.live" else next(target_reads)

    result = verify_claims(
        "## Acceptance criteria\n- [x] Metric `grug.review.completed` appears",
        query,
        baselines={"metric": "grug.live"},
        attempts=2,
        pause=lambda seconds: None,
    )
    assert result.status == "observed"
    assert "attempt 2" in result.evidence


def test_unconfigured_baseline_cannot_certify_missing():
    result = verify_claims(
        "## Acceptance criteria\n- Log event `review accepted` appears",
        lambda source, name: 0,
        baselines={},
        attempts=2,
        pause=lambda seconds: None,
    )
    assert result.status == "unverifiable"
    assert "baseline" in result.evidence


def test_retry_waits_before_second_query():
    waits = []
    result = verify_claims(
        "## Acceptance criteria\n- Metric `grug.review.completed` appears",
        lambda source, name: 1 if name == "grug.live" else 0,
        baselines={"metric": "grug.live"},
        attempts=2,
        retry_seconds=30,
        pause=waits.append,
    )
    assert result.status == "missing"
    assert waits == [30]


def test_extraction_ignores_fenced_examples_and_keeps_subheadings():
    body = """## Acceptance criteria
```
- Metric `fake.example` appears
```
### Runtime
- Metric `grug.review.completed` appears
## Out of scope
- Metric `outside.example` appears
"""
    assert [item.target for item in parse_observables(body)] == [
        "grug.review.completed"
    ]


def test_multiple_claims_report_each_source_and_preserve_uncertainty():
    body = """## Acceptance criteria
- Metric `grug.review.completed` appears
- Log event `review accepted` appears
"""
    result = verify_claims(
        body,
        lambda source, name: 1 if source == "metric" else 0,
        baselines={"metric": "grug.live", "log": "service alive"},
        attempts=2,
        pause=lambda seconds: None,
    )
    assert result.status == "unverifiable"
    assert "grug.review.completed" in result.evidence
    assert "review accepted" in result.evidence


def test_cli_queries_pr_claim_and_baseline_after_deploy(tmp_path, capsys):
    body = tmp_path / "pr.md"
    body.write_text(
        "## Acceptance criteria\n- Metric `grug.review.completed` appears\n"
    )
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import sys\n"
        "assert len(sys.argv) == 6\n"
        "assert int(sys.argv[4]) >= int(sys.argv[3]) == 100\n"
        "assert sys.argv[5] == ('' if sys.argv[2] == 'grug.live' else "
        "'Metric `grug.review.completed` appears')\n"
        "print(2 if sys.argv[2] == 'grug.live' else 0)\n"
    )
    code = main(
        [
            "--body-file",
            str(body),
            "--query-command",
            sys.executable,
            str(probe),
            "--baseline",
            "metric=grug.live",
            "--retry-seconds",
            "0",
            "--since-epoch",
            "100",
        ]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "missing" in out
    assert "attempt 2" in out
    assert "grug.live" in out


def test_cli_reports_failed_lookup_instead_of_missing(tmp_path, capsys):
    body = tmp_path / "pr.md"
    body.write_text(
        "## Acceptance criteria\n- Metric `grug.review.completed` appears\n"
    )
    probe = tmp_path / "probe.py"
    probe.write_text("raise RuntimeError('probe failed')\n")
    code = main(
        [
            "--body-file",
            str(body),
            "--query-command",
            sys.executable,
            str(probe),
            "--baseline",
            "metric=grug.live",
            "--retry-seconds",
            "0",
            "--since-epoch",
            "100",
        ]
    )
    out = capsys.readouterr().out
    assert code == 2
    assert "unverifiable" in out
    assert "probe failed" in out


def test_endpoint_baseline_must_share_origin():
    with pytest.raises(ValueError, match="same origin"):
        verify_claims(
            "## Acceptance criteria\n- GET `https://example.com/check` returns 200",
            lambda source, name: 0,
            baselines={"endpoint": "https://other.example.com/livez"},
            attempts=2,
            pause=lambda seconds: None,
        )


def test_explicit_unquoted_metric_and_endpoint_and_quoted_log_are_parsed():
    body = """## Acceptance criteria
- Metric grug.review.completed increases
- Log event "review accepted" appears
- GET https://example.com/livez returns 200
"""
    assert [(item.source, item.target) for item in parse_observables(body)] == [
        ("metric", "grug.review.completed"),
        ("log", "review accepted"),
        ("endpoint", "https://example.com/livez"),
    ]


def test_cli_fetches_the_pr_body_it_checks(monkeypatch, capsys):
    calls = []

    def run(argv):
        calls.append(argv)
        if argv[:3] == ["gh", "pr", "view"]:
            return '{"body":"## Acceptance criteria\\n- Metric `grug.review.completed` appears"}'
        return "1"

    monkeypatch.setattr(shepherd_verify, "_run", run)
    code = main(
        [
            "--pr",
            "7",
            "--repo",
            "example-org/example-repo",
            "--query-command",
            "probe",
            "--since-epoch",
            "100",
        ]
    )
    assert code == 0
    assert calls[0][:6] == [
        "gh",
        "pr",
        "view",
        "7",
        "-R",
        "example-org/example-repo",
    ]
    assert "observed" in capsys.readouterr().out


def test_missing_requires_live_baseline_on_every_attempt():
    baselines = iter([2, 2, 0])

    def query(source, name):
        return next(baselines) if name == "grug.live" else 0

    result = verify_claims(
        "## Acceptance criteria\n- Metric `grug.review.completed` appears",
        query,
        baselines={"metric": "grug.live"},
        attempts=3,
        pause=lambda seconds: None,
    )
    assert result.status == "unverifiable"
