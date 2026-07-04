"""Shadow guard for the extracted shared package (#77, ADR-0014).

Post-extraction drift class: a copy of a services/_shared/ module
reappearing under a service tree. Because the service dir precedes
_shared/ on sys.path, such a copy would silently SHADOW the shared
module for that service only — the exact one-side-drift failure the
retired drift-lint existed to catch, now in import-resolution form.

Runs in the webhook suite (check.python gates every services/** PR);
infra/scripts/attest_mirror_policy_consistency.py holds the same
invariant for the temper spec-0010 grounding.
"""

from __future__ import annotations

from pathlib import Path

SERVICES = Path(__file__).resolve().parent.parent.parent  # services/
SHARED = SERVICES / "_shared"


def _shared_relpaths() -> list[str]:
    return [
        str(p.relative_to(SHARED))
        for p in SHARED.rglob("*.py")
        if "__pycache__" not in p.parts
    ]


def test_shared_tree_is_nonempty():
    # Guard the guard: if _shared/ ever moved, the shadowing test below
    # would vacuously pass. Anchor on modules that must exist.
    rels = _shared_relpaths()
    assert "observability.py" in rels
    assert str(Path("adapters") / "install_store.py") in rels
    assert str(Path("personas") / "registry.py") in rels
    assert len(rels) > 40


def test_no_service_tree_shadows_a_shared_module():
    offenders = []
    for rel in _shared_relpaths():
        for svc in ("api", "webhook"):
            candidate = SERVICES / svc / rel
            if candidate.exists():
                offenders.append(str(candidate.relative_to(SERVICES.parent)))
    assert not offenders, (
        "These files SHADOW services/_shared/ modules on sys.path — edit the "
        f"shared copy instead (ADR-0014): {offenders}"
    )


def test_no_mirrored_headers_remain():
    # The ADR-0001 `# MIRRORED — sibling at ...` line-1 convention died with
    # the extraction; a new one appearing means someone resurrected the
    # api/webhook mirror pattern by hand. (The match is exact-shape on
    # purpose: api/crypto/kms_envelope.py carries an unrelated cross-repo
    # "# MIRRORED from ..." provenance note that is not part of ADR-0001.)
    offenders = []
    for tree in (SERVICES / "api", SERVICES / "webhook", SHARED):
        for p in tree.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            try:
                first = p.read_text().splitlines()[0]
            except IndexError:
                continue
            if first.startswith("# MIRRORED — sibling at"):
                offenders.append(str(p.relative_to(SERVICES.parent)))
    assert not offenders, f"MIRRORED headers should not exist post-#77: {offenders}"
