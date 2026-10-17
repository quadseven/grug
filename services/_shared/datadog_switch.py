"""The one Datadog on/off switch (#1208).

Datadog is shut down (#1129). Every runtime Datadog path - ddtrace APM,
LLM Observability, DogStatsD gauges/counts, the Datadog HTTP API reads
(Omen, the Warder SLO gate) and the bench LLMObs export - asks
`datadog_enabled()` first and does nothing when it is False. No call to
Datadog or to a node agent, and no log line about either.

    GRUG_DATADOG_ENABLED=true   # 1/true/yes/on; anything else (or unset) = off

Off is the default. The integrations are kept, not deleted: setting the
switch restores the previous behavior exactly.

This module is also the process launcher that replaces a bare
`ddtrace-run` prefix, so ddtrace stays installed and importable but is
only bootstrapped when the switch is on:

    python -m datadog_switch uvicorn main:app ...

On: exec `ddtrace-run <argv>`. Off: exec `<argv>` directly with
DD_TRACE_ENABLED=false and DD_LLMOBS_ENABLED=false forced into the
environment, so a stray `import ddtrace` cannot start a trace writer and
the `DD_LLMOBS_ENABLED=true` the manifests still carry is overridden.
"""

from __future__ import annotations

import os
import sys

ENV_VAR = "GRUG_DATADOG_ENABLED"
_TRUTHY = ("1", "true", "yes", "on")


def datadog_enabled() -> bool:
    """True only when GRUG_DATADOG_ENABLED is explicitly truthy. Read on
    every call (not cached) so tests and a re-exec see the current env."""
    return os.getenv(ENV_VAR, "").strip().lower() in _TRUTHY


def launch_argv(argv: list[str]) -> tuple[list[str], dict[str, str]]:
    """The (argv, env) the launcher execs for `argv`. Pure, for tests."""
    env = dict(os.environ)
    if datadog_enabled():
        return ["ddtrace-run", *argv], env
    env["DD_TRACE_ENABLED"] = "false"
    env["DD_LLMOBS_ENABLED"] = "false"
    return list(argv), env


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        sys.stderr.write("usage: python -m datadog_switch <command> [args...]\n")
        raise SystemExit(2)
    cmd, env = launch_argv(args)
    os.execvpe(cmd[0], cmd, env)


if __name__ == "__main__":  # pragma: no cover - exercised via main()
    main()
