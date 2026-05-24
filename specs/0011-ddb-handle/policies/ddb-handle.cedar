// spec 0011 — DdbHandle Cedar policy
// Default-deny. Module-internal pattern; principals are CI attesters
// and the Lambda runtime itself.

permit (
    principal in [Grug::DdbHandleAttester],
    action in [
        Grug::Action::"AttestLazyInitContract",
        Grug::Action::"AttestDoubleCheckedContract",
        Grug::Action::"AttestCoverageContract"
    ],
    resource is Grug::DdbHandle
);

permit (
    principal in [Grug::ApiLambda, Grug::WebhookLambda],
    action in [
        Grug::Action::"Initialize",
        Grug::Action::"Activate",
        Grug::Action::"Serve"
    ],
    resource is Grug::DdbHandle
);
