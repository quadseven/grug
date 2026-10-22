"""Tests for the wrong-target lane-work check (#1369).

Recorded shape (anonymized): a lane PR on a ticket that asked to keep three
named workloads off a memory-pressured node added a node exclusion to two
CI runner pools instead. Its comments and its new test named the three
workloads; no changed line of configuration did. Elder passed it.
"""

from __future__ import annotations

import time

from personas.code_reviewer import wrong_target
from personas.code_reviewer.criteria_coverage import LinkedCriterion
from personas.code_reviewer.diff_parser import file_changes, parse_diff
from personas.code_reviewer.lane_work import lane_work_findings
from personas.code_reviewer.wrong_target import scan_wrong_target, ticket_targets

_FOOTER = "\n---\nLoom ticket example.iac-12-r1, lane codex, model gpt-6-luna.\nRefs example/iac#12\n"
_TITLE = "Keep workloads off the memory-pressured node"
_BOXES = (
    (12, 1, "open", "shop-api, shop-webhook, and shop-worker exclude the small node while retaining their arm64 requirements."),
    (12, 2, "open", "Deployment configuration validation succeeds."),
)

_POOL_OLD = ["template:", "  spec:", "    nodeSelector:", "      kubernetes.io/arch: arm64"]
_AFFINITY = [
    "    # shop-api, shop-webhook and shop-worker deploys run on this pool;",
    "    # keeping the pool off the small node keeps them off it too.",
    "    affinity:",
    "      nodeAffinity:",
    "        requiredDuringSchedulingIgnoredDuringExecution:",
    "          nodeSelectorTerms:",
    "            - matchExpressions:",
    "                - key: kubernetes.io/hostname",
    "                  operator: NotIn",
    "                  values: [small-node-01]",
]
_TEST = [
    '"""The shop-api, shop-webhook and shop-worker deploy pool avoids the small node."""',
    "def test_pool_excludes_small_node():",
    "    assert 'small-node-01' in open('values.yaml').read()",
]


def _added_file(path: str, lines: list[str]) -> str:
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\nindex 0000000..1111111\n"
        f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n"
        + "".join(f"+{ln}\n" for ln in lines)
    )


def _appended(path: str, old: list[str], new: list[str]) -> str:
    return (
        f"diff --git a/{path} b/{path}\nindex 2222222..3333333 100644\n"
        f"--- a/{path}\n+++ b/{path}\n@@ -1,{len(old)} +1,{len(old) + len(new)} @@\n"
        + "".join(f" {ln}\n" for ln in old)
        + "".join(f"+{ln}\n" for ln in new)
    )


def _recorded_diff() -> str:
    return (
        _appended("manifests/arc-runners/values.yaml", _POOL_OLD, _AFFINITY)
        + _added_file("manifests/arc-runners/tests/test_small_node.py", _TEST)
    )


def _findings(diff: str, *, boxes=_BOXES, title=_TITLE, body="## Why\nMemory pressure.\n" + _FOOTER):
    return lane_work_findings(
        diff_text=diff, hunks=parse_diff(diff), file_contents={},
        base_line_counts={}, boxes=boxes, pr_title=title, pr_body=body,
    )


def _scan(diff: str, texts: list[str], title: str = _TITLE):
    criteria = tuple(LinkedCriterion(12, n, t) for n, t in enumerate(texts, start=1))
    return scan_wrong_target(file_changes(diff), parse_diff(diff), criteria=criteria, pr_title=title)


# --- the recorded pull request ------------------------------------------------


def test_recorded_lane_pr_gets_a_high_intent_mismatch_naming_the_missed_targets():
    found = [f for f in _findings(_recorded_diff()) if f.rule_name == "intent-mismatch"]
    assert len(found) == 1, found
    finding = found[0]
    assert finding.severity == "high"
    assert finding.file == "manifests/arc-runners/values.yaml"
    assert finding.line in {h for h in parse_diff(_recorded_diff())[0].new_lines}
    for name in ("shop-api", "shop-webhook", "shop-worker"):
        assert f"`{name}`" in finding.message
    assert "only in comments or tests" in finding.message


def test_changing_a_named_workload_is_on_target():
    diff = _recorded_diff() + _appended(
        "manifests/shop-api/deployment.yaml", ["spec:", "  template:"], _AFFINITY[2:],
    )
    assert _findings(diff) == ()


def test_a_named_target_in_a_changed_code_line_is_on_target():
    lines = _AFFINITY + ["    # applies to:", "    podSelector: {app: shop-worker}"]
    diff = _appended("manifests/arc-runners/values.yaml", _POOL_OLD, lines)
    assert _scan(diff, [_BOXES[0][3]]) == ()


def test_a_ticket_that_names_no_target_gets_no_finding():
    texts = ["The deploys stay off the small node.", "Validation succeeds."]
    assert _scan(_recorded_diff(), texts, title="Keep deploys off the small node") == ()


