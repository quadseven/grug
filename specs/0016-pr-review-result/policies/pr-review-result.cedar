// spec 0016 — PrReviewResult Cedar policy
// Default-deny. Principals:
//   - ChecksClientAttester : CI/test attester for the 4 contract groups
//   - WebhookLambda        : services/webhook/ — composes and POSTs reviews
//   - ApiLambda            : services/api/ — read-only access for /me + dashboard
//
// IOA invariants `MustComposeBeforePost` + `MustPostBeforeRetire` +
// `RetiredIsTerminal` are authoritative; Cedar `forbid` is defense-in-depth.

permit (
    principal in [Grug::ChecksClientAttester],
    action in [
        Grug::Action::"AttestEventContract",
        Grug::Action::"AttestSchemaContract",
        Grug::Action::"AttestDataclassContract",
        Grug::Action::"AttestRetryContract"
    ],
    resource is Grug::PrReviewResult
);

permit (
    principal in [Grug::WebhookLambda],
    action in [
        Grug::Action::"Compose",
        Grug::Action::"Post",
        Grug::Action::"Retire"
    ],
    resource is Grug::PrReviewResult
);

permit (
    principal is Grug::ApiLambda,
    action == Grug::Action::"Read",
    resource is Grug::PrReviewResult
) when {
    resource.installationId == principal.installationId
};

// Scoped to MUTATION actions only — Read survives terminal state so the
// dashboard can surface historical reviews.
forbid (
    principal,
    action in [
        Grug::Action::"AttestEventContract",
        Grug::Action::"AttestSchemaContract",
        Grug::Action::"AttestDataclassContract",
        Grug::Action::"AttestRetryContract",
        Grug::Action::"Compose",
        Grug::Action::"Post",
        Grug::Action::"Retire"
    ],
    resource is Grug::PrReviewResult
) when {
    resource.status == "Retired"
};
