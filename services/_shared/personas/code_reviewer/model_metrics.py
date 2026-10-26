"""Per-backend / per-model Elder telemetry (#540).

Three signals, all tagged `backend` + `model` (the same two values the ledger
`reviewer` label `grug-elder/<model>` is built from, never a repo or person):

- `grug.elder.finding_posted` (count): one per producing origin of every
  inline finding Elder posts. The denominator, emitted on every review that
  posts, so a rate never depends on a verdict arriving.
- `grug.elder.finding_verdict` (count, + `verdict:accepted|rejected`): one per
  producing origin when a trusted human verdict lands (reaction).
- `grug.elder.reviewer_precision` / `grug.elder.reviewer_labeled` (gauges):
  accepted / labeled and the labeled total for that model, derived from the
  ingested ledger corpus (`ledger.reviewer_precision`), refreshed whenever a
  verdict touches the model.

A finding with no recorded producer is tagged `unknown`/`unknown`.
Emission never raises: telemetry must not break a review or the poller.
"""

from __future__ import annotations

import logging
from typing import Iterable, Mapping

_UNKNOWN = "unknown"
_LEGACY_LABEL = "grug-elder"

log = logging.getLogger("grug.persona.code_reviewer.model_metrics")


def origin_dims(origins: Iterable[Mapping] | None) -> list[tuple[str, str, str]]:
    """(ledger reviewer label, backend, model) per DISTINCT producer, in order.

    Distinct by (backend, model). Label rule: `grug-elder/<model>`, else `grug-elder/<backend>`, else the
    single legacy `grug-elder` with `unknown` tags when nothing is recorded."""
    seen: dict[tuple[str, str], tuple[str, str, str]] = {}
    for origin in origins or []:
        model = origin.get("model")
        backend = origin.get("backend")
        has_model = isinstance(model, str) and bool(model)
        has_backend = isinstance(backend, str) and bool(backend)
        if has_model:
            label = f"{_LEGACY_LABEL}/{model}"
        elif has_backend:
            label = f"{_LEGACY_LABEL}/{backend}"
        else:
            continue
        b = backend if has_backend else _UNKNOWN
        m = model if has_model else _UNKNOWN
        # Keyed by (backend, model): the same model name served by two
        # backends stays two producers for telemetry, while both share one
        # ledger label (the ledger key is the label, so those rows merge).
        seen.setdefault((b, m), (label, b, m))
    return list(seen.values()) or [(_LEGACY_LABEL, _UNKNOWN, _UNKNOWN)]


def emit_finding_posted(origins: Iterable[Mapping] | None) -> None:
    try:
        from observability import emit_count  # type: ignore
        for _label, backend, model in origin_dims(origins):
            emit_count("grug.elder.finding_posted", 1,
                       {"backend": backend, "model": model})
    except Exception as e:  # noqa: BLE001 - telemetry never breaks a review
        log.warning("elder_model_metric_failed", extra={"kind": type(e).__name__})


def emit_verdict(
    origins: Iterable[Mapping] | None, accepted: bool, ledger_rows: list,
) -> None:
    """Count the verdict per producer and publish each producer's precision
    from `ledger_rows` (parsed `ledger.LedgerRow`s)."""
    try:
        from ledger import reviewer_precision  # type: ignore
        from observability import emit_count, emit_gauge  # type: ignore
        scores = reviewer_precision(ledger_rows)
        for label, backend, model in origin_dims(origins):
            tags = {"backend": backend, "model": model}
            emit_count("grug.elder.finding_verdict", 1, {
                **tags, "verdict": "accepted" if accepted else "rejected",
            })
            score = scores.get(label)
            if score is not None and score.total:
                emit_gauge("grug.elder.reviewer_precision", score.precision, tags)
                emit_gauge("grug.elder.reviewer_labeled", float(score.total), tags)
    except Exception as e:  # noqa: BLE001 - telemetry never breaks the poller
        log.warning("elder_model_metric_failed", extra={"kind": type(e).__name__})
