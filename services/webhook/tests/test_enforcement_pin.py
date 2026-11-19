"""The Chief Enforcement ruleset pins every required "Grug - ..." check to the
Grug App, so a same-named check from any other App or workflow cannot satisfy
the rule. New rulesets are written pinned; the self-heal repairs one that is
not, and leaves a correctly pinned one alone."""

from __future__ import annotations

import copy
from unittest.mock import patch

import pytest
from enforcement import (
    GRUG_DOR_CHECK_NAME,
    GRUG_TPM_RULESET_NAME,
    ensure_enforcement,
    pin_grug_checks,
)
from github_rulesets_client import EnforcementDetection
from personas.tribe import CHECK_ELDER

APP_ID = 424242
CI_CHECK = "build"
ELDER_BLOCK = {"code_reviewer_enabled": True, "code_reviewer_blocking": True}


@pytest.fixture(autouse=True)
def _app_id():
    with patch("enforcement._grug_app_id", return_value=APP_ID):
        yield


def _ruleset(*checks: dict) -> dict:
    return {
        "id": 555,
        "name": GRUG_TPM_RULESET_NAME,
        "rules": [
            {"type": "deletion"},
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": list(checks),
                },
            },
        ],
    }


def _checks(rules: list[dict]) -> list[dict]:
    return [
        c
        for r in rules
        if r["type"] == "required_status_checks"
        for c in r["parameters"]["required_status_checks"]
    ]


def _heal(ruleset: dict):
    """Run the whole self-heal on an existing managed ruleset; return the
    update_ruleset mock."""
    original = copy.deepcopy(ruleset)
    with patch("enforcement.detect_enforcement",
               return_value=EnforcementDetection("grug_managed", 555)), \
         patch("adapters.install_store.get_enforcement_id", return_value=555), \
         patch("adapters.install_store.get_repo_config", return_value=ELDER_BLOCK), \
         patch("enforcement.get_ruleset", side_effect=lambda *a: copy.deepcopy(ruleset)), \
         patch("enforcement.update_ruleset") as mock_update, \
         patch("enforcement.create_ruleset") as mock_create, \
         patch("observability.emit_enforcement_metric"):
        assert ensure_enforcement("tok", "o", "r", "main", 1, 2) == "grug_managed"
    mock_create.assert_not_called()
    assert ruleset == original
    return mock_update


def test_new_ruleset_pins_every_grug_check_to_the_app():
    with patch("enforcement.detect_enforcement",
               return_value=EnforcementDetection("none", None)), \
         patch("adapters.install_store.get_enforcement_id", return_value=None), \
         patch("adapters.install_store.get_repo_config", return_value=ELDER_BLOCK), \
         patch("enforcement.repo_is_empty", return_value=False), \
         patch("adapters.install_store.set_enforcement_id"), \
         patch("enforcement.create_ruleset", return_value={"id": 9}) as mock_create, \
         patch("observability.emit_enforcement_metric"):
        assert ensure_enforcement("tok", "o", "r", "main", 1, 2) == "grug_managed"

    args, kwargs = mock_create.call_args
    assert args[4] == [GRUG_DOR_CHECK_NAME, CHECK_ELDER]
    assert kwargs["integration_id"] == APP_ID


def test_create_ruleset_body_carries_the_integration_id():
    from github_rulesets_client import create_ruleset
    from unittest.mock import MagicMock

    resp = MagicMock(status_code=201)
    resp.json.return_value = {"id": 1}
    with patch("httpx.post", return_value=resp) as mock_post:
        create_ruleset("tok", "o", "r", "Grug - Chief Enforcement",
                       [GRUG_DOR_CHECK_NAME, CHECK_ELDER], integration_id=APP_ID)

    checks = mock_post.call_args.kwargs["json"]["rules"][0]["parameters"][
        "required_status_checks"
    ]
    assert checks == [
        {"context": GRUG_DOR_CHECK_NAME, "integration_id": APP_ID},
        {"context": CHECK_ELDER, "integration_id": APP_ID},
    ]


def test_unpinned_ruleset_is_repaired_on_self_heal():
    ruleset = _ruleset(
        {"context": GRUG_DOR_CHECK_NAME},
        {"context": CHECK_ELDER},
        {"context": CI_CHECK},
    )
    mock_update = _heal(ruleset)

    mock_update.assert_called_once()
    new_rules = mock_update.call_args.args[4]
    assert new_rules[0] == {"type": "deletion"}
    assert new_rules[1]["parameters"]["strict_required_status_checks_policy"] is True
    assert _checks(new_rules) == [
        {"context": GRUG_DOR_CHECK_NAME, "integration_id": APP_ID},
        {"context": CHECK_ELDER, "integration_id": APP_ID},
        {"context": CI_CHECK},  # a non-Grug check is never pinned by us
    ]


def test_check_pinned_to_another_app_is_repaired():
    ruleset = _ruleset({"context": GRUG_DOR_CHECK_NAME, "integration_id": 1})
    mock_update = _heal(ruleset)

    new_checks = _checks(mock_update.call_args.args[4])
    assert new_checks[0] == {"context": GRUG_DOR_CHECK_NAME, "integration_id": APP_ID}


def test_correctly_pinned_ruleset_is_left_alone():
    ruleset = _ruleset(
        {"context": GRUG_DOR_CHECK_NAME, "integration_id": APP_ID},
        {"context": CHECK_ELDER, "integration_id": APP_ID},
        {"context": CI_CHECK, "integration_id": 15368},
    )
    _heal(ruleset).assert_not_called()


def test_pin_is_idempotent_and_reports_whether_it_wrote():
    ruleset = _ruleset({"context": GRUG_DOR_CHECK_NAME})
    with patch("enforcement.get_ruleset", return_value=ruleset), \
         patch("enforcement.update_ruleset") as mock_update:
        assert pin_grug_checks("tok", "o", "r", 555) is True
    pinned = mock_update.call_args.args[4]

    with patch("enforcement.get_ruleset", return_value={"rules": pinned}), \
         patch("enforcement.update_ruleset") as mock_update:
        assert pin_grug_checks("tok", "o", "r", 555) is False
    mock_update.assert_not_called()


def test_pin_collapses_a_duplicate_the_pin_creates():
    ruleset = _ruleset(
        {"context": GRUG_DOR_CHECK_NAME},
        {"context": GRUG_DOR_CHECK_NAME, "integration_id": APP_ID},
    )
    with patch("enforcement.get_ruleset", return_value=ruleset), \
         patch("enforcement.update_ruleset") as mock_update:
        assert pin_grug_checks("tok", "o", "r", 555) is True

    assert _checks(mock_update.call_args.args[4]) == [
        {"context": GRUG_DOR_CHECK_NAME, "integration_id": APP_ID},
    ]
