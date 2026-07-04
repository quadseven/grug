"""Guard dependency watch tests (#491) - the owned dependabot pass."""
from __future__ import annotations

from unittest.mock import patch

import httpx

from personas.guard import dep_watch as dw


def test_parse_manifest_pins_requirements():
    pins = dw.parse_manifest_pins("requirements.txt", "\n".join([
        "# comment",
        "httpx==0.27.0",
        "boto3>=1.34  # unpinned - skipped",
        "cryptography==42.0.5",
        "httpx==0.27.0",  # dup - deduped
        "",
    ]))
    assert [(p.name, p.version, p.line) for p in pins] == [
        ("httpx", "0.27.0", 2), ("cryptography", "42.0.5", 4),
    ]


def test_parse_manifest_pins_never_crashes_on_junk():
    pins = dw.parse_manifest_pins("pyproject.toml", "[[weird\n%%%\nname==\n==1.0\n")
    assert pins == ()


def _wire(monkeypatch, *, enabled=True, vulns=None, existing_report=None):
    monkeypatch.setattr(dw, "get_repo_config", lambda i, r: {"dep_watch_enabled": enabled})
    monkeypatch.setattr(dw, "_discover_manifests", lambda t, o, r: ["requirements.txt"])
    monkeypatch.setattr(dw, "_fetch_manifest",
                        lambda t, o, r, p: "requests==2.19.0\n" if p == "requirements.txt" else None)
    monkeypatch.setattr(dw, "_audit", lambda deps: vulns if vulns is not None else {})
    monkeypatch.setattr(dw, "claim_dep_watch_report", lambda i, r: True)
    monkeypatch.setattr(dw, "_existing_report", lambda t, o, r: existing_report)
    writes = []
    monkeypatch.setattr(
        dw.httpx, "post",
        lambda url, **kw: writes.append(("post", url, kw.get("json"))) or httpx.Response(
            201, json={"number": 9}, request=httpx.Request("POST", url)),
    )
    monkeypatch.setattr(
        dw.httpx, "patch",
        lambda url, **kw: writes.append(("patch", url, kw.get("json"))) or httpx.Response(
            200, json={}, request=httpx.Request("PATCH", url)),
    )
    return writes


def test_vulnerable_pin_files_quarantine_issue(monkeypatch):
    writes = _wire(monkeypatch, vulns={("requests", "2.19.0"): ["GHSA-x", "CVE-y"]})
    n = dw.run_dep_watch_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}])
    assert n == 1
    verb, url, body = writes[0]
    assert verb == "post" and url.endswith("/issues")
    assert "quarantine report" in body["title"]
    assert "requests" in body["body"] and "GHSA-x" in body["body"]
    assert dw._REPORT_MARKER in body["body"]


def test_existing_report_is_refreshed_not_duplicated(monkeypatch):
    writes = _wire(monkeypatch, vulns={("requests", "2.19.0"): ["GHSA-x"]}, existing_report=7)
    n = dw.run_dep_watch_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}])
    assert n == 1
    verb, url, _ = writes[0]
    assert verb == "patch" and url.endswith("/issues/7")


def test_clean_pins_file_nothing(monkeypatch):
    writes = _wire(monkeypatch, vulns={})
    assert dw.run_dep_watch_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}]) == 0
    assert writes == []


def test_disabled_repo_costs_no_calls(monkeypatch):
    monkeypatch.setattr(dw, "get_repo_config", lambda i, r: {"dep_watch_enabled": False})
    monkeypatch.setattr(
        dw, "_fetch_manifest",
        lambda t, o, r, p: (_ for _ in ()).throw(AssertionError("no fetch expected")),
    )
    assert dw.run_dep_watch_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}]) == 0


def test_lost_claim_writes_nothing(monkeypatch):
    writes = _wire(monkeypatch, vulns={("requests", "2.19.0"): ["GHSA-x"]})
    dw.claim_dep_watch_report  # noqa: B018
    import personas.guard.dep_watch as mod
    mod_claim = lambda i, r: False
    with patch.object(dw, "claim_dep_watch_report", mod_claim):
        assert dw.run_dep_watch_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}]) == 0
    assert writes == []


