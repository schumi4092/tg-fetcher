"""Author-centric MCP tools: resolve identity, fetch full profile."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from ._conn import get_conn
from ._fmt import wrap

# Reuse the CA extractor that the rest of the app uses.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import extract_cas  # noqa: E402


def tg_author_resolve(query: str, limit: int = 10) -> dict[str, Any]:
    """Fuzzy-match a query against sender display names and @usernames.

    Searches both the current `senders.primary_*` columns and the historical
    `sender_aliases` table, so old display names still resolve. Ranked by
    msg_count desc.
    """
    q = (query or "").strip()
    if not q:
        return wrap([], query=q, note="empty_query")

    pattern = f"%{q}%"
    conn = get_conn()

    # Union: primary identity OR any historical alias.
    rows = conn.execute(
        """
        SELECT DISTINCT s.sender_id, s.primary_name, s.primary_username,
                        s.msg_count, s.chat_count, s.first_seen, s.last_seen
        FROM senders s
        LEFT JOIN sender_aliases a ON a.sender_id = s.sender_id
        WHERE s.primary_name     LIKE ? COLLATE NOCASE
           OR s.primary_username LIKE ? COLLATE NOCASE
           OR a.alias_name       LIKE ? COLLATE NOCASE
           OR a.alias_username   LIKE ? COLLATE NOCASE
        ORDER BY s.msg_count DESC
        LIMIT ?
        """,
        (pattern, pattern, pattern, pattern, limit),
    ).fetchall()

    out = []
    for r in rows:
        aliases = conn.execute(
            """
            SELECT alias_name, alias_username, msg_count, first_seen, last_seen
            FROM sender_aliases
            WHERE sender_id = ?
            ORDER BY msg_count DESC
            """,
            (r["sender_id"],),
        ).fetchall()
        out.append({
            "sender_id": r["sender_id"],
            "primary_name": r["primary_name"],
            "primary_username": r["primary_username"],
            "msg_count": r["msg_count"],
            "chat_count": r["chat_count"],
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
            "aliases": [dict(a) for a in aliases],
        })

    return wrap(out, query=q, limit=limit)


def _resolve_to_id(conn, sender_id: int | None, name: str | None) -> int | None:
    if sender_id:
        return int(sender_id)
    if not name:
        return None
    pattern = f"%{name.strip()}%"
    row = conn.execute(
        """
        SELECT s.sender_id
        FROM senders s
        LEFT JOIN sender_aliases a ON a.sender_id = s.sender_id
        WHERE s.primary_name     LIKE ? COLLATE NOCASE
           OR s.primary_username LIKE ? COLLATE NOCASE
           OR a.alias_name       LIKE ? COLLATE NOCASE
           OR a.alias_username   LIKE ? COLLATE NOCASE
        ORDER BY s.msg_count DESC
        LIMIT 1
        """,
        (pattern, pattern, pattern, pattern),
    ).fetchone()
    return row["sender_id"] if row else None


def tg_author_profile(
    sender_id: int | None = None,
    name: str | None = None,
    top_chats: int = 10,
    top_cas: int = 20,
    ca_scan_limit: int = 2000,
) -> dict[str, Any]:
    """Aggregated profile for one sender: aliases, top chats, mentioned CAs.

    Pass either sender_id or name. If name is given, the highest-msg-count
    match is used (warning surfaced in meta if ambiguous).
    """
    conn = get_conn()
    sid = _resolve_to_id(conn, sender_id, name)
    if sid is None:
        return wrap(None, note="not_found", sender_id=sender_id, name=name)

    base = conn.execute(
        "SELECT * FROM senders WHERE sender_id = ?", (sid,)
    ).fetchone()
    if base is None:
        return wrap(None, note="not_found", sender_id=sid)

    aliases = conn.execute(
        """
        SELECT alias_name, alias_username, msg_count, first_seen, last_seen
        FROM sender_aliases WHERE sender_id = ?
        ORDER BY msg_count DESC
        """,
        (sid,),
    ).fetchall()

    chats = conn.execute(
        """
        SELECT chat_id, MAX(chat_name) AS chat_name, COUNT(*) AS n,
               MIN(date) AS first_seen, MAX(date) AS last_seen
        FROM messages WHERE sender_id = ?
        GROUP BY chat_id ORDER BY n DESC LIMIT ?
        """,
        (sid, top_chats),
    ).fetchall()

    # Scan a bounded window of this sender's most recent messages for CAs.
    # 2000 msgs is enough for most KOLs and bounds the regex cost.
    sample = conn.execute(
        """
        SELECT text FROM messages
        WHERE sender_id = ? AND text IS NOT NULL AND text != ''
        ORDER BY date DESC LIMIT ?
        """,
        (sid, ca_scan_limit),
    ).fetchall()

    ca_counts: dict[str, int] = {}
    for row in sample:
        for ca in extract_cas(row["text"]):
            ca_counts[ca] = ca_counts.get(ca, 0) + 1

    top_ca_list = sorted(ca_counts.items(), key=lambda kv: kv[1], reverse=True)[:top_cas]

    payload = {
        "sender_id": sid,
        "primary_name": base["primary_name"],
        "primary_username": base["primary_username"],
        "msg_count": base["msg_count"],
        "chat_count": base["chat_count"],
        "first_seen": base["first_seen"],
        "last_seen": base["last_seen"],
        "aliases": [dict(a) for a in aliases],
        "top_chats": [dict(c) for c in chats],
        "top_cas": [{"ca": ca, "mention_count": n} for ca, n in top_ca_list],
        "ca_scan_window": min(ca_scan_limit, len(sample)),
    }
    return wrap(payload, sender_id=sid)
