// spec 0017 — CodeReviewerDispatch Cedar policy
// Default-deny. Principals:
//   - DispatchAttester : CI/test attester for the gate + publish-independence
//                        contract groups
//   - WebhookLambda    : services/webhook/ — runs dispatch_code_review:
//                        resolves mode, gates via _publish_shape, publishes
//                        the check-run + review on independent surfaces
//   - ApiLambda        : services/api/ — read-only access for /me + dashboard
//
// IOA invariants `MustGateBeforePublish` + `MustPublishBeforeRetire`
// + `RetiredIsTerminal` are authoritative; Cedar `forbid` is defense-in-depth.

permit (
    principal in [Grug::DispatchAttester],
    action in [
        Grug::Action::"AttestGateContract",
        Grug::Action::"AttestPublishIndependenceContract"
    ],
    resource is Grug::CodeReviewerDispatch
);

permit (
    principal in [Grug::WebhookLambda],
    action in [
        Grug::Action::"Gate",
        Grug::Action::"Publish",
        Grug::Action::"Retire"
    ],
    resource is Grug::CodeReviewerDispatch
);

permit (
    principal is Grug::ApiLambda,
    action == Grug::Action::"Read",
    resource is Grug::CodeReviewerDispatch
) when {
    resource.installationId == principal.installationId
};

// Scoped to MUTATION actions only — Read survives terminal state so the
// dashboard can surface historical dispatches.
forbid (
    principal,
    action in [
        Grug::Action::"AttestGateContract",
        Grug::Action::"AttestPublishIndependenceContract",
        Grug::Action::"Gate",
        Grug::Action::"Publish",
        Grug::Action::"Retire"
    ],
    resource is Grug::CodeReviewerDispatch
) when {
    resource.status == "Retired"
};
