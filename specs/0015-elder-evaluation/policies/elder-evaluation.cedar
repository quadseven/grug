// spec 0015 — ElderEvaluation Cedar policy
// Default-deny. Principals:
//   - ChecksClientAttester : CI/test attester for the 3 contract groups
//   - WebhookLambda        : services/webhook/ — parses diffs, runs the
//                            LLM, evaluates, composes into a CheckRunResult
//   - ApiLambda            : services/api/ — read-only access for /me + dashboard
//
// IOA invariants `MustParseBeforeEvaluate` + `MustEvaluateBeforeCompose`
// + `RetiredIsTerminal` are authoritative; Cedar `forbid` is defense-in-depth.

permit (
    principal in [Grug::ChecksClientAttester],
    action in [
        Grug::Action::"AttestParseDiffContract",
        Grug::Action::"AttestEvaluateDiffContract",
        Grug::Action::"AttestComposeContract"
    ],
    resource is Grug::ElderEvaluation
);

permit (
    principal in [Grug::WebhookLambda],
    action in [
        Grug::Action::"Parse",
        Grug::Action::"Evaluate",
        Grug::Action::"Compose",
        Grug::Action::"Retire"
    ],
    resource is Grug::ElderEvaluation
);

permit (
    principal is Grug::ApiLambda,
    action == Grug::Action::"Read",
    resource is Grug::ElderEvaluation
) when {
    resource.installationId == principal.installationId
};

// Scoped to MUTATION actions only — Read survives terminal state so the
// dashboard can surface historical evaluations.
forbid (
    principal,
    action in [
        Grug::Action::"AttestParseDiffContract",
        Grug::Action::"AttestEvaluateDiffContract",
        Grug::Action::"AttestComposeContract",
        Grug::Action::"Parse",
        Grug::Action::"Evaluate",
        Grug::Action::"Compose",
        Grug::Action::"Retire"
    ],
    resource is Grug::ElderEvaluation
) when {
    resource.status == "Retired"
};
