"""Repo-grounded verification pass tests (#708, epic #707).

Each of the three kill classes reproduces a REAL Elder marking rejected by
the PR author on 2026-07-18 (PRs #694/#698/#706) - the regression contract
is: with verification in place, that marking dies before publication, with
a machine-readable reason. The false-kill guards pin the other direction:
verification must never kill a finding whose claim the repo evidence
actually supports (or cannot refute).
"""

from __future__ import annotations

import pytest

from personas.code_reviewer.persona import Finding
from personas.code_reviewer.verify import verify_findings


def _finding(**kw) -> Finding:
    base = dict(
        file="services/webhook/x.py",
        line=5,
        severity="high",
        rule_name="some-rule",
        message="msg",
        suggestion=None,
    )
    base.update(kw)
    return Finding(**base)


# --- class 1: non-code file (PR #706: runbook prose flagged as command
# injection) ---------------------------------------------------------------

_RUNBOOK = """# Ops Runbook

Drain procedure:

1. `kubectl drain <node> --ignore-daemonsets`
2. `make dd-downtime-start SCOPE="node:<node>"`
"""


def test_code_execution_rule_on_prose_file_is_killed():
    f = _finding(
        file="docs/RUNBOOK.md",
        line=5,
        rule_name="unvalidated-external-input",
        message="command injection hole in user-supplied node name",
    )
    kept, killed = verify_findings((f,), {"docs/RUNBOOK.md": _RUNBOOK})
    assert kept == ()
    assert len(killed) == 1
    assert killed[0].reason == "non_code_file"
    assert killed[0].finding is f


def test_docs_claim_rule_on_prose_file_survives():
    """Prose files still get PROSE-class findings (claim drift, typos) -
    only code-execution-class rules die there."""
    f = _finding(
        file="docs/RUNBOOK.md",
        line=5,
        rule_name="doc-code-claim-drift",
        message="the documented cap does not match the env default",
    )
    kept, killed = verify_findings((f,), {"docs/RUNBOOK.md": _RUNBOOK})
    assert kept == (f,)
    assert killed == ()


# --- class 2: sync context (PR #698: time.sleep / missing-await flagged
# inside a plain def that runs via asyncio.to_thread) ----------------------

_SYNC_MODULE = '''\
import time


def publish_with_retry(fn):
    while True:
        try:
            fn()
            return
        except OSError:
            time.sleep(0.5)
'''

_ASYNC_MODULE = '''\
import asyncio
import time


async def handler():
    time.sleep(0.5)
    return 1
'''


def test_async_family_rule_in_sync_def_is_killed():
    f = _finding(
        file="services/x.py",
        line=9,
        rule_name="sync-io-in-async",
        message="blocking time.sleep stalls the async event loop",
    )
    kept, killed = verify_findings((f,), {"services/x.py": _SYNC_MODULE})
    assert kept == ()
    assert killed[0].reason == "sync_context"


def test_async_family_rule_in_async_def_survives():
    f = _finding(
        file="services/x.py",
        line=6,
        rule_name="sync-io-in-async",
        message="blocking time.sleep stalls the async event loop",
    )
    kept, killed = verify_findings((f,), {"services/x.py": _ASYNC_MODULE})
    assert kept == (f,)
    assert killed == ()


def test_missing_await_is_not_sync_context_killable():
    """Workflow review on PR #710 (reversing an earlier kill): an imported
    coroutine callable invoked in an all-sync module is a REAL
    coroutine-never-awaited bug - the module's own lack of async syntax
    proves nothing about imported callables. missing-await survives."""
    f = _finding(
        file="services/x.py",
        line=7,
        rule_name="missing-await",
        message="the coroutine is created but never runs; add await",
    )
    kept, killed = verify_findings((f,), {"services/x.py": _SYNC_MODULE})
    assert kept == (f,)
    assert killed == ()


def test_async_rule_at_module_level_is_inconclusive_and_survives():
    """A flagged line outside any function cannot be proven sync-context
    (module-level code CAN run inside a loop via exec/import tricks) -
    inconclusive keeps the finding."""
    f = _finding(
        file="services/x.py",
        line=1,
        rule_name="sync-io-in-async",
        message="blocking import-time sleep",
    )
    kept, killed = verify_findings((f,), {"services/x.py": _SYNC_MODULE})
    assert kept == (f,)
    assert killed == ()


