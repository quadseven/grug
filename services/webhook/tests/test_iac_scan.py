"""Tests for iac_scan — IaC misconfiguration detection (#447, ADR-0007 Track 1).

Covers: per-file-type recall, file-type-AWARENESS (a k8s pattern must not fire
on Terraform and vice versa), diff-scoping (added lines only), dedup, cost
bounds, the IAC_MISCONFIG vuln_class, and that the source flows through the
existing dispatch concatenation.
"""

from __future__ import annotations

from personas.code_reviewer.diff_parser import parse_diff
from personas.code_reviewer.iac_scan import IAC_MISCONFIG, scan_iac


def _hunks(diff):
    return parse_diff(diff)


def _diff(path, *added):
    body = "".join(f"+{line}\n" for line in added)
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n+++ b/{path}\n"
        f"@@ -0,0 +1,{len(added)} @@\n"
        f"{body}"
    )


# --- per-file-type recall ---------------------------------------------------


def test_flags_open_cidr_in_terraform():
    cands = scan_iac(_hunks(_diff("main.tf", '  cidr_blocks = ["0.0.0.0/0"]')))
    assert len(cands) == 1
    assert cands[0].vuln_class == IAC_MISCONFIG
    assert cands[0].file == "main.tf"
    assert "0.0.0.0/0" in cands[0].snippet


def test_flags_privileged_container_in_k8s_yaml():
    cands = scan_iac(_hunks(_diff("deploy.yaml", "    privileged: true")))
    assert len(cands) == 1
    assert "privileged" in cands[0].snippet


def test_flags_run_as_root_and_host_network():
    cands = scan_iac(_hunks(_diff(
        "pod.yml",
        "      runAsNonRoot: false",
        "  hostNetwork: true",
    )))
    kinds = " ".join(c.snippet for c in cands)
    assert len(cands) == 2
    assert "root" in kinds and "host network" in kinds


def test_flags_public_acl_in_terraform():
    cands = scan_iac(_hunks(_diff("s3.tf", '  acl = "public-read-write"')))
    assert len(cands) == 1
    assert "public" in cands[0].snippet.lower()


def test_flags_dockerfile_user_root_and_latest():
    cands = scan_iac(_hunks(_diff(
        "Dockerfile",
        "FROM python:latest",
        "USER root",
    )))
    assert len(cands) == 2


def test_flags_pipe_to_shell_anywhere():
    cands = scan_iac(_hunks(_diff("Dockerfile", "RUN curl https://x.sh | sudo bash")))
    assert len(cands) == 1
    assert "pipe-to-shell" in cands[0].snippet


# --- file-type AWARENESS (no cross-firing) ----------------------------------


def test_k8s_pattern_does_not_fire_on_terraform():
    # `privileged: true` is a k8s shape; on a .tf file it must NOT match.
    cands = scan_iac(_hunks(_diff("main.tf", "  privileged: true")))
    assert cands == ()


def test_dockerfile_pattern_does_not_fire_on_yaml():
    cands = scan_iac(_hunks(_diff("values.yaml", "USER root")))
    assert cands == ()


def test_non_iac_file_is_skipped_entirely():
    # A python file containing a k8s-looking line is not IaC — skip it whole.
    cands = scan_iac(_hunks(_diff("app.py", "    privileged = True  # privileged: true")))
    assert cands == ()


# --- diff-scoping, dedup, clean config --------------------------------------


def test_only_added_lines_are_scanned():
    # A removed `0.0.0.0/0` (a PR CLOSING an open CIDR) must not be flagged.
    diff = (
        "diff --git a/main.tf b/main.tf\n--- a/main.tf\n+++ b/main.tf\n"
        "@@ -1,2 +1,2 @@\n"
        '-  cidr_blocks = ["0.0.0.0/0"]\n'
        '+  cidr_blocks = ["10.0.0.0/8"]\n'
    )
    assert scan_iac(_hunks(diff)) == ()


def test_same_misconfig_twice_is_deduped():
    cands = scan_iac(_hunks(_diff(
        "sg.tf",
        '  ingress { cidr_blocks = ["0.0.0.0/0"] }',
        '  egress  { cidr_blocks = ["0.0.0.0/0"] }',
    )))
    assert len(cands) == 1  # same (file, kind, matched) -> one candidate


def test_clean_config_produces_no_candidates():
    cands = scan_iac(_hunks(_diff(
        "deploy.yaml",
        "    runAsNonRoot: true",
        "    privileged: false",
        "    readOnlyRootFilesystem: true",
    )))
    assert cands == ()


def test_empty_input():
    assert scan_iac(()) == ()


# --- the source is wired into dispatch --------------------------------------


def test_scan_iac_is_in_dispatch_concatenation():
    import inspect

    from personas.guard import dispatch

    src = inspect.getsource(dispatch)
    assert "scan_iac(hunks)" in src, "scan_iac must be concatenated into the candidate sources"
