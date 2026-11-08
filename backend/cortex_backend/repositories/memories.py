"""Permanent-memory persistence boundaries for API resources."""

from __future__ import annotations

from copy import deepcopy
from threading import RLock
from typing import Any, Protocol


class MemoryRepositoryError(RuntimeError):
    """Safe failure raised by a memory repository."""


class MemoryRepository(Protocol):
    """Validated permanent-memory operations required by the API."""

    def get_memos(self) -> list[str]: ...

    def add_memo(self, memo: str) -> list[str]: ...

    def replace_memos(
        self, memos: list[str], *, expected_memos: list[str] | None = None
    ) -> list[str]: ...

    def clear_memos(self) -> None: ...


class LegacyPermanentMemoryRepository:
    """Adapt the existing atomic JSON memory manager."""

    def __init__(self, memory_manager: Any):
        self._memory = memory_manager

    def get_memos(self) -> list[str]:
        return self._memory.get_memos()

    def add_memo(self, memo: str) -> list[str]:
        self._memory.add_memo(memo)
        return self._memory.get_memos()

    def replace_memos(
        self, memos: list[str], *, expected_memos: list[str] | None = None
    ) -> list[str]:
        self._memory.update_memos(memos, expected_memos=expected_memos)
        return self._memory.get_memos()

    def clear_memos(self) -> None:
        self._memory.clear_memos()


class InMemoryMemoryRepository:
    """Deterministic validated repository used by factory and API tests."""

    MAX_MEMOS = 100
    MAX_MEMO_LENGTH = 500

    def __init__(self, memos: list[str] | None = None):
        self._lock = RLock()
        self._memos: list[str] = []
        self.replace_memos(memos or [])

    @classmethod
    def _normalize(cls, memos: list[str]) -> list[str]:
        if not isinstance(memos, list):
            raise MemoryRepositoryError("Memos must be a list.")
        normalized: list[str] = []
        seen: set[str] = set()
        for memo in memos:
            if not isinstance(memo, str):
                raise MemoryRepositoryError("Memory entries must be strings.")
            value = memo.strip()
            if not value:
                continue
            if len(value) > cls.MAX_MEMO_LENGTH:
                raise MemoryRepositoryError("Memory entries are too long.")
            key = value.casefold()
            if key in seen:
                continue
            if len(normalized) >= cls.MAX_MEMOS:
                raise MemoryRepositoryError("Too many memories.")
            seen.add(key)
            normalized.append(value)
        return normalized

    def get_memos(self) -> list[str]:
        with self._lock:
            return list(self._memos)

    def add_memo(self, memo: str) -> list[str]:
        with self._lock:
            self._memos = self._normalize(self._memos + [memo])
            return list(self._memos)

    def replace_memos(
        self, memos: list[str], *, expected_memos: list[str] | None = None
    ) -> list[str]:
        with self._lock:
            # The editor replaces a complete list. Only the exact snapshot it
            # last loaded may be replaced; another client's new memories win.
            if expected_memos is not None and self._memos != expected_memos:
                raise ValueError(
                    "Memories changed while you were editing. Refresh and review your changes."
                )
            self._memos = self._normalize(deepcopy(memos))
            return list(self._memos)

    def clear_memos(self) -> None:
        with self._lock:
            self._memos.clear()
