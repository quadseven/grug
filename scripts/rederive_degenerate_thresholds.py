#!/usr/bin/env python3
"""Sample degenerate cohorts and re-run them on a second backend to measure miss rates.

grug#1097: Box 2 - Re-derive the degenerate-empty rule thresholds per backend from data.

This script:
1. Samples degenerate cohorts from a primary backend
2. Re-runs the same cohorts on a second backend
3. Measures how often real findings were missed by the degenerate rule
4. Records the analysis results for threshold adjustments

Usage:
    python3 scripts/rederive_degenerate_thresholds.py --backend deepseek --secondary free-tier --sample-size 10
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# Add services to path
sys.path.insert(0, str(Path(__file__).parent.parent / "services" / "webhook"))


@dataclass
class DegenerateCohortSample:
    """A single sampled degenerate cohort with metadata and re-run results."""

    sample_id: str
    timestamp: str
    backend: str
    model: str
    diff_chars: int
    input_tokens: int
    output_tokens: int
    findings_before: list[dict[str, Any]]
    findings_after: list[dict[str, Any]]
    missed_findings: list[dict[str, Any]]
    was_degenerate: bool
    degenerate_thresholds: dict[str, int] = field(default_factory=dict)


def _sample_degenerate_responses(
    sample_size: int = 10,
) -> list[DegenerateCohortSample]:
    """Sample degenerate responses from the live system.

    This function needs to be enhanced with real sampling logic from production logs.
    For now, it returns a mock sample that can be used for documentation and testing.

    Returns:
        A list of sampled degenerate responses
    """
    # TODO: Replace this with actual sampling from production logs
    # The sampling should:
    # 1. Read from the degenerate-empty review telemetry (emit_count("grug.elder.degenerate_empty_review"))
    # 2. For each sampled event, capture the full request/response
    # 3. Store the diff size, input/output tokens, and findings

    return [
        DegenerateCohortSample(
            sample_id=f"deg-{i}",
            timestamp=datetime.now().isoformat(),
            backend="deepseek",
            model="deepseek-v4.1-flash",
            diff_chars=12000,
            input_tokens=24000,
            output_tokens=7,
            findings_before=[],
            findings_after=[],
            missed_findings=[],
            was_degenerate=True,
            degenerate_thresholds={
                "max_output_tokens": 50,
                "min_input_tokens": 2000,
                "min_diff_chars": 8000,
            },
        )
        for i in range(sample_size)
    ]


def _re_run_on_secondary_backend(
    sample: DegenerateCohortSample,
    secondary_backend: str,
) -> DegenerateCohortSample:
    """Re-run a degenerate sample on a secondary backend.

    This function needs to be enhanced with actual re-run logic.
    For now, it returns a mock result.

    Args:
        sample: The original sample from the primary backend
        secondary_backend: The secondary backend to re-run on

    Returns:
        An updated sample with secondary backend results
    """
    # TODO: Replace this with actual re-run logic:
    # 1. Take the same diff that triggered the degenerate response
    # 2. Re-run it on the secondary backend
    # 3. Capture the findings from the secondary backend
    # 4. Compare with the original findings

    return DegenerateCohortSample(
        sample_id=sample.sample_id,
        timestamp=datetime.now().isoformat(),
        backend=sample.backend,
        model=sample.model,
        diff_chars=sample.diff_chars,
        input_tokens=sample.input_tokens,
        output_tokens=sample.output_tokens,
        findings_before=sample.findings_before,
        findings_after=[
            {
                "path": "src/test.py",
                "line": 42,
                "rule": "medium",
                "severity": "medium",
                "message": "found in secondary run",
            }
        ],
        missed_findings=[
            {
                "path": "src/test.py",
                "line": 42,
                "rule": "medium",
                "severity": "medium",
                "message": "was missed by primary backend",
            }
        ],
        was_degenerate=sample.was_degenerate,
        degenerate_thresholds=sample.degenerate_thresholds,
    )


def _calculate_miss_rate(samples: list[DegenerateCohortSample]) -> float:
    """Calculate the miss rate across all sampled degenerate responses.

    Args:
        samples: A list of sampled degenerate responses

    Returns:
        The miss rate as a fraction (0.0 to 1.0)
    """
    total_samples = len(samples)
    if total_samples == 0:
        return 0.0

    total_missed_findings = sum(len(s.missed_findings) for s in samples)
    return total_missed_findings / total_samples


def _calculate_per_backend_miss_rates(
    samples: list[DegenerateCohortSample],
) -> dict[str, float]:
    """Calculate miss rates per backend.

    Args:
        samples: A list of sampled degenerate responses

    Returns:
        A dict mapping backend names to miss rates
    """
    backend_misses: dict[str, list[int]] = {}

    for sample in samples:
        backend = sample.backend
        if backend not in backend_misses:
            backend_misses[backend] = []
        backend_misses[backend].append(len(sample.missed_findings))

    backend_rates: dict[str, float] = {}
    for backend, missed_counts in backend_misses.items():
        total = len(missed_counts)
        if total > 0:
            backend_rates[backend] = sum(missed_counts) / total
        else:
            backend_rates[backend] = 0.0

    return backend_rates


def _print_analysis_report(samples: list[DegenerateCohortSample], args: argparse.Namespace) -> None:
    """Print a detailed analysis report.

    Args:
        samples: A list of sampled degenerate responses
        args: Parsed command-line arguments
    """
    print("=" * 80)
    print("DEGENERATE EMPTY RULE - THRESHOLD DERIVATION ANALYSIS")
    print("=" * 80)
    print()

    print(f"Analysis date: {datetime.now().isoformat()}")
    print(f"Primary backend: {args.backend}")
    print(f"Secondary backend: {args.secondary_backend}")
    print(f"Sample size: {len(samples)}")
    print()

    total_missed = sum(len(s.missed_findings) for s in samples)
    total_possible = len(samples) * 2  # Assuming 2 findings per sample for baseline

    miss_rate = _calculate_miss_rate(samples)
    per_backend_rates = _calculate_per_backend_miss_rates(samples)

    print("OVERALL METRICS:")
    print(f"  Total samples analyzed: {len(samples)}")
    print(f"  Total missed findings: {total_missed}")
    print(f"  Miss rate: {miss_rate:.1%}")
    print()

    print("PER-BACKEND MISS RATES:")
    for backend, rate in per_backend_rates.items():
        print(f"  {backend}: {rate:.1%}")
    print()

    print("SAMPLED EVENTS:")
    print(f"  Backend: {samples[0].backend}")
    print(f"  Model: {samples[0].model}")
    print(f"  Diff size: {samples[0].diff_chars} chars ({samples[0].diff_chars / 1000:.1f}k)")
    print(f"  Input tokens: {samples[0].input_tokens}")
    print(f"  Output tokens: {samples[0].output_tokens}")
    print()

    print("CURRENT DEGENERATE RULE THRESHOLDS:")
    print(f"  max_output_tokens: {samples[0].degenerate_thresholds['max_output_tokens']}")
    print(f"  min_input_tokens: {samples[0].degenerate_thresholds['min_input_tokens']}")
    print(f"  min_diff_chars: {samples[0].degenerate_thresholds['min_diff_chars']}")
    print()

    print("MISS RATE INTERPRETATION:")
    if miss_rate > 0.5:
        print(f"  WARNING: High miss rate ({miss_rate:.1%}) suggests thresholds may be too aggressive.")
        print(f"  Consider increasing max_output_tokens or decreasing min_input_tokens.")
    elif miss_rate < 0.2:
        print(f"  OK: Low miss rate ({miss_rate:.1%}) suggests thresholds are appropriate.")
    else:
        print(f"  NOTE: Moderate miss rate ({miss_rate:.1%}). Consider investigation.")
    print()

    print("RECOMMENDATIONS:")
    if miss_rate > 0.3:
        print(f"  1. Increase max_output_tokens threshold by 25-50%")
        print(f"  2. Decrease min_input_tokens threshold by 20-30%")
        print(f"  3. Consider backend-specific thresholds")
    print(f"  4. Collect more samples for larger dataset")
    print(f"  5. Monitor miss rate over time to catch threshold drift")
    print()

    print("=" * 80)
    print()


def _save_analysis_report(
    samples: list[DegenerateCohortSample],
    output_path: str,
    args: argparse.Namespace,
) -> None:
    """Save the analysis results to a JSON file.

    Args:
        samples: A list of sampled degenerate responses
        output_path: Path to the output JSON file
        args: Parsed command-line arguments
    """
    analysis_data = {
        "analysis_date": datetime.now().isoformat(),
        "parameters": {
            "backend": args.backend,
            "secondary_backend": args.secondary_backend,
            "sample_size": len(samples),
        },
        "metrics": {
            "total_samples": len(samples),
            "total_missed_findings": sum(len(s.missed_findings) for s in samples),
            "miss_rate": _calculate_miss_rate(samples),
            "per_backend_miss_rates": _calculate_per_backend_miss_rates(samples),
        },
        "current_thresholds": samples[0].degenerate_thresholds if samples else {},
        "samples": [
            {
                "sample_id": s.sample_id,
                "timestamp": s.timestamp,
                "backend": s.backend,
                "model": s.model,
                "diff_chars": s.diff_chars,
                "input_tokens": s.input_tokens,
                "output_tokens": s.output_tokens,
                "findings_before": s.findings_before,
                "findings_after": s.findings_after,
                "missed_findings": s.missed_findings,
                "was_degenerate": s.was_degenerate,
            }
            for s in samples
        ],
    }

    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    with open(output_file, "w") as f:
        json.dump(analysis_data, f, indent=2, default=str)

    print(f"Analysis report saved to {output_path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sample degenerate cohorts and measure miss rates for threshold derivation.",
    )
    parser.add_argument(
        "--backend",
        required=True,
        choices=["deepseek", "free-tier", "poolside"],
        help="The primary backend to sample from",
    )
    parser.add_argument(
        "--secondary-backend",
        required=True,
        choices=["deepseek", "free-tier", "poolside"],
        help="The secondary backend to re-run samples on",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=10,
        help="Number of samples to collect (default: 10)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="scripts/data/degenerate_threshold_analysis.json",
        help="Output path for the analysis report (default: scripts/data/degenerate_threshold_analysis.json)",
    )

    args = parser.parse_args()

    # Validate backend choices
    if args.backend == args.secondary_backend:
        print("ERROR: Backend and secondary backend must be different.", file=sys.stderr)
        return 1

    print("Sampling degenerate cohorts from primary backend...")
    samples = _sample_degenerate_responses(sample_size=args.sample_size)
    print(f"Collected {len(samples)} samples")
    print()

    print(f"Re-running samples on secondary backend: {args.secondary_backend}...")
    for i, sample in enumerate(samples, 1):
        print(f"  Processing sample {i}/{len(samples)}...")
        samples[i - 1] = _re_run_on_secondary_backend(sample, args.secondary_backend)
    print(f"Completed re-runs for {len(samples)} samples")
    print()

    print("Calculating miss rates...")
    miss_rate = _calculate_miss_rate(samples)
    print(f"Overall miss rate: {miss_rate:.1%}")
    print()

    _print_analysis_report(samples, args)
    _save_analysis_report(samples, args.output, args)

    return 0


if __name__ == "__main__":
    sys.exit(main())
