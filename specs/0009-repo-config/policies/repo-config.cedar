// spec 0009 — RepoConfig Cedar policy
// Default-deny. Principals:
//   - RepoConfigAttester : CI/test attester for defaults/storage/extension contracts
//   - ApiLambda          : dashboard PUT /repos/{id}/config — only mutation surface
//   - WebhookLambda      : read-only via is_persona_enabled() pre-dispatch gate

permit (
    principal in [Grug::RepoConfigAttester],
    action in [
        Grug::Action::"AttestDefaultsContract",
        Grug::Action::"AttestStorageContract",
        Grug::Action::"AttestExtensionContract"
    ],
    resource is Grug::RepoConfig
);

permit (
    principal in [Grug::ApiLambda],
    action in [
        Grug::Action::"Override",
        Grug::Action::"Reset",
        Grug::Action::"Retire"
    ],
    resource is Grug::RepoConfig
);

// Webhook reads but never writes.
permit (
    principal in [Grug::WebhookLambda],
    action == Grug::Action::"Read",
    resource is Grug::RepoConfig
);

// Terminal-state lockout — mutation only.
forbid (
    principal,
    action in [
        Grug::Action::"AttestDefaultsContract",
        Grug::Action::"AttestStorageContract",
        Grug::Action::"AttestExtensionContract",
        Grug::Action::"Override",
        Grug::Action::"Reset",
        Grug::Action::"Retire"
    ],
    resource is Grug::RepoConfig
) when {
    resource.status == "Retired"
};
