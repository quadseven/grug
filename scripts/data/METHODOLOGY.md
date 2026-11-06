# Degenerate-Empty Rule Threshold Derivation Methodology

## Overview

This document describes the methodology for deriving the degenerate-empty rule thresholds per backend from data, as required for grug#1097 box 2.

## Problem Statement

The degenerate-empty rule (grug#1065, grug#1097) currently uses hard-coded thresholds:
- `max_output_tokens: 50`
- `min_input_tokens: 2_000`
- `min_diff_chars: 8_000`

These thresholds were based on a single data point (nemotron-3-super:free on 2026-09-26) and now fire approximately 100 times per day on DeepSeek alone. The rule may be discarding plausible clean answers from other backends, starving later cohorts.

## Goal

Re-derive the thresholds per backend from production data by:
1. Sampling degenerate cohorts per backend
2. Re-running them on second backends
3. Measuring miss rates (how often real findings were missed)
4. Documenting the per-backend data collection methodology

## Data Collection Methodology

### Step 1: Identify Degenerate Events in Production

#### Source 1: Observability Metrics

The degenerate-empty rule emits a count metric:
```python
emit_count("grug.elder.degenerate_empty_review", 1, tags={"backend": backend.value, "model": model})
```

This metric contains:
- `backend`: The backend name (e.g., "deepseek", "free-tier", "poolside")
- `model`: The model name (e.g., "deepseek-v4.1-flash")

#### Source 2: Review Logs

Review logs contain the full request/response for each degenerate event:
```python
log.warning(
    "llm_review_degenerate_empty",
    extra={
        "backend": backend.value,
        "model": model,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
    },
)
```

### Step 2: Sample Degenerate Cohorts

#### Sampling Strategy

1. **Random Sampling**: Randomly sample N events from each backend's degenerate-empty metric
2. **Stratified Sampling**: Ensure representation from each model that triggers the rule
3. **Size-Based Sampling**: Prioritize larger diffs (closer to threshold boundaries)

#### Sample Size Recommendations

- **Initial Analysis**: 50 samples per backend
- **Production Baseline**: 200+ samples per backend
- **High-Miss-Rate Investigation**: 500+ samples per backend

#### Sample Metadata Collection

For each sampled event, collect:
- `sample_id`: Unique identifier for the sample
- `timestamp`: When the event occurred
- `backend`: Backend name
- `model`: Model name
- `diff_chars`: Character count of the reviewed diff
- `input_tokens`: Prompt token count
- `output_tokens`: Response token count
- `findings`: The findings returned by the backend
- `request_id`: Related request/pipeline identifier (for re-runs)

### Step 3: Re-Run Samples on Second Backends

#### Re-Run Methodology

For each sampled degenerate response:

1. **Extract the Original Diff**: Use `request_id` or pipeline identifier to retrieve the original diff
2. **Re-run on Secondary Backend**: Submit the same diff to the secondary backend
3. **Capture Secondary Findings**: Store the findings from the secondary backend
4. **Compare Results**: Identify findings that only appear in the secondary run

#### Secondary Backend Selection

Choose the secondary backend based on the primary backend:

| Primary Backend | Recommended Secondary Backends |
|-----------------|--------------------------------|
| deepseek-v4.1-flash | free-tier (nemotron), poolside |
| free-tier | deepseek-v4.1-flash, poolside |
| poolside | deepseek-v4.1-flash, free-tier |

#### Handling Authentication

Re-runs require valid authentication for both backends. The script should:
1. Use environment variables or a configuration file for API keys
2. Support environment-specific configurations (dev, staging, prod)
3. Log authentication failures explicitly

### Step 4: Calculate Miss Rates

#### Per-Sample Miss Rate

For each sample, the miss rate is:
```
missed_findings = findings_secondary - findings_primary
miss_rate_sample = len(missed_findings) / expected_findings_in_diff
```

#### Per-Backend Miss Rate

Aggregate miss rates across all samples for a backend:
```
miss_rate_backend = total_missed_findings / total_expected_findings
```

#### Expected Findings in Diff

To determine the expected number of findings in a diff:
1. Have domain experts review a representative sample of diffs
2. Calculate average findings per 1,000 characters of diff
3. Use this average to estimate expected findings for each sample

## Analysis Framework

### Threshold Drift Detection

Compare per-backend miss rates to a baseline:
- **Low Miss Rate (< 20%)**: Thresholds are appropriate for this backend
- **Moderate Miss Rate (20-40%)**: Investigate whether thresholds should be adjusted
- **High Miss Rate (> 40%)**: Thresholds are likely too aggressive; consider adjustments

### Suggested Threshold Adjustments

Based on miss rate analysis, adjust thresholds per backend:

| Miss Rate Range | Adjustments |
|-----------------|-------------|
| < 20% | Keep current thresholds or increase by 10-25% |
| 20-40% | Increase max_output_tokens by 25-50% or decrease min_input_tokens by 20-30% |
| > 40% | Significantly increase max_output_tokens or decrease min_input_tokens |

### Documentation Requirements

For each backend, document:
1. Sample size and demographics
2. Miss rate and interpretation
3. Current thresholds
4. Recommended threshold adjustments
5. Evidence and reasoning

## Implementation

### Script Structure

The re-derivation script (`scripts/rederive_degenerate_thresholds.py`) provides:
1. Sampling functionality
2. Re-run capability
3. Miss rate calculation
4. Report generation
5. JSON output

### Configuration

Configuration is stored in a YAML file (e.g., `scripts/config/degenerate_thresholds.yaml`):

```yaml
backends:
  deepseek:
    models:
      - deepseek-v4.1-flash
    secondary_backends:
      - free-tier
      - poolside
    threshold_targets:
      max_output_tokens: [50, 60, 75, 100]
      min_input_tokens: [2000, 1800, 1500, 1200]

  free-tier:
    models:
      - nemotron-3-super:free
    secondary_backends:
      - deepseek
    threshold_targets:
      max_output_tokens: [50, 60, 75, 100]
      min_input_tokens: [2000, 1800, 1500, 1200]

  poolside:
    models:
      - poolside-laguna-s-2.1
    secondary_backends:
      - deepseek
    threshold_targets:
      max_output_tokens: [50, 60, 75, 100]
      min_input_tokens: [2000, 1800, 1500, 1200]
```

### Data Storage

- **Raw Samples**: `scripts/data/raw_samples/YYYY-MM-DD/<backend>/<sample_id>.json`
- **Analysis Reports**: `scripts/data/degenerate_threshold_analysis.json`
- **Configuration**: `scripts/config/degenerate_thresholds.yaml`

## Production Deployment

### Pre-Deployment Checklist

1. [ ] Sample at least 50 events per backend
2. [ ] Run re-runs on at least one secondary backend per sample
3. [ ] Calculate per-backend miss rates
4. [ ] Document findings in this issue
5. [ ] Identify appropriate threshold adjustments
6. [ ] Test threshold adjustments in staging environment
7. [ ] Get approval from operator before deployment

### Deployment Process

1. **Update Thresholds**: Modify `services/_shared/llm_client.py`:
   ```python
   # Before (hard-coded for all backends)
   _DEGENERATE_MAX_OUTPUT_TOKENS = 50

   # After (backend-specific)
   if backend == Backend.DEEPSEEK:
       _DEGENERATE_MAX_OUTPUT_TOKENS = 75
   elif backend == Backend.FREE_TIER:
       _DEGENERATE_MAX_OUTPUT_TOKENS = 60
   elif backend == Backend.POOLSIDE:
       _DEGENERATE_MAX_OUTPUT_TOKENS = 50
   ```

2. **Update Related Logic**: Ensure all functions that use these thresholds are updated accordingly

3. **Deploy to Staging**: Run the full test suite to ensure no regressions

4. **Monitor**: Watch the degenerate-empty review metric for the first 48 hours after deployment

5. **Deploy to Production**: After successful staging, deploy to production

## Success Criteria

This methodology is successful when:

1. [ ] Degenerate events are being sampled from production
2. [ ] Re-runs on secondary backends capture the miss rate
3. [ ] Miss rates are calculated per backend
4. [ ] The analysis report is documented in this issue
5. [ ] Threshold adjustments are recommended based on the data
6. [ ] Threshold adjustments are tested and deployed

## Future Enhancements

1. **Continuous Monitoring**: Set up a daily job that samples and recalculates miss rates
2. **Automated Threshold Adjustment**: Implement A/B testing for threshold variations
3. **Machine Learning**: Use ML models to predict optimal thresholds based on diff characteristics
4. **Real-time Alerting**: Alert when miss rates exceed thresholds
5. **Automated Threshold Updates**: Propose and apply threshold changes based on miss rate analysis

## References

- Original issue: quadseven/grug#1097
- Degenerate rule: grug#1065
- Related PR: quadseven/grug#1204 (boxes 1 and 3)
- Related PR: quadseven/grug#1643 (diff size threshold)

## Questions and Open Items

- [ ] How to get the original diff from a request_id in production?
- [ ] What is the expected number of findings per diff for accurate miss rate calculation?
- [ ] Should we sample from all backends or prioritize based on usage?
- [ ] What happens if the secondary backend fails or times out during re-run?
- [ ] How to handle API rate limits during re-runs?
