"""Tests for the weakened-verification lane check (#1367).

Recorded shapes (anonymized): a lane PR flipped a capability constant and
rewrote the tests that pinned the old truth (`assertTrue` to `assertFalse`,
`assertNotIn` to `assertIn`); another deleted an operator-pinned test that a
behavior never posts a comment; two rewrote a leak-scan workflow to drop its
deny-list layer, its role secret and the `id-token: write` grant. Elder
reviewed each as a test edit.
"""

from __future__ import annotations

import itertools
import time

import pytest
from personas.code_reviewer import weakened_verification as wv
from personas.code_reviewer.criteria_coverage import LinkedCriterion
from personas.code_reviewer.diff_parser import file_changes, parse_diff
from personas.code_reviewer.lane_work import lane_work_findings
from personas.code_reviewer.linked_issue import RULE_CATEGORIES

_TITLE = "Show each raider's item level on the raid card"
_CRITERIA = (
    LinkedCriterion(7, 1, "The raid card shows a row per raider with average item level."),
)


def _diff(path: str, *hunks: tuple[str, list[str]]) -> str:
    """A modified file; each hunk is (header suffix, body lines with their
    +/-/space prefix). Line numbers are derived from the bodies."""
    out = f"diff --git a/{path} b/{path}\nindex 1111111..2222222 100644\n--- a/{path}\n+++ b/{path}\n"
    old = new = 10
    for context, body in hunks:
        n_old = sum(1 for ln in body if not ln.startswith("+"))
        n_new = sum(1 for ln in body if not ln.startswith("-"))
        out += f"@@ -{old},{n_old} +{new},{n_new} @@{context}\n" + "".join(f"{ln}\n" for ln in body)
        old += n_old + 40
        new += n_new + 40
    return out


def _scan(diff: str, *, title: str = _TITLE, criteria=_CRITERIA):
    return wv.scan_weakened_verification(
        file_changes(diff), parse_diff(diff), criteria=criteria, pr_title=title,
    )


_FLIPPED = _diff("tests/test_raidrun.py", ("", [
    "     def test_the_module_holds(self):",
    "-        self.assertTrue(raidrun.CLEARS)",
    "+        self.assertFalse(raidrun.CLEARS)",
    "         line = raidrun.run_line()",
    '-        self.assertNotIn("does not clear", line)',
    '-        self.assertIn("runs back after a wipe", line)',
    '+        self.assertIn("does not clear", line)',
    '+        self.assertNotIn("runs back after a wipe", line)',
    "-        assert not raidrun.GATHERS",
    "+        assert raidrun.GATHERS",
]))


def test_flipped_assertions_the_ticket_does_not_name_are_high():
    (f,) = _scan(_FLIPPED)
    assert (f.rule_name, f.severity) == ("weakened-test", "high")
    assert f.file == "tests/test_raidrun.py"
    assert "flips 4 assertion(s)" in f.message
    assert "self.assertTrue(raidrun.CLEARS)" in f.message


def test_a_lone_flip_is_left_to_the_model():
    # One flipped assertion with nothing deleted was as common in good lane
    # work (a behavior the ticket changed) as in damaging work.
    diff = _diff("tests/test_bags.py", ("", [
        "     def test_a_trip_makes_room(self):",
        "-        self.assertTrue(",
        "+        self.assertFalse(",
        "             makes_room(FAMILY, LOOTED)",
    ]))
    assert _scan(diff) == ()


def test_flips_the_ticket_names_are_not_flagged():
    title = 'The run line says "does not clear" and "runs back after a wipe" is gone; CLEARS and GATHERS flip'
    assert _scan(_FLIPPED, title=title) == ()


def test_a_true_to_false_expected_value_is_a_flip():
    diff = _diff("tests/test_flags.py", ("", [
        "     def test_flag(self):",
        "-        self.assertEqual(config.ENABLED, True)",
        "+        self.assertEqual(config.ENABLED, False)",
        "-        self.assertEqual(config.STRICT, True)",
        "+        self.assertEqual(config.STRICT, False)",
    ]))
    (f,) = _scan(diff)
    assert "flips 2 assertion(s)" in f.message


