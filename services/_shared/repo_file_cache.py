"""Bounded in-process memory of file reads at an immutable commit.

A review calls for the same repo files once per cohort, chunk and retry. A
commit sha never changes what it holds, so an answer stays true for as long
as it is kept, and that includes "this file does not exist". Only a real
answer belongs here: a timeout, 5xx or 403 says nothing about the file, so
the caller leaves that path out and asks again next time.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Hashable


class RepoFileCache:
    """Per-head map of path to text, or to None when the file is absent.
    Holds at most `max_heads` heads and evicts the least recently used.
    Safe across the consumer's concurrent reviews."""

    def __init__(self, max_heads: int) -> None:
        self._max_heads = max_heads
        self._heads: OrderedDict[Hashable, dict[str, str | None]] = OrderedDict()
        self._lock = threading.Lock()

    def known(self, head: Hashable) -> dict[str, str | None]:
        """A copy of what is already answered for `head`, empty if nothing."""
        with self._lock:
            files = self._heads.get(head)
            if files is None:
                return {}
            self._heads.move_to_end(head)
            return dict(files)

    def remember(self, head: Hashable, path: str, text: str | None) -> None:
        with self._lock:
            self._heads.setdefault(head, {})[path] = text
            self._heads.move_to_end(head)
            while len(self._heads) > self._max_heads:
                self._heads.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._heads.clear()
