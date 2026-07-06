# WEBHOOK-ONLY (NOT mirrored): async-persona offload + its async-job routing.
# The api service has no webhook ACK path, so there is no api sibling — like
# dispatcher.py / main.py / consumer.py. Per ADR-0014, only modules BOTH
# services run live in services/_shared/; this machinery's only consumer is
# the webhook, so it stays here.
"""Async offload of persona reviews off the webhook ACK path (#272/#466/#469).

`receive_github_webhook` must ACK GitHub in <10s, but the async personas
(Elder's 1-2 LLM calls, Guard's scan+judge, Smasher's Trial Job round-trip)
run far over GitHub's ~10s delivery timeout. So each review runs OFF the
ACK path: the handler enqueues it and returns immediately.

On the k8s runtime (#354/#368) the offload is an in-process background
thread: the webhook pod already carries every dependency the persona paths
need (GitHub-App auth, LLM keys, Postgres, LLM-Obs, KMS), so a separate
worker would duplicate ~15 env vars + secret grants + a role. The thread
runs with the pod's full lifetime; a pod restart mid-review drops the
in-flight review, the same best-effort contract Lambda's async invoke had
(a dropped review re-triggers on the next push). See `specs/DESIGN.md`
→ "Async Elder offload (#272)".

Routing: an async job is tagged with the `grug_async_job` sentinel so a
router can dispatch raw async events to the persona's `run_*_job`.

GENERALIZATION (#77, ADR-0014): Elder, Guard, and Smasher each used to
carry a near-identical copy of the enqueue+run machinery here (the third
copy fired the rule-of-three; ADR-0012/ADR-0013 both deferred to #77).
One generic `_enqueue_review` + `_run_job` now implements the contract,
parameterized by `_AsyncPersonaSpec`. The per-persona wrappers below are
LOAD-BEARING seams, not sugar: they are live patch targets in the test
suites and the lazy-import targets of the `webhook_dispatch` modules, and
they pin the persona-specific contract values (monitored log-line names,
claim-key shapes, personas) that must never drift:

- Log names stay byte-identical per persona
  (`elder_job_done`, `guard_enqueue_invoke_error`, ...) — DD monitors key
  on them.
- Elder claims the RAW delivery GUID (legacy #272 behavior — the claim
  rows already in the store are keyed that way); Guard/Smasher claim
  NAMESPACED `{delivery_id}:<persona>` because all three personas dispatch
  from the SAME webhook delivery — a raw-GUID claim would let whichever
  ran first mark the delivery consumed and silently skip the others.
- Elder's `claim_review`/result persona is the legacy key
  `code_reviewer`, while its self-recover rerun persona is `elder` —
  both pre-date this refactor and are preserved exactly.
"""

from __future__ import annotations

import importlib
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.async_dispatch")

# Sentinel marking an async job. Routing keys on `event.get("grug_async_job")`
# truthiness; the value names the job kind so job types can fan out from one
# router.
ASYNC_JOB_KEY = "grug_async_job"
ELDER_REVIEW_JOB = "elder_review"
GUARD_REVIEW_JOB = "guard_review"
SMASHER_REVIEW_JOB = "smasher_review"
WALKTHROUGH_REVIEW_JOB = "walkthrough_review"

# Thread names are bounded so `ps`/py-spy views stay aligned:
# prefix + delivery-GUID slice, 19 chars total (elder-/guard- keep 13 GUID
# chars, smasher- keeps 11 — the pre-generalization values, preserved).
_THREAD_NAME_LEN = 19


