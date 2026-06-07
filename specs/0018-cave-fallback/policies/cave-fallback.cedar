// spec 0018 — CaveFallback Cedar policy
// Default-deny. Principals:
//   - FallbackAttester  : CI/test attester for the enqueue + result contract
//                         groups
//   - WebhookLambda      : services/webhook/ — enqueues a FallbackJob on
//                          all_failed+flag (Enqueue) and consumes results to
//                          heal the verdict (Heal)
//   - CaveConnector      : the grug-cave-connector (#316) — the worker on the
//                          operator's private network that Answers (writes a
//                          FallbackResult after reviewing via the Cave); it
//                          NEVER touches GitHub or the verdict store
//   - ApiLambda          : services/api/ — read-only for the dashboard
//
// IOA invariants `MustEnqueueBeforeAnswer` + `MustAnswerBeforeHeal`
// + `MustHealBeforeRetire` + `RetiredIsTerminal` are authoritative; Cedar
// `forbid` is defense-in-depth.

permit (
    principal in [Grug::FallbackAttester],
    action in [
        Grug::Action::"AttestEnqueueContract",
        Grug::Action::"AttestResultContract"
    ],
    resource is Grug::CaveFallback
);

permit (
    principal in [Grug::WebhookLambda],
    action in [
        Grug::Action::"Enqueue",
        Grug::Action::"Heal",
        Grug::Action::"Retire"
    ],
    resource is Grug::CaveFallback
);

// The connector can ONLY Answer — it cannot enqueue, heal, or retire. This
// mirrors the creds boundary: the connector writes a result and nothing else.
permit (
    principal in [Grug::CaveConnector],
    action == Grug::Action::"Answer",
    resource is Grug::CaveFallback
);

permit (
    principal is Grug::ApiLambda,
    action == Grug::Action::"Read",
    resource is Grug::CaveFallback
) when {
    resource.installationId == principal.installationId
};

// Scoped to MUTATION actions only — Read survives terminal state so the
// dashboard can surface historical fallbacks.
forbid (
    principal,
    action in [
        Grug::Action::"AttestEnqueueContract",
        Grug::Action::"AttestResultContract",
        Grug::Action::"Enqueue",
        Grug::Action::"Answer",
        Grug::Action::"Heal",
        Grug::Action::"Retire"
    ],
    resource is Grug::CaveFallback
) when {
    resource.status == "Retired"
};
