// spec 0002 — TpmEvaluation Cedar policy
// Default-deny. Principals:
//   - ChecksClientAttester : CI/test attester for the 3 contract groups
//   - WebhookLambda        : services/webhook/ — evaluates PRs end-to-end
//                            and composes into a CheckRunResult
//   - ApiLambda            : services/api/ — read-only access for /me + dashboard
//
// IOA invariants `MustEvaluateBeforeCompose` + `RetiredIsTerminal` are
// authoritative; Cedar `forbid` is defense-in-depth.

permit (
    principal in [Grug::ChecksClientAttester],
    action in [
        Grug::Action::"AttestEvaluationContract",
        Grug::Action::"AttestComposeContract",
        Grug::Action::"AttestScopeReviewAdvisoryContract"
    ],
    resource is Grug::TpmEvaluation
);

permit (
    principal in [Grug::WebhookLambda],
    action in [
        Grug::Action::"Evaluate",
        Grug::Action::"Compose",
        Grug::Action::"Retire"
    ],
    resource is Grug::TpmEvaluation
);

permit (
    principal is Grug::ApiLambda,
    action == Grug::Action::"Read",
    resource is Grug::TpmEvaluation
) when {
    resource.installationId == principal.installationId
};

// Scoped to MUTATION actions only — Read survives terminal state so the
// dashboard can surface historical evaluations. Peer-review HIGH (4x).
forbid (
    principal,
    action in [
        Grug::Action::"AttestEvaluationContract",
        Grug::Action::"AttestComposeContract",
        Grug::Action::"AttestScopeReviewAdvisoryContract",
        Grug::Action::"Evaluate",
        Grug::Action::"Compose",
        Grug::Action::"Retire"
    ],
    resource is Grug::TpmEvaluation
) when {
    resource.status == "Retired"
};
