// spec 0003 — Installation Cedar policy
// Default-deny. Principals:
//   - InstallAttester : CI/test attester for identity + allowlist + repo-config
//   - WebhookLambda   : services/webhook/ — drives state from installation/*
//                       webhook events (Activate, Suspend, Reactivate, Uninstall)
//   - ApiLambda       : services/api/ — read-only for /installations + dashboard
//
// IOA invariants `MustActivateBeforeSuspend` + `UninstalledIsTerminal` are
// authoritative; Cedar `forbid` is defense-in-depth.

permit (
    principal in [Grug::InstallAttester],
    action in [
        Grug::Action::"AttestIdentityContract",
        Grug::Action::"AttestAllowlistGateContract",
        Grug::Action::"AttestRepoConfigContract"
    ],
    resource is Grug::Installation
);

permit (
    principal in [Grug::WebhookLambda],
    action in [
        Grug::Action::"Activate",
        Grug::Action::"Suspend",
        Grug::Action::"Reactivate",
        Grug::Action::"Uninstall"
    ],
    resource is Grug::Installation
);

permit (
    principal is Grug::ApiLambda,
    action == Grug::Action::"Read",
    resource is Grug::Installation
) when {
    resource.installedByUserId == principal.userId
};

// Scoped to MUTATION actions only — Read survives so the dashboard can
// show uninstalled installs in audit history. Peer-review HIGH (4x).
forbid (
    principal,
    action in [
        Grug::Action::"AttestIdentityContract",
        Grug::Action::"AttestAllowlistGateContract",
        Grug::Action::"AttestRepoConfigContract",
        Grug::Action::"Activate",
        Grug::Action::"Suspend",
        Grug::Action::"Reactivate",
        Grug::Action::"Uninstall"
    ],
    resource is Grug::Installation
) when {
    resource.status == "Uninstalled"
};
