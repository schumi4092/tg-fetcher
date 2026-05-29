"""Tracker / sniper bot detection — used by watchtower routes."""

import re


# These dump structured price+holder dumps that drown out human commentary.
# URL signatures > sender-name heuristics (sender names like "Rick" can also be
# real humans).
_RE_MD_LINK = re.compile(r"\[[^\]]+\]\([^)]+\)")
_BOT_URL_SIGS = (
    "rickburpbot", "bananagun_bot", "maestrosniperbot", "sigmatrading",
    "bloomevmbot", "photon-base.tinyastro", "stonksbasecto", "rickbot",
    "/dsapp?startapp=", "?referralcode=sr2o2",
)


def is_bot_message(text):
    """Return True if message looks like automated tracker/bot output.

    Heuristic — combine markdown-link density with known bot URL signatures.
    Conservative: prefer false negatives (let one bot through) over hiding
    real human messages.
    """
    if not text:
        return False
    # Tracker bots dump ≥6 markdown links in their structured output. Humans
    # almost never compose messages with this density of [text](url).
    if len(_RE_MD_LINK.findall(text)) >= 6:
        return True
    lt = text.lower()
    if any(sig in lt for sig in _BOT_URL_SIGS):
        return True
    return False


def fetch_context_for_hits(conn, hits, before=1, after=1):
    """Fetch ±N adjacent messages by msg_id within each hit's chat.

    Returns dict keyed by (chat_id, hit_msg_id) → {"before": [...], "after": [...]}.
    Adjacent-by-msg_id approximates "next/previous chat message" because Telethon
    assigns monotonically increasing msg_id within a chat.
    """
    if not hits or (before == 0 and after == 0):
        return {}
    by_chat = {}
    for h in hits:
        if h.get("chat_id") and h.get("msg_id") is not None:
            by_chat.setdefault(str(h["chat_id"]), set()).add(int(h["msg_id"]))

    out = {}
    for chat_id, hit_msg_ids in by_chat.items():
        # Build list of all msg_ids we want to fetch (hit + offsets).
        wanted = set()
        for mid in hit_msg_ids:
            for d in range(-before, after + 1):
                if d != 0:
                    wanted.add(mid + d)
        if not wanted:
            continue
        placeholders = ",".join("?" * len(wanted))
        rows = conn.execute(
            f"SELECT msg_id, date, sender_name, sender_username, text, media "
            f"FROM messages WHERE chat_id = ? AND msg_id IN ({placeholders})",
            [chat_id] + list(wanted),
        ).fetchall()
        chat_msgs = {r["msg_id"]: dict(r) for r in rows}
        for mid in hit_msg_ids:
            ctx_before, ctx_after = [], []
            for d in range(-before, 0):
                if (mid + d) in chat_msgs:
                    ctx_before.append(chat_msgs[mid + d])
            for d in range(1, after + 1):
                if (mid + d) in chat_msgs:
                    ctx_after.append(chat_msgs[mid + d])
            out[(chat_id, mid)] = {"before": ctx_before, "after": ctx_after}
    return out
