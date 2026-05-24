// spec 0001 — CheckRunResult Cedar policy
// Default-deny. Principals:
//   - ChecksClientAttester  : the CI/build agent that asserts the cross-field +
//                             schema + dataclass contracts hold (test suite or
//                             pre-deploy lint).
//   - WebhookLambda         : services/webhook/ — the only path that posts a
//                             CRR to GitHub (Composed → Posted).
//   - ApiLambda             : services/api/ — read-only access for dashboard.
//   - Installation          : the GitHub App install scope; per-install CRRs.
//
// IOA invariants `MustComposeBeforePost` / `RetiredIsTerminal` are the
// authoritative ordering check; Cedar `forbid` is defense-in-depth.

permit (
    principal in [Grug::ChecksClientAttester],
    action in [
        Grug::Action::"AttestCrossFieldInvariant",
        Grug::Action::"AttestSchemaContract",
        Grug::Action::"AttestDataclassContract"
    ],
    resource is Grug::CheckRunResult
);

permit (
    principal in [Grug::WebhookLambda],
    action in [
        Grug::Action::"Compose",
        Grug::Action::"Post",
        Grug::Action::"Retire"
    ],
    resource is Grug::CheckRunResult
);

permit (
    principal is Grug::ApiLambda,
    action == Grug::Action::"Read",
    resource is Grug::CheckRunResult
) when {
    resource.installationId == principal.installationId
};

// Terminal-state lockout — IOA invariant `RetiredIsTerminal` is authoritative.
// Scoped to MUTATION actions only: Read survives so the dashboard can
// surface historical check-run state. Peer-review HIGH (4x).
forbid (
    principal,
    action in [
        Grug::Action::"AttestCrossFieldInvariant",
        Grug::Action::"AttestSchemaContract",
        Grug::Action::"AttestDataclassContract",
        Grug::Action::"Compose",
        Grug::Action::"Post",
        Grug::Action::"Retire"
    ],
    resource is Grug::CheckRunResult
) when {
    resource.status == "Retired"
};