def test_definite_write_failure_releases_claim(monkeypatch):
    """Codex PR #492: a 4xx on the report write releases the weekly
    claim so the next tick retries (claim = FILED report, not attempt)."""
    monkeypatch.setattr(dw, "get_repo_config", lambda i, r: {"dep_watch_enabled": True})
    monkeypatch.setattr(dw, "_discover_manifests", lambda t, o, r: ["requirements.txt"])
    monkeypatch.setattr(dw, "_fetch_manifest",
                        lambda t, o, r, p: "requests==2.19.0\n" if p == "requirements.txt" else None)
    monkeypatch.setattr(dw, "_audit", lambda deps: {("requests", "2.19.0"): ["GHSA-x"]})
    monkeypatch.setattr(dw, "_existing_report", lambda t, o, r: None)
    monkeypatch.setattr(dw, "claim_dep_watch_report", lambda i, r: True)
    released = []
    monkeypatch.setattr(dw, "release_dep_watch_report",
                        lambda i, r: released.append(r))
    resp = httpx.Response(422, request=httpx.Request("POST", "https://x"))
    monkeypatch.setattr(
        dw.httpx, "post",
        lambda url, **kw: (_ for _ in ()).throw(
            httpx.HTTPStatusError("422", request=resp.request, response=resp)),
    )
    assert dw.run_dep_watch_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}]) == 0
    assert released == ["o/r"]


def test_lookup_failure_does_not_burn_claim(monkeypatch):
    """Codex PR #492 (Pulse r3 lesson): the read-only existing-report
    lookup runs BEFORE the claim - its failure leaves no claim behind."""
    monkeypatch.setattr(dw, "get_repo_config", lambda i, r: {"dep_watch_enabled": True})
    monkeypatch.setattr(dw, "_discover_manifests", lambda t, o, r: ["requirements.txt"])
    monkeypatch.setattr(dw, "_fetch_manifest",
                        lambda t, o, r, p: "requests==2.19.0\n" if p == "requirements.txt" else None)
    monkeypatch.setattr(dw, "_audit", lambda deps: {("requests", "2.19.0"): ["GHSA-x"]})
    monkeypatch.setattr(
        dw, "_existing_report",
        lambda t, o, r: (_ for _ in ()).throw(httpx.ConnectTimeout("gh down", request=None)),
    )
    claims = []
    monkeypatch.setattr(dw, "claim_dep_watch_report",
                        lambda i, r: claims.append(r) or True)
    assert dw.run_dep_watch_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}]) == 0
    assert claims == []  # never claimed - next tick retries freely


def test_discover_manifests_matches_sca_contract(monkeypatch):
    """Codex PR #492 r2: discovery walks the whole tree with sca's own
    manifest regex - nested paths + variants covered, cap logged."""
    tree = {"tree": [
        {"type": "blob", "path": "requirements.txt"},
        {"type": "blob", "path": "requirements-dev.txt"},
        {"type": "blob", "path": "constraints.txt"},
        {"type": "blob", "path": "services/api/requirements.txt"},
        {"type": "blob", "path": "setup.cfg"},
        {"type": "blob", "path": "src/main.py"},
        {"type": "tree", "path": "requirements.txt.d"},
    ], "truncated": False}
    monkeypatch.setattr(
        dw.httpx, "get",
        lambda url, **kw: httpx.Response(200, json=tree, request=httpx.Request("GET", url)),
    )
    paths = dw._discover_manifests("tok", "o", "r")
    assert "requirements-dev.txt" in paths
    assert "constraints.txt" in paths
    assert "services/api/requirements.txt" in paths
    assert "setup.cfg" in paths
    assert "src/main.py" not in paths


def test_existing_report_is_marker_based_not_title(monkeypatch):
    """Codex PR #492 r2: identity is the BODY MARKER - a user issue that
    merely mentions the title is never overwritten; a marker-bearing
    issue with an edited title is still found."""
    issues = [
        {"number": 3, "title": "[grug-guard] Dependency quarantine report discussion",
         "body": "user thread, no marker"},
        {"number": 5, "title": "renamed by a human",
         "body": "old report\n" + dw._REPORT_MARKER},
    ]
    monkeypatch.setattr(
        dw.httpx, "get",
        lambda url, **kw: httpx.Response(200, json=issues, request=httpx.Request("GET", url)),
    )
    assert dw._existing_report("tok", "o", "r") == 5
