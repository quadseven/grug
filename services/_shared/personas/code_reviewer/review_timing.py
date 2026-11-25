"""Elapsed wall-clock seconds for one Elder review invocation."""

from __future__ import annotations

import logging
import os
import time
from typing import Literal

from github_app_auth import with_install_token_retry
from github_checks_client import CheckRunResult, patch_check_run

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.code_reviewer.timing")

_Stage = Literal["context", "model", "analyzers", "post"]


class ReviewTiming:
    def __init__(self, started: float) -> None:
        self.started = self._stage_started = started
        self.stage: _Stage = "context"
        self._seconds: dict[_Stage, float] = dict.fromkeys(
            ("context", "model", "analyzers", "post"), 0.0,
        )
        self._checks: dict[int, CheckRunResult] = {}

    def advance(self, stage: _Stage, *, completed: bool = True) -> None:
        now = time.monotonic()
        # Failed work contributes to total wall time but leaves the stage at zero.
        if completed:
            self._seconds[self.stage] += now - self._stage_started
        self.stage, self._stage_started = stage, now

    def record_check(self, result: CheckRunResult, response: dict | None) -> None:
        check_id = (response or {}).get("id")
        if isinstance(check_id, int):
            self._checks[check_id] = result

    def publish(
        self, installation_id: int, owner: str, repo: str, pr_number: int, head_sha: str,
    ) -> None:
        if not self._checks:
            return
        now = time.monotonic()
        self._seconds[self.stage] += now - self._stage_started
        seconds = {
            "total": round(now - self.started),
            **{stage: round(elapsed) for stage, elapsed in self._seconds.items()},
        }
        line = "elder-timing: " + " ".join(f"{key}={value}" for key, value in seconds.items())
        identity = {"repo": f"{owner}/{repo}", "pr_number": pr_number, "head_sha": head_sha}
        log.info("elder_timing", extra={**identity, **seconds})
        # The duration includes the original check and review writes. Update
        # only their summary output by ID, after the verdict is already visible.
        for check_id, result in self._checks.items():
            try:
                with_install_token_retry(
                    installation_id,
                    lambda token, check_id=check_id, result=result: patch_check_run(
                        token, owner, repo, check_id, result, output_only=True, summary_footer=line,
                    ),
                )
            except Exception as error:  # noqa: BLE001 - timing cannot change the verdict
                log.warning("elder_timing_publish_failed", extra={**identity, "kind": type(error).__name__})
