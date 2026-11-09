"""Module layout guards (grug#1692): no name shadowing, runtime config named
for what it is, and no command module over the size cap."""

import importlib
import os
from pathlib import Path

os.environ.setdefault("DISCORD_TOKEN", "fake_token")

PKG = Path(__file__).resolve().parent.parent / "src" / "grugthink"
MAX_LINES = 600


def test_grugthink_bot_imports_by_plain_path():
    module = importlib.import_module("src.grugthink.discord_bot")
    assert module.GrugThinkBot.__module__ == "src.grugthink.discord_bot"


def test_package_attribute_resolves_to_same_class():
    from src.grugthink import GrugThinkBot

    module = importlib.import_module("src.grugthink.discord_bot")
    assert GrugThinkBot is module.GrugThinkBot


def test_runtime_config_importable_and_legacy_name_gone():
    importlib.import_module("src.grugthink.runtime_config")
    assert not (PKG / ("config_" + "legacy.py")).exists()


def test_no_module_shares_a_name_with_a_package():
    packages = {p.name for p in PKG.iterdir() if p.is_dir() and (p / "__init__.py").exists()}
    modules = {p.stem for p in PKG.glob("*.py")}
    assert packages & modules == set()
    for sub in PKG.rglob("*"):
        if sub.is_dir() and (sub / "__init__.py").exists():
            names = {p.stem for p in sub.glob("*.py")}
            subpkgs = {p.name for p in sub.iterdir() if p.is_dir() and (p / "__init__.py").exists()}
            assert names & subpkgs == set(), sub


def test_command_modules_stay_under_size_cap():
    for path in (PKG / "bot").glob("commands*.py"):
        assert len(path.read_text().splitlines()) <= MAX_LINES, path.name
