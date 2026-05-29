"""
One-time backfill: populate `senders` + `sender_aliases` from existing messages.

Idempotent: safe to re-run. CREATE TABLE IF NOT EXISTS + upsert by (sender_id)
and (sender_id, alias_name, alias_username).

The existing `trusted_senders` table keeps its original whitelist semantic and
is not touched here.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "tg_memory.db"


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS senders (
    sender_id        INTEGER PRIMARY KEY,
    primary_name     TEXT NOT NULL DEFAULT '',
    primary_username TEXT NOT NULL DEFAULT '',
    msg_count        INTEGER NOT NULL DEFAULT 0,
    chat_count       INTEGER NOT NULL DEFAULT 0,
    first_seen       TEXT NOT NULL DEFAULT '',
    last_seen        TEXT NOT NULL DEFAULT '',
    updated_at       TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_senders_name     ON senders(primary_name);
CREATE INDEX IF NOT EXISTS idx_senders_username ON senders(primary_username);

CREATE TABLE IF NOT EXISTS sender_aliases (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id      INTEGER NOT NULL,
    alias_name     TEXT NOT NULL DEFAULT '',
    alias_username TEXT NOT NULL DEFAULT '',
    msg_count      INTEGER NOT NULL DEFAULT 0,
    first_seen     TEXT NOT NULL DEFAULT '',
    last_seen      TEXT NOT NULL DEFAULT '',
    UNIQUE(sender_id, alias_name, alias_username)
);

CREATE INDEX IF NOT EXISTS idx_aliases_sender   ON sender_aliases(sender_id);
CREATE INDEX IF NOT EXISTS idx_aliases_name     ON sender_aliases(alias_name);
CREATE INDEX IF NOT EXISTS idx_aliases_username ON sender_aliases(alias_username);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def backfill(conn: sqlite3.Connection) -> dict:
    t0 = time.time()

    # 1. Per-sender aggregates: msg_count, chat_count, first/last seen
    aggregates = conn.execute(
        """
        SELECT sender_id,
               COUNT(*)                  AS msg_count,
               COUNT(DISTINCT chat_id)   AS chat_count,
               MIN(date)                 AS first_seen,
               MAX(date)                 AS last_seen
        FROM messages
        WHERE sender_id IS NOT NULL
        GROUP BY sender_id
        """
    ).fetchall()

    # 2. Per-(sender, name, username) alias rows
    alias_rows = conn.execute(
        """
        SELECT sender_id,
               COALESCE(sender_name, '')     AS alias_name,
               COALESCE(sender_username, '') AS alias_username,
               COUNT(*)                       AS msg_count,
               MIN(date)                      AS first_seen,
               MAX(date)                      AS last_seen
        FROM messages
        WHERE sender_id IS NOT NULL
        GROUP BY sender_id, alias_name, alias_username
        """
    ).fetchall()

    # 3. For primary_name / primary_username pick the alias with the highest
    #    msg_count for that sender (most-used identity).
    primary_by_sender: dict[int, tuple[str, str]] = {}
    best_count: dict[int, int] = {}
    for sid, name, username, n, _fs, _ls in alias_rows:
        if n > best_count.get(sid, -1):
            best_count[sid] = n
            primary_by_sender[sid] = (name, username)

    # 4. Upsert senders
    senders_payload = []
    for sid, msg_count, chat_count, first_seen, last_seen in aggregates:
        name, username = primary_by_sender.get(sid, ("", ""))
        senders_payload.append((sid, name, username, msg_count, chat_count, first_seen, last_seen))

    conn.executemany(
        """
        INSERT INTO senders (sender_id, primary_name, primary_username,
                             msg_count, chat_count, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sender_id) DO UPDATE SET
            primary_name     = excluded.primary_name,
            primary_username = excluded.primary_username,
            msg_count        = excluded.msg_count,
            chat_count       = excluded.chat_count,
            first_seen       = excluded.first_seen,
            last_seen        = excluded.last_seen,
            updated_at       = datetime('now', 'localtime')
        """,
        senders_payload,
    )

    # 5. Upsert aliases
    conn.executemany(
        """
        INSERT INTO sender_aliases (sender_id, alias_name, alias_username,
                                    msg_count, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(sender_id, alias_name, alias_username) DO UPDATE SET
            msg_count  = excluded.msg_count,
            first_seen = excluded.first_seen,
            last_seen  = excluded.last_seen
        """,
        alias_rows,
    )

    conn.commit()

    return {
        "senders": len(aggregates),
        "aliases": len(alias_rows),
        "elapsed_secs": round(time.time() - t0, 2),
    }


def main() -> int:
    db_path = os.environ.get("TG_FETCHER_DB", str(DB_PATH))
    if not Path(db_path).exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(db_path)
    try:
        ensure_schema(conn)
        result = backfill(conn)
    finally:
        conn.close()

    print(f"senders backfilled: {result['senders']}")
    print(f"aliases backfilled: {result['aliases']}")
    print(f"elapsed:            {result['elapsed_secs']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
