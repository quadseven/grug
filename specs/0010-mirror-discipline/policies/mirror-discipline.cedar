// spec 0010 — MirrorDiscipline Cedar policy
// Default-deny. Meta-spec for ADR-0001 enforcement (no live runtime principal).
//   - MirrorAttester : CI/test attester
//   - DriftLintCI    : the drift-lint workflow itself, drives Sync / Drift transitions

permit (
    principal in [Grug::MirrorAttester],
    action in [
        Grug::Action::"AttestHeaderContract",
        Grug::Action::"AttestBodyIdentityContract",
        Grug::Action::"AttestEnforcementContract",
        Grug::Action::"AttestExtractionContract"
    ],
    resource is Grug::MirrorDiscipline
);

permit (
    principal in [Grug::DriftLintCI],
    action in [
        Grug::Action::"Sync",
        Grug::Action::"Drift",
        Grug::Action::"Extract"
    ],
    resource is Grug::MirrorDiscipline
);

forbid (
    principal,
    action in [
        Grug::Action::"AttestHeaderContract",
        Grug::Action::"AttestBodyIdentityContract",
        Grug::Action::"AttestEnforcementContract",
        Grug::Action::"AttestExtractionContract",
        Grug::Action::"Sync",
        Grug::Action::"Drift",
        Grug::Action::"Extract"
    ],
    resource is Grug::MirrorDiscipline
) when {
    resource.status == "Extracted"
};
