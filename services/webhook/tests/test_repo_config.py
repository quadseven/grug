"""Unit tests for the pure `.grug.yaml` parser (personas/code_reviewer/repo_config)."""
from __future__ import annotations

from personas.code_reviewer import repo_config as rc


def test_missing_or_blank_file_changes_nothing():
    for text in (None, "", "   \n# only a comment\n"):
        cfg = rc.parse_repo_config(text)
        assert cfg == rc.RepoConfig()
        assert cfg.problems == ()
        assert rc.config_note(cfg) == ""


def test_all_three_keys_parse():
    cfg = rc.parse_repo_config(
        "ignore:\n  - vendor/**\n  - '*.lock'\n"
        "path_instructions:\n"
        "  - path: 'k8s/**'\n    instructions: check resource limits\n"
        "min_inline_severity: medium\n"
    )
    assert cfg.ignore == ("vendor/**", "*.lock")
    assert cfg.path_instructions == (
        rc.PathInstruction("k8s/**", "check resource limits"),
    )
    assert cfg.min_inline_severity == "medium"
    assert cfg.problems == ()


def test_invalid_yaml_is_one_note_and_otherwise_no_change():
    cfg = rc.parse_repo_config("ignore: [unclosed\n  : :\n")
    assert cfg.ignore == () and cfg.path_instructions == ()
    assert cfg.min_inline_severity is None
    assert len(cfg.problems) == 1
    note = rc.config_note(cfg)
    assert note.count(".grug.yaml") >= 1
    assert note.count("\n\n") == 1  # a single paragraph


def test_non_mapping_top_level_is_malformed():
    cfg = rc.parse_repo_config("- just\n- a list\n")
    assert len(cfg.problems) == 1


def test_unknown_keys_are_noted_once_and_known_keys_still_apply():
    cfg = rc.parse_repo_config("ignore: ['a/**']\nfoo: 1\nbar: 2\n")
    assert cfg.ignore == ("a/**",)
    assert len(cfg.problems) == 1
    assert "foo" in cfg.problems[0] and "bar" in cfg.problems[0]
    assert rc.config_note(cfg).count("\n\n") == 1


def test_bad_severity_is_ignored_with_a_note():
    cfg = rc.parse_repo_config("min_inline_severity: urgent\n")
    assert cfg.min_inline_severity is None
    assert len(cfg.problems) == 1


def test_bad_path_instruction_entries_are_skipped_good_ones_kept():
    cfg = rc.parse_repo_config(
        "path_instructions:\n"
        "  - path: 'a/**'\n    instructions: ok\n"
        "  - path: 'b/**'\n"
        "  - just a string\n"
    )
    assert cfg.path_instructions == (rc.PathInstruction("a/**", "ok"),)
    assert len(cfg.problems) == 1


def test_ignore_must_be_a_list_of_strings():
    cfg = rc.parse_repo_config("ignore: 'vendor/**'\n")
    assert cfg.ignore == ()
    assert len(cfg.problems) == 1


def test_glob_semantics():
    cfg = rc.RepoConfig(ignore=("vendor/**", "*.lock", "docs/*.md", "gen/**/out.py"))
    assert rc.is_ignored("vendor/a/b.go", cfg)
    assert rc.is_ignored("poetry.lock", cfg)
    assert rc.is_ignored("sub/dir/uv.lock", cfg)  # slash-free pattern: any depth
    assert rc.is_ignored("docs/a.md", cfg)
    assert not rc.is_ignored("docs/deep/a.md", cfg)  # * stays in one segment
    assert rc.is_ignored("gen/out.py", cfg)  # ** matches zero dirs
    assert rc.is_ignored("gen/x/y/out.py", cfg)
    assert not rc.is_ignored("src/vendor/a.go", cfg)
    assert not rc.is_ignored("src/x.py", rc.RepoConfig())


def test_instructions_block_only_for_matching_paths():
    cfg = rc.RepoConfig(path_instructions=(
        rc.PathInstruction("k8s/**", "check resource limits"),
        rc.PathInstruction("docs/**", "check links"),
    ))
    block = rc.instructions_block(["k8s/app.yaml", "src/x.py"], cfg)
    assert block is not None
    assert "check resource limits" in block and "k8s/app.yaml" in block
    assert "check links" not in block
    assert rc.instructions_block(["src/x.py"], cfg) is None


def test_inline_floor():
    cfg = rc.RepoConfig(min_inline_severity="high")
    assert not rc.meets_inline_floor("low", cfg)
    assert not rc.meets_inline_floor("medium", cfg)
    assert rc.meets_inline_floor("high", cfg)
    assert rc.meets_inline_floor("critical", cfg)
    assert rc.meets_inline_floor("low", rc.RepoConfig())
