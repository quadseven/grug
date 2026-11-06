"""Purpose-scoped installation tokens (grug#1577).

The App holds `administration: write` and `contents: write`. A review
token must carry neither write, a ruleset token adds administration, and a
commit token adds contents. Each request asks only for what the
installation grants, so an install without `administration` still mints.
"""

from __future__ import annotations

import ast
import pathlib
from unittest.mock import MagicMock, patch

import pytest

FULL_GRANT = {
    "administration": "write",
    "checks": "write",
    "contents": "write",
    "issues": "write",
    "metadata": "read",
    "pull_requests": "write",
    "statuses": "write",
    "workflows": "write",
}


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    import github_app_auth as gh
    from ports.token_cache import InMemoryTokenCache

    monkeypatch.setattr(gh, "_cache", InMemoryTokenCache())
    monkeypatch.setattr(gh, "_checked_install_permissions", set())
    monkeypatch.setattr(gh, "_missing_install_permissions", {})
    monkeypatch.setattr(gh, "get_app_jwt", lambda: "app-jwt")


def _github(grant: dict):
    """Fake GitHub: GET returns the installation grant, POST mints a token
    named after the call count. Returns (get_mock, post_mock)."""
    get_resp = MagicMock()
    get_resp.raise_for_status = MagicMock()
    get_resp.json = MagicMock(return_value={"id": 7, "permissions": dict(grant)})
    get_mock = MagicMock(return_value=get_resp)
    minted = {"n": 0}

    def _post(url, **kwargs):
        minted["n"] += 1
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={
            "token": f"tok-{minted['n']}",
            "permissions": kwargs["json"]["permissions"],
        })
        return resp

    return get_mock, MagicMock(side_effect=_post)


def _requested(post_mock) -> list[dict]:
    return [c.kwargs["json"]["permissions"] for c in post_mock.call_args_list]


def test_review_token_requests_no_administration_and_read_only_contents():
    import github_app_auth as gh

    get_mock, post_mock = _github(FULL_GRANT)
    with patch("httpx.get", get_mock), patch("httpx.post", post_mock):
        gh.get_install_token(7)
    (perms,) = _requested(post_mock)
    assert "administration" not in perms
    assert "workflows" not in perms
    assert perms["contents"] == "read"
    assert perms["pull_requests"] == "write"
    assert perms["checks"] == "write"
    assert perms["issues"] == "write"
    assert perms["statuses"] == "write"
    assert perms["metadata"] == "read"
    assert get_mock.call_args.args[0].endswith("/app/installations/7")


def test_ruleset_token_adds_administration_write_but_cannot_push():
    import github_app_auth as gh

    get_mock, post_mock = _github(FULL_GRANT)
    with patch("httpx.get", get_mock), patch("httpx.post", post_mock):
        gh.get_install_token(7, purpose=gh.TokenPurpose.RULESET)
    (perms,) = _requested(post_mock)
    assert perms["administration"] == "write"
    assert perms["contents"] == "read"
    assert "workflows" not in perms


def test_commit_token_adds_contents_write_but_no_administration():
    import github_app_auth as gh

    get_mock, post_mock = _github(FULL_GRANT)
    with patch("httpx.get", get_mock), patch("httpx.post", post_mock):
        gh.get_install_token(7, purpose=gh.TokenPurpose.COMMIT)
    (perms,) = _requested(post_mock)
    assert perms["contents"] == "write"
    assert "administration" not in perms
    assert "workflows" not in perms


def test_install_without_administration_gets_a_ruleset_token_without_it():
    """Requesting a permission the install lacks is a 422; the intersection
    keeps such an install minting and reviewing."""
    import github_app_auth as gh

    grant = {k: v for k, v in FULL_GRANT.items() if k != "administration"}
    get_mock, post_mock = _github(grant)
    with patch("httpx.get", get_mock), patch("httpx.post", post_mock):
        gh.get_install_token(7, purpose=gh.TokenPurpose.RULESET)
        gh.get_install_token(7)
    for perms in _requested(post_mock):
        assert "administration" not in perms
        assert set(perms) <= set(grant)


def test_read_only_grant_is_never_raised_by_a_ceiling():
    import github_app_auth as gh

    perms = gh.purpose_permissions(
        gh.TokenPurpose.COMMIT, {"contents": "read", "metadata": "read"},
    )
    assert perms == {"contents": "read", "metadata": "read"}


@pytest.mark.parametrize("purpose", ["review", "ruleset", "commit"])
def test_unlisted_permission_never_reaches_a_token_as_a_write(purpose):
    """Writes are an allowlist: a later grant the purposes do not name
    (actions, environments) is capped at read, and secrets is dropped."""
    import github_app_auth as gh

    grant = dict(FULL_GRANT, actions="write", environments="write",
                 secrets="write", deployments="admin")
    perms = gh.purpose_permissions(gh.TokenPurpose(purpose), grant)
    assert perms["actions"] == "read"
    assert perms["environments"] == "read"
    assert perms["deployments"] == "read"
    assert "secrets" not in perms
    assert perms["pull_requests"] == "write"


