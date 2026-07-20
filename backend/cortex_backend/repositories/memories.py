"""Permanent-memory persistence boundaries for API resources."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Protocol


class MemoryRepositoryError(RuntimeError):
    """Safe failure raised by a memory repository."""


class MemoryRepository(Protocol):
    """Validated permanent-memory operations required by the API."""

    def get_memos(self) -> list[str]: ...

    def add_memo(self, memo: str) -> list[str]: ...

    def replace_memos(self, memos: list[str]) -> list[str]: ...

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

    def replace_memos(self, memos: list[str]) -> list[str]:
        self._memory.update_memos(memos)
        return self._memory.get_memos()

    def clear_memos(self) -> None:
        self._memory.clear_memos()


class InMemoryMemoryRepository:
    """Deterministic validated repository used by factory and API tests."""

    MAX_MEMOS = 100
    MAX_MEMO_LENGTH = 500

    def __init__(self, memos: list[str] | None = None):
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
        return list(self._memos)

    def add_memo(self, memo: str) -> list[str]:
        self._memos = self._normalize(self._memos + [memo])
        return self.get_memos()

    def replace_memos(self, memos: list[str]) -> list[str]:
        self._memos = self._normalize(deepcopy(memos))
        return self.get_memos()

    def clear_memos(self) -> None:
        self._memos.clear()