@dataclass(frozen=True)
class _AsyncPersonaSpec:
    """Contract values for one async persona (keyed by REGISTRY key).

    A new async persona = one REGISTRY entry with dispatch_style="async" +
    one row in _ASYNC_PERSONAS + its thin wrappers; the coverage test in
    test_async_dispatch_registry.py fails until the row exists.
    """

    # ASYNC_JOB_KEY value carried on the job dict. Nothing routes on it
    # in the in-process path (_spawn_local targets the runner directly);
    # it tags jobs for a future/external router and for log forensics.
    job_kind: str
    log_prefix: str  # monitored log-line prefix (elder/guard/smasher)
    # None = claim the RAW delivery GUID (Elder legacy); otherwise the
    # namespace suffix producing `{delivery_id}:<namespace>`.
    claim_namespace: str | None
    review_persona: str  # claim_review + result-dict persona key
    rerun_persona: str  # self-recover (#418) rerun-lane persona
    dispatch_path: str  # "module.path:callable" — imported LAZILY per run
    runner_name: str  # module-global run_*_job name (late-bound for patching)

    def __post_init__(self) -> None:
        # Contract values only - reject illegal states at import time, not
        # inside a daemon thread mid-review.
        if self.claim_namespace is not None and not self.claim_namespace:
            raise ValueError(f"{self.log_prefix}: empty claim_namespace (use None for the raw-GUID claim)")
        if ":" not in self.dispatch_path:
            raise ValueError(f"{self.log_prefix}: dispatch_path must be 'module.path:callable'")
        if not self.runner_name.startswith("run_"):
            raise ValueError(f"{self.log_prefix}: runner_name must be a run_*_job module global")
        if not self.log_prefix or len(self.log_prefix) >= _THREAD_NAME_LEN - 1:
            raise ValueError("log_prefix must be non-empty and leave room for a GUID slice")

    def claim_key(self, delivery_id: str) -> str:
        """The delivery-claim key: RAW GUID for Elder (legacy #272 - the
        claim rows already in the store are keyed that way), namespaced
        `{delivery_id}:<persona>` for everyone else (all async personas
        dispatch from the SAME delivery; a raw claim would let whichever
        ran first consume it and silently skip the others)."""
        if self.claim_namespace is None:
            return delivery_id
        return f"{delivery_id}:{self.claim_namespace}"


_ASYNC_PERSONAS: dict[str, _AsyncPersonaSpec] = {
    "code_reviewer": _AsyncPersonaSpec(
        job_kind=ELDER_REVIEW_JOB,
        log_prefix="elder",
        claim_namespace=None,  # RAW GUID — legacy #272 claim shape
        review_persona="code_reviewer",
        rerun_persona="elder",
        dispatch_path="personas.code_reviewer.dispatch:dispatch_code_review",
        runner_name="run_elder_job",
    ),
    "guard": _AsyncPersonaSpec(
        job_kind=GUARD_REVIEW_JOB,
        log_prefix="guard",
        claim_namespace="guard",
        review_persona="guard",
        rerun_persona="guard",
        dispatch_path="personas.guard.dispatch:dispatch_guard_review",
        runner_name="run_guard_job",
    ),
    "smasher": _AsyncPersonaSpec(
        job_kind=SMASHER_REVIEW_JOB,
        log_prefix="smasher",
        claim_namespace="smasher",
        review_persona="smasher",
        rerun_persona="smasher",
        dispatch_path="personas.smasher.dispatch:dispatch_smasher_review",
        runner_name="run_smasher_job",
    ),
    "walkthrough": _AsyncPersonaSpec(
        job_kind=WALKTHROUGH_REVIEW_JOB,
        log_prefix="walkthrough",
        claim_namespace="walkthrough",
        review_persona="walkthrough",
        rerun_persona="walkthrough",
        dispatch_path="personas.walkthrough.dispatch:dispatch_walkthrough_review",
        runner_name="run_walkthrough_job",
    ),
}