_DELETED = _diff("services/webhook/tests/test_rerun.py", (" def test_shutdown_sweep(monkeypatch):", [
    "     rerun._unregister_board_note(token)",
    " ",
    " ",
    "-def test_learn_outcomes_never_post_a_comment(monkeypatch):",
    '-    """Every learn outcome is a reaction on the reply."""',
    "+def test_learn_acks_post_collapsible_comment(monkeypatch):",
    '+    """Every learn outcome posts an acknowledgment comment."""',
    "     posted = []",
]), (" def test_learn_outcomes_never_post_a_comment(monkeypatch):", [
    '     rerun._run_learn(11, "o/r", 7, 5001, 4000, "reply")',
    '-    assert posted and all(url.endswith("/reactions") for url, body in posted)',
    '+    assert [b for (u, b) in posted if u.endswith("/replies")]',
]))


def test_one_deleted_test_the_ticket_does_not_name_is_medium():
    # The def goes in one hunk and its assertion in the next, which git
    # labels with the removed test as its enclosing function. One deleted
    # test was as common in good lane work as in damaging work: medium.
    (f,) = _scan(_DELETED, title="Learnings are stored and applied to later reviews")
    assert (f.rule_name, f.severity) == ("weakened-test", "medium")
    assert "`test_learn_outcomes_never_post_a_comment`" in f.message
    assert "deletes 1 test(s)" in f.message


_DELETED_TWO = _diff("tests/test_tokens.py", ("", [
    "-import tokens",
    "+import tokens  # the control plane reads the token now",
    " ",
    " ",
    "-def test_the_token_is_never_logged():",
    "-    assert 'secret' not in tokens.log_line()",
    "-",
    "-",
    "-def test_a_missing_token_fails_the_deploy():",
    "-    assert tokens.deploy(token=None) == 1",
    "-",
    "-",
    " def test_kept():",
    "     assert tokens.ok()",
]))


# Replay case weakened-tests-01 (assay suites/loom_gates): the same deleted
# test, and the diff also deletes the docstring that stated the rule it
# pinned.
_RULE_GOES = _diff("service/rerun.py", ("", [
    " def ack_learn(post, verdict):",
    '-    """Every learn outcome is a reaction, never a comment."""',
    '+    """Every learn outcome is a reaction plus an acknowledgment comment."""',
    "     post('reactions', {'content': '+1' if verdict['durable'] else 'eyes'})",
    "+    post('replies', {'body': 'Markings remembered'})",
]))

# Replay case weakened-tests-02: a give-up guard and the test that pinned it
# go in one diff, on a ticket that asked for a sooner retry.
_GUARD_GOES = _diff("gear.py", ("", [
    "-MAX_TRIES = 3",
    "+RETRY_SECONDS = 7200",
    " ",
    " ",
]), ("", [
    "     for row in rows:",
    "-        if tries.get(row['item'], 0) >= MAX_TRIES:",
    "-            continue  # held: said once, never retried silently",
    "         out.append(row)",
])) + _diff("tests/test_gear.py", ("", [
    " from gear import equips_to_queue",
    "-",
    "-",
    "-def test_after_three_tries_it_is_held_and_said():",
    "-    rows = [{'item': 7}]",
    "-    assert equips_to_queue(rows, {7: 3}) == []",
    " ",
    " ",
    " def test_first_try_queues():",
    "     assert equips_to_queue([{'item': 7}], {}) == [{'item': 7}]",
    "+",
    "+",
    "+def test_held_equip_is_tried_again():",
    "+    assert equips_to_queue([{'item': 7}], {7: 3}) == [{'item': 7}]",
]))


def test_a_deleted_test_that_goes_with_the_rule_it_pinned_is_high():
    title = "Learnings: reply-mined review preferences applied to later reviews"
    (f,) = _scan(_DELETED + _RULE_GOES, title=title)
    assert (f.rule_name, f.severity) == ("weakened-test", "high")
    assert "also deletes the production lines `test_learn_outcomes_never_post_a_comment`" in f.message


