"""Address extraction: pull every CA / wallet-address-shaped string out of a
sender's messages, dedup, and surface context."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

from ._conn import get_conn
from ._fmt import wrap
from .tools_authors import _resolve_to_id

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import extract_cas  # noqa: E402

_EVM_RE = re.compile(r"0x[a-fA-F0-9]{40}")
_SOL_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


def _chain_hint(addr: str) -> str:
    if addr.startswith("0x"):
        return "evm"
    return "solana"


def _snippet(text: str, addr: str, radius: int = 80) -> str:
    if not text:
        return ""
    idx = text.lower().find(addr.lower()) if addr.startswith("0x") else text.find(addr)
    if idx < 0:
        return text[: 2 * radius]
    start = max(0, idx - radius)
    end = min(len(text), idx + len(addr) + radius)
    s = text[start:end].replace("\n", " ")
    return ("…" if start > 0 else "") + s + ("…" if end < len(text) else "")


def tg_extract_addresses(
    sender_id: int | None = None,
    name: str | None = None,
    since: str | None = None,
    until: str | None = None,
    chat_id: str | None = None,
    scan_limit: int = 2000,
    top: int = 50,
) -> dict[str, Any]:
    """Scan a sender's recent messages, extract every CA / address, dedup.

    Returns each address with its frequency, first/last seen, and a one-line
    context snippet from the most recent mention.

    Note: this conflates contract addresses (CAs) and wallet addresses (EOAs)
    because they're indistinguishable from text alone. Downstream consumers
    (e.g. `tg_match_wallet_for_author`) decide which is which via chain-flow.
    """
    conn = get_conn()
    sid = _resolve_to_id(conn, sender_id, name)
    if sid is None:
        return wrap([], note="not_found", sender_id=sender_id, name=name)

    where = ["sender_id = ?", "text IS NOT NULL", "text != ''"]
    params: list[Any] = [sid]
    if since:
        where.append("date >= ?")
        params.append(since)
    if until:
        where.append("date <= ?")
        params.append(until)
    if chat_id:
        where.append("chat_id = ?")
        params.append(str(chat_id))

    rows = conn.execute(
        f"""
        SELECT id, msg_id, date, chat_id, chat_name, text
        FROM messages
        WHERE {' AND '.join(where)}
        ORDER BY date DESC
        LIMIT ?
        """,
        (*params, scan_limit),
    ).fetchall()

    # addr -> stats
    stats: dict[str, dict[str, Any]] = {}
    for r in rows:
        text = r["text"] or ""
        # extract_cas handles EVM lowercasing + basic dedup within message
        cas = extract_cas(text)
        # also catch any other base58 / hex strings that look address-shaped
        # but aren't picked by extract_cas (rare; mostly equal)
        for m in _EVM_RE.findall(text):
            if m.lower() not in cas:
                cas.append(m.lower())
        for m in _SOL_RE.findall(text):
            if m not in cas and any(c.isdigit() for c in m):
                cas.append(m)

        for addr in cas:
            s = stats.setdefault(addr, {
                "address": addr,
                "chain_hint": _chain_hint(addr),
                "occurrences": 0,
                "first_seen": r["date"],
                "last_seen": r["date"],
                "last_chat_id": r["chat_id"],
                "last_chat_name": r["chat_name"],
                "last_msg_id": r["msg_id"],
                "snippet": _snippet(text, addr),
            })
            s["occurrences"] += 1
            # rows are date DESC, so first hit is most recent → already correct
            if r["date"] < s["first_seen"]:
                s["first_seen"] = r["date"]

    ranked = sorted(stats.values(), key=lambda x: x["occurrences"], reverse=True)
    truncated = len(ranked) > top
    return wrap(ranked[:top], sender_id=sid, scan_window=len(rows),
                since=since, until=until, chat_id=chat_id,
                total_addresses_found=len(ranked), truncated=truncated)
