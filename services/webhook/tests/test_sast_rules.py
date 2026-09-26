"""Tests for the vendored SAST ruleset itself, not the runner around it.

WHY A SEPARATE FILE. test_sast.py mocks the engine subprocess, so it proves the
RUNNER handles output correctly and says nothing about whether the rules match
anything. Those are different failures: on 2026-08-15 the engine ran correctly,
exited 0, and found nothing outside Python, because the entire ruleset was one
file of ten Python rules. Every runner test passed throughout.

Two layers here:

  1. Structural checks that ALWAYS run - ids unique, `metadata.vuln_class`
     present (the runner DROPS any finding without one, so a rule missing it is
     silently dead), languages declared.
  2. Engine-backed checks against planted vulnerabilities, skipped when no
     engine is on PATH. These are the ones that would have caught the real gap,
     because a rule can be perfectly well-formed and match nothing.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

RULES_DIR = Path(__file__).resolve().parents[1] / "sast_rules"
ENGINE = shutil.which("opengrep") or shutil.which("semgrep")


def _rules() -> list[dict]:
    out: list[dict] = []
    for f in sorted(RULES_DIR.glob("*.yml")) + sorted(RULES_DIR.glob("*.yaml")):
        doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        out.extend(doc.get("rules", []))
    return out


# --- structural: always run ----------------------------------------------

def test_every_rule_declares_a_vuln_class():
    """The runner drops any finding whose rule has no metadata.vuln_class.

    A rule missing it is not a degraded rule, it is an invisible one - it
    matches, produces a result, and the mapper discards it without a log.
    """
    missing = [r["id"] for r in _rules()
               if not (r.get("metadata") or {}).get("vuln_class")]
    assert missing == [], f"rules with no vuln_class will be silently dropped: {missing}"


def test_rule_ids_are_unique():
    ids = [r["id"] for r in _rules()]
    dupes = {i for i in ids if ids.count(i) > 1}
    assert dupes == set(), f"duplicate rule ids: {dupes}"


def test_every_rule_declares_languages():
    missing = [r["id"] for r in _rules() if not r.get("languages")]
    assert missing == [], f"rules with no languages: {missing}"


def test_every_language_the_fleet_uses_has_at_least_one_rule():
    """Ansible was named in the ticket and silently missing from the first draft.

    Asserting "not python-only" was too weak - it passed the moment ONE yaml
    rule existed, which is exactly how Ansible got dropped while the file's own
    header still claimed to cover it.
    """
    langs = {lang for r in _rules() for lang in r.get("languages", [])}
    assert "yaml" in langs, "the YAML-heavy repos have no coverage"
    ids = " ".join(r["id"] for r in _rules())
    # k8s deliberately absent: iac_scan.py owns those and dispatch.py
    # concatenates both scanners without dedup, so a rule here would
    # double-report. See the header of yaml_ci_k8s.yml.
    for shape in ("gha", "ansible"):
        assert shape in ids, f"no rule targets {shape}-shaped YAML"


def test_ruleset_covers_more_than_python():
    """The gap that started #859.

    The infrastructure this reviews is mostly YAML - CI workflows, k8s
    manifests, Ansible. A Python-only ruleset reports a SAST capability while
    covering none of it.
    """
    langs = {lang for r in _rules() for lang in r.get("languages", [])}
    assert langs - {"python"}, (
        f"ruleset only covers {langs}; the YAML-heavy repos have no coverage"
    )


def test_swift_has_rule_coverage():
    """The other half of #859's title: "the private app's Swift have none".

    #862 closed the YAML side; this is the Swift side. Before swift.yml
    existed, every Swift file staged for a scan was dropped before opengrep
    ever looked at it (confirmed live and logged as
    `sast_opengrep_files_not_scanned`, #866) - not "scanned, found nothing",
    a file that was never scanned at all.
    """
    langs = {lang for r in _rules() for lang in r.get("languages", [])}
    assert "swift" in langs, "no rule targets Swift - a Swift PR gets zero SAST coverage"


# --- engine-backed: the checks that would have caught the real gap --------

PLANTED = {
    "workflow-injection": (
        ".github/workflows/x.yml",
        "name: x\n"
        "on: [issues]\n"
        "jobs:\n"
        "  a:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo \"${{ github.event.issue.title }}\"\n",
    ),
    "ansible-tls-disabled": (
        "ansible/roles/x/tasks/main.yml",
        "- name: fetch\n"
        "  ansible.builtin.uri:\n"
        "    url: https://example.invalid\n"
        "    validate_certs: no\n",
    ),
    "ansible-shell-interpolation": (
        "ansible/roles/x/tasks/run.yml",
        "- name: run\n"
        "  ansible.builtin.shell: rm -rf {{ target_dir }}\n",
    ),
    "swift-hardcoded-credential": (
        "Sources/AuthService.swift",
        "struct AuthService {\n"
        '    let apiKey = "sk_live_abcdef1234567890"\n'
        "}\n",
    ),
    "swift-weak-crypto": (
        "Sources/Hasher.swift",
        "import CryptoKit\n"
        "func hashPassword(_ password: String) -> Insecure.MD5Digest {\n"
        "    return Insecure.MD5.hash(data: Data(password.utf8))\n"
        "}\n",
    ),
    "swift-cleartext-secret-log": (
        "Sources/Session.swift",
        "func debugLog(sessionToken: String) {\n"
        '    print("session token is \\(sessionToken)")\n'
        "}\n",
    ),
    "swift-unsafe-deserialization": (
        "Sources/Cache.swift",
        "func loadCache(data: Data) -> Any? {\n"
        "    return NSKeyedUnarchiver.unarchiveObject(with: data)\n"
        "}\n",
    ),
    "swift-command-injection": (
        "Sources/Runner.swift",
        "func run(userInput: String) {\n"
        "    let task = Process()\n"
        '    task.launchPath = "/bin/sh"\n'
        '    task.arguments = ["-c", "echo " + userInput]\n'
        "    task.launch()\n"
        "}\n",
    ),
    "swift-path-traversal": (
        "Sources/Files.swift",
        "func readFile(name: String) -> Data? {\n"
        '    return FileManager.default.contents(atPath: "/var/data/" + name)\n'
        "}\n",
    ),
}


def _scan(files: dict[str, str]) -> list[str]:
    """Run the real engine over the real rules; return matched rule ids."""
    with tempfile.TemporaryDirectory() as tmp:
        for rel, body in files.items():
            p = Path(tmp) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
        proc = subprocess.run(
            [ENGINE, "scan", "--config", str(RULES_DIR), "--json", "--quiet",
             "--disable-version-check", "--no-rewrite-rule-ids", tmp],
            capture_output=True, text=True, check=False,
            # LAYER onto os.environ rather than replacing it, matching
            # sast.scan_opengrep. A hardcoded PATH here would fail to find an
            # engine installed anywhere else - and the failure mode would be
            # "tests skip", i.e. silently green.
            env={**os.environ, "HOME": tmp, "XDG_CACHE_HOME": f"{tmp}/.cache",
                 "PYTHONUTF8": "1", "LANG": "C.UTF-8"},
        )
        if proc.returncode != 0:
            pytest.fail(f"engine exited {proc.returncode}: {proc.stderr[-800:]}")
        # A zero exit does not promise parseable JSON. Without this, a truncated
        # or non-JSON payload surfaces as a bare JSONDecodeError with no engine
        # output attached, which is a slow thing to debug from CI alone.
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            pytest.fail(
                f"engine exited 0 but emitted unparseable JSON ({exc}); "
                f"stdout head: {proc.stdout[:400]!r} stderr: {proc.stderr[-400:]!r}"
            )

        # COVERAGE ASSERTION, not politeness (#863). Without it,
        # test_clean_files_produce_no_findings passes VACUOUSLY when the
        # engine scans nothing at all - the precision guard is strongest
        # exactly when coverage is zero, which is the same shape as the
        # ruleset that matched nothing that started #859.
        #
        # Measured 2026-08-15: the engine drops files it has no language for
        # (a .swift, an unknown extension) with `errors: []` and no mention
        # anywhere except their absence from paths.scanned. Exit code 0.
        scanned = {str(f).replace(f"{tmp}/", "", 1)
                   for f in (data.get("paths") or {}).get("scanned") or []}
        missed = sorted(set(files) - scanned)
        if missed:
            pytest.fail(
                f"engine never scanned {missed} - the assertions below would "
                f"pass without looking at them (scanned: {sorted(scanned)})"
            )
        # The engine's own named failures. Distinct from the above: these are
        # files it tried and could not handle, which it does report.
        if data.get("errors"):
            pytest.fail(f"engine reported errors: {data['errors']}")
    return [r["check_id"].rsplit(".", 1)[-1] for r in data.get("results", [])]


@pytest.mark.skipif(ENGINE is None, reason="no opengrep/semgrep on PATH")
@pytest.mark.parametrize("name", sorted(PLANTED))
def test_planted_vulnerability_is_caught(name: str):
    rel, body = PLANTED[name]
    matched = _scan({rel: body})
    assert matched, f"planted {name} in {rel} matched NO rule"


@pytest.mark.skipif(ENGINE is None, reason="no opengrep/semgrep on PATH")
def test_clean_files_produce_no_findings():
    """Precision guard. A ruleset that flags everything gets switched off.

    The safe form of each rule, which must NOT fire: a workflow that routes the
    untrusted value through env: and references it as a quoted shell variable,
    and an Ansible task that leaves TLS verification on and uses an argv list
    rather than interpolating into a shell string.
    """
    clean = {
        ".github/workflows/ok.yml":
            "name: ok\non: [push]\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - env:\n          T: ${{ github.event.issue.title }}\n"
            "        run: echo \"$T\"\n",
        "ansible/roles/x/tasks/ok.yml":
            "- name: fetch\n  ansible.builtin.uri:\n"
            "    url: https://example.invalid\n    validate_certs: yes\n"
            "- name: run\n  ansible.builtin.command:\n    argv: [rm, -rf, /tmp/x]\n",
        # Swift negative control: a variable name that avoids the
        # hardcoded-credential keyword regex, SHA256 (not MD5/SHA1), a fixed
        # executable path (not a shell), a path built from an allowlisted
        # value rather than concatenation, and the SECURE unarchiver API.
        "Sources/OkService.swift":
            "import Foundation\nimport CryptoKit\n\n"
            "struct OkService {\n"
            '    let configPathRef = "ssm:/app/config-path"\n\n'
            "    func hashData(_ data: Data) -> String {\n"
            "        let digest = SHA256.hash(data: data)\n"
            '        return digest.map { String(format: "%02hhx", $0) }.joined()\n'
            "    }\n\n"
            "    func run() {\n"
            "        let task = Process()\n"
            '        task.executableURL = URL(fileURLWithPath: "/usr/bin/true")\n'
            "        try? task.run()\n"
            "    }\n\n"
            "    func loadCache(data: Data) -> Any? {\n"
            "        return try? NSKeyedUnarchiver.unarchivedObject(ofClass: NSString.self, from: data)\n"
            "    }\n"
            "}\n",
    }
    assert _scan(clean) == []