def test_a_deleted_test_that_goes_with_its_give_up_guard_is_high():
    criteria = (
        LinkedCriterion(9, 1, "A held equip is tried again within 2 hours."),
        LinkedCriterion(9, 2, "A refused equip backs off in stages."),
    )
    (f,) = _scan(_GUARD_GOES, title="A held equip is tried again within 2 hours", criteria=criteria)
    assert (f.rule_name, f.severity) == ("weakened-test", "high")
    assert "`test_after_three_tries_it_is_held_and_said`" in f.message


def test_code_that_goes_without_a_limit_word_leaves_a_lone_deletion_medium():
    # The deleted code shares the test's words but states no limit: a
    # helper replaced on purpose, not a guard.
    helper = _diff("service/rerun.py", ("", [
        "-def learn_outcomes_post(comment):",
        "-    return comment",
        " ",
    ]))
    (f,) = _scan(_DELETED + helper, title="Learnings are stored and applied to later reviews")
    assert f.severity == "medium"


def test_a_guard_removed_on_a_removal_ticket_is_medium():
    (f,) = _scan(_GUARD_GOES, title="Remove the give-up hold on equips")
    assert f.severity == "medium"


def test_two_deleted_tests_the_ticket_does_not_name_are_high():
    (f,) = _scan(_DELETED_TWO, title="Settings come from the control plane")
    assert (f.rule_name, f.severity) == ("weakened-test", "high")
    assert "deletes 2 test(s)" in f.message


def test_a_deleted_test_the_ticket_names_is_medium():
    title = "Learn outcomes post a comment instead of never posting one"
    (f,) = _scan(_DELETED, title=title)
    assert f.severity == "medium"


def test_a_deleted_test_on_a_removal_ticket_is_medium():
    (f,) = _scan(_DELETED, title="Retire the reaction-only acknowledgment")
    assert f.severity == "medium"
    assert "asks for a removal" in f.message


def test_an_adjective_is_not_a_removal_ticket():
    (f,) = _scan(_DELETED_TWO, title="Settings are listable and deletable")
    assert f.severity == "high"


def test_a_renamed_test_whose_body_stays_is_not_deleted():
    diff = _diff("tests/test_station.py", ("", [
        "-    def test_build_work_comes_first(self):",
        "+    def test_build_work_comes_first_while_shipped_work_is_young(self):",
        "         self.assertIn('Issue: 1', self.text())",
    ]))
    assert _scan(diff) == ()


def test_a_close_rename_with_a_reworded_assertion_is_not_deleted():
    diff = _diff("tests/test_view.py", ("", [
        "-    def test_every_operator_route_has_a_form(self):",
        "+    def test_every_operator_action_has_a_form(self):",
        "         used = forms()",
        '-        self.assertEqual(used, routes - {"/api/frame"})',
        '+        self.assertEqual(used, routes - {"/api/frame", "/api/watch"})',
    ]))
    assert _scan(diff) == ()


# Tests moved between files in one diff (#1496). A split test file removes
# tests from one file and adds them to others; they are not deleted.
_UNIT_BODY = {
    "unittest": lambda n: [
        f"    def test_{n}(self):",
        f"        self.assertEqual(widget.{n}(), 1)",
        f"        self.assertTrue(widget.{n}_ok())",
    ],
    "pytest": lambda n: [
        f"def test_{n}():",
        f"    assert widget.{n}() == 1",
        f"    assert widget.{n}_ok()",
    ],
}
_NAMES = ("alpha_is_stable", "beta_is_stable", "gamma_is_stable")


def _split(kind, removed, added, *, added_path="tests/test_part.py"):
    """`removed` tests leave tests/test_big.py; `added` tests join `added_path`."""
    body = _UNIT_BODY[kind]
    gone = [f"-{ln}" for n in removed for ln in body(n)]
    came = [f"+{ln}" for n in added for ln in body(n)]
    return _diff("tests/test_big.py", ("", gone)) + _diff(added_path, ("", came))


def test_a_deleted_test_file_is_medium_even_when_tests_appear_elsewhere():
    diff = (
        "diff --git a/tests/test_old.py b/tests/test_old.py\ndeleted file mode 100644\n"
        "index 1111111..0000000\n--- a/tests/test_old.py\n+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n-def test_x():\n-    assert x()\n"
        + _diff("tests/test_new.py", ("", ["+def test_x():", "+    assert x()"]))
    )
    (f,) = _scan(diff)
    assert f.severity == "medium"
    assert "`tests/test_old.py`" in f.message