def test_async_rule_on_unparseable_file_survives():
    f = _finding(
        file="services/x.py",
        line=2,
        rule_name="sync-io-in-async",
        message="blocking sleep",
    )
    kept, killed = verify_findings((f,), {"services/x.py": "def broken(:\n    pass"})
    assert kept == (f,)
    assert killed == ()


# --- class 3: fix already present (PR #694: 'strip the raw SSM value' on
# the exact line that already calls .strip()) ------------------------------

_STRIPPED_MODULE = '''\
import os


def _app_id() -> str:
    return _get(os.environ["GITHUB_APP_ID_SSM"]).strip()
'''


def test_suggested_fix_already_on_anchored_line_is_killed():
    f = _finding(
        file="services/auth.py",
        line=5,
        rule_name="moderate-string-comparison-failure",
        message="app ID compared unstripped; whitespace breaks comparison",
        suggestion="apply .strip() to the raw SSM value",
    )
    kept, killed = verify_findings((f,), {"services/auth.py": _STRIPPED_MODULE})
    assert kept == ()
    assert killed[0].reason == "fix_already_present"


def test_suggested_fix_absent_from_anchor_survives():
    f = _finding(
        file="services/auth.py",
        line=5,
        rule_name="moderate-string-comparison-failure",
        message="app ID compared unstripped",
        suggestion="apply .casefold() before comparing",
    )
    kept, killed = verify_findings((f,), {"services/auth.py": _STRIPPED_MODULE})
    assert kept == (f,)
    assert killed == ()


def test_no_suggestion_skips_already_present_check():
    f = _finding(
        file="services/auth.py",
        line=5,
        rule_name="moderate-string-comparison-failure",
        message="app ID compared unstripped",
        suggestion=None,
    )
    kept, killed = verify_findings((f,), {"services/auth.py": _STRIPPED_MODULE})
    assert kept == (f,)


def test_prose_suggestion_without_code_tokens_skips_the_check():
    """A suggestion with no code-ish token ('rethink this design') must not
    trigger substring kills on ordinary words."""
    f = _finding(
        file="services/auth.py",
        line=5,
        rule_name="moderate-string-comparison-failure",
        message="msg",
        suggestion="rethink the comparison design entirely",
    )
    kept, killed = verify_findings((f,), {"services/auth.py": _STRIPPED_MODULE})
    assert kept == (f,)


# --- cross-cutting guards -------------------------------------------------


def test_file_missing_from_contents_is_inconclusive_and_survives():
    f = _finding(file="services/nowhere.py", rule_name="sync-io-in-async")
    kept, killed = verify_findings((f,), {})
    assert kept == (f,)
    assert killed == ()


def test_ordinary_finding_on_code_file_passes_through():
    f = _finding(
        file="services/x.py", line=9, rule_name="null-deref",
        message="x may be None here",
    )
    kept, killed = verify_findings((f,), {"services/x.py": _SYNC_MODULE})
    assert kept == (f,)
    assert killed == ()


def test_order_preserved_and_mixed_verdicts():
    dead = _finding(
        file="docs/a.md", line=1, rule_name="sql-injection", message="inject",
    )
    alive1 = _finding(file="services/x.py", line=9, rule_name="null-deref")
    alive2 = _finding(
        file="services/x.py", line=6, rule_name="broad-except-masks-bug",
    )
    kept, killed = verify_findings(
        (alive1, dead, alive2),
        {"docs/a.md": "# doc", "services/x.py": _SYNC_MODULE},
    )
    assert kept == (alive1, alive2)
    assert [k.finding for k in killed] == [dead]


# --- PR #710 review tightenings (FLINT round 1) ----------------------


def test_docs_class_rule_with_execution_vocabulary_survives_on_prose():
    """A claim-drift rule quoting execution vocabulary ('timeout') must not
    prose-kill - docs-class rules legitimately anchor in markdown."""
    f = _finding(
        file="docs/RUNBOOK.md",
        line=5,
        rule_name="doc-async-claim-drift",
        message="the documented timeout does not match the env default",
    )
    kept, killed = verify_findings((f,), {"docs/RUNBOOK.md": _RUNBOOK})
    assert kept == (f,)
    assert killed == ()


def test_sync_def_in_module_with_async_code_survives():
    """A lexically-sync helper in a module that ALSO has async code could
    be called from the loop directly - inconclusive keeps it."""
    src = (
        "import time\n"
        "\n"
        "async def handler():\n"
        "    helper()\n"
        "\n"
        "def helper():\n"
        "    time.sleep(1)\n"
    )
    f = _finding(
        file="services/x.py",
        line=7,
        rule_name="sync-io-in-async",
        message="blocking sleep in a helper reachable from the handler",
    )
    kept, killed = verify_findings((f,), {"services/x.py": src})
    assert kept == (f,)
    assert killed == ()


