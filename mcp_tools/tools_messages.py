"""Message-centric MCP tools: by-author listing and FTS full-text search."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from ._conn import get_conn
from ._fmt import wrap

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import build_fts_query  # noqa: E402


def _msg_link(chat_id: str, msg_id: int) -> str | None:
    """Build a t.me deep link if chat_id looks like a public channel/group.

    Telegram public channels: chat_id like -100<channel_id>; the URL form is
    https://t.me/c/<channel_id_no_prefix>/<msg_id>. For private chats we can't
    produce a usable link.
    """
    if not chat_id or not msg_id:
        return None
    s = str(chat_id)
    if s.startswith("-100"):
        return f"https://t.me/c/{s[4:]}/{msg_id}"
    return None


def _select_msgs(conn, where_sql: str, params: tuple, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT id, msg_id, date, chat_id, chat_name,
               sender_id, sender_name, sender_username,
               text, topic_id
        FROM messages
        WHERE {where_sql}
        ORDER BY date DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    out = []
    for r in rows:
        d = {k: r[k] for k in r.keys()}
        d["msg_link"] = _msg_link(d["chat_id"], d["msg_id"])
        out.append(d)
    return out


def tg_messages_by_author(
    sender_id: int,
    since: str | None = None,
    until: str | None = None,
    chat_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """All messages from one sender, newest first. Optional date / chat filters.

    Dates use ISO format (YYYY-MM-DD or full datetime). Messages.date is stored
    as Taipei-local ISO strings so direct lexical comparison works.
    """
    conn = get_conn()
    where = ["sender_id = ?"]
    params: list[Any] = [int(sender_id)]
    if since:
        where.append("date >= ?")
        params.append(since)
    if until:
        where.append("date <= ?")
        params.append(until)
    if chat_id:
        where.append("chat_id = ?")
        params.append(str(chat_id))

    msgs = _select_msgs(conn, " AND ".join(where), tuple(params), limit)
    truncated = len(msgs) == limit
    return wrap(msgs, sender_id=int(sender_id), since=since, until=until,
                chat_id=chat_id, limit=limit, truncated=truncated)


def tg_messages_search(
    query: str,
    sender_id: int | None = None,
    chat_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Full-text search across messages, with optional sender/chat/date filters.

    Uses messages_fts (FTS5). Returns newest-first matches.
    """
    q = (query or "").strip()
    if not q:
        return wrap([], note="empty_query")

    conn = get_conn()
    fts_q = build_fts_query(q)

    where = ["m.id IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)"]
    params: list[Any] = [fts_q]
    if sender_id:
        where.append("m.sender_id = ?")
        params.append(int(sender_id))
    if chat_id:
        where.append("m.chat_id = ?")
        params.append(str(chat_id))
    if since:
        where.append("m.date >= ?")
        params.append(since)
    if until:
        where.append("m.date <= ?")
        params.append(until)

    rows = conn.execute(
        f"""
        SELECT m.id, m.msg_id, m.date, m.chat_id, m.chat_name,
               m.sender_id, m.sender_name, m.sender_username,
               m.text, m.topic_id
        FROM messages m
        WHERE {' AND '.join(where)}
        ORDER BY m.date DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()

    msgs = []
    for r in rows:
        d = {k: r[k] for k in r.keys()}
        d["msg_link"] = _msg_link(d["chat_id"], d["msg_id"])
        msgs.append(d)

    truncated = len(msgs) == limit
    return wrap(msgs, query=q, fts_query=fts_q,
                sender_id=sender_id, chat_id=chat_id,
                since=since, until=until, limit=limit, truncated=truncated)
