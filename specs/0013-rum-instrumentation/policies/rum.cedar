// spec 0013 — RumInstrumentation Cedar policy
// Default-deny. Principals: CI attesters (the two attest_rum_*.py scripts)
// and the web deploy role (wrangler in the web.deploy.yml job).

permit (
    principal in [Grug::RumInstrumentationAttester],
    action in [
        Grug::Action::"AttestPulumiRegistration",
        Grug::Action::"AttestSpaSdkLoaded",
        Grug::Action::"AttestStaticSdkLoaded",
        Grug::Action::"AttestServiceTagCanonical"
    ],
    resource is Grug::RumInstrumentation
);

permit (
    principal in [Grug::WebDeployer],
    action in [
        Grug::Action::"Bundle",
        Grug::Action::"Deploy",
        Grug::Action::"Emit"
    ],
    resource is Grug::RumInstrumentation
);
