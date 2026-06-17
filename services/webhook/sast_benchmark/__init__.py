"""SAST benchmark / eval harness for Elder (#399, ADR-0006).

Measures Elder's vuln-detection recall + precision against a fixed corpus,
per backend. The acceptance gate for the SAST detection slices (#400/#401)
and a standing regression guard.

Webhook-only tool (NOT mirrored, NOT on the request path). The pure scoring
core runs in CI with no LLM; the live runner records the baseline on demand.
"""
