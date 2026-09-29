"""SQLite failure modes the storage tests need to reproduce on demand.

Kept apart from ``support.py`` so the persistence tests share one definition of
"a volume that cannot do write-ahead logging" without touching the helpers every
other test module imports.
"""

from __future__ import annotations

import re
import sqlite3

import pytest

_WAL_REQUEST = re.compile(r"\s*PRAGMA\s+journal_mode\s*=\s*WAL\s*", re.IGNORECASE)


class NoWalConnection(sqlite3.Connection):
    """A connection to a volume that cannot provide shared memory.

    SQLite does not raise there: it answers the request for WAL with the mode
    the database is really in, which is not WAL."""

    def execute(self, sql, *args):
        if _WAL_REQUEST.fullmatch(sql):
            sql = "SELECT 'delete'"
        return super().execute(sql, *args)


def volume_without_write_ahead_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every ``sqlite3.connect`` in the process open a ``NoWalConnection``."""
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        kwargs.setdefault("factory", NoWalConnection)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
