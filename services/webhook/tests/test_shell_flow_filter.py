"""Tests for the shell control-flow post-filter (#771).

The recorded input is the real incident: Elder raised
`missing-error-handling` on line 4 of a keepalived track script whose
`if ! dig ...; then exit 1; fi` under `set -euo pipefail` IS the
exit-status check. The filter must drop that finding, keep a genuinely
unchecked command, and recognise every control-flow form the issue lists.
"""

from __future__ import annotations

import pytest
from personas.code_reviewer.persona import CodeReviewEvaluation, Finding
from personas.code_reviewer.shell_flow_filter import (
    filter_checked_shell_findings,
    is_shell_file,
    status_is_checked,
)

# Recorded from the #771 incident (check-dnsdist.sh, flagged at line 4).
_CHECK_DNSDIST = """#!/bin/bash
# keepalived track_script - 0 healthy, 1 fail
set -euo pipefail
if ! dig @127.0.0.1 google.com +time=1 +tries=1 +short >/dev/null 2>&1; then
  exit 1
fi
exit 0
"""

_UNCHECKED = """#!/bin/bash
dig @127.0.0.1 example.com +short >/dev/null
echo done
"""


def _finding(path: str, line: int, rule: str = "missing-error-handling",
             severity: str = "high") -> Finding:
    return Finding(
        file=path, line=line, severity=severity, rule_name=rule,
        message="The dig result is unchecked.", suggestion=None,
    )


def test_recorded_check_dnsdist_produces_zero_findings():
    path = "deploy/keepalived/check-dnsdist.sh"
    kept, dropped = filter_checked_shell_findings(
        (_finding(path, 4),), {path: _CHECK_DNSDIST},
    )
    assert kept == ()
    assert dropped == 1


def test_bare_unchecked_command_without_errexit_still_flags():
    path = "probe.sh"
    finding = _finding(path, 2)
    kept, dropped = filter_checked_shell_findings((finding,), {path: _UNCHECKED})
    assert kept == (finding,)
    assert dropped == 0


@pytest.mark.parametrize("script", [
    "if dig a; then\n  echo ok\nfi\n",
    "if ! dig a; then\n  exit 1\nfi\n",
    "if true; then :\nelif dig a; then\n  echo ok\nfi\n",
    "while dig a; do\n  sleep 1\ndone\n",
    "until dig a; do\n  sleep 1\ndone\n",
    "dig a && echo ok\n",
    "dig a || echo failed\n",
    "dig a || true\n",
    "dig a || exit 3\n",
    "dig a || return 2\n",
    "if out=$(dig a); then\n  echo \"$out\"\nfi\n",
    "dig a\ncase \"$?\" in\n  0) ;;\n  *) exit 1 ;;\nesac\n",
    "dig a\nrc=$?\n",
    "dig a \\\n  +short || exit 1\n",
    "set -e\ndig a\n",
    "set -o errexit\ndig a\n",
    "set -euo pipefail\ndig a | head -1\n",
])
def test_each_control_flow_form_counts_as_checked(script):
    text = "#!/bin/bash\n" + script
    line = next(
        i for i, raw in enumerate(text.split("\n"), start=1) if "dig a" in raw
    )
    assert status_is_checked(text, line), script


@pytest.mark.parametrize("script", [
    # errexit does not see a pipeline's first stage without pipefail.
    "set -e\ndig a | head -1\n",
    # `local x=$(cmd)` returns local's status, not the substitution's.
    "set -e\nf() {\n  local out=$(dig a)\n}\n",
    # a later `set +e` turns errexit back off.
    "set -e\nset +e\ndig a\n",
    # a `||` inside a string or a comment is not a check.
    "dig a 'x || y'\n",
    "dig a # || exit 1\n",
])
def test_masked_or_unchecked_forms_still_flag(script):
    text = "#!/bin/bash\n" + script
    line = next(
        i for i, raw in enumerate(text.split("\n"), start=1) if "dig a" in raw
    )
    assert not status_is_checked(text, line), script


def test_shebang_errexit_flag_counts():
    assert status_is_checked("#!/bin/bash -e\ndig a\n", 2)


def test_only_shell_files_and_only_this_rule_are_touched():
    py = "deploy.py"
    sh = "run.sh"
    contents = {py: "if not ok:\n    run()\n", sh: "set -e\ndig a\n"}
    other_rule = _finding(sh, 2, rule="missing-timeout")
    python_finding = _finding(py, 1)
    kept, dropped = filter_checked_shell_findings(
        (other_rule, python_finding), contents,
    )
    assert kept == (other_rule, python_finding)
    assert dropped == 0


def test_unknown_text_or_bad_line_fails_open():
    finding = _finding("gone.sh", 4)
    assert filter_checked_shell_findings((finding,), {}) == ((finding,), 0)
    far = _finding("run.sh", 99)
    assert filter_checked_shell_findings((far,), {"run.sh": "set -e\n"}) == (
        (far,), 0,
    )


def test_shell_detected_by_suffix_or_shebang():
    assert is_shell_file("x.sh", "")
    assert is_shell_file("bin/tool", "#!/usr/bin/env bash\n")
    assert is_shell_file("bin/tool", "#!/bin/sh\n")
    assert not is_shell_file("bin/tool", "#!/usr/bin/env python3\n")


def test_dispatch_drops_the_recorded_fp_before_publication():
    from personas.code_reviewer import dispatch as cr_dispatch

    path = "deploy/keepalived/check-dnsdist.sh"
    keep = _finding("probe.sh", 2)
    evaluation = CodeReviewEvaluation(
        findings=(_finding(path, 4), keep), conclusion="failure",
    )
    filtered = cr_dispatch._filter_checked_shell_commands(
        evaluation, {path: _CHECK_DNSDIST, "probe.sh": _UNCHECKED},
        installation_id=1, owner="o", repo_name="r", pull_number=2,
    )
    assert filtered.findings == (keep,)
