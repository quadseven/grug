// spec 0007 — UserIdentity Cedar policy
// Default-deny. Principals:
//   - IdentityAttester : CI/test attester for projection + defaults + promotion contracts
//   - ApiLambda        : services/api/ — OAuth callback writes (Create) + admin-promotion writes (Promote)
//   - WebhookLambda    : NEVER constructs / mutates UserIdentity — webhook has no KMS perms + no OAuth surface

permit (
    principal in [Grug::IdentityAttester],
    action in [
        Grug::Action::"AttestProjectionContract",
        Grug::Action::"AttestDefaultsContract",
        Grug::Action::"AttestPromotionContract"
    ],
    resource is Grug::UserIdentity
);

permit (
    principal in [Grug::ApiLambda],
    action in [
        Grug::Action::"Create",
        Grug::Action::"Promote",
        Grug::Action::"Revoke"
    ],
    resource is Grug::UserIdentity
);

// Read is unrestricted to API consumers reading their own row.
permit (
    principal is Grug::ApiLambda,
    action == Grug::Action::"Read",
    resource is Grug::UserIdentity
) when {
    resource.githubUserId == principal.githubUserId
};

// Webhook NEVER touches UserIdentity — explicit forbid as a service-scope wall.
forbid (
    principal is Grug::WebhookLambda,
    action,
    resource is Grug::UserIdentity
);

// Terminal-state lockout — Read survives so audit can still see historical rows;
// only mutation actions are forbidden on Revoked. Peer-review pattern.
forbid (
    principal,
    action in [
        Grug::Action::"AttestProjectionContract",
        Grug::Action::"AttestDefaultsContract",
        Grug::Action::"AttestPromotionContract",
        Grug::Action::"Create",
        Grug::Action::"Promote",
        Grug::Action::"Revoke"
    ],
    resource is Grug::UserIdentity
) when {
    resource.status == "Revoked"
};