@pytest.mark.parametrize("kind", ["unittest", "pytest"])
def test_tests_moved_to_another_file_with_the_same_name_are_not_deleted(kind):
    assert _scan(_split(kind, _NAMES, _NAMES)) == ()


@pytest.mark.parametrize("kind", ["unittest", "pytest"])
def test_tests_moved_under_new_names_with_the_same_body_are_not_deleted(kind):
    body = _UNIT_BODY[kind]
    gone = [f"-{ln}" for n in _NAMES[:2] for ln in body(n)]
    came = [
        f"+{ln}" for n in _NAMES[:2]
        for ln in body(n)
    ]
    # Rename: keep the body (same normalized assertions), change the name.
    came = [ln.replace("_is_stable", "_holds_up_under_load_xyz", 1) if "def " in ln else ln
            for ln in came]
    diff = _diff("tests/test_big.py", ("", gone)) + _diff("tests/test_part.py", ("", came))
    assert _scan(diff) == ()


@pytest.mark.parametrize("kind", ["unittest", "pytest"])
def test_a_removed_test_with_no_counterpart_in_the_diff_is_still_marked(kind):
    diff = _split(kind, _NAMES, _NAMES[:1])
    (f,) = _scan(diff)
    assert f.rule_name == "weakened-test"
    assert "deletes 2 test(s)" in f.message
    assert "`test_beta_is_stable`" in f.message
    assert "`test_gamma_is_stable`" in f.message
    assert "`test_alpha_is_stable`" not in f.message


@pytest.mark.parametrize("kind", ["unittest", "pytest"])
def test_a_move_that_weakens_the_test_is_still_marked(kind):
    body = _UNIT_BODY[kind]
    gone = [f"-{ln}" for n in _NAMES[:2] for ln in body(n)]
    # The same names come back with the second assertion dropped.
    came = [f"+{ln}" for n in _NAMES[:2] for ln in body(n)[:-1]]
    diff = _diff("tests/test_big.py", ("", gone)) + _diff("tests/test_part.py", ("", came))
    (f,) = _scan(diff)
    assert "deletes 2 test(s)" in f.message


def test_the_same_body_under_a_new_name_with_fewer_assertions_is_still_marked():
    body = _UNIT_BODY["pytest"]
    gone = [f"-{ln}" for ln in body("alpha_is_stable")]
    came = ["+def test_something_else_entirely():", "+    assert widget.alpha_is_stable() == 1"]
    diff = _diff("tests/test_big.py", ("", gone)) + _diff("tests/test_part.py", ("", came))
    (f,) = _scan(diff)
    assert "`test_alpha_is_stable`" in f.message


def test_a_move_of_some_tests_marks_only_the_ones_left_behind():
    (f,) = _scan(_split("pytest", _NAMES, _NAMES[:2]))
    assert "deletes 1 test(s)" in f.message
    assert "`test_gamma_is_stable`" in f.message


def _pytest_test(name, *asserts, decorators=(), pre=()):
    return [*decorators, f"def test_{name}():", *pre, *(f"    {a}" for a in asserts)]


def _gone(*tests):
    return [f"-{ln}" for t in tests for ln in t]


def _came(*tests):
    return [f"+{ln}" for t in tests for ln in t]


_TWO = ("assert shared.check() == 1", "assert shared.ok()")


def test_a_same_name_test_with_an_unrelated_body_elsewhere_does_not_excuse():
    diff = _diff("tests/test_big.py", ("", _gone(_pytest_test("basic", *_TWO)))) + _diff(
        "tests/test_part.py",
        ("", _came(_pytest_test("basic", "assert other.thing() == 2", "assert other.more()"))),
    )
    (f,) = _scan(diff)
    assert "`test_basic`" in f.message


def test_a_same_name_test_sharing_half_the_assertions_excuses():
    diff = _diff("tests/test_big.py", ("", _gone(_pytest_test("basic", *_TWO)))) + _diff(
        "tests/test_part.py",
        ("", _came(_pytest_test("basic", _TWO[0], "assert other.more()"))),
    )
    assert _scan(diff) == ()


