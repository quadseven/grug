# MIRRORED — sibling at services/webhook/personas/registry.py; keep in lockstep. See docs/adr/0001-mirror-with-rule-of-three-deferral.md.
"""Persona registry — the declarative table of grug's personas (#465, epic #464).

This captures, in one place, the per-persona facts that used to live
scattered across `dispatcher._handle_pull_request` (hand-written if-blocks),
`pg_install_store._DEFAULT_PERSONA_CONFIG`, and each persona module's check-run
constant. `dispatcher._handle_pull_request` iterates REGISTRY (ADR-0010): for
each spec it resolves enablement + blocking, builds a `PullRequestContext`,
imports `dispatch_module`, and calls its `dispatch_pull_request(ctx)`.

Adding a persona = one `PersonaSpec` entry here + one
`personas/<key>/webhook_dispatch.py` module + its flag keys in
`pg_install_store._DEFAULT_PERSONA_CONFIG` (+ the frontend toggle). See
ADR-0002 (canonical caveman name is authoritative; the code `key` stays
historical), ADR-0003 (verdict model), and ADR-0010 (registry dispatch).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

# How a persona runs relative to the webhook ACK path.
#   "inline" — runs synchronously in dispatch (fast, no LLM), like Chief/TPM.
#   "async"  — offloaded off the ACK path (LLM latency), like Elder.
DispatchStyle = Literal["inline", "async"]

# What to do when the webhook payload lacks a repo_id (a rare malformed event).
# The two live personas chose OPPOSITE defaults by hand; the registry makes the
# choice explicit per persona instead of folklore in the dispatcher.
#   "enabled"  — treat as on (Chief's choice: a missing id must not skip DoR).
#   "disabled" — treat as off (Elder's choice: never run an LLM review blind).
MissingRepoPolicy = Literal["enabled", "disabled"]


@dataclass(frozen=True, slots=True)
class PersonaSpec:
    """One persona's dispatch-relevant facts. `key` is the historical code key
    (RepoConfig flags, activity-log persona_key); `canonical` is the caveman
    name rendered to users (ADR-0002). `enabled_flag` / `blocking_flag` name the
    RepoConfig booleans; `blocking_flag` is None for personas with no blocking
    mode. `check_run_name` is the GitHub check-run title the persona posts.
    `events` lists the webhook events the persona dispatches on;
    `dispatch_module` is the import path of the module exposing the
    `dispatch_pull_request(ctx: PullRequestContext) -> dict[str, str]`
    entrypoint (resolved lazily at dispatch time, ADR-0010)."""

    key: str
    canonical: str
    check_run_name: str
    enabled_flag: str
    enabled_default: bool
    blocking_flag: str | None
    blocking_default: bool
    dispatch_style: DispatchStyle
    missing_repo_policy: MissingRepoPolicy
    events: tuple[str, ...]
    dispatch_module: str


@dataclass(frozen=True, slots=True)
class PullRequestContext:
    """The uniform payload the dispatch loop hands every persona's
    `dispatch_pull_request` (ADR-0010). Personas read what they need; new
    personas get the full event coordinates without a signature change.
    `payload` is the full webhook payload (Elder's enqueue ships it to the
    async worker); `blocking` is the persona's own blocking flag resolved
    from RepoConfig (always False for personas with no `blocking_flag`)."""

    installation_id: int
    owner: str
    repo_name: str
    head_sha: str
    pr_number: int
    pr_body: str
    payload: dict[str, Any]
    delivery_id: str
    blocking: bool


# The two personas that ship today, declared exactly as the previously
# hand-wired code behaved (locked in by test_registry.py + the dispatcher
# suite). New personas (Guard #466, Warder #471, Pulse #472) append here
# as they land.
REGISTRY: tuple[PersonaSpec, ...] = (
    PersonaSpec(
        key="tpm",
        canonical="chief",
        check_run_name="Grug — Definition of Ready",
        enabled_flag="tpm_enabled",
        enabled_default=True,
        blocking_flag=None,
        blocking_default=False,
        dispatch_style="inline",
        missing_repo_policy="enabled",
        events=("pull_request",),
        dispatch_module="personas.tpm.webhook_dispatch",
    ),
    PersonaSpec(
        key="code_reviewer",
        canonical="elder",
        check_run_name="Grug — Code Review",
        enabled_flag="code_reviewer_enabled",
        enabled_default=True,
        blocking_flag="code_reviewer_blocking",
        blocking_default=False,
        dispatch_style="async",
        missing_repo_policy="disabled",
        events=("pull_request",),
        dispatch_module="personas.code_reviewer.webhook_dispatch",
    ),
)

_BY_KEY = {p.key: p for p in REGISTRY}
_BY_CANONICAL = {p.canonical: p for p in REGISTRY}


def by_key(key: str) -> PersonaSpec | None:
    """Look up a persona by its historical code key (e.g. `code_reviewer`)."""
    return _BY_KEY.get(key)


def by_canonical(name: str) -> PersonaSpec | None:
    """Look up a persona by its canonical caveman name (e.g. `elder`)."""
    return _BY_CANONICAL.get(name)


def default_persona_config() -> dict[str, bool]:
    """Derive the RepoConfig persona-flag defaults from the registry. The
    store's `_DEFAULT_PERSONA_CONFIG` stays a LITERAL dict (temper spec 0009
    attests that shape as the extension point); registry<->dict equality is
    locked by test_registry.py so the two cannot drift (ADR-0010)."""
    cfg: dict[str, bool] = {}
    for p in REGISTRY:
        cfg[p.enabled_flag] = p.enabled_default
        if p.blocking_flag is not None:
            cfg[p.blocking_flag] = p.blocking_default
    return cfg
