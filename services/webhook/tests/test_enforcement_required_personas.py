"""grug#1202: a persona set to BLOCK becomes a REQUIRED check on the
Grug-managed ruleset, and stops being one when it drops to WARN or OFF.

Before this, ensure_enforcement only ever required `Grug - Chief`, so a red
Elder check never stopped a merge. These tests pin:

- create: the new ruleset requires Chief plus every BLOCK persona that may
  be required (today: Elder only);
- update: an existing Grug-managed ruleset gains or loses that persona's
  context, with the FULL rules array re-sent and every non-Grug context and
  every other rule passed through unchanged (the legacy-alias healer's
  discipline);
- idempotency: a ruleset that already carries the context (the hand-added
  interim edit) is not PUT again and never gets a duplicate;
- requirability: a persona that can end without a terminal check run
  (Guard, Totem/Warder, Smasher) is never added, even at BLOCK.
"""

from __future__ import annotations

import copy
from unittest.mock import patch

import pytest
from github_rulesets_client import EnforcementDetection

from enforcement import (
    GRUG_DOR_CHECK_NAME,
    GRUG_TPM_RULESET_NAME,
    ensure_enforcement,
    reconcile_required_checks,
    required_persona_checks,
)
from personas import registry
from personas.tribe import (
    CHECK_ELDER,
    CHECK_GUARD,
    CHECK_SMASHER,
    CHECK_WARDER,
    LEGACY_CHECK_ELDER,
)

ELDER_BLOCK = {"code_reviewer_enabled": True, "code_reviewer_blocking": True}
ELDER_WARN = {"code_reviewer_enabled": True, "code_reviewer_blocking": False}
ELDER_OFF = {"code_reviewer_enabled": False, "code_reviewer_blocking": True}

_DELETION_RULE = {"type": "deletion"}
_NON_FF_RULE = {"type": "non_fast_forward"}


def _ruleset(*contexts: dict, strict: bool = True) -> dict:
    """A Grug-managed ruleset carrying an admin-added extra rule on each side
    of the required_status_checks rule, so a PUT that drops or reorders any
    other rule shows up in the assertion."""
    return {
        "id": 555,
        "name": GRUG_TPM_RULESET_NAME,
        "rules": [
            _DELETION_RULE,
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": strict,
                    "required_status_checks": list(contexts),
                },
            },
            _NON_FF_RULE,
        ],
    }


def _contexts(rules: list[dict]) -> list[str]:
    out: list[str] = []
    for rule in rules:
        if rule.get("type") == "required_status_checks":
            out.extend(
                c["context"] for c in rule["parameters"]["required_status_checks"]
            )
    return out


def _ensure_managed(cfg: dict, ruleset: dict):
    """Run ensure_enforcement against an existing Grug-managed ruleset and
    return the update_ruleset mock. migrate_check_context reads the same
    ruleset; it is canonical in these fixtures, so only the persona
    reconcile may PUT."""
    original = copy.deepcopy(ruleset)
    with patch("enforcement.detect_enforcement",
               return_value=EnforcementDetection("grug_managed", 555)), \
         patch("adapters.install_store.get_enforcement_id", return_value=555), \
         patch("adapters.install_store.get_repo_config", return_value=cfg), \
         patch("enforcement.get_ruleset", return_value=ruleset), \
         patch("enforcement.update_ruleset") as mock_update, \
         patch("enforcement.create_ruleset") as mock_create, \
         patch("observability.emit_enforcement_metric"):
        state = ensure_enforcement("tok", "o", "r", "main", 1, 2)
    assert state == "grug_managed"
    mock_create.assert_not_called()
    assert ruleset == original, "the fetched ruleset must not be mutated in place"
    return mock_update


# -- create -----------------------------------------------------------------