def test_a_reformatted_copy_in_the_same_file_does_not_excuse():
    # Only another file's test counts as a counterpart; the in-file rules are
    # unchanged. The copy differs in spacing, so the file still loses a line.
    compact = ("assert shared.check()==1", "assert shared.ok()")
    diff = _diff(
        "tests/test_big.py",
        ("", _gone(_pytest_test("alpha_is_stable", *_TWO))),
        ("", _came(_pytest_test("unrelated_thing_entirely", *compact))),
    )
    (f,) = _scan(diff)
    assert "`test_alpha_is_stable`" in f.message


def test_a_renamed_test_with_the_same_assertions_in_another_file_excuses():
    diff = _diff("tests/test_big.py", ("", _gone(_pytest_test("alpha_is_stable", *_TWO)))) + _diff(
        "tests/test_part.py", ("", _came(_pytest_test("unrelated_thing_entirely", *_TWO))),
    )
    assert _scan(diff) == ()


def test_one_counterpart_excuses_only_one_removed_test():
    diff = _diff(
        "tests/test_big.py",
        ("", _gone(_pytest_test("alpha_is_stable", *_TWO), _pytest_test("beta_is_stable", *_TWO))),
    ) + _diff("tests/test_part.py", ("", _came(_pytest_test("alpha_is_stable", *_TWO))))
    (f,) = _scan(diff)
    assert "deletes 1 test(s)" in f.message
    assert "`test_beta_is_stable`" in f.message


@pytest.mark.parametrize("variant", ["unittest", "mark_skip", "xfail", "call", "skipif"])
def test_a_skipped_counterpart_does_not_excuse(variant):
    decorators, pre = {
        "unittest": (['@unittest.skip("later")'], []),
        "mark_skip": (["@pytest.mark.skip"], []),
        "xfail": (["@pytest.mark.xfail"], []),
        "skipif": (['@pytest.mark.skipif(True, reason="x")'], []),
        "call": ([], ['    pytest.skip("later")']),
    }[variant]
    diff = _diff("tests/test_big.py", ("", _gone(_pytest_test("alpha_is_stable", *_TWO)))) + _diff(
        "tests/test_part.py",
        ("", _came(_pytest_test("alpha_is_stable", *_TWO, decorators=decorators, pre=pre))),
    )
    (f,) = _scan(diff)
    assert "`test_alpha_is_stable`" in f.message


def test_a_same_name_test_seen_only_as_context_does_not_excuse():
    diff = _diff("tests/test_big.py", ("", _gone(_pytest_test("alpha_is_stable", *_TWO)))) + _diff(
        "tests/test_part.py",
        ("", [f" {ln}" for ln in _pytest_test("alpha_is_stable", *_TWO)] + ["+X = 1"]),
    )
    (f,) = _scan(diff)
    assert "`test_alpha_is_stable`" in f.message


def test_a_single_assertion_needs_the_same_name_to_be_excused():
    one = "assert widget.ready()"
    diff = _diff("tests/test_big.py", ("", _gone(_pytest_test("alpha_is_stable", one)))) + _diff(
        "tests/test_part.py", ("", _came(_pytest_test("unrelated_thing_entirely", one))),
    )
    (f,) = _scan(diff)
    assert "`test_alpha_is_stable`" in f.message
    same_name = _diff("tests/test_big.py", ("", _gone(_pytest_test("alpha_is_stable", one)))) + _diff(
        "tests/test_part.py", ("", _came(_pytest_test("alpha_is_stable", one))),
    )
    assert _scan(same_name) == ()


def test_a_counterpart_that_only_verifies_mock_calls_does_not_excuse():
    mock = ("fake.assert_called_once()", "fake.assert_not_called()")
    diff = _diff("tests/test_big.py", ("", _gone(_pytest_test("alpha_is_stable", *_TWO)))) + _diff(
        "tests/test_part.py", ("", _came(_pytest_test("alpha_is_stable", *mock))),
    )
    assert _scan(diff)
    both = _diff("tests/test_big.py", ("", _gone(_pytest_test("alpha_is_stable", *mock)))) + _diff(
        "tests/test_part.py", ("", _came(_pytest_test("alpha_is_stable", *mock))),
    )
    assert _scan(both) == ()