def test_bare_assign_suggestion_token_no_longer_extracted():
    """`timeout=30` must not truncate to `timeout=` and match an unrelated
    `timeout=None` - assign-form tokens are not extracted at all."""
    src = "def f():\n    call(timeout=None)\n"
    f = _finding(
        file="services/x.py",
        line=2,
        rule_name="missing-timeout-guard",
        message="the call needs a bounded timeout",
        suggestion="pass timeout=30 to the call",
    )
    kept, killed = verify_findings((f,), {"services/x.py": src})
    assert kept == (f,)
    assert killed == ()


def test_fix_token_on_neighboring_line_only_does_not_kill():
    """The already-present check reads the ANCHOR line only - a matching
    token two lines away proves nothing about the flagged line."""
    src = (
        "def f(raw, other):\n"
        "    a = other.strip()\n"
        "    return compare(raw)\n"
    )
    f = _finding(
        file="services/x.py",
        line=3,
        rule_name="moderate-string-comparison-failure",
        message="raw compared unstripped",
        suggestion="apply .strip() to raw before comparing",
    )
    kept, killed = verify_findings((f,), {"services/x.py": src})
    assert kept == (f,)
    assert killed == ()


# --- PR #710 review tightenings (workflow round 2) ------------------------


def test_credential_leak_on_readme_survives_prose_kill():
    """Workflow review on PR #710: prose files genuinely carry leaked
    secrets - a README token is a real critical, not a category error."""
    f = _finding(
        file="README.md",
        line=3,
        severity="critical",
        rule_name="credential-leak",
        message="hardcoded API token in the curl example",
    )
    kept, killed = verify_findings((f,), {"README.md": "# x\n\ncurl -H 'tok'\n"})
    assert kept == (f,)
    assert killed == ()


def test_word_boundary_markers_do_not_collide_with_prose_words():
    """'grace' must not hit 'race', 'nullable' must not hit 'null',
    'out of sync' must not hit 'sync' - markers are word-boundaried."""
    f = _finding(
        file="docs/guide.md",
        line=2,
        rule_name="stale-guidance",
        message="the grace period for nullable fields is out of sync",
    )
    kept, killed = verify_findings((f,), {"docs/guide.md": "# g\ntext\n"})
    assert kept == (f,)
    assert killed == ()


def test_txt_files_are_not_prose():
    """CMakeLists.txt / requirements.txt are executable/dependency code -
    .txt earns no prose exemption."""
    f = _finding(
        file="CMakeLists.txt",
        line=1,
        rule_name="command-injection",
        message="unquoted variable expansion in execute_process",
    )
    kept, killed = verify_findings(
        (f,), {"CMakeLists.txt": "execute_process(COMMAND ${X})\n"},
    )
    assert kept == (f,)
    assert killed == ()


def test_thread_deadlock_rule_in_sync_code_survives():
    """A 'blocking' thread-deadlock claim is TRUE in fully synchronous
    code - bare 'blocking' rules are no longer sync_context-killable."""
    src = (
        "import threading\n"
        "lock = threading.Lock()\n"
        "\n"
        "def worker():\n"
        "    lock.acquire()\n"
        "    lock.acquire()\n"
    )
    f = _finding(
        file="services/x.py",
        line=6,
        rule_name="blocking-lock-deadlock",
        message="second acquire self-deadlocks the worker thread",
    )
    kept, killed = verify_findings((f,), {"services/x.py": src})
    assert kept == (f,)
    assert killed == ()


# --- class 4: mitigation present in a DIFFERENT form than the suggestion
# (the live grug PR #766 ssrf family) --------------------------------------
#
# PR #766 published THREE high-severity ssrf markings against a URL where
# every interpolation was already `quote(..., safe='')`. class 3
# (fix_already_present) structurally cannot catch these: it token-matches the
# model's OWN suggestion, which said "validate"/"allowlist" - words that
# appear nowhere on the line.

_SSRF_SANITIZED = (
    "from urllib.parse import quote\n"
    "\n"
    "def _discover_files(token, owner, repo):\n"
    "    url = f\"https://api.github.com/repos/{quote(owner, safe=\'\')}/{quote(repo, safe=\'\')}/git/trees/HEAD\"\n"
    "    return httpx.get(url, timeout=10)\n"
)

