// spec 0012 — Landing Cedar policy
// Default-deny. Principals: CI attesters (the three attest_landing_*.py
// scripts) and the deploy role (wrangler in the web.deploy.yml job).

permit (
    principal in [Grug::LandingAttester],
    action in [
        Grug::Action::"AttestInstallUrlCanonical",
        Grug::Action::"AttestCtaRouting",
        Grug::Action::"AttestUrlSlugCanonical"
    ],
    resource is Grug::Landing
);

permit (
    principal in [Grug::WebDeployer],
    action in [
        Grug::Action::"Build",
        Grug::Action::"Bundle",
        Grug::Action::"Publish"
    ],
    resource is Grug::Landing
);
