"""Run a controlled, same-model Elder replay on the pinned historical corpus."""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import asdict
from io import BytesIO
from pathlib import Path

from bench.replay_arm import (
    MAX_REQUEST_TIMEOUT_SECONDS,
    PROVIDER_FALLBACK_REJECTED,
    REPLAY_MAX_COHORTS,
    REQUEST_TIMEOUT_ENV,
    InvalidReplay,
    configured_request_timeout,
)
from bench.replay_inputs import CORPUS_SHA256, load_snapshots

SCORER_REVISION = "621b7655c4adb737a1a93404cf6687b98ff34fe4"
BASELINE_REVISION = "a0e314393b1f99a3bd22d47eef8b3445103f3286"
ARM = Path(__file__).with_name("replay_arm.py")
BACKEND = "cave"
_REPORTABLE_FAILURE_REASONS = frozenset({
    "backend_request_failed", "backend_status", "backend_timeout", "incomplete_model_output",
    "incomplete_publication_verdicts", "incomplete_review", "incomplete_review_findings",
    "invalid_model_envelope", "missing_model_response", "review_cancelled",
    PROVIDER_FALLBACK_REJECTED,
    "served_model_mismatch", "unexpected_model_route", "unexpected_network_request",
})


class ReplayError(RuntimeError):
    """The pair cannot support a comparison."""