def test_tokens_are_cached_per_installation_and_purpose():
    import github_app_auth as gh

    get_mock, post_mock = _github(FULL_GRANT)
    with patch("httpx.get", get_mock), patch("httpx.post", post_mock):
        review = gh.get_install_token(7)
        ruleset = gh.get_install_token(7, purpose=gh.TokenPurpose.RULESET)
        commit = gh.get_install_token(7, purpose=gh.TokenPurpose.COMMIT)
        assert gh.get_install_token(7) == review
        assert gh.get_install_token(7, purpose=gh.TokenPurpose.RULESET) == ruleset
    assert len({review, ruleset, commit}) == 3
    assert post_mock.call_count == 3
    # The grant is read once and shared by every purpose.
    assert get_mock.call_count == 1


def test_force_refresh_rereads_the_grant_for_that_purpose_only():
    import github_app_auth as gh

    get_mock, post_mock = _github(FULL_GRANT)
    with patch("httpx.get", get_mock), patch("httpx.post", post_mock):
        review = gh.get_install_token(7)
        gh.get_install_token(7, purpose=gh.TokenPurpose.RULESET, force_refresh=True)
        assert gh.get_install_token(7) == review
    assert get_mock.call_count == 2


def test_with_install_token_retry_passes_the_purpose_on_first_try_and_refresh(monkeypatch):
    import httpx

    import github_app_auth as gh

    seen: list[tuple[str, bool]] = []

    def fake_get(installation_id, *, force_refresh=False, purpose=gh.TokenPurpose.REVIEW):
        seen.append((gh.TokenPurpose(purpose).value, force_refresh))
        return f"t{len(seen)}"

    monkeypatch.setattr(gh, "get_install_token", fake_get)
    calls = {"n": 0}

    def fn(token):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.HTTPStatusError(
                "401", request=httpx.Request("GET", "https://api.github.com/x"),
                response=httpx.Response(401),
            )
        return token

    assert gh.with_install_token_retry(7, fn, purpose=gh.TokenPurpose.RULESET) == "t2"
    assert seen == [("ruleset", False), ("ruleset", True)]
    seen.clear()
    gh.with_install_token_retry(7, lambda t: t)
    assert seen == [("review", False)]


# --- caller audit -----------------------------------------------------------

_SERVICES = pathlib.Path(__file__).resolve().parents[2]
_RULESET_CALLS = frozenset({
    "ensure_enforcement", "heal_enforcement", "remove_enforcement",
    "reconcile_required_checks", "detect_enforcement", "create_ruleset",
    "update_ruleset", "delete_ruleset", "get_ruleset", "list_rulesets",
    "migrate_check_context", "reconcile_persona_contexts",
})


def _called_names(node: ast.AST) -> set[str]:
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            names.add(f.id if isinstance(f, ast.Name) else getattr(f, "attr", ""))
    return names


def _purpose(call: ast.Call) -> str | None:
    for kw in call.keywords:
        if kw.arg == "purpose" and isinstance(kw.value, ast.Attribute):
            return kw.value.attr
    return None


def _token_calls():
    """Yield (path, call, names the wrapped callable calls) for every
    with_install_token_retry call outside tests and the auth module."""
    for path in sorted(_SERVICES.rglob("*.py")):
        if "tests" in path.parts or "github_app_auth" in path.parts:
            continue
        tree = ast.parse(path.read_text())
        defs = {
            n.name: n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for call in ast.walk(tree):
            if not (isinstance(call, ast.Call) and len(call.args) >= 2):
                continue
            f = call.func
            if (f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")) != "with_install_token_retry":
                continue
            target = call.args[1]
            names = _called_names(target)
            if isinstance(target, ast.Name):
                names.add(target.id)
            # One level into same-module helpers the callable calls.
            for name in list(names):
                if name in defs:
                    names |= _called_names(defs[name])
            yield path, call, names


def test_every_ruleset_caller_mints_a_ruleset_token():
    rulesets = [
        (p, c) for p, c, names in _token_calls() if names & _RULESET_CALLS
    ]
    # Dispatcher (3), poller (1), installations API (5).
    assert len(rulesets) >= 9, rulesets
    wrong = [
        f"{p.relative_to(_SERVICES)}:{c.lineno}"
        for p, c in rulesets if _purpose(c) != "RULESET"
    ]
    assert not wrong, f"ruleset calls on a non-ruleset token: {wrong}"


def test_only_ruleset_callers_mint_a_ruleset_token():
    stray = [
        f"{p.relative_to(_SERVICES)}:{c.lineno}"
        for p, c, names in _token_calls()
        if _purpose(c) == "RULESET" and not names & _RULESET_CALLS
    ]
    assert not stray, stray


def test_branch_writes_and_thread_resolution_mint_a_commit_token():
    commits = {
        str(p.relative_to(_SERVICES))
        for p, c, _ in _token_calls() if _purpose(c) == "COMMIT"
    }
    assert commits == {
        "webhook/autofix.py",
        "_shared/personas/code_reviewer/thread_resolver.py",
    }


def test_fix_and_docstrings_commit_through_the_commit_token(monkeypatch):
    """`/grug fix` and `/grug docstrings` land their commit on a commit
    token even though the command's token_fn is the review token."""
    import autofix
    import github_app_auth as gh

    purposes: list[str] = []
    monkeypatch.setattr(
        gh, "with_install_token_retry",
        lambda iid, fn, purpose=gh.TokenPurpose.REVIEW: purposes.append(
            gh.TokenPurpose(purpose).value,
        ) or "new-head",
    )

    def review_token_fn(fn):
        raise AssertionError("commit must not use the review token")

    apply = autofix.commit_apply(7)
    assert apply(review_token_fn, "o", "r", "sha", {"a.py": "x"}, "msg", 1) == "new-head"
    assert purposes == ["commit"]
