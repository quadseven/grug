from personas.code_reviewer.repo_ruff_policy import (
    RepoRuffPolicy,
    parse_repo_ruff_policy,
)


def test_missing_or_malformed_config_means_no_policy():
    assert parse_repo_ruff_policy(None) == RepoRuffPolicy()
    assert parse_repo_ruff_policy("[tool.ruff\ninvalid") == RepoRuffPolicy()
    assert parse_repo_ruff_policy('tool = "invalid"') == RepoRuffPolicy()


def test_reads_select_ignore_and_per_file_ignores():
    policy = parse_repo_ruff_policy('''
[tool.ruff.lint]
select = ["B", "S608"]
extend-select = ["ASYNC"]
ignore = ["B008"]
extend-ignore = ["S101"]

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["S102"]
''')
    assert policy.drops("S608", "src/query.py")
    assert policy.drops("B008", "src/app.py")
    assert policy.drops("S102", "tests/test_exec.py")
    assert not policy.drops("S102", "src/app.py")


def test_reads_ruff_toplevel_legacy_keys():
    policy = parse_repo_ruff_policy('[tool.ruff]\nselect = ["S"]\nignore = ["S608"]\n')
    assert policy.drops("S102", "module.py")
    assert policy.drops("S608", "module.py")


def test_rule_wildcards_match_as_globs():
    policy = parse_repo_ruff_policy('[tool.ruff.lint]\nignore = ["S6*"]\n')
    assert policy.drops("S608", "src/query.py")
    assert not policy.drops("S708", "src/query.py")


def test_all_selects_every_code():
    policy = parse_repo_ruff_policy('[tool.ruff.lint]\nselect = ["ALL"]\n')
    assert policy.drops("S608", "src/query.py")


def test_empty_or_bare_star_selector_drops_nothing():
    # Repo-authored input: a pattern that is empty once its asterisks are
    # stripped must not behave like "ALL".
    policy = parse_repo_ruff_policy('[tool.ruff.lint]\nignore = ["", "*", "**"]\nselect = "S1,"\n')
    assert not policy.drops("S608", "src/query.py")
    assert policy.drops("S102", "src/app.py")