_SSRF_PARTIAL = (
    "from urllib.parse import quote\n"
    "\n"
    "def _discover_files(token, owner, repo):\n"
    "    url = f\"https://api.github.com/repos/{quote(owner, safe=\'\')}/{repo}/git/trees/HEAD\"\n"
    "    return httpx.get(url, timeout=10)\n"
)

_SSRF_MSG = (
    "Server-side request forgery. owner and repo are interpolated into the "
    "GitHub API URL without validation."
)


def test_fully_sanitized_interpolations_are_killed():
    f = _finding(
        file="services/x.py", line=4, rule_name="ssrf", message=_SSRF_MSG,
        suggestion="Validate owner/repo against an allowlist before use.",
    )
    kept, killed = verify_findings((f,), {"services/x.py": _SSRF_SANITIZED})
    assert kept == ()
    assert len(killed) == 1
    assert killed[0].reason == "mitigation_present"


def test_one_bare_interpolation_survives():
    """Load-bearing negative: a PARTIALLY sanitized line is exactly the
    reported vulnerability, so it must never be killed."""
    f = _finding(
        file="services/x.py", line=4, rule_name="ssrf", message=_SSRF_MSG,
        suggestion="Validate owner/repo against an allowlist before use.",
    )
    kept, killed = verify_findings((f,), {"services/x.py": _SSRF_PARTIAL})
    assert kept == (f,)
    assert killed == ()


def test_unrelated_rule_family_untouched_on_sanitized_line():
    """Only interpolation-injection families qualify - a timeout finding on
    the same sanitized line is a different claim and must survive."""
    f = _finding(
        file="services/x.py", line=4, rule_name="missing-timeout",
        message="no timeout on the outbound request", suggestion="Add timeout=10.",
    )
    kept, killed = verify_findings((f,), {"services/x.py": _SSRF_SANITIZED})
    assert kept == (f,)
    assert killed == ()


def test_line_without_interpolation_is_inconclusive():
    f = _finding(
        file="services/x.py", line=5, rule_name="ssrf", message=_SSRF_MSG,
    )
    kept, killed = verify_findings((f,), {"services/x.py": _SSRF_SANITIZED})
    assert kept == (f,)
    assert killed == ()


def test_mitigation_kill_needs_the_file():
    f = _finding(file="services/x.py", line=4, rule_name="ssrf", message=_SSRF_MSG)
    kept, killed = verify_findings((f,), {})
    assert kept == (f,)
    assert killed == ()


# --- fix_block_present (live 2026-09-22): the model anchored on a
# docstring and proposed, as its fix, the exact block already below it ------

_MEMBERS_MODULE = """import registry


def _fetch_members() -> dict:
    \"\"\"Every group the registry lists.

    One read and one guard.
    \"\"\"
    names = registry.names()
    holes = ", ".join(["%s"] * len(names))
    conn = _connect()
    try:
        with conn.cursor() as cur:
            rows = _wide_guarded(cur, _MEMBER_QUERY.format(holes=holes),  # noqa: S608
                                 tuple(names), "", "member")
    finally:
        conn.close()
    return {"rows": rows, "names": names}


def _fetch_other() -> dict:
    conn = _connect()
    rows = conn.cursor().fetchall()
    return {"rows": rows}
"""

_MEMBERS_FIX = """    conn = _connect()
    try:
        with conn.cursor() as cur:
            rows = _wide_guarded(cur, _MEMBER_QUERY.format(holes=holes),  # noqa: S608
                                 tuple(names), "", "member")
    finally:
        conn.close()"""


def test_suggested_block_already_in_flagged_function_is_killed():
    f = _finding(
        file="map_server.py", line=5, severity="medium", rule_name="resource-leak",
        message="connection fetched outside the shelter of try",
        suggestion=_MEMBERS_FIX,
    )
    kept, killed = verify_findings((f,), {"map_server.py": _MEMBERS_MODULE})
    assert kept == ()
    assert killed[0].reason == "fix_block_present"


def test_suggested_block_absent_from_flagged_function_survives():
    # `_fetch_other` really does leak its connection; the same suggestion
    # existing in a DIFFERENT function must not prove it fixed.
    f = _finding(
        file="map_server.py", line=23, severity="medium", rule_name="resource-leak",
        message="connection never closed", suggestion=_MEMBERS_FIX,
    )
    kept, killed = verify_findings((f,), {"map_server.py": _MEMBERS_MODULE})
    assert kept == (f,)
    assert killed == ()