def test_a_mock_only_counterpart_does_not_excuse_a_test_with_real_assertions():
    removed = _pytest_test("alpha_is_stable", "fake.assert_called_once()", "assert widget.ready()")
    came = _pytest_test("alpha_is_stable", "fake.assert_called_once()", "fake.assert_not_called()")
    diff = _diff("tests/test_big.py", ("", _gone(removed))) + _diff(
        "tests/test_part.py", ("", _came(came)),
    )
    (f,) = _scan(diff)
    assert "`test_alpha_is_stable`" in f.message


def test_a_same_name_test_sharing_under_half_the_assertions_does_not_excuse():
    four = tuple(f"assert widget.f{i}() == {i}" for i in range(4))
    other = (four[0], "assert q.a()", "assert q.b()", "assert q.c()")
    diff = _diff("tests/test_big.py", ("", _gone(_pytest_test("alpha_is_stable", *four)))) + _diff(
        "tests/test_part.py", ("", _came(_pytest_test("alpha_is_stable", *other))),
    )
    (f,) = _scan(diff)
    assert "`test_alpha_is_stable`" in f.message


def test_the_marking_does_not_depend_on_the_order_of_the_files():
    parts = [
        _diff("tests/test_a.py", ("", _gone(
            _pytest_test("one_is_stable", *_TWO), _pytest_test("two_is_stable", *_TWO),
            _pytest_test("three_is_stable", "assert z.a() == 1", "assert z.b()")))),
        _diff("tests/test_b.py", ("", _came(_pytest_test("one_is_stable", *_TWO)))),
        _diff("tests/test_c.py", ("", _came(_pytest_test("renamed_other_thing", *_TWO)))),
    ]
    seen = set()
    for order in itertools.permutations(parts):
        found = _scan("".join(order))
        seen.add(tuple(sorted(f.message for f in found)))
    assert len(seen) == 1
    (messages,) = seen
    assert len(messages) == 1
    assert "`test_three_is_stable`" in messages[0]


def test_the_move_check_stays_fast_on_a_large_diff():
    n = 4000
    gone = _gone(*(_pytest_test(f"case_{i}", f"assert w.f{i}() == 1", f"assert w.g{i}()") for i in range(n)))
    unmatched = _came(*(_pytest_test(f"other_{i}", f"assert v.f{i}() == 2", f"assert v.h{i}()") for i in range(n)))
    matched = _came(*(_pytest_test(f"case_{i}", f"assert w.f{i}() == 1", f"assert w.g{i}()") for i in range(n)))
    start = time.monotonic()
    assert _scan(_diff("tests/test_big.py", ("", gone)) + _diff("tests/test_part.py", ("", unmatched)))
    assert _scan(_diff("tests/test_big.py", ("", gone)) + _diff("tests/test_part.py", ("", matched))) == ()
    assert time.monotonic() - start < 5


def test_a_cut_short_hunk_is_not_evaluated():
    diff = _DELETED.replace(
        '+    assert [b for (u, b) in posted if u.endswith("/replies")]',
        '+    assert [b for (u, b) in posted if u.endswith("/replies")]\n\\ truncated: 29 more lines',
    )
    assert _scan(diff) == ()


def test_a_deleted_test_file_is_medium():
    diff = (
        "diff --git a/tests/test_old.py b/tests/test_old.py\ndeleted file mode 100644\n"
        "index 1111111..0000000\n--- a/tests/test_old.py\n+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n-def test_x():\n-    assert x()\n"
        + _diff("app.py", ("", ["-x = 1", "+x = 2"]))
    )
    (f,) = _scan(diff)
    assert (f.severity, f.file) == ("medium", "app.py")
    assert "`tests/test_old.py`" in f.message


