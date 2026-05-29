"""AI-friendly response wrapper.

Every tool returns:
    { "data": <payload>, "meta": { source, db, ts, row_count, truncated, ... } }

Modeled after wx-cli's meta-wrapper convention so the agent can tell at a
glance how fresh the data is, where it came from, and whether it was
truncated.
"""

from __future__ import annotations

import datetime
from typing import Any

from ._conn import db_path


def wrap(data: Any, *, truncated: bool = False, **extra: Any) -> dict[str, Any]:
    row_count: int | None
    if isinstance(data, list):
        row_count = len(data)
    elif data is None:
        row_count = 0
    else:
        row_count = None

    meta: dict[str, Any] = {
        "source": "tg-fetcher",
        "db": db_path(),
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "truncated": truncated,
    }
    if row_count is not None:
        meta["row_count"] = row_count
    meta.update(extra)

    return {"data": data, "meta": meta}


def row_to_dict(row) -> dict[str, Any]:
    """sqlite3.Row -> dict, stripping None to '' for string columns is the
    caller's responsibility — this is just structural conversion."""
    return {k: row[k] for k in row.keys()}
