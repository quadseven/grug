"""Repo-grounded verification pass tests (#708, epic #707).

Each of the three kill classes reproduces a REAL Elder marking rejected by
the PR author on 2026-07-18 (PRs #694/#698/#706) - the regression contract
is: with verification in place, that marking dies before publication, with
a machine-readable reason. The false-kill guards pin the other direction:
verification must never kill a finding whose claim the repo evidence
actually supports (or cannot refute).
"""

from __future__ import annotations

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


# --- PR #710 review tightenings (CodeRabbit round 1) ----------------------


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