def _slim_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Project the GitHub PR payload to ONLY the fields the persona workers
    (`dispatch_code_review` and siblings) read: `action`, the PR number +
    head sha, the repo owner/name, and the installation id. The worker
    re-fetches the diff from GitHub by those IDs, so forwarding the full
    payload is unnecessary: the slim projection keeps the job minimal and
    bounded regardless of PR size (a long body + two full repo objects +
    sender/org are all dropped). Mirrors the dispatch functions' reads —
    keep in sync if one starts consuming new payload fields.
    """
    pr = payload.get("pull_request") or {}
    repo = payload.get("repository") or {}
    return {
        "action": payload.get("action", ""),
        "pull_request": {
            "number": pr.get("number"),
            "head": {"sha": (pr.get("head") or {}).get("sha")},
        },
        "repository": {
            "owner": {"login": (repo.get("owner") or {}).get("login")},
            "name": repo.get("name"),
        },
        "installation": {"id": (payload.get("installation") or {}).get("id")},
    }


def _enqueue_review(
    spec: _AsyncPersonaSpec,
    *,
    payload: dict[str, Any],
    delivery_id: str,
    blocking: bool,
) -> bool:
    """Fire-and-forget offload to run one persona's review async.

    Returns ``True`` if the async job was accepted, ``False`` on any
    failure (logged). Best-effort by design: the caller logs the failure
    and returns ``result="enqueue_failed"`` — it does NOT fall back to a
    synchronous run, because that would re-block the ACK path and break
    the <10s guarantee. A dropped review re-triggers on the next push.

    The k8s runtime (``GRUG_K8S_RUNTIME`` set in the pod manifests, #368)
    runs the job in-process on a background thread: the ACK handler
    returns immediately while the thread runs with the pod's full
    lifetime. This is the only async path post-Lambda (#354); local /
    test (the flag unset) has no offload and returns False.

    k8s trade-off (vs a queue + worker Deployment, recorded in
    specs/DESIGN.md): a pod restart mid-review drops the in-flight
    review — a best-effort contract. In exchange we avoid a new queue +
    consumer + IAM surface for the hot path.
    """
    job = {
        ASYNC_JOB_KEY: spec.job_kind,
        "delivery_id": delivery_id,
        "blocking": blocking,
        # Slim projection — NOT the full payload. See _slim_payload.
        "payload": _slim_payload(payload),
    }
    if os.getenv("GRUG_K8S_RUNTIME"):
        return _spawn_local(spec, job)
    log.warning(
        f"{spec.log_prefix}_enqueue_no_runtime", extra={"delivery_id": delivery_id}
    )
    return False


def _spawn_local(spec: _AsyncPersonaSpec, job: dict[str, Any]) -> bool:
    """Run the persona job on a daemon thread (k8s runtime, #368).

    daemon=True is deliberate: on pod shutdown (deploy rollout, node
    drain) an in-flight review dies WITHOUT blocking termination —
    matching the documented best-effort contract. The runner owns
    idempotency + never-raise, so the thread body needs no wrapper.

    The runner is resolved through the MODULE GLOBAL (`globals()[...]`)
    at spawn time, not captured in the spec table, so
    `patch.object(async_dispatch, "run_elder_job", ...)` keeps working.
    """
    delivery_id = str(job.get("delivery_id", ""))
    guid_chars = _THREAD_NAME_LEN - len(spec.log_prefix) - 1
    try:
        threading.Thread(
            target=globals()[spec.runner_name],
            args=(job,),
            name=f"{spec.log_prefix}-{delivery_id[:guid_chars]}",
            daemon=True,
        ).start()
        return True
    except Exception as e:  # noqa: BLE001 — best-effort enqueue; never break the ACK
        # Same monitor contract as always: one error line per dropped
        # review, with the cause kind.
        log.error(
            f"{spec.log_prefix}_enqueue_invoke_error",
            extra={"delivery_id": delivery_id, "kind": type(e).__name__},
        )
        return False


def _resolve_dispatch(dispatch_path: str):
    """Import `module.path:callable` LAZILY (#272): keeps the ACK path
    cold-start cheap (the persona dep graph only loads in the async job),
    AND — load-bearing — an import failure inside the runner's try must
    degrade, never escape. getattr on the imported module also respects
    test patches of the dispatch function."""
    module_path, func_name = dispatch_path.split(":", 1)
    module = importlib.import_module(module_path)
    return getattr(module, func_name)


def _run_job(spec: _AsyncPersonaSpec, event: dict[str, Any]) -> dict[str, str]:
    """Worker entry for one async persona review job.

    Two-layer idempotency so the review is never double-posted:
    the delivery claim (`install_store.claim_delivery`, raw or namespaced
    per spec — see the module docstring) skips a GitHub redelivery /
    retry of the SAME delivery, and the EXACT head SHA
    (`install_store.claim_review`, #397) skips a same-SHA re-trigger
    across DIFFERENT deliveries — a non-push event (`edited` on the PR
    body, `ready_for_review`) that carries an already-reviewed head SHA.
    Every NEW head SHA still wins a fresh review.

    NEVER re-raises: we own idempotency + the advisory-degrade contract
    inside the dispatch functions, so all failures are logged and
    returned as a status dict instead (no retry storms). On an unhandled
    dispatch error the head-SHA claim is already consumed, so #418
    self-recovery enqueues ONE durable re-run — without it, that SHA's
    check would be suppressed until a new push (codex PR #482).

    Claims are best-effort: a store hiccup on either claim degrades to
    RUNNING the review (fail OPEN — a possible duplicate beats a
    silently-skipped review).
    """
    delivery_id = str(event.get("delivery_id", ""))
    try:
        # Lazy import INSIDE the guard (#272): a failed import degrades to
        # running, same as a store hiccup on the claim.
        from adapters.install_store import claim_delivery

        if not claim_delivery(spec.claim_key(delivery_id)):
            log.info(
                f"{spec.log_prefix}_job_duplicate_skipped",
                extra={"delivery_id": delivery_id},
            )
            return {"status": "skipped", "reason": "duplicate_delivery"}
    except Exception as e:  # noqa: BLE001 — claim is best-effort; degrade to running
        log.warning(
            f"{spec.log_prefix}_job_claim_failed_running_anyway",
            extra={"delivery_id": delivery_id, "kind": type(e).__name__},
        )

    payload = event.get("payload") or {}

    try:
        from adapters.install_store import claim_review

        install_id, repo, pr_number = _pr_ids(payload)
        head_sha = ((payload.get("pull_request") or {}).get("head") or {}).get("sha")
        if install_id and repo and pr_number and head_sha:
            if not claim_review(
                install_id=install_id,
                repo=repo,
                pr_number=pr_number,
                persona=spec.review_persona,
                head_sha=head_sha,
            ):
                log.info(
                    f"{spec.log_prefix}_job_duplicate_sha_skipped",
                    extra={
                        "delivery_id": delivery_id, "repo": repo,
                        "pr": pr_number, "head_sha": head_sha,
                    },
                )
                return {"status": "skipped", "reason": "duplicate_head_sha"}
    except Exception as e:  # noqa: BLE001 — claim is best-effort; degrade to running
        log.warning(
            f"{spec.log_prefix}_job_review_claim_failed_running_anyway",
            extra={"delivery_id": delivery_id, "kind": type(e).__name__},
        )

    blocking = bool(event.get("blocking", False))
    try:
        dispatch = _resolve_dispatch(spec.dispatch_path)
        result = dispatch(payload, blocking=blocking)
        log.info(
            f"{spec.log_prefix}_job_done",
            extra={"delivery_id": delivery_id, **result},
        )
        return result
    except Exception as e:  # noqa: BLE001 — never retry-storm; degrade contract owns this
        log.error(
            f"{spec.log_prefix}_job_unhandled",
            extra={"delivery_id": delivery_id, "kind": type(e).__name__},
            exc_info=True,
        )
        self_recover_review(payload, delivery_id, persona=spec.rerun_persona)
        return {"persona": spec.review_persona, "result": "unhandled_error"}


# --- Per-persona wrappers -------------------------------------------------
# Thin by design; see the module docstring for why they exist and what
# contract values they pin. Signatures are keyword-only, matching the
# pre-generalization functions exactly.


def enqueue_elder_review(
    *, payload: dict[str, Any], delivery_id: str, blocking: bool,
) -> bool:
    """Offload the Elder LLM review (#272). Contract: `_enqueue_review`."""
    return _enqueue_review(
        _ASYNC_PERSONAS["code_reviewer"],
        payload=payload, delivery_id=delivery_id, blocking=blocking,
    )


def run_elder_job(event: dict[str, Any]) -> dict[str, str]:
    """Worker entry for an async Elder review job. Contract: `_run_job`."""
    return _run_job(_ASYNC_PERSONAS["code_reviewer"], event)


def enqueue_guard_review(
    *, payload: dict[str, Any], delivery_id: str, blocking: bool,
) -> bool:
    """Offload the Guard security review (#466). Contract: `_enqueue_review`."""
    return _enqueue_review(
        _ASYNC_PERSONAS["guard"],
        payload=payload, delivery_id=delivery_id, blocking=blocking,
    )


def run_guard_job(event: dict[str, Any]) -> dict[str, str]:
    """Worker entry for an async Guard review (#466). Contract: `_run_job`."""
    return _run_job(_ASYNC_PERSONAS["guard"], event)


def enqueue_smasher_review(
    *, payload: dict[str, Any], delivery_id: str, blocking: bool,
) -> bool:
    """Offload the Smasher Trial (#469). Contract: `_enqueue_review`."""
    return _enqueue_review(
        _ASYNC_PERSONAS["smasher"],
        payload=payload, delivery_id=delivery_id, blocking=blocking,
    )


def run_smasher_job(event: dict[str, Any]) -> dict[str, str]:
    """Worker entry for an async Smasher Trial (#469). Contract: `_run_job`."""
    return _run_job(_ASYNC_PERSONAS["smasher"], event)


def enqueue_walkthrough_review(
    *, payload: dict[str, Any], delivery_id: str, blocking: bool,
) -> bool:
    """Offload the Teller PR walkthrough (#554). Contract: `_enqueue_review`."""
    return _enqueue_review(
        _ASYNC_PERSONAS["walkthrough"],
        payload=payload, delivery_id=delivery_id, blocking=blocking,
    )


def run_walkthrough_job(event: dict[str, Any]) -> dict[str, str]:
    """Worker entry for an async Teller walkthrough (#554). Contract: `_run_job`."""
    return _run_job(_ASYNC_PERSONAS["walkthrough"], event)


def _pr_ids(payload: dict[str, Any]) -> tuple[int | None, str | None, int | None]:
    """Extract (install_id, "owner/name", pr_number) from the slim payload for a
    self-recovery re-run. Any missing -> None (caller skips)."""
    inst = (payload.get("installation") or {}).get("id")
    repo = payload.get("repository") or {}
    owner = (repo.get("owner") or {}).get("login")
    name = repo.get("name")
    pr_number = (payload.get("pull_request") or {}).get("number")
    full = f"{owner}/{name}" if owner and name else None
    return (
        int(inst) if inst is not None else None,
        full,
        int(pr_number) if pr_number is not None else None,
    )


def self_recover_review(
    payload: dict[str, Any], delivery_id: str, *, persona: str,
) -> None:
    """Enqueue ONE durable re-run for a dropped review (#418/#478) - the
    lazy-import seam for the webhook-dispatch modules AND the runner's
    unhandled-error recovery. Bounded: enqueues at most once per drop -
    the rerun CONSUMER retries via SQS redrive, never re-enqueues (it
    calls the dispatch function directly, not the runner), so there is no
    loop. The rerun lane is SQS-backed and consumed by the separate
    grug-consumer deployment, so it survives exactly the class of
    pod-local breakage that made an in-process enqueue fail. Best-effort:
    a failure to enqueue is logged, never raised (the caller is already
    in its degrade path).

    Log-line names keep their legacy elder_ prefix for monitor
    continuity; the persona rides in `extra` so a Guard/Smasher recovery
    is attributable.
    """
    try:
        install_id, repo, pr_number = _pr_ids(payload)
        if not (install_id and repo and pr_number):
            log.warning(
                "elder_self_recover_skipped_no_ids",
                extra={"delivery_id": delivery_id, "persona": persona},
            )
            return
        from rerun import enqueue_rerun

        enqueue_rerun(
            install_id=install_id, repo=repo, pr_number=pr_number, persona=persona
        )
        log.info(
            "elder_self_recover_enqueued",
            extra={
                "delivery_id": delivery_id, "repo": repo,
                "pr": pr_number, "persona": persona,
            },
        )
    except Exception as e:  # noqa: BLE001 — recovery is best-effort, never raises
        # exc_info for symmetry with the *_job_unhandled lines: if recovery is
        # systematically broken (queue misconfig), the stack speeds triage.
        log.error(
            "elder_self_recover_failed",
            extra={
                "delivery_id": delivery_id,
                "kind": type(e).__name__,
                "persona": persona,
            },
            exc_info=True,
        )