def test_short_suggestion_block_never_proves_anything():
    f = _finding(
        file="map_server.py", line=5, severity="medium", rule_name="resource-leak",
        message="close the connection", suggestion="    finally:\n        conn.close()",
    )
    kept, _ = verify_findings((f,), {"map_server.py": _MEMBERS_MODULE})
    assert kept == (f,)


# --- class 6: collection-lockstep caller-not-updated (grug #763, the
# PR #762 marking) ------------------------------------------------------
#
# Elder saw the DoR rule registry grow by one entry and asserted that the
# persona-purity call-target ALLOWLIST had to grow in lockstep. It did not:
# the new rule dispatches inside run_all, so it is never a call target of
# evaluate_pull_request, and the attester passed on the reviewed head. The
# caller-not-updated rule is scoped to a changed signature, return shape or
# raise; a "list A grew, so list B must grow" claim is outside it.

# infra/scripts/attest_dor_checks_five_rules.py at the PR #762 review head
# (9f7c499), lines 20-32. The marking anchored on line 29 (the registry entry).
_ATTESTER_AT_762_HEAD = "\n" * 19 + '''"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

CANONICAL_RULES: frozenset[str] = frozenset(
    {"check_why", "check_acceptance", "check_estimate", "check_scope_fence", "check_issue_link", "check_linked_issue_completeness"}
)

DOR_CHECK_PATHS: tuple[Path, ...] = (
'''

# The marking Elder published on PR #762, verbatim.
_PR_762_MESSAGE = (
    "Grug see the attester's CANONICAL_RULES list grows to include "
    "'check_linked_issue_completeness', but the attest persona purity "
    "allowlist in services/_shared/personas/tpm/dor_checks.py still names "
    "only five rules. The pure-call-list in attest_persona_purity.py line "
    "35-37 must also add check_linked_issue_completeness, else the "
    "attestation will flag an extra call target that does not exist in the "
    "spec's allowed list. So speaks Grug."
)
_ATTESTER_PATH = "infra/scripts/attest_dor_checks_five_rules.py"


def test_pr_762_registry_vs_allowlist_marking_is_killed():
    f = _finding(
        file=_ATTESTER_PATH, line=29, severity="medium",
        rule_name="caller-not-updated", message=_PR_762_MESSAGE,
    )
    kept, killed = verify_findings((f,), {_ATTESTER_PATH: _ATTESTER_AT_762_HEAD})
    assert kept == ()
    assert [k.reason for k in killed] == ["collection_lockstep"]
    assert killed[0].finding is f


def test_real_stale_caller_marking_survives():
    """The rule's own shape - a changed signature with a stale caller -
    must never die, even when the message also mentions a list."""
    source = "\n" * 4 + "def fetch(id, *, tenant):\n    return ids_list\n"
    f = _finding(
        line=5, rule_name="caller-not-updated",
        message="fetch's signature grew a required tenant parameter, but "
        "caller src/jobs.py:42 still passes the old 1-arg form from its "
        "ids list; the retry list must also add tenant.",
    )
    kept, killed = verify_findings((f,), {"services/webhook/x.py": source})
    assert kept == (f,)
    assert killed == ()


def test_lockstep_claim_anchored_on_a_signature_line_survives():
    """Message vocabulary alone never kills: when the anchored line IS a
    def/raise/return, the change may be the rule's real shape."""
    source = "\n" * 4 + "def handlers(registry):\n"
    f = _finding(
        line=5, rule_name="caller-not-updated",
        message="the HANDLERS list grows, so the dispatch allowlist must "
        "also add the new handler",
    )
    kept, killed = verify_findings((f,), {"services/webhook/x.py": source})
    assert kept == (f,)
    assert killed == ()


def test_single_collection_claim_survives():
    """One collection plus its consumer (an enum gained a member, a match
    does not handle it) is not the two-list lockstep shape - keep it."""
    source = "\n" * 4 + "    NEW_STATE = 'new'\n"
    f = _finding(
        line=5, rule_name="caller-not-updated",
        message="the State enum grows NEW_STATE but the dispatcher in "
        "jobs.py still only handles the old members",
    )
    kept, killed = verify_findings((f,), {"services/webhook/x.py": source})
    assert kept == (f,)
    assert killed == ()


