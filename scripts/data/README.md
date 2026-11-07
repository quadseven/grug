# Degenerate-Empty Rule Threshold Derivation

This directory contains the methodology and tools for re-deriving the degenerate-empty rule thresholds per backend from data, as required for grug#1097 box 2.

## Overview

The degenerate-empty rule currently uses hard-coded thresholds that were based on a single data point. This repository provides a framework for:

1. Sampling degenerate cohorts per backend
2. Re-running them on second backends
3. Measuring miss rates (how often real findings were missed)
4. Documenting the per-backend data collection methodology
5. Preparing for threshold adjustments based on the data

## Files

### Main Script

- **`rederive_degenerate_thresholds.py`**: The main script that implements the threshold derivation methodology. It:
  - Samples degenerate responses from production (currently returns mock data, needs integration with real production logs)
  - Re-runs samples on a secondary backend
  - Calculates miss rates
  - Generates analysis reports
  - Saves results to JSON format

### Tests

- **`tests/test_rederive_degenerate_thresholds.py`**: Comprehensive test suite that validates:
  - Sample generation and metadata
  - Re-run functionality on secondary backends
  - Miss rate calculation
  - Per-backend miss rate calculation
  - Report generation

### Documentation

- **`METHODOLOGY.md`**: Detailed methodology document covering:
  - Problem statement
  - Data collection methodology
  - Sampling strategy
  - Re-run methodology
  - Miss rate calculation
  - Analysis framework
  - Implementation details
  - Success criteria
  - Future enhancements

## Usage

### Run the Analysis

```bash
python3 scripts/rederive_degenerate_thresholds.py \
  --backend deepseek \
  --secondary-backend free-tier \
  --sample-size 50 \
  --output scripts/data/degenerate_threshold_analysis.json
```

### Run Tests

```bash
pytest scripts/tests/test_rederive_degenerate_thresholds.py -v
```

## Current State

### Implementation Status

**Mock Data (TODO)**:
- The current implementation returns mock degenerate samples
- Re-runs use simulated secondary backend responses
- Real production data collection and re-run logic needs to be implemented

**Framework Ready**:
- Data structures and types are defined
- Miss rate calculation logic is implemented
- Report generation is working
- Test suite is passing

### Required Integrations

To make this fully functional, the following integrations are needed:

1. **Production Log Sampling**:
   - Read from the `grug.elder.degenerate_empty_review` metric
   - Extract full request/response details from logs
   - Store diff size, input/output tokens, and findings

2. **Secondary Backend Re-runs**:
   - Implement API calls to re-run sampled diffs on secondary backends
   - Handle authentication and API rate limits
   - Capture findings from secondary runs
   - Compare with original findings

3. **Expected Findings Calculation**:
   - Establish a baseline for expected findings per diff size
   - Use this to calculate more accurate miss rates

## Current Thresholds

The current hard-coded thresholds in `services/_shared/llm_client.py`:

```python
_DEGENERATE_MAX_OUTPUT_TOKENS = 50
_DEGENERATE_MIN_INPUT_TOKENS = 2_000
_DEGENERATE_MIN_DIFF_CHARS = 8_000
```

## Expected Results

After collecting and analyzing production data, the script will produce:

1. **Miss Rates Per Backend**: A percentage showing how often real findings were missed
2. **Threshold Recommendations**: Adjusted thresholds based on miss rate analysis
3. **Detailed Sample Data**: Complete information about each sampled degenerate event
4. **Analysis Report**: JSON file with all findings and recommendations

## Success Criteria

This work is complete when:

- [x] Framework is implemented and tested
- [x] Documentation is complete
- [x] Sample generation works (currently returns mock data)
- [ ] Production data collection is implemented
- [ ] Secondary backend re-run logic is implemented
- [ ] Analysis is run on real production data
- [ ] Results are documented in the issue
- [ ] Threshold adjustments are recommended
- [ ] Threshold adjustments are tested and deployed

## References

- Original issue: quadseven/grug#1097
- Degenerate rule: grug#1065
- Related PR: quadseven/grug#1204 (boxes 1 and 3, already merged)
- Related PR: quadseven/grug#1643 (diff size threshold)

## Questions

- How to get the original diff from a request_id in production?
- What is the expected number of findings per diff for accurate miss rate calculation?
- Should we sample from all backends or prioritize based on usage?
- What happens if the secondary backend fails or times out during re-run?
- How to handle API rate limits during re-runs?
