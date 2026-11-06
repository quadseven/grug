"""Tests for the degenerate threshold derivation script.

grug#1097: Box 2 - Re-derive the degenerate-empty rule thresholds per backend from data.

This test suite validates:
1. Sample generation and validation
2. Re-run on secondary backends
3. Miss rate calculation
4. Per-backend miss rate calculation
5. Report generation
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import the functions we're testing
from rederive_degenerate_thresholds import (
    _calculate_miss_rate,
    _calculate_per_backend_miss_rates,
    _re_run_on_secondary_backend,
    _sample_degenerate_responses,
)


class TestSampleGeneration:
    """Tests for sampling degenerate responses."""

    def test_sample_degenerate_responses_creates_samples(self):
        """Test that sampling creates the expected number of samples."""
        samples = _sample_degenerate_responses(sample_size=10)

        assert len(samples) == 10
        assert all(s.was_degenerate for s in samples)

    def test_sample_degenerate_responses_has_metadata(self):
        """Test that each sample has required metadata."""
        samples = _sample_degenerate_responses(sample_size=3)

        for sample in samples:
            assert sample.sample_id is not None
            assert sample.timestamp is not None
            assert sample.backend is not None
            assert sample.model is not None
            assert sample.diff_chars is not None
            assert sample.input_tokens is not None
            assert sample.output_tokens is not None
            assert sample.findings_before == []
            assert sample.findings_after == []
            assert sample.missed_findings == []

    def test_sample_degenerate_responses_has_thresholds(self):
        """Test that each sample has current thresholds."""
        samples = _sample_degenerate_responses(sample_size=3)

        for sample in samples:
            assert "max_output_tokens" in sample.degenerate_thresholds
            assert "min_input_tokens" in sample.degenerate_thresholds
            assert "min_diff_chars" in sample.degenerate_thresholds
            assert sample.degenerate_thresholds["max_output_tokens"] == 50
            assert sample.degenerate_thresholds["min_input_tokens"] == 2000


class TestReRunOnSecondaryBackend:
    """Tests for re-running samples on secondary backends."""

    def test_re_run_on_free_tier(self):
        """Test re-running a sample on the free-tier backend."""
        sample = _sample_degenerate_responses(sample_size=1)[0]

        result = _re_run_on_secondary_backend(sample, "free-tier")

        assert result.backend == sample.backend
        assert result.sample_id == sample.sample_id
        assert len(result.findings_after) > 0
        assert len(result.missed_findings) > 0

    def test_re_run_on_poolside(self):
        """Test re-running a sample on the poolside backend."""
        sample = _sample_degenerate_responses(sample_size=1)[0]

        result = _re_run_on_secondary_backend(sample, "poolside")

        assert result.backend == sample.backend
        assert result.sample_id == sample.sample_id
        assert len(result.findings_after) > 0
        assert len(result.missed_findings) > 0

    def test_re_run_preserves_original_data(self):
        """Test that re-run preserves original sample data."""
        sample = _sample_degenerate_responses(sample_size=1)[0]

        result = _re_run_on_secondary_backend(sample, "free-tier")

        assert result.diff_chars == sample.diff_chars
        assert result.input_tokens == sample.input_tokens
        assert result.output_tokens == sample.output_tokens
        assert result.was_degenerate == sample.was_degenerate


class TestMissRateCalculation:
    """Tests for calculating miss rates."""

    def test_calculate_miss_rate_zero_samples(self):
        """Test that miss rate is zero when no samples are provided."""
        assert _calculate_miss_rate([]) == 0.0

    def test_calculate_miss_rate_no_missed_findings(self):
        """Test that miss rate is zero when no findings are missed."""
        samples = [
            _sample_degenerate_responses(sample_size=1)[0]
            for _ in range(5)
        ]

        for sample in samples:
            sample.missed_findings = []

        assert _calculate_miss_rate(samples) == 0.0

    def test_calculate_miss_rate_all_missed(self):
        """Test that miss rate is 1.0 when all findings are missed."""
        samples = [
            _sample_degenerate_responses(sample_size=1)[0]
            for _ in range(5)
        ]

        for sample in samples:
            sample.missed_findings = [
                {"path": "test.py", "line": 1, "rule": "medium"}
            ]

        assert _calculate_miss_rate(samples) == 1.0

    def test_calculate_miss_rate_partial_miss(self):
        """Test that miss rate is calculated correctly for partial misses."""
        samples = [
            _sample_degenerate_responses(sample_size=1)[0]
            for _ in range(10)
        ]

        # 3 samples have 1 missed finding each
        for i in range(3):
            samples[i].missed_findings = [
                {"path": "test.py", "line": 1, "rule": "medium"}
            ]

        # 2 samples have 2 missed findings each
        for i in range(3, 5):
            samples[i].missed_findings = [
                {"path": "test.py", "line": 1, "rule": "medium"},
                {"path": "test2.py", "line": 2, "rule": "high"},
            ]

        # 5 samples have no missed findings
        # Miss rate = 7 total missed findings / 10 samples = 0.7
        assert _calculate_miss_rate(samples) == 0.7


class TestPerBackendMissRates:
    """Tests for calculating miss rates per backend."""

    def test_calculate_per_backend_miss_rates(self):
        """Test that per-backend miss rates are calculated correctly."""
        samples = [
            _sample_degenerate_responses(sample_size=1)[0]
            for _ in range(10)
        ]

        # 5 deepseek samples with 1 missed finding each
        for i in range(5):
            samples[i].backend = "deepseek"
            samples[i].missed_findings = [
                {"path": "test.py", "line": 1, "rule": "medium"}
            ]

        # 5 free-tier samples with 2 missed findings each
        for i in range(5, 10):
            samples[i].backend = "free-tier"
            samples[i].missed_findings = [
                {"path": "test.py", "line": 1, "rule": "medium"},
                {"path": "test2.py", "line": 2, "rule": "high"},
            ]

        rates = _calculate_per_backend_miss_rates(samples)

        assert rates["deepseek"] == 1.0  # 5 missed / 5 samples
        assert rates["free-tier"] == 2.0  # 10 missed / 5 samples

    def test_calculate_per_backend_miss_rates_empty(self):
        """Test that empty samples return empty rates."""
        assert _calculate_per_backend_miss_rates([]) == {}

    def test_calculate_per_backend_miss_rates_single_backend(self):
        """Test that single backend samples return that backend's rate."""
        samples = [
            _sample_degenerate_responses(sample_size=1)[0]
            for _ in range(10)
        ]

        for i in range(10):
            samples[i].backend = "deepseek"
            samples[i].missed_findings = [
                {"path": "test.py", "line": 1, "rule": "medium"}
            ]

        rates = _calculate_per_backend_miss_rates(samples)

        assert rates == {"deepseek": 1.0}