def test_lockstep_kill_needs_the_source():
    """No source snapshot is no evidence: keep (inconclusive-keeps bias)."""
    f = _finding(
        file=_ATTESTER_PATH, line=29, rule_name="caller-not-updated",
        message=_PR_762_MESSAGE,
    )
    kept, killed = verify_findings((f,), {})
    assert kept == (f,)
    assert killed == ()


def test_lockstep_kill_is_scoped_to_caller_not_updated():
    f = _finding(
        file=_ATTESTER_PATH, line=29, rule_name="logic-error",
        message=_PR_762_MESSAGE,
    )
    kept, killed = verify_findings((f,), {_ATTESTER_PATH: _ATTESTER_AT_762_HEAD})
    assert kept == (f,)
    assert killed == ()


# --- return_value_used: a "return value discarded" claim refuted by the
# file's own call sites (mod-overseer PR 1083: a dead-code marking on the
# definition said the result "is discarded" while the only caller tested it
# in an `if (!...)`) ------------------------------------------------------

_CPP_PATH = "src/overseer_decisions.cpp"
_CPP_DEFINITION_LINE = 2


def _gear_source(call_line: str) -> str:
    return (
        "// GearReadOneEquipSpell(...) reads one spell; true on success\n"
        "bool GearReadOneEquipSpell(SpellEffects const& effects, Resolved& resolved)\n"
        "{\n"
        "    return effects.valid;\n"
        "}\n"
        "\n"
        "void GearReadItemSpells(Item const& spell, Resolved& resolved)\n"
        "{\n"
        f"    {call_line}\n"
        "}\n"
    )


_GEAR_CONSUMED = _gear_source(
    "if (!GearReadOneEquipSpell(spell.effects, resolved)) resolved.unresolved = true;"
)
_GEAR_BARE = _gear_source("GearReadOneEquipSpell(spell.effects, resolved);")
_DISCARDED_MESSAGE = (
    "the return value of `GearReadOneEquipSpell` is discarded, so a failed "
    "read is never reported"
)


def _discarded_finding(**kw) -> Finding:
    base = {
        "file": _CPP_PATH, "line": _CPP_DEFINITION_LINE,
        "rule_name": "dead-code", "message": _DISCARDED_MESSAGE,
    }
    base.update(kw)
    return _finding(**base)


def test_discarded_return_claim_is_killed_when_every_caller_uses_it():
    f = _discarded_finding()
    kept, killed = verify_findings((f,), {_CPP_PATH: _GEAR_CONSUMED})
    assert kept == ()
    assert [k.reason for k in killed] == ["return_value_used"]
    assert killed[0].finding is f


def test_discarded_return_claim_reads_callers_in_other_fetched_files():
    """The caller may live in another file the dispatch fetched."""
    header = "bool GearReadOneEquipSpell(SpellEffects const& e, Resolved& r);\n"
    caller = "void f() {\n    bool ok = GearReadOneEquipSpell(e, r);\n}\n"
    f = _discarded_finding(file="src/gear.h", line=1)
    kept, killed = verify_findings(
        (f,), {"src/gear.h": header, "src/gear.cpp": caller},
    )
    assert kept == ()
    assert [k.reason for k in killed] == ["return_value_used"]


def test_discarded_return_claim_survives_a_bare_call():
    """A bare statement call really discards the result: keep."""
    f = _discarded_finding()
    kept, killed = verify_findings((f,), {_CPP_PATH: _GEAR_BARE})
    assert kept == (f,)
    assert killed == ()


def test_discarded_return_claim_survives_when_one_of_two_callers_is_bare():
    source = _GEAR_CONSUMED + "void g() {\n    GearReadOneEquipSpell(e, r);\n}\n"
    f = _discarded_finding()
    kept, killed = verify_findings((f,), {_CPP_PATH: source})
    assert kept == (f,)
    assert killed == ()


@pytest.mark.parametrize("call_line", [
    "(void)GearReadOneEquipSpell(spell.effects, resolved);",
    "if (spell.valid) GearReadOneEquipSpell(spell.effects, resolved);",
    'Log("GearReadOneEquipSpell(spell) failed");',
])
def test_discarded_return_claim_survives_a_discard_or_unreadable_site(call_line):
    """An explicit discard is evidence for the claim; a call after `)` or
    inside a string is no evidence either way. Both keep the finding."""
    source = _GEAR_CONSUMED + "void g() {\n    " + call_line + "\n}\n"
    f = _discarded_finding()
    kept, killed = verify_findings((f,), {_CPP_PATH: source})
    assert kept == (f,)
    assert killed == ()