@pytest.mark.parametrize(
    ("cfg", "expected"),
    [
        (ELDER_BLOCK, [GRUG_DOR_CHECK_NAME, CHECK_ELDER]),
        (ELDER_WARN, [GRUG_DOR_CHECK_NAME]),
        (ELDER_OFF, [GRUG_DOR_CHECK_NAME]),
    ],
    ids=["elder-block", "elder-warn", "elder-off"],
)
def test_create_requires_elder_only_when_blocking(cfg, expected):
    with patch("enforcement.detect_enforcement",
               return_value=EnforcementDetection("none", None)), \
         patch("adapters.install_store.get_enforcement_id", return_value=None), \
         patch("adapters.install_store.get_repo_config", return_value=cfg), \
         patch("adapters.install_store.set_enforcement_id"), \
         patch("enforcement.create_ruleset", return_value={"id": 9}) as mock_create, \
         patch("observability.emit_enforcement_metric"):
        assert ensure_enforcement("tok", "o", "r", "main", 1, 2) == "grug_managed"

    mock_create.assert_called_once_with(
        "tok", "o", "r", GRUG_TPM_RULESET_NAME, expected,
    )


# -- update an existing Grug-managed ruleset ----------------------------------

def test_existing_ruleset_gains_elder_when_blocking():
    ruleset = _ruleset({"context": GRUG_DOR_CHECK_NAME}, {"context": "ci / build"})
    mock_update = _ensure_managed(ELDER_BLOCK, ruleset)

    mock_update.assert_called_once()
    owner_args = mock_update.call_args.args[:4]
    rules = mock_update.call_args.args[4]
    assert owner_args == ("tok", "o", "r", 555)
    # Full rules array, same order, other rules byte-for-byte.
    assert [r["type"] for r in rules] == [
        "deletion", "required_status_checks", "non_fast_forward",
    ]
    assert rules[0] == _DELETION_RULE
    assert rules[2] == _NON_FF_RULE
    assert rules[1]["parameters"]["strict_required_status_checks_policy"] is True
    assert _contexts(rules) == [GRUG_DOR_CHECK_NAME, "ci / build", CHECK_ELDER]


@pytest.mark.parametrize("cfg", [ELDER_WARN, ELDER_OFF], ids=["warn", "off"])
def test_existing_ruleset_drops_elder_when_not_blocking(cfg):
    ruleset = _ruleset(
        {"context": GRUG_DOR_CHECK_NAME},
        {"context": CHECK_ELDER},
        {"context": "ci / build", "integration_id": 15368},
    )
    mock_update = _ensure_managed(cfg, ruleset)

    mock_update.assert_called_once()
    rules = mock_update.call_args.args[4]
    assert rules[0] == _DELETION_RULE
    assert rules[2] == _NON_FF_RULE
    checks = rules[1]["parameters"]["required_status_checks"]
    # Chief stays; the non-Grug context keeps its integration scoping.
    assert checks == [
        {"context": GRUG_DOR_CHECK_NAME},
        {"context": "ci / build", "integration_id": 15368},
    ]


def test_legacy_elder_alias_counts_as_elder_for_removal():
    """A ruleset still naming Elder's pre-rename title is still requiring
    Elder; WARN must remove that too, not leave a stale gate behind."""
    ruleset = _ruleset({"context": GRUG_DOR_CHECK_NAME}, {"context": LEGACY_CHECK_ELDER})
    with patch("enforcement.detect_enforcement",
               return_value=EnforcementDetection("grug_managed", 555)), \
         patch("adapters.install_store.get_enforcement_id", return_value=555), \
         patch("adapters.install_store.get_repo_config", return_value=ELDER_WARN), \
         patch("enforcement.migrate_check_context", return_value=False), \
         patch("enforcement.get_ruleset", return_value=ruleset), \
         patch("enforcement.update_ruleset") as mock_update, \
         patch("observability.emit_enforcement_metric"):
        ensure_enforcement("tok", "o", "r", "main", 1, 2)

    assert _contexts(mock_update.call_args.args[4]) == [GRUG_DOR_CHECK_NAME]