def test_a_diff_that_never_names_the_targets_is_left_to_the_reviewer():
    diff = _appended("manifests/arc-runners/values.yaml", _POOL_OLD, _AFFINITY[2:])
    assert _scan(diff, [_BOXES[0][3]]) == ()


def test_a_diff_that_changes_only_tests_and_comments_gets_no_finding():
    diff = _added_file("manifests/arc-runners/tests/test_small_node.py", _TEST)
    assert _scan(diff, [_BOXES[0][3]]) == ()


# --- what counts as a target ----------------------------------------------------


def test_backticked_identifiers_and_paths_are_targets_commands_and_phrases_are_not():
    texts = [
        "`render_queue` drains in order and `config/queue.yaml` sets the limit.",
        "`php -v` reports 8.4 and the page shows `the string`.",
        "The worker reads limits.toml at start.",
        "`ruff` passes.",
    ]
    assert ticket_targets(texts) == ["render_queue", "config/queue.yaml", "limits.toml"]


def test_a_lone_or_english_hyphenated_word_is_not_a_target():
    texts = [
        "The read-only view and the read-write view both load.",
        "A cross-user or cross-connector transplant fails.",
        "The on-demand controls load in one-line form.",
    ]
    assert ticket_targets(texts) == []


def test_live_keep_and_remove_clauses_name_no_targets():
    texts = [
        "Live: `job_events` holds a row within two hours.",
        "Verified on the handset: the stored `not_after` moves.",
        "`stream.py` retains its lifecycle.",
        "`MAX_CHANNELS` is removed from `index.html`.",
        "The signal reuses `RouterProximity` from `BondMode.kt`.",
        "`map_server.py` contains no business logic.",
    ]
    assert ticket_targets(texts) == []


def test_a_target_is_read_from_its_own_clause():
    texts = ["`queue_worker` retries a failed upload, while `uploader.py` stays unchanged."]
    assert ticket_targets(texts) == ["queue_worker"]


def test_a_test_named_as_the_target_is_met_by_the_test_file():
    texts = ["`tests/test_queue.py::test_drains_in_order` fails on main and passes."]
    test = ["def test_drains_in_order():", "    assert drain() == [1, 2]"]
    diff = _added_file("tests/test_queue.py", test) + _appended(
        "app/queue.py", ["def drain():"], ["    # test_drains_in_order covers this", "    return sorted(items)"],
    )
    assert _scan(diff, texts) == ()
    # The same diff against a target that is not a test draws the finding.
    assert len(_scan(diff, ["`drain_in_order` returns items in order."])) == 0
    named = diff + _appended("app/queue.py", ["x = 1"], ["# drain_in_order lives in the worker"])
    assert len(_scan(named, ["`drain_in_order` returns items in order."])) == 1


def test_a_dotted_module_target_is_met_by_its_file():
    texts = ["`bus.brain` refuses a ticket in the wrong state."]
    diff = _appended("bus/brain.py", ["def merge():"], ["    refuse()"])
    assert _scan(diff, texts) == ()


# --- bounds -------------------------------------------------------------------------


def test_a_diff_past_the_scan_bound_is_not_evaluated(monkeypatch):
    monkeypatch.setattr(wrong_target, "MAX_SCAN_CHARS", 200)
    assert _scan(_recorded_diff(), [_BOXES[0][3]]) == ()
    monkeypatch.setattr(wrong_target, "MAX_SCAN_CHARS", 4_000_000)
    assert len(_scan(_recorded_diff(), [_BOXES[0][3]])) == 1


def test_too_many_targets_is_not_evaluated(monkeypatch):
    monkeypatch.setattr(wrong_target, "MAX_TARGETS", 2)
    assert _scan(_recorded_diff(), [_BOXES[0][3]]) == ()


def test_the_scan_is_linear_on_a_large_diff():
    # 400 files of 120 changed lines: 48,000 lines, with 40 targets (the cap) the
    # ticket names that no line changes.
    parts = [
        _appended(
            f"svc/mod_{i:03d}.py", ["def run():"],
            [f"    step_{n} = compute({n})  # pass {i}" for n in range(120)],
        )
        for i in range(400)
    ]
    parts.append(_added_file("tests/test_mods.py", [f"# names_target_{k}" for k in range(40)]))
    diff = "".join(parts)
    texts = [" ".join(f"`names_target_{k}`" for k in range(40)) + " are updated."]
    changes, hunks = file_changes(diff), parse_diff(diff)
    criteria = (LinkedCriterion(12, 1, texts[0]),)
    start = time.perf_counter()
    found = scan_wrong_target(changes, hunks, criteria=criteria, pr_title="Update the targets")
    elapsed = time.perf_counter() - start
    assert len(found) == 1
    assert elapsed < 1.0, elapsed