def test_discarded_return_claim_with_no_call_site_is_kept():
    """Only the definition and a comment mention it: inconclusive, keep."""
    source = _gear_source("return;")
    f = _discarded_finding()
    kept, killed = verify_findings((f,), {_CPP_PATH: source})
    assert kept == (f,)
    assert killed == ()


def test_discarded_return_claim_in_python_reads_not_and_assignment():
    source = (
        "def save(row):\n"
        "    return db.write(row)\n"
        "\n"
        "def handler(row):\n"
        "    if not save(row):\n"
        "        raise RuntimeError('lost')\n"
        "    ok = save(row)\n"
    )
    f = _finding(
        line=1, rule_name="unused-return-value",
        message="the result of `save` is ignored by its callers",
    )
    kept, killed = verify_findings((f,), {"services/webhook/x.py": source})
    assert kept == ()
    assert [k.reason for k in killed] == ["return_value_used"]


def test_other_dead_code_claims_are_not_read_as_return_value_claims():
    """A dead-code marking about an unused function is a different claim."""
    f = _discarded_finding(
        message="`GearReadOneEquipSpell` is never called and can be removed",
    )
    kept, killed = verify_findings((f,), {_CPP_PATH: _GEAR_CONSUMED})
    assert kept == (f,)
    assert killed == ()


# --- callee_not_async: a missing-await claim whose callee is a plain `def`
# in the fetched files (every sampled marking of this class was declined;
# one PR got the same claim three times). Adding the `await` it asks for
# would raise TypeError on the returned value. ------------------------------

_LEGS_PATH = "services/probe/legs.py"
_RUNNER_PATH = "services/probe/runner.py"
_RUNNER = (
    "from probe.legs import Legs\n"
    "\n"
    "\n"
    "async def run(a: Legs) -> bool:\n"
    "    ok = a._probe(a.paths[0])\n"
    "    return ok\n"
)
_RUNNER_LINE = 5
_AWAIT_MESSAGE = (
    "`a._probe(a.paths[0])` is a coroutine that is never awaited, so `ok` "
    "is always a truthy coroutine object"
)


def _legs(prefix: str = "def") -> str:
    return (
        "class Legs:\n"
        "    paths = ()\n"
        "\n"
        f"    {prefix} _probe(self, leg):\n"
        "        return leg.ok\n"
    )


def _await_finding(**kw) -> Finding:
    base = {
        "file": _RUNNER_PATH, "line": _RUNNER_LINE,
        "rule_name": "missing-await", "message": _AWAIT_MESSAGE,
    }
    base.update(kw)
    return _finding(**base)


def test_missing_await_on_a_plain_def_callee_is_killed():
    f = _await_finding()
    kept, killed = verify_findings(
        (f,), {_RUNNER_PATH: _RUNNER, _LEGS_PATH: _legs()},
    )
    assert kept == ()
    assert [k.reason for k in killed] == ["callee_not_async"]
    assert killed[0].finding is f


def test_missing_await_claim_in_the_message_alone_is_read():
    """The slug may be generic; the claim can live in the message only."""
    f = _await_finding(rule_name="async-correctness")
    kept, killed = verify_findings(
        (f,), {_RUNNER_PATH: _RUNNER, _LEGS_PATH: _legs()},
    )
    assert kept == ()
    assert [k.reason for k in killed] == ["callee_not_async"]


def test_missing_await_on_an_async_def_callee_survives():
    f = _await_finding()
    kept, killed = verify_findings(
        (f,), {_RUNNER_PATH: _RUNNER, _LEGS_PATH: _legs("async def")},
    )
    assert kept == (f,)
    assert killed == ()


def test_missing_await_on_a_callee_in_no_fetched_file_survives():
    """The definition was not fetched: inconclusive, keep."""
    f = _await_finding()
    kept, killed = verify_findings((f,), {_RUNNER_PATH: _RUNNER})
    assert kept == (f,)
    assert killed == ()


def test_missing_await_survives_when_any_same_named_def_is_async():
    """Two classes share the method name and one is async: keep."""
    other = "class Other:\n    async def _probe(self, leg):\n        return leg\n"
    f = _await_finding()
    kept, killed = verify_findings(
        (f,),
        {_RUNNER_PATH: _RUNNER, _LEGS_PATH: _legs(), "services/probe/o.py": other},
    )
    assert kept == (f,)
    assert killed == ()


