"""Chat-level summaries: read directly from daily_summaries."""

from __future__ import annotations

from typing import Any

from ._conn import get_conn
from ._fmt import wrap


def tg_chat_summary(
    chat_id: str,
    date: str | None = None,
    slot: str | None = None,
    limit: int = 7,
) -> dict[str, Any]:
    """Return daily summaries for a chat.

    - If `date` given: returns the summary(ies) for that date (one per slot).
    - Otherwise: returns the latest `limit` summaries, newest first.
    """
    conn = get_conn()
    where = ["chat_id = ?"]
    params: list[Any] = [str(chat_id)]
    if date:
        where.append("date = ?")
        params.append(date)
    if slot:
        where.append("summary_slot = ?")
        params.append(slot)

    rows = conn.execute(
        f"""
        SELECT id, date, chat_id, chat_name, hours, message_count,
               summary, summary_slot, period_start, period_end, source, created_at
        FROM daily_summaries
        WHERE {' AND '.join(where)}
        ORDER BY date DESC, summary_slot DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()

    payload = [{k: r[k] for k in r.keys()} for r in rows]
    return wrap(payload, chat_id=str(chat_id), date=date, slot=slot, limit=limit)


def tg_list_chats(limit: int = 50) -> dict[str, Any]:
    """Help tool: list all chats by msg volume so the agent can pick a chat_id."""
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT chat_id, MAX(chat_name) AS chat_name, COUNT(*) AS n,
               MIN(date) AS first_seen, MAX(date) AS last_seen
        FROM messages
        GROUP BY chat_id
        ORDER BY n DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return wrap([dict(r) for r in rows], limit=limit)
