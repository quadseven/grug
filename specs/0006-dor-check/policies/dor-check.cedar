// spec 0006 — DorCheck Cedar policy
// Default-deny. Principals:
//   - DorCheckAttester : CI/test attester (also covers the byte-identical
//                        mirror discipline between services/{api,webhook}/)
//   - WebhookLambda    : services/webhook/ — runs the 5 rules on every PR
//   - ApiLambda        : services/api/ — runs the 5 rules for the
//                        /pull-requests/{n}/dry-run dashboard preview
//
// IOA invariants `MustEvaluateBeforeCompose` + `RetiredIsTerminal` are
// authoritative; Cedar `forbid` is defense-in-depth.

permit (
    principal in [Grug::DorCheckAttester],
    action in [
        Grug::Action::"AttestRuleSetContract",
        Grug::Action::"AttestResultShapeContract",
        Grug::Action::"AttestFunctionShapeContract",
        Grug::Action::"AttestMirrorContract"
    ],
    resource is Grug::DorCheck
);

permit (
    principal in [Grug::WebhookLambda, Grug::ApiLambda],
    action in [
        Grug::Action::"Evaluate",
        Grug::Action::"Compose",
        Grug::Action::"Retire"
    ],
    resource is Grug::DorCheck
);

// Scoped to MUTATION actions only — Read survives terminal state so the
// dashboard / audit logs can read historical rule results.
// Peer-review HIGH (4x).
forbid (
    principal,
    action in [
        Grug::Action::"AttestRuleSetContract",
        Grug::Action::"AttestResultShapeContract",
        Grug::Action::"AttestFunctionShapeContract",
        Grug::Action::"AttestMirrorContract",
        Grug::Action::"Evaluate",
        Grug::Action::"Compose",
        Grug::Action::"Retire"
    ],
    resource is Grug::DorCheck
) when {
    resource.status == "Retired"
};