@pytest.mark.parametrize("legs", [
    # a plain def that hands back a coroutine still has to be awaited
    (
        "async def _real(leg):\n    return leg\n\n"
        "class Legs:\n    def _probe(self, leg):\n        return _real(leg)\n"
    ),
    (
        "import asyncio\n\nclass Legs:\n    def _probe(self, leg):\n"
        "        return asyncio.ensure_future(leg)\n"
    ),
    (
        "from typing import Awaitable\n\nclass Legs:\n"
        "    def _probe(self, leg) -> Awaitable[bool]:\n        return leg\n"
    ),
    # rebound to something awaitable elsewhere in the fetched files
    (
        "class Legs:\n    def _probe(self, leg):\n        return leg\n\n"
        "def patch(a):\n    a._probe = make_async(a._probe)\n"
    ),
])
def test_missing_await_survives_a_plain_def_that_may_return_an_awaitable(legs):
    f = _await_finding()
    kept, killed = verify_findings(
        (f,), {_RUNNER_PATH: _RUNNER, _LEGS_PATH: legs},
    )
    assert kept == (f,)
    assert killed == ()


def test_missing_await_on_a_non_python_anchor_survives():
    f = _await_finding(file="web/src/run.ts", line=1)
    kept, killed = verify_findings(
        (f,), {"web/src/run.ts": "const ok = a._probe(a.paths[0]);\n",
               _LEGS_PATH: _legs()},
    )
    assert kept == (f,)
    assert killed == ()


# --- redaction_artifact: a finding built on Grug's own input redaction.
# The env-secret pattern rewrote a test lambda's `api_key: fake_achievements`
# to `api_key: [REDACTED:env-secret]`, and Elder posted two high findings
# (a NameError, and "str has no .get") against a test that passes. ---------

_TEST_PATH = "tests/test_achievements.py"
_TEST_SOURCE = (
    "from unittest import mock\n"
    "\n"
    "fake_achievements = {1: ['a']}\n"
    "\n"
    "def test_sync():\n"
    "    client = mock.Mock()\n"
    "    client.side_effect = lambda steam_id, app_id, api_key: fake_achievements.get(app_id, [])\n"
    "    assert client(1, 1, 'k') == ['a']\n"
)
_TEST_LINE = 7


def _redaction_finding(**kw) -> Finding:
    base = {
        "file": _TEST_PATH, "line": _TEST_LINE,
        "rule_name": "test-fidelity",
        "message": (
            "`[REDACTED:env-secret].get(app_id, [])` calls .get on a str, "
            "so the side_effect raises AttributeError"
        ),
    }
    base.update(kw)
    return _finding(**base)


def test_finding_built_on_the_redaction_placeholder_is_killed():
    f = _redaction_finding()
    kept, killed = verify_findings((f,), {_TEST_PATH: _TEST_SOURCE})
    assert kept == ()
    assert [k.reason for k in killed] == ["redaction_artifact"]
    assert killed[0].finding is f


def test_secret_class_finding_citing_a_placeholder_survives():
    f = _redaction_finding(
        rule_name="secret-in-log-or-trace",
        message="`[REDACTED:github-pat]` is written to the trace output",
    )
    kept, killed = verify_findings((f,), {_TEST_PATH: _TEST_SOURCE})
    assert kept == (f,)
    assert killed == ()


def test_placeholder_that_really_is_in_the_head_file_survives():
    """The code itself carries the placeholder text: not our artifact."""
    source = _TEST_SOURCE.replace(
        "fake_achievements.get(app_id, [])", "'[REDACTED:env-secret]'.get(app_id, [])",
    )
    f = _redaction_finding()
    kept, killed = verify_findings((f,), {_TEST_PATH: source})
    assert kept == (f,)
    assert killed == ()


@pytest.mark.parametrize("line", [
    # a quoted literal is a real secret, whatever the rule says
    "    client.side_effect = lambda steam_id, app_id, api_key='hunter2222xy': None\n",
    # a key=value inside a string is a real secret too
    "    client.side_effect = 'password=hunter2222xy'\n",
    # nothing near the anchor matches the redaction pattern at all
    "    client.side_effect = lambda steam_id, app_id, key: fake.get(app_id, [])\n",
])
def test_placeholder_over_a_literal_or_unmatched_line_survives(line):
    source = _TEST_SOURCE.splitlines(keepends=True)
    source[_TEST_LINE - 1] = line
    f = _redaction_finding()
    kept, killed = verify_findings((f,), {_TEST_PATH: "".join(source)})
    assert kept == (f,)
    assert killed == ()
