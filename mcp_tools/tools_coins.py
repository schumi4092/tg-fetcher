"""Coin-mention search: wraps the existing `db.search_coin`."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from ._fmt import wrap

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import search_coin  # noqa: E402


def tg_coin_mentions(
    query: str,
    limit_per_chat: int = 3,
    max_chats: int = 30,
    days: int | None = None,
) -> dict[str, Any]:
    """Find TG mentions for a ticker / name / CA.

    Wraps the existing `db.search_coin`, which auto-detects whether the query
    is an EVM CA, a Solana CA, or a free-text ticker / name, and returns
    per-chat groupings, related events, and CA disambiguation candidates.
    """
    q = (query or "").strip()
    if not q:
        return wrap(None, note="empty_query")

    result = search_coin(
        q,
        limit_per_chat=limit_per_chat,
        max_chats=max_chats,
        days=days,
    )
    return wrap(result, query=q, days=days)
