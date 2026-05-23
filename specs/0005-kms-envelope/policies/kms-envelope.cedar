// spec 0005 — KmsEnvelope Cedar policy
// Default-deny. The service-scope contract is the load-bearing rule
// here: the **webhook Lambda must never decrypt user tokens** (its
// IAM role lacks `kms:Decrypt` on the user-CMK alias). Cedar
// enforces this in app-layer as a second wall.
//
// Principals:
//   - KmsAttester   : CI/test attester for crypto + service-scope + corruption contracts
//   - ApiLambda     : services/api/ — only service permitted to call KMS at app level
//
// IOA invariants `MustEncryptBeforeDecrypt` + `MustObserveCorruptBeforePurge`
// + `PurgedIsTerminal` are authoritative; Cedar `forbid` below is the
// service-scope hard wall (NOT defense-in-depth — primary enforcement).

permit (
    principal in [Grug::KmsAttester],
    action in [
        Grug::Action::"AttestCryptoContract",
        Grug::Action::"AttestServiceScopeContract",
        Grug::Action::"AttestCorruptionHandlerContract"
    ],
    resource is Grug::KmsEnvelope
);

permit (
    principal is Grug::ApiLambda,
    action in [
        Grug::Action::"Encrypt",
        Grug::Action::"Decrypt",
        Grug::Action::"ObserveCorrupt",
        Grug::Action::"PurgeCorrupt"
    ],
    resource is Grug::KmsEnvelope
) when {
    resource.userId == principal.userId
};

// Service-scope hard wall — webhook must NEVER decrypt user tokens.
// This is the primary rule, not defense-in-depth: IAM enforces it at
// the AWS plane; Cedar enforces it at the app plane; the spec bool
// `webhook_never_decrypts_user_tokens_per_persistence_concepts` asserts
// both layers stay in lockstep.
forbid (
    principal is Grug::WebhookLambda,
    action in [
        Grug::Action::"Decrypt",
        Grug::Action::"ObserveCorrupt",
        Grug::Action::"PurgeCorrupt"
    ],
    resource is Grug::KmsEnvelope
);

// Scoped to MUTATION actions only — Read on a Purged envelope returns
// "this row is gone, do not re-decrypt" rather than failing closed.
// Peer-review HIGH (4x).
forbid (
    principal,
    action in [
        Grug::Action::"AttestCryptoContract",
        Grug::Action::"AttestServiceScopeContract",
        Grug::Action::"AttestCorruptionHandlerContract",
        Grug::Action::"Encrypt",
        Grug::Action::"Decrypt",
        Grug::Action::"ObserveCorrupt",
        Grug::Action::"PurgeCorrupt"
    ],
    resource is Grug::KmsEnvelope
) when {
    resource.status == "Purged"
};
