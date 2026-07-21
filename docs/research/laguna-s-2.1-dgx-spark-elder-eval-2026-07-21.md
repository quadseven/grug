# Laguna S 2.1 on DGX Spark: Elder evaluation

Date: 2026-07-21

Status: qualified for staged promotion as the shared general brain; Qwen
remains the review coder/security/test/upstream specialist

## Result

Laguna S 2.1 NVFP4 ran Elder's complete 22-PR monolithic review evaluation on
one 128 GB NVIDIA DGX Spark. All 22 requests returned HTTP 200 on their first
attempt. There were no transport timeouts, retries, parse failures, length
stops, request aborts, or emitted false-positive classes.

Compared with the existing Sparkles baseline, Laguna improved overall catch
from 13/71 (0.1831) to 15/71 (0.2113) while retaining zero measured noise.
The improvement was not uniform:

| Finding class | Existing baseline | Laguna S 2.1 | Catches |
| --- | ---: | ---: | ---: |
| correctness | 0.125 | 0.125 | 2/16 |
| security-scope | 0.364 | 0.273 | 3/11 |
| silent-failure | 0.067 | 0.533 | 8/15 |
| simplification | 0.000 | 0.000 | 0/7 |
| test-gap | 0.250 | 0.000 | 0/12 |
| type-design | 0.000 | 0.000 | 0/3 |
| upstream-semantics | 0.429 | 0.286 | 2/7 |

This result supports promoting Laguna as the general reasoner and
silent-failure/robustness specialist. It does not support replacing Qwen
wholesale: the existing baseline remained stronger on security scope, test
gaps, and upstream semantics, so Grug retains its Qwen coder arm.

## Reproducibility identity

- Grug commit: `7ba385a86bd35efef49ec7a5bd7496f1351c6a30`
- Ledger SHA-256: `c141a695f3e34fa9bb53dc935636b11b840884a5550e14679763649cf28aa331`
- Prompt SHA-256: `05d158700d8629eddb40ba5b299ba0c7522efeb42ef60e16e98f759c3699653b`
- Model: `poolside/Laguna-S-2.1-NVFP4`
- Model revision: `216d1f13878dd4e715bc7412848d0f330e95bba6`
- DFlash revision: `723794750422b3efbf3a7b3af76dffb4ba035943`
- Host: `srv-sparkles`, NVIDIA DGX Spark GB10, 128 GB unified memory
- vLLM: `0.25.1`
- FlashInfer: `0.6.15.dev20260712`
- PyTorch: `2.11.0+cu130`
- Quantization backend selected by vLLM: `FLASHINFER_CUTLASS` NVFP4
- Context configuration: 262,144 tokens
- KV cache: FP8, 32.45 GiB, 927,013 aggregate tokens
- Structured output: Elder's exact required findings JSON schema
- Thinking: model default (enabled)
- Sampling: temperature 0.7, top-p 0.95, model top-k 20

The checkpoint and draft model were loaded from their immutable local snapshot
paths. The transient server used Poolside's published DGX Spark recipe:

```text
vllm serve <model-snapshot> \
  --served-model-name poolside/Laguna-S-2.1-NVFP4 \
  --speculative-config=<matching-DFlash-snapshot,15-tokens> \
  --enable-auto-tool-choice \
  --tool-call-parser poolside_v1 \
  --reasoning-parser poolside_v1 \
  --override-generation-config=<temperature-0.7,top-p-0.95> \
  --max-num-seqs 32 \
  --max-model-len 262144 \
  --gpu-memory-utilization 0.85
```

`CUTE_DSL_ARCH=sm_121a` and `MAX_JOBS=4` were set for the GB10 cold JIT.

## Runtime evidence

- Model weight size reported by vLLM: 66.98 GiB
- Model plus DFlash memory: 69.34 GiB
- Cold weight load: 471.07 seconds
- Compile, profile, KV allocation, and warmup: 109.26 seconds
- Full 22-case Elder run wall time: 744.02 seconds
- Full run result: 22/22 HTTP 200, all first attempt
- Independent hardest-case run, PR 366: 45.00 seconds, valid correctness
  finding, no parse error
- PR 366 input diff: 237,138 characters, hunk-bounded to 200,000
- PR 494 input diff: 266,627 characters, hunk-bounded to 200,000
- Server lifetime requests: 25 stop completions, zero length/error/abort stops
- Aggregate prompt tokens: 642,856
- Aggregate completion tokens: 9,606
- Aggregate request latency: 789.852 seconds
- Aggregate time to first token: 226.259 seconds
- DFlash: 5,185 accepted of 50,541 drafted tokens

The 25 server requests were two structured-output smoke tests, one independent
hard-case replay, and the 22-case run. The thinking-off strict-schema smoke
returned valid JSON in 5.41 seconds. The thinking-on strict-schema smoke
returned separated reasoning plus valid JSON in 10.20 seconds.

## Known limitations and warnings

- vLLM logged 11 xgrammar `Failed to advance FSM` messages during one request.
  The request still returned HTTP 200 and Elder parsed it, but this needs a
  focused structured-decoding regression test before production promotion.
- Transformers warned that Laguna's nested rope parameters were unrecognized,
  and vLLM warned that sliding-attention layers would reuse global rope
  parameters. The deployment followed Poolside's exact pinned recipe, but the
  warnings should be reconciled with Poolside/vLLM before treating long-context
  output quality as proven.
- The tokenizer emitted the upstream `fix_mistral_regex` warning. The smoke and
  Elder runs succeeded, but tokenizer equivalence to Poolside's evaluations was
  not independently established.
- The evaluation fetches live GitHub diffs and does not persist their hashes.
  The ledger and prompt are pinned above; the fetched PR representations are
  not. This prevents byte-for-byte replay of this exact run.
- The existing baseline does not record its model revision, quantization,
  runtime, or server configuration. It is the committed historical Sparkles
  score and is treated as the Qwen comparison used by this repository, not as
  a fully reproducible external benchmark.
- This is the monolithic Elder evaluation. It does not prove that substituting
  Laguna into the current staged production pipeline improves published review
  quality.

## Operational cleanup

The test used a transient systemd unit on direct port 8000. It did not change
the Spark gateway or Grug manifests. Ollama was stopped only after the resident
Qwen model became idle and unloaded. After the evaluation, the Laguna unit was
stopped, port 8000 was closed, Ollama was restored active with no model loaded,
GPU utilization returned to zero, and available unified memory returned to
116 GiB. Model and compilation caches remain on disk for a faster repeat run.

## Next qualification gate

Run Laguna only for the silent-failure/robustness semantic cohort, preserve
Qwen for security, test, and upstream-semantics cohorts, and use the existing
Qwen judge to arbitrate the union. Compare published findings, not just raw
discovery, before changing a production model default.