def _command(args, *, cwd, label, timeout, **kwargs):
    try:
        return subprocess.run(
            args, cwd=cwd, timeout=timeout, check=True, text=True,
            capture_output=True, **kwargs,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        # Child diagnostics can carry prompts, provider URLs or response bodies.
        raise ReplayError(f"{label} failed ({type(exc).__name__})") from None


def _checkout_revision(root: Path, *paths: str) -> str:
    revision = _command(["git", "rev-parse", "HEAD"], cwd=root, timeout=15,
                        label="checkout revision").strip()
    changes = _command(["git", "status", "--porcelain", "--", *paths], cwd=root, timeout=15,
                       label="checkout status")
    if changes:
        raise ReplayError(f"checkout {revision[:12]} has modified replay inputs")
    return revision


def _export_checkout(root: Path, revision: str, output: Path, *paths: str) -> None:
    """Run the pair from committed bytes, outside the mutable checkouts."""
    env = {**os.environ, "GIT_ALLOW_PROTOCOL": "", "GIT_NO_LAZY_FETCH": "1"}
    try:
        archive = subprocess.run(
            ["git", "--no-replace-objects", "archive", "--format=tar", revision, "--", *paths],
            cwd=root, env=env, capture_output=True, check=True, timeout=60,
        ).stdout
        output.mkdir(mode=0o700)
        with tarfile.open(fileobj=BytesIO(archive), mode="r:") as source:
            source.extractall(output, filter="data")
    except (OSError, tarfile.TarError, subprocess.SubprocessError) as exc:
        raise ReplayError(f"could not freeze checkout {revision[:12]} ({type(exc).__name__})") from None


def _number(value: object) -> bool:
    return type(value) is int or type(value) is float and math.isfinite(value)


def _arm_result(payload, *, backend: str, model: str, label: str) -> dict:
    if not isinstance(payload, dict) or payload.get("status") != "scored":
        reason = payload.get("reason") if isinstance(payload, dict) else None
        suffix = f" ({reason})" if isinstance(reason, str) and reason in _REPORTABLE_FAILURE_REASONS else ""
        raise ReplayError(f"{label} did not produce a complete verdict{suffix}")
    if payload.get("model") != model or payload.get("backend") != backend:
        raise ReplayError(f"{label} did not report the requested backend and served model")
    seconds = payload.get("verdict_seconds")
    if not _number(seconds) or seconds <= 0:
        raise ReplayError(f"{label} did not report a usable verdict time")
    findings = payload.get("findings")
    if not isinstance(findings, list) or any(not _inline_finding(row) for row in findings):
        raise ReplayError(f"{label} returned malformed inline findings")
    return {"status": "scored", "verdict_seconds": seconds, "findings": findings}


def _inline_finding(row) -> bool:
    return (isinstance(row, dict) and type(row.get("line")) is int and row["line"] > 0
            and all(isinstance(row.get(key), str) for key in ("path", "severity", "text", "rule"))
            and bool(row["path"]) and row["severity"] in {"low", "medium", "high", "critical"})


def _run_arm(checkout, snapshot, backend, model, request_timeout_seconds):
    # Only the explicitly configured local benchmark endpoint is inherited.
    env = {name: os.environ[name] for name in ("PATH", "HOME", "TMPDIR", "LANG",
                                               "GRUG_BENCH_CAVE_URL", "GRUG_BENCH_CAVE_MODEL",
                                               "GRUG_BENCH_CAVE_KEY")
           if name in os.environ}
    env[REQUEST_TIMEOUT_ENV] = str(request_timeout_seconds)
    env["PYTHONPATH"] = os.pathsep.join(str(checkout / name) for name in
                                     ("services/webhook", "services/_shared"))
    env["DD_TRACE_ENABLED"] = "false"
    env["DD_LLMOBS_ENABLED"] = "false"
    env["DD_INSTRUMENTATION_TELEMETRY_ENABLED"] = "false"
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    env["AWS_CONFIG_FILE"] = os.devnull
    env["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
    data = {**asdict(snapshot), "root": str(snapshot.root), "backend": backend, "model": model}
    raw = _command([sys.executable, "-B", str(ARM)], cwd=checkout, timeout=1200,
                   label=snapshot.case_id, env=env, input=json.dumps(data))
    try:
        return json.loads(raw)
    except ValueError:
        raise ReplayError(f"{snapshot.case_id} returned unreadable replay output") from None


def _save(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _scored_metrics(report, expected_ids, revision):
    try:
        rows = report["cases"]
        summary = report["summary"]["public"]["all"]
        status = {row["id"]: row["status"] for row in rows}
        metrics = {key: summary[key] for key in (
            "defects", "caught", "catch_rate", "false_positives_per_clean_pr", "median_time_to_verdict_s",
        )}
    except (KeyError, TypeError):
        raise ReplayError("the scorer report has an unexpected shape") from None
    if (report.get("mode") != "replay" or report.get("grug_sha") != revision
            or report.get("corpus_sha256") != CORPUS_SHA256
            or len(rows) != len(expected_ids) or set(status) != expected_ids
            or any(value != "scored" for value in status.values())
            or summary.get("not_scored") != {}):
        raise ReplayError("the scorer did not attest every case at the pinned corpus and revision")
    if any(not _number(value) or value < 0 for value in metrics.values()):
        raise ReplayError("the scorer did not report complete benchmark metrics")
    return metrics


def _score(suite, findings_path, revision, expected_ids):
    output = findings_path.with_name(findings_path.stem + "-scores.json")
    _command([sys.executable, "-B", "prr_runner.py", "--findings", str(findings_path),
              "--grug-sha", revision, "--out", str(output)], cwd=suite, timeout=60,
             label="pinned benchmark scorer")
    return _scored_metrics(json.loads(output.read_text(encoding="utf-8")), expected_ids, revision)


def measure(snapshots, *, suite, output, checkouts, revisions, model, pace,
            run_arm=_run_arm, score=_score) -> dict:
    expected_ids = {case.case_id for case in snapshots}
    if not snapshots or len(expected_ids) != len(snapshots):
        raise ReplayError("the replay needs a nonempty set of unique cases")
    try:
        request_timeout_seconds = configured_request_timeout()
    except InvalidReplay:
        raise ReplayError(
            f"{REQUEST_TIMEOUT_ENV} must be greater than 0 and at most {MAX_REQUEST_TIMEOUT_SECONDS} seconds"
        ) from None
    output.mkdir(mode=0o700)
    results: dict[str, dict] = {arm: {} for arm in checkouts}
    for index, snapshot in enumerate(snapshots):
        order = ("baseline", "candidate") if index % 2 == 0 else ("candidate", "baseline")
        for arm in order:
            label = f"{snapshot.case_id} ({arm})"
            pace(label)
            raw = run_arm(checkouts[arm], snapshot, BACKEND, model, request_timeout_seconds)
            results[arm][snapshot.case_id] = _arm_result(raw, backend=BACKEND, model=model, label=label)
    pace.check()
    scores = {}
    for arm, cases in results.items():
        findings_path = output / f"{arm}.json"
        _save(findings_path, {"cases": cases})
        scores[arm] = score(suite, findings_path, revisions[arm], expected_ids)
    comparison = {
        "method": "tier1-published-inline-fixed-model",
        "max_review_cohorts": REPLAY_MAX_COHORTS,
        "request_timeout_seconds": request_timeout_seconds,
        "corpus_sha256": CORPUS_SHA256,
        "diff_sha256": {case.case_id: case.diff_sha256 for case in snapshots},
        "backend": BACKEND, "served_model": model, "revisions": revisions, "scores": scores,
        "delta": {key: value - scores["baseline"][key] for key, value in scores["candidate"].items()},
    }
    _save(output / "comparison.json", comparison)
    return comparison


def _measure_frozen(snapshots, *, suite, output, roots, model, pace):
    revisions = {
        arm: _checkout_revision(root, "services/webhook", "services/_shared")
        for arm, root in roots.items()
    }
    if revisions["baseline"] != BASELINE_REVISION or revisions["candidate"] == BASELINE_REVISION:
        raise ReplayError("baseline must be the pinned pre-epic revision and candidate must differ")
    with tempfile.TemporaryDirectory(prefix="elder-pair-", dir=output.parent) as directory:
        work = Path(directory)
        checkouts = {arm: work / arm for arm in roots}
        for arm, root in roots.items():
            _export_checkout(root, revisions[arm], checkouts[arm], "services/webhook", "services/_shared")
        _export_checkout(suite.parent.parent, SCORER_REVISION, work / "scorer", "suites/pr_review")
        measure(snapshots, suite=work / "scorer/suites/pr_review", output=output,
                checkouts=checkouts, revisions=revisions, model=model, pace=pace)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", required=True, type=Path, help="pinned public benchmark suite checkout")
    parser.add_argument("--repositories", required=True, type=Path, help="local clones holding frozen PR commits")
    parser.add_argument("--check-inputs", action="store_true", help="validate the corpus and diffs without any model calls")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--model", help="the concrete model expected in every provider response")
    parser.add_argument("--output", type=Path, help="a new directory for replay and score files")
    args = parser.parse_args(argv)
    suite = args.suite.resolve()
    try:
        if _checkout_revision(suite, ".") != SCORER_REVISION:
            raise ReplayError("the scorer checkout is not at the pinned revision")
        snapshots = load_snapshots(suite, args.repositories.resolve())
        if args.check_inputs:
            print(f"Verified {len(snapshots)} frozen pull request diffs.")
            return 0
        if not all((args.baseline, args.candidate, args.model, args.output)):
            parser.error("replay needs --baseline, --candidate, --model and --output")
        from elder_eval.thermal import pacer_from_env

        pace = pacer_from_env(os.environ)
        if pace is None or not os.environ.get("GRUG_BENCH_CAVE_URL"):
            raise ReplayError("replay needs the configured local benchmark endpoint and thermal guards")
        if args.model == "spark:warm-any" or args.model != os.environ.get("GRUG_BENCH_CAVE_MODEL"):
            raise ReplayError("the replay model must be concrete and match the benchmark configuration")
        roots = {"baseline": args.baseline.resolve(), "candidate": args.candidate.resolve()}
        _measure_frozen(snapshots, suite=suite, output=args.output.resolve(), roots=roots,
                        model=args.model, pace=pace)
    except ReplayError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Replay stopped ({type(exc).__name__}).", file=sys.stderr)
        return 1
    print(f"Both revisions scored all {len(snapshots)} cases.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
