"""Stable FIFO group identities shared by rerun queue producers."""

from __future__ import annotations

import hashlib


def _group_id(prefix: str, *parts: object) -> str:
    material = "\x1f".join(str(part) for part in parts)
    return f"{prefix}:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def review_group_id(install_id: int, repo: str, pr_number: int) -> str:
    """Serialize normal Elder reviews per pull request."""
    return _group_id("elder-review-pr", install_id, repo, pr_number)


def rerun_group_id(
    install_id: int, repo: str, pr_number: int, persona: str,
) -> str:
    """Serialize explicit reruns per pull-request persona."""
    return _group_id("rerun-pr", install_id, repo, pr_number, persona)


def ask_group_id(install_id: int, repo: str, pr_number: int) -> str:
    """Serialize questions per PR without blocking unrelated workloads."""
    return _group_id("ask-pr", install_id, repo, pr_number)
