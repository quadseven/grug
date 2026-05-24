// spec 0004 — TokenCache Cedar policy
// Default-deny. Principals:
//   - TokenAttester    : CI/test attester for TTL + retry + keying contracts
//   - WebhookLambda    : services/webhook/ — refreshes InstallTokens for
//                        Checks API posts via with_install_token_retry
//   - ApiLambda        : services/api/ — refreshes InstallTokens for
//                        GitHub-as-user calls (/installations, /repos)
//
// IOA invariants `AtMostOneInFlightRefresh` + `MustRefreshBeforeExpire`
// are authoritative; Cedar `forbid` is defense-in-depth.

permit (
    principal in [Grug::TokenAttester],
    action in [
        Grug::Action::"AttestTtlContract",
        Grug::Action::"AttestRetryContract",
        Grug::Action::"AttestKeyingContract"
    ],
    resource is Grug::TokenCache
);

permit (
    principal in [Grug::WebhookLambda, Grug::ApiLambda],
    action in [
        Grug::Action::"Refresh",
        Grug::Action::"Settle",
        Grug::Action::"Expire",
        Grug::Action::"Invalidate"
    ],
    resource is Grug::TokenCache
);