def test_legacy_elder_alias_satisfies_blocking_without_a_duplicate():
    ruleset = _ruleset({"context": GRUG_DOR_CHECK_NAME}, {"context": LEGACY_CHECK_ELDER})
    with patch("enforcement.detect_enforcement",
               return_value=EnforcementDetection("grug_managed", 555)), \
         patch("adapters.install_store.get_enforcement_id", return_value=555), \
         patch("adapters.install_store.get_repo_config", return_value=ELDER_BLOCK), \
         patch("enforcement.migrate_check_context", return_value=False), \
         patch("enforcement.get_ruleset", return_value=ruleset), \
         patch("enforcement.update_ruleset") as mock_update, \
         patch("observability.emit_enforcement_metric"):
        ensure_enforcement("tok", "o", "r", "main", 1, 2)

    mock_update.assert_not_called()


def test_hand_added_elder_is_left_alone_when_blocking():
    """The interim fix added `Grug - Elder` by hand to many rulesets, some
    with an integration_id. Reconciling one of those must be a no-op: no
    PUT, no duplicate, no rewrite of the hand-added entry."""
    ruleset = _ruleset(
        {"context": GRUG_DOR_CHECK_NAME},
        {"context": CHECK_ELDER, "integration_id": 4242},
    )
    mock_update = _ensure_managed(ELDER_BLOCK, ruleset)
    mock_update.assert_not_called()


def test_reconcile_is_idempotent_across_two_passes():
    """Second pass over the ruleset the first pass produced changes nothing."""
    ruleset = _ruleset({"context": GRUG_DOR_CHECK_NAME})
    first = _ensure_managed(ELDER_BLOCK, ruleset)
    healed = {**ruleset, "rules": first.call_args.args[4]}
    second = _ensure_managed(ELDER_BLOCK, healed)
    second.assert_not_called()
    assert _contexts(healed["rules"]).count(CHECK_ELDER) == 1


def test_elder_is_added_to_the_rule_that_carries_chief():
    """Two required_status_checks rules: the persona joins Chief's rule, the
    other rule is passed through untouched."""
    ruleset = {
        "id": 555,
        "rules": [
            {"type": "required_status_checks", "parameters": {
                "strict_required_status_checks_policy": False,
                "required_status_checks": [{"context": "lint"}],
            }},
            {"type": "required_status_checks", "parameters": {
                "strict_required_status_checks_policy": False,
                "required_status_checks": [{"context": GRUG_DOR_CHECK_NAME}],
            }},
        ],
    }
    mock_update = _ensure_managed(ELDER_BLOCK, ruleset)
    rules = mock_update.call_args.args[4]
    assert rules[0] == ruleset["rules"][0]
    assert [c["context"] for c in rules[1]["parameters"]["required_status_checks"]] == [
        GRUG_DOR_CHECK_NAME, CHECK_ELDER,
    ]


def test_reconcile_failure_never_blocks_the_ensure_return():
    """Same posture as the alias healer: a GitHub error while reconciling
    is logged, and ensure_enforcement still reports the detected state."""
    ruleset = _ruleset({"context": GRUG_DOR_CHECK_NAME})
    with patch("enforcement.detect_enforcement",
               return_value=EnforcementDetection("grug_managed", 555)), \
         patch("adapters.install_store.get_enforcement_id", return_value=555), \
         patch("adapters.install_store.get_repo_config", return_value=ELDER_BLOCK), \
         patch("enforcement.get_ruleset", return_value=ruleset), \
         patch("enforcement.update_ruleset", side_effect=RuntimeError("boom")), \
         patch("observability.emit_enforcement_metric"):
        assert ensure_enforcement("tok", "o", "r", "main", 1, 2) == "grug_managed"


# -- requirability (acceptance box 3) ----------------------------------------

def test_only_elder_may_be_a_required_check():
    """Elder is the only persona proven to always reach a terminal check
    conclusion (see PersonaSpec.may_be_required). Widening this set is a
    deliberate act: prove the new persona's terminal guarantee first."""
    requirable = {spec.key for spec in registry.REGISTRY if spec.may_be_required}
    assert requirable == {"code_reviewer"}