_LEAK_SCAN = _diff(".github/workflows/check.leak-scan.yml", ("", [
    "     uses: example/shared/.github/workflows/_reusable.leak-scan.yml@0123456789abcdef0123456789abcdef01234567",
    "     permissions:",
    "       contents: read",
    "-      # Mint the OIDC token for the deny-list read role.",
    "-      id-token: write",
    "     with:",
    "-      allowed-repo-refs: shared, other",
    "+      allowed-repo-refs: shared",
    "       enforce: false",
    "-      deny-list-param: /example/terms",
    "-    secrets:",
    "-      aws-role-arn: ${{ secrets.SCAN_ROLE_ARN }}",
]))


def test_a_workflow_that_drops_its_deny_list_layer_is_high():
    title = "Run the shared leak scan on every PR"
    (f,) = _scan(_LEAK_SCAN, title=title, criteria=())
    assert (f.rule_name, f.severity) == ("removed-verification-layer", "high")
    assert f.file == ".github/workflows/check.leak-scan.yml"
    assert "`deny-list-param: /example/terms`" in f.message
    assert "`id-token: write`" in f.message
    assert "SCAN_ROLE_ARN" in f.message


def test_a_ticket_that_asks_to_remove_the_layer_is_medium():
    (f,) = _scan(_LEAK_SCAN, title="Remove the deny-list layer from the leak scan", criteria=())
    assert f.severity == "medium"


def test_re_pinning_a_scanner_action_is_not_a_removal():
    diff = _diff(".github/workflows/codeql.yml", ("", [
        "     steps:",
        "-      - uses: github/codeql-action/analyze@v3",
        "+      - uses: github/codeql-action/analyze@v4",
        "-        timeout-minutes: 10",
        "+        timeout-minutes: 20",
    ]))
    assert _scan(diff, criteria=()) == ()


def test_a_secret_change_alone_is_not_a_layer():
    diff = _diff(".github/workflows/deploy.yml", ("", [
        "     with:",
        "-      token: ${{ secrets.OLD_TOKEN }}",
        "+      token: ${{ secrets.NEW_TOKEN }}",
        "-      id-token: write",
    ]))
    assert _scan(diff, criteria=()) == ()


def test_lane_work_findings_runs_the_check():
    found = lane_work_findings(
        diff_text=_LEAK_SCAN + _FLIPPED, hunks=parse_diff(_LEAK_SCAN + _FLIPPED),
        file_contents={}, base_line_counts={}, boxes=(),
        pr_title="Run the shared leak scan on every PR", pr_body="",
    )
    assert {(f.rule_name, f.severity) for f in found} == {
        ("removed-verification-layer", "high"), ("weakened-test", "high"),
    }


def test_the_rules_have_board_categories():
    assert RULE_CATEGORIES["weakened-test"] == "test fidelity"
    assert RULE_CATEGORIES["removed-verification-layer"] == "security"


# --- cost (#1367 review: linear, capped) --------------------------------------


def test_the_scan_is_linear_on_a_large_diff():
    # 2,000 deleted tests and 2,000 flips in 400 test files, plus a 20,000
    # line workflow rewrite: about 54,000 hunk lines.
    parts = []
    for i in range(400):
        body = []
        for j in range(5):
            body += [
                f"-    def test_case_{i}_{j}_pins_widget(self):",
                f"-        self.assertEqual(widget_{j}(), {j})",
                f"-        self.assertTrue(flag_{i}_{j})",
                f"+        self.assertFalse(flag_{i}_{j})",
                "         pass",
            ]
        parts.append(_diff(f"tests/test_mod_{i}.py", ("", body)))
    parts.append(_diff(".github/workflows/big.yml", ("", [f"-      step-{n}: run" for n in range(10_000)]
                                                     + [f"+      step-{n}: run2" for n in range(10_000)])))
    diff = "".join(parts)
    changes, hunks = file_changes(diff), parse_diff(diff)
    start = time.perf_counter()
    found = wv.scan_weakened_verification(changes, hunks, criteria=_CRITERIA, pr_title=_TITLE)
    elapsed = time.perf_counter() - start
    assert len(found) == 400
    assert elapsed < 1.0, elapsed


def test_past_the_line_cap_the_scan_is_not_evaluated(monkeypatch):
    monkeypatch.setattr(wv, "MAX_DIFF_LINES", 5)
    assert _scan(_FLIPPED) == ()
    assert _scan(_LEAK_SCAN, criteria=()) == ()
