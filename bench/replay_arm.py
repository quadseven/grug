"""Run one fixed-model Elder replay using local, immutable Git evidence.

The caller supplies a validated case. The child uses the checked-out
revision's production reviewer and publication gates. Only the configured
Cave benchmark endpoint receives requests. Mutable repo preferences and
telemetry are excluded from the paired measurement.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

_SHA = re.compile(r"[0-9a-f]{40}")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*")
_COMPLETIONS_PATH = "/v1/chat/completions"
_FREE_PROVIDER_FALLBACK_ROUTES = ("free-pool", "openrouter", "poolside")
PROVIDER_FALLBACK_REJECTED = "provider_fallback_rejected"
INCOMPLETE_PUBLICATION_VERDICTS = "incomplete_publication_verdicts"
REQUEST_TIMEOUT_ENV = "GRUG_BENCH_TIMEOUT_SECONDS"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 150
MAX_REQUEST_TIMEOUT_SECONDS = 300
# The pinned corpus needs up to 14 cohorts on both revisions. Use the same
# modest cushion in each arm instead of the live ceiling of 10, which crops it.
REPLAY_MAX_COHORTS = 16

# Cave maps generic JSON to a mode that can truncate multi-item replies
# (#544). Use the same required-keys structure as the discovery schema.
_CAVE_VERDICTS_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "judge_verdicts",
        "schema": {
            "type": "object",
            "properties": {
                "verdicts": {
                    "type": "array",
                    "maxItems": 25,  # Both pinned reviewers use this judge batch size.
                    "items": {
                        "type": "object",
                        "properties": {
                            "index": {"type": "integer"},
                            "is_real_bug": {"type": "boolean"},
                            "confidence": {"type": "number"},
                            "reasoning": {"type": "string"},
                        },
                        "required": ["index", "is_real_bug", "confidence", "reasoning"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["verdicts"],
            "additionalProperties": False,
        },
    },
}


class InvalidReplay(RuntimeError):
    """An invalid or incomplete run, with a safe public reason code."""


class IncompletePublicationVerdicts(InvalidReplay):
    """Structured coverage counts without verdict indices or provider data."""

    def __init__(self, phase: str, expected: int, indices: list[int], wire_reason: str | None):
        super().__init__(INCOMPLETE_PUBLICATION_VERDICTS)
        self.coverage: dict[str, str | int] = {
            "phase": phase,
            "expected": expected,
            "returned": len(indices),
            "covered": len({index for index in indices if 0 <= index < expected}),
        }
        if wire_reason is not None:
            self.coverage["wire_reason"] = wire_reason


def configured_request_timeout() -> float:
    try:
        seconds = float(os.environ.get(REQUEST_TIMEOUT_ENV, str(DEFAULT_REQUEST_TIMEOUT_SECONDS)))
    except ValueError:
        raise InvalidReplay("invalid_request_timeout") from None
    if not 0 < seconds <= MAX_REQUEST_TIMEOUT_SECONDS:
        raise InvalidReplay("invalid_request_timeout")
    return seconds


@dataclass(frozen=True)
class FrozenCase:
    case_id: str
    repo: str
    pr: int
    root: str
    base_sha: str
    head_sha: str
    diff: str
    diff_sha256: str
    backend: str
    model: str

    def validate(self) -> None:
        if not all(_SHA.fullmatch(sha) for sha in (self.base_sha, self.head_sha)):
            raise InvalidReplay("invalid_snapshot")
        if not self.diff or hashlib.sha256(self.diff.encode()).hexdigest() != self.diff_sha256:
            raise InvalidReplay("invalid_diff")
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+", self.repo):
            raise InvalidReplay("invalid_repository")
        if type(self.pr) is not int or self.pr <= 0:
            raise InvalidReplay("invalid_pull_request")
        if (
            self.backend != "cave"
            or not _MODEL.fullmatch(self.model)
            or self.model.lower().endswith((":warm-any", ":latest"))
        ):
            raise InvalidReplay("invalid_backend_or_model")

    def _git(self, *args: str, absent: int | None = None) -> bytes:
        cmd = ["git", "--no-optional-locks", "--no-replace-objects", "-C", self.root, *args]
        env = {
            "PATH": os.defpath,
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_ATTR_SOURCE": self.head_sha,
            "GIT_ALLOW_PROTOCOL": "",
            "GIT_NO_LAZY_FETCH": "1",
        }
        result = subprocess.run(cmd, env=env, capture_output=True, timeout=30, check=False)
        if absent is not None and result.returncode == absent:
            return b""
        if result.returncode:
            raise InvalidReplay("snapshot_read_failed")
        return result.stdout

    def blob(self, path: str, *, allow_nonblob: bool = False) -> bytes | None:
        file = PurePosixPath(path)
        if not path or file.is_absolute() or ".." in file.parts:
            raise InvalidReplay("invalid_source_path")
        entry = self._git("ls-tree", "-z", self.head_sha, "--", f":(literal){path}")
        if not entry.endswith(b"\x00") or entry.count(b"\x00") != 1:
            raise InvalidReplay("snapshot_path_missing")
        meta, _, name = entry[:-1].partition(b"\t")
        _, file_type, oid = meta.split(b" ")
        if name.decode() != path:
            raise InvalidReplay("snapshot_path_missing")
        if file_type == b"commit" and allow_nonblob:
            # The patch already carries a gitlink's changed object IDs.
            return None
        if file_type != b"blob":
            raise InvalidReplay("snapshot_path_not_blob")
        return self._git("cat-file", "blob", oid.decode())

    def matches(self, symbol: str, limit: int) -> list[str]:
        rows = self._git(
            "grep", "--no-textconv", "-I", "-lzF", "-e", symbol,
            self.head_sha, "--", "*.py", absent=1,
        )
        prefix = self.head_sha.encode() + b":"
        paths = [row[len(prefix):].decode() for row in rows.split(b"\x00") if row.startswith(prefix)]
        return paths[:limit]

    def github_get(self, url: str, *, params: dict, **_kwargs):
        import httpx

        request = httpx.Request("GET", url)
        address = urlsplit(url)
        if address.netloc != "api.github.com" or address.scheme != "https":
            raise InvalidReplay("unexpected_context_route")
        if address.path == "/search/code":
            query = params["q"]
            suffix = f'" repo:{self.repo}'
            if not query.startswith('"') or not query.endswith(suffix):
                raise InvalidReplay("unexpected_context_query")
            paths = self.matches(query[1:-len(suffix)], int(params["per_page"]))
            return httpx.Response(200, json={"items": [{"path": path} for path in paths]}, request=request)
        prefix = f"/repos/{self.repo}/contents/"
        if not address.path.startswith(prefix) or params.get("ref") != self.head_sha:
            raise InvalidReplay("unexpected_context_route")
        path = unquote(address.path[len(prefix):])
        return httpx.Response(200, content=self.blob(path), request=request)


def _evidence(case: FrozenCase, llm_client):
    from personas.code_reviewer import cross_file, diff_parser
    from review_evidence import assemble_evidence

    parsed = diff_parser.parse_diff(case.diff)
    if not parsed:
        raise InvalidReplay("empty_reviewable_diff")
    budget = llm_client.review_cohort_char_budget()
    if hasattr(diff_parser, "fit_hunks_to_cohorts"):
        hunks, _, oversized = diff_parser.fit_hunks_to_cohorts(parsed, budget, drop_oversized=False)
    else:
        # The older dispatch only identified and dropped oversize hunks.
        hunks, oversized = diff_parser.split_oversized_hunks(parsed, budget)
    if oversized:
        raise InvalidReplay("unreviewable_snapshot_diff")
    paths = dict.fromkeys(h.file_path for h in hunks)
    contents = {}
    for path in paths:
        blob = case.blob(path, allow_nonblob=True)
        if blob is not None:
            contents[path] = blob.decode()
    symbols = cross_file.extract_symbols(hunks, contents)
    owner, repository = case.repo.split("/", 1)
    with patch.object(cross_file.httpx, "get", case.github_get):
        callers = cross_file.fetch_cross_file_context(
            "replay", owner, repository, symbols,
            head_sha=case.head_sha, exclude_paths=frozenset(paths),
        )
    evidence = assemble_evidence(
        [llm_client.Hunk(path=h.file_path, body=h.body) for h in hunks],
        pr_context={"repo": case.repo, "pr_number": case.pr, "head_sha": case.head_sha,
                    "review_phase": "paired-replay"},
        file_contents=contents,
        cross_file_contents=callers,
    )
    return hunks, evidence


class FixedBackend:
    """Fence the process transport and record failed or incomplete calls."""

    def __init__(self, backend, post, *, parse_review=None):
        self.backend = backend
        self.post = post
        self.parse_review = parse_review
        self.allowed = ContextVar("replay_model_request", default=False)
        self.rejected: str | None = None
        self.responses = 0

    def refuse(self, reason: str):
        self.rejected = self.rejected or reason
        raise InvalidReplay(reason)

    def call(self, config, messages, cancel_event=None):
        from httpx import TimeoutException

        if cancel_event is not None and cancel_event.is_set():
            self.refuse("review_cancelled")
        identity = getattr(config.backend, "value", config.backend)
        if (
            identity != "cave"
            or config.url != self.backend.url
            or config.model != self.backend.model
        ):
            if identity in _FREE_PROVIDER_FALLBACK_ROUTES:
                self.refuse(PROVIDER_FALLBACK_REJECTED)
            self.refuse("unexpected_model_route")
        schema_field = (
            "verdicts" if config.extra_headers.get("X-Spark-Caller") == "grug-elder-judge"
            else "findings"
        )
        batch = dict(config.extra_body)
        if schema_field == "verdicts":
            batch["response_format"] = _CAVE_VERDICTS_RESPONSE_FORMAT
        request = replace(self.backend, extra_body={**self.backend.extra_body, **batch})
        token = self.allowed.set(True)
        try:
            response = self.post(request, messages)
        except (TimeoutError, TimeoutException):
            self.refuse("backend_timeout")
        except Exception as exc:
            self.rejected = self.rejected or "backend_request_failed"
            raise InvalidReplay("backend_request_failed") from exc
        finally:
            self.allowed.reset(token)
        self._check(response, schema_field)
        self.responses += 1
        return response

    def _check(self, response, schema_field: str) -> None:
        if response.status_code != 200:
            self.refuse("backend_status")
        try:
            body = response.json()
            if body["model"] != self.backend.model:
                self.refuse("served_model_mismatch")
            choices = body["choices"]
            if len(choices) != 1 or choices[0]["finish_reason"] != "stop":
                self.refuse("incomplete_model_output")
            message = choices[0]["message"]
            if message.get("refusal") or not isinstance(message["content"], str):
                self.refuse("incomplete_model_output")
            content = json.loads(message["content"])
            if not isinstance(content[schema_field], list):
                self.refuse("invalid_model_envelope")
        except (KeyError, ValueError, TypeError, IndexError, AttributeError):
            self.refuse("invalid_model_envelope")
        if schema_field == "findings" and self.parse_review is not None:
            parsed = self.parse_review(response)
            if parsed.error or len(parsed.findings) != len(content["findings"]):
                self.refuse("incomplete_review_findings")

    @contextmanager
    def fenced(self, llm_client):
        import httpx
        from botocore.client import BaseClient

        real_send = httpx.Client.send
        real_single = httpx.Client._send_single_request

        def check_request(request):
            try:
                model = json.loads(request.content).get("model")
            except (AttributeError, ValueError):
                model = None
            if (
                not self.allowed.get()
                or request.method != "POST"
                or request.url != httpx.URL(self.backend.url)
                or model != self.backend.model
            ):
                self.refuse("unexpected_network_request")

        def send(client, request, *args, **kwargs):
            check_request(request)
            return real_send(client, request, *args, **{**kwargs, "follow_redirects": False})

        def send_single(client, request):
            check_request(request)
            return real_single(client, request)

        def no_network(*_args, **_kwargs):
            self.refuse("unexpected_network_request")

        def scoped(operation):
            def guarded(*args, **kwargs):
                if not self.allowed.get():
                    no_network()
                return operation(*args, **kwargs)
            return guarded

        with ExitStack() as stack:
            stack.enter_context(patch.object(llm_client, "_call_backend", self.call))
            stack.enter_context(patch.object(httpx.Client, "send", send))
            stack.enter_context(patch.object(httpx.Client, "_send_single_request", send_single))
            stack.enter_context(patch.object(httpx.AsyncClient, "send", no_network))
            stack.enter_context(patch.object(BaseClient, "_make_api_call", no_network))
            stack.enter_context(patch.object(socket, "getaddrinfo", scoped(socket.getaddrinfo)))
            for method in ("connect", "connect_ex", "send", "sendall", "sendto", "sendmsg"):
                operation = getattr(socket.socket, method, None)
                if operation is not None:
                    stack.enter_context(patch.object(socket.socket, method, scoped(operation)))
            # These signals have no frozen snapshot and must avoid their stores.
            for name in ("_team_practices_block", "_few_shot_block",
                         "_repo_learnings_block", "_repo_guidelines_block"):
                stack.enter_context(patch.object(llm_client, name, lambda _context: ""))
            yield


def _complete_verdicts(verdicts, count: int, *, phase: str, wire_reason: str | None):
    indices = [verdict.finding_index for verdict in verdicts]
    if sorted(indices) != list(range(count)):
        raise IncompletePublicationVerdicts(phase, count, indices, wire_reason)
    return verdicts


def _measure(case: FrozenCase, *, post=None) -> dict:
    import llm_client
    from elder_eval import runner
    from sast_benchmark.backends import configured_backends
    from sast_benchmark.runner import _post

    backend = next((b for b in configured_backends() if b.name == case.backend), None)
    if backend is None or backend.model != case.model:
        raise InvalidReplay("unconfigured_fixed_backend")
    wire = FixedBackend(backend, _post if post is None else post, parse_review=llm_client._parse_review)

    def grade(evaluation, evidence, install_id):
        verdicts = runner.grade_findings(evaluation, evidence, install_id)
        return _complete_verdicts(
            verdicts, len(evaluation.findings), phase="grade", wire_reason=wire.rejected,
        )

    def refute(findings, evidence, install_id):
        verdicts = runner.production_refute(findings, evidence, install_id)
        return _complete_verdicts(
            verdicts, len(findings), phase="refute", wire_reason=wire.rejected,
        )

    hunks, evidence = _evidence(case, llm_client)
    with wire.fenced(llm_client):
        started = time.monotonic()
        response = llm_client.review_diff(evidence, installation_id=0)
        if response.kind != "reviewed" or response.error or (
            response.coverage is not None and not response.coverage.complete
        ):
            raise InvalidReplay(wire.rejected or "incomplete_review")
        findings = runner._published_findings(response, hunks, evidence, grade=grade, refute=refute)
        elapsed = time.monotonic() - started
        if wire.rejected or not wire.responses:
            raise InvalidReplay(wire.rejected or "missing_model_response")

    return {
        "status": "scored", "backend": case.backend, "model": case.model,
        "verdict_seconds": elapsed,
        "findings": [
            {"path": f.file, "line": f.line, "severity": f.severity,
             "text": f.message, "rule": f.rule_name}
            for f in findings if not getattr(f, "outside_diff_reason", None)
        ],
    }


def evaluate(payload: dict, *, post=None) -> dict:
    """Return a scored record or a sanitized, non-scoring status."""
    try:
        case = FrozenCase(**payload)
        case.validate()
        endpoint = os.environ.get("GRUG_BENCH_CAVE_URL", "")
        address = urlsplit(endpoint)
        if (
            address.scheme not in ("http", "https")
            or not address.hostname
            or address.username is not None
            or address.query or address.fragment or address.path != _COMPLETIONS_PATH
        ):
            raise InvalidReplay("unconfigured_fixed_backend")
        locked = {
            "PATH": os.defpath,
            "GRUG_BENCH_CAVE_URL": endpoint,
            "GRUG_BENCH_CAVE_KEY": os.environ.get("GRUG_BENCH_CAVE_KEY", ""),
            "GRUG_BENCH_CAVE_MODEL": case.model,
            REQUEST_TIMEOUT_ENV: str(configured_request_timeout()),
            "GRUG_BENCH_RETRY_ATTEMPTS": "1",
            "GRUG_CAVE_GATEWAY_URL": endpoint.removesuffix(_COMPLETIONS_PATH),
            "GRUG_CAVE_REVIEW_MODEL": case.model,
            "GRUG_CAVE_REASONER_MODEL": case.model,
            "GRUG_CAVE_JUDGE_MODEL": case.model,
            "GRUG_CAVE_TRUSTED_EMPTY_MODELS": case.model,
            "GRUG_REVIEW_BACKEND_PRIORITY": "cave",
            "GRUG_REVIEW_DEPTH": "tiered",
            "GRUG_REVIEW_MAX_COHORTS": str(REPLAY_MAX_COHORTS),
            "DD_TRACE_ENABLED": "false",
            "DD_LLMOBS_ENABLED": "false",
            "DD_INSTRUMENTATION_TELEMETRY_ENABLED": "false",
            "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_DEFAULT_REGION": "us-east-1",
            "AWS_CONFIG_FILE": os.devnull,
            "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        }
        with patch.dict(os.environ, locked, clear=True):
            return _measure(case, post=post)
    except IncompletePublicationVerdicts as exc:
        return {"status": "invalid", "reason": str(exc), "publication": exc.coverage}
    except InvalidReplay as exc:
        return {"status": "invalid", "reason": str(exc)}
    except (OSError, RuntimeError, TypeError, ValueError, AttributeError, subprocess.SubprocessError):
        return {"status": "invalid", "reason": "replay_execution_failed"}


def main() -> int:
    logging.disable(logging.CRITICAL)
    try:
        result = evaluate(json.load(sys.stdin))
    except (TypeError, ValueError):
        result = {"status": "invalid", "reason": "invalid_replay_input"}
    print(json.dumps(result, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
