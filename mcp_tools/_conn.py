"""Shared SQLite connection helper for the MCP server.

The MCP server is a long-lived process; we keep one connection per server
instance and rely on SQLite's thread-safety mode. Read-only queries dominate,
so we open the DB with `?mode=ro` to make accidental writes impossible from
MCP tool handlers.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from threading import Lock

_DEFAULT_DB = Path(__file__).resolve().parent.parent / "tg_memory.db"

_conn: sqlite3.Connection | None = None
_lock = Lock()


def db_path() -> str:
    return os.environ.get("TG_FETCHER_DB", str(_DEFAULT_DB))


def get_conn() -> sqlite3.Connection:
    """Return a process-wide read-only connection."""
    global _conn
    with _lock:
        if _conn is None:
            uri = f"file:{db_path()}?mode=ro"
            _conn = sqlite3.connect(
                uri,
                uri=True,
                check_same_thread=False,
                isolation_level=None,
            )
            _conn.row_factory = sqlite3.Row
        return _conn
