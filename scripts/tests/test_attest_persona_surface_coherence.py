"""Tests for scripts/attest_persona_surface_coherence.py (#768).

The attest script fails when a backend repo-config flag is neither
rendered as a dashboard control nor listed in the explicit OPERATOR_ONLY
set with a stated reason.
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "attest_persona_surface_coherence.py"


def run_attest(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=REPO,
        check=False,
    )


def test_attest_script_exists():
    assert SCRIPT.exists(), "attest_persona_surface_coherence.py must exist"


def test_attest_passes_on_coherent_surface():
    """The real repo surface must be coherent (all flags have a control
    or an OPERATOR_ONLY entry)."""
    proc = run_attest()
    assert proc.returncode == 0, f"attest failed:\n{proc.stdout}\n{proc.stderr}"


def test_attest_fails_on_silently_absent_flag(tmp_path):
    """A flag in the backend model but absent from both the UI and
    OPERATOR_ONLY must fail the check."""
    # The script must expose its core check as an importable function
    # so we can feed it synthetic surfaces.
    sys.path.insert(0, str(REPO / "scripts"))
    try:
        import attest_persona_surface_coherence as attest

        missing = attest.find_silently_absent_flags(
            backend_flags={"tpm_enabled", "sneaky_new_flag"},
            ui_flags={"tpm_enabled"},
            operator_only={"other_flag": "reason"},
        )
        assert missing == {"sneaky_new_flag"}
    finally:
        sys.path.remove(str(REPO / "scripts"))


def test_persona_panel_mode_flags_count_as_ui_controls():
    """grug#1206: the Personas panel's BLOCK/WARN/OFF controls write the
    flags named on each PERSONAS entry, so those flags are UI-surfaced and
    must not also sit in OPERATOR_ONLY."""
    sys.path.insert(0, str(REPO / "scripts"))
    try:
        import attest_persona_surface_coherence as attest

        ui = attest.ui_flags()
        for flag in (
            "code_reviewer_enabled", "code_reviewer_blocking",
            "guard_enabled", "guard_blocking",
            "warder_enabled", "warder_gate_blocking", "smasher_enabled",
        ):
            assert flag in ui, f"{flag} not parsed as a dashboard control"
            assert flag not in attest.OPERATOR_ONLY, f"{flag} is wired; drop it from OPERATOR_ONLY"
    finally:
        sys.path.remove(str(REPO / "scripts"))