@pytest.mark.parametrize(
    ("cfg", "check"),
    [
        ({"guard_enabled": True, "guard_blocking": True}, CHECK_GUARD),
        ({"warder_enabled": True, "warder_gate_blocking": True}, CHECK_WARDER),
        ({"smasher_enabled": True}, CHECK_SMASHER),
    ],
    ids=["guard", "totem", "smasher"],
)
def test_non_requirable_persona_at_block_is_never_added(cfg, check):
    full_cfg = {**ELDER_WARN, **cfg}
    assert check not in required_persona_checks(full_cfg)

    ruleset = _ruleset({"context": GRUG_DOR_CHECK_NAME})
    mock_update = _ensure_managed(full_cfg, ruleset)
    mock_update.assert_not_called()


def test_non_requirable_persona_context_added_by_hand_is_preserved():
    """Grug only manages contexts it may add. A `Grug - Guard` an admin
    required by hand is theirs: never removed, even with Guard at WARN."""
    ruleset = _ruleset(
        {"context": GRUG_DOR_CHECK_NAME},
        {"context": CHECK_GUARD},
        {"context": CHECK_ELDER},
    )
    cfg = {**ELDER_WARN, "guard_enabled": True, "guard_blocking": False}
    mock_update = _ensure_managed(cfg, ruleset)
    assert _contexts(mock_update.call_args.args[4]) == [GRUG_DOR_CHECK_NAME, CHECK_GUARD]


def test_spec_rejects_may_be_required_without_a_blocking_flag():
    with pytest.raises(ValueError, match="may_be_required"):
        registry.PersonaSpec(
            key="x", canonical="x", check_run_name="Grug - X",
            enabled_flag="x_enabled", enabled_default=False,
            blocking_flag=None, blocking_default=False,
            dispatch_style="inline", missing_repo_policy="disabled",
            events=("pull_request",), dispatch_module="personas.x",
            may_be_required=True,
        )


# -- settings-path reconcile (acceptance box 2) -------------------------------

def test_settings_reconcile_updates_an_existing_ruleset():
    ruleset = _ruleset({"context": GRUG_DOR_CHECK_NAME})
    with patch("enforcement.detect_enforcement",
               return_value=EnforcementDetection("grug_managed", 555)), \
         patch("adapters.install_store.get_enforcement_id", return_value=555), \
         patch("adapters.install_store.get_repo_config", return_value=ELDER_BLOCK), \
         patch("enforcement.get_ruleset", return_value=ruleset), \
         patch("enforcement.update_ruleset") as mock_update, \
         patch("enforcement.create_ruleset") as mock_create:
        state = reconcile_required_checks("tok", "o", "r", "main", 1, 2)

    assert state == "grug_managed"
    mock_create.assert_not_called()
    assert _contexts(mock_update.call_args.args[4]) == [GRUG_DOR_CHECK_NAME, CHECK_ELDER]


@pytest.mark.parametrize("state", ["none", "external", "permission_denied"])
def test_settings_reconcile_never_creates_or_touches_a_foreign_ruleset(state):
    """A mode flip only edits Grug's own ruleset. It must not create one
    (that is the Fix button / tpm toggle's job, and force_disable_enforcement
    exists to stop exactly that), and must not edit an external one."""
    with patch("enforcement.detect_enforcement",
               return_value=EnforcementDetection(state, 777)), \
         patch("adapters.install_store.get_enforcement_id", return_value=None), \
         patch("adapters.install_store.get_repo_config", return_value=ELDER_BLOCK), \
         patch("enforcement.get_ruleset") as mock_get, \
         patch("enforcement.update_ruleset") as mock_update, \
         patch("enforcement.create_ruleset") as mock_create:
        assert reconcile_required_checks("tok", "o", "r", "main", 1, 2) == state

    mock_get.assert_not_called()
    mock_update.assert_not_called()
    mock_create.assert_not_called()


def test_required_persona_checks_falls_back_to_registry_defaults():
    """An unset flag means the persona's registry default, the same rule
    the dispatcher uses: Elder defaults to enabled + blocking."""
    assert required_persona_checks({}) == [CHECK_ELDER]
