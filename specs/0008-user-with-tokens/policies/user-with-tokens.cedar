// spec 0008 — UserWithTokens Cedar policy
// Default-deny. The service-scope hard wall is load-bearing: WebhookLambda
// must NEVER construct this type. Both IAM (no KMS:Decrypt grant) and
// Cedar enforce this.
//
// Principals:
//   - TokenAttester : CI/test attester for the 3 contract groups
//   - ApiLambda     : services/api/ — only service permitted to call decrypt + construct

permit (
    principal in [Grug::TokenAttester],
    action in [
        Grug::Action::"AttestConstructionContract",
        Grug::Action::"AttestDecryptBoundary",
        Grug::Action::"AttestFieldContract"
    ],
    resource is Grug::UserWithTokens
);

permit (
    principal in [Grug::ApiLambda],
    action in [
        Grug::Action::"Decrypt",
        Grug::Action::"Discard"
    ],
    resource is Grug::UserWithTokens
) when {
    resource.githubUserId == principal.userId
};

// Service-scope hard wall — webhook must NEVER construct/decrypt user tokens.
// Primary rule, not defense-in-depth.
forbid (
    principal is Grug::WebhookLambda,
    action,
    resource is Grug::UserWithTokens
);

// Terminal-state lockout — mutation actions only.
forbid (
    principal,
    action in [
        Grug::Action::"AttestConstructionContract",
        Grug::Action::"AttestDecryptBoundary",
        Grug::Action::"AttestFieldContract",
        Grug::Action::"Decrypt",
        Grug::Action::"Discard"
    ],
    resource is Grug::UserWithTokens
) when {
    resource.status == "Discarded"
};