class TestReportGeneration:
    """Tests for generating analysis reports."""

    def test_save_analysis_report_creates_file(self):
        """Test that saving a report creates a valid JSON file."""
        samples = [
            _sample_degenerate_responses(sample_size=3)[0]
            for _ in range(5)
        ]

        # Set up some backend data
        for i in range(3):
            samples[i].backend = "deepseek"
            samples[i].missed_findings = [
                {"path": "test.py", "line": 1, "rule": "medium"}
            ]
        for i in range(3, 5):
            samples[i].backend = "free-tier"
            samples[i].missed_findings = [
                {"path": "test.py", "line": 1, "rule": "medium"},
                {"path": "test2.py", "line": 2, "rule": "high"},
            ]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            temp_path = f.name

        try:
            from rederive_degenerate_thresholds import _save_analysis_report

            _save_analysis_report(samples, temp_path, argparse.Namespace(
                backend="deepseek",
                secondary_backend="free-tier",
                sample_size=5,
                output=temp_path
            ))

            with open(temp_path) as f:
                data = json.load(f)

            assert "analysis_date" in data
            assert "parameters" in data
            assert "metrics" in data
            assert "current_thresholds" in data
            assert "samples" in data
            assert len(data["samples"]) == 5
            assert "total_samples" in data["metrics"]
            assert "total_missed_findings" in data["metrics"]
            assert "miss_rate" in data["metrics"]
            assert "per_backend_miss_rates" in data["metrics"]
        finally:
            Path(temp_path).unlink(missing_ok=True)


class TestValidation:
    """Tests for validating the approach."""

    def test_simulated_deepseek_high_miss_rate(self):
        """Test analysis of a simulated high miss rate scenario."""
        samples = [
            _sample_degenerate_responses(sample_size=1)[0]
            for _ in range(20)
        ]

        # Simulate high miss rate by having every sample miss findings
        for sample in samples:
            sample.missed_findings = [
                {"path": "test.py", "line": 1, "rule": "medium"}
            ]

        miss_rate = _calculate_miss_rate(samples)

        assert miss_rate == 1.0
        assert miss_rate > 0.5
        assert miss_rate < 1.1

    def test_simulated_free_tier_low_miss_rate(self):
        """Test analysis of a simulated low miss rate scenario."""
        samples = [
            _sample_degenerate_responses(sample_size=1)[0]
            for _ in range(20)
        ]

        # Simulate low miss rate by having most samples not miss findings
        for i in range(16):
            samples[i].missed_findings = []
        for i in range(16, 20):
            samples[i].missed_findings = [
                {"path": "test.py", "line": 1, "rule": "medium"}
            ]

        miss_rate = _calculate_miss_rate(samples)

        assert 0.0 <= miss_rate <= 0.25

    def test_sample_consistency(self):
        """Test that samples have consistent threshold data."""
        samples = _sample_degenerate_responses(sample_size=10)

        thresholds = samples[0].degenerate_thresholds

        for sample in samples:
            assert sample.degenerate_thresholds == thresholds

        assert thresholds["max_output_tokens"] == 50
        assert thresholds["min_input_tokens"] == 2000
        assert thresholds["min_diff_chars"] == 8000


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
