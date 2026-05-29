"""Wallet-holder inference helpers for coin routes."""

from collections import defaultdict

from db import get_db_ctx, to_taipei_str
from routes._entities import CA_SOL_BLOCKLIST, RE_CA_EVM, RE_CA_SOL
import wallet_aggregator


def normalize_holder_ca(value):
    """Return canonical CA string for holder lookups, or None if invalid."""
    raw = (value or "").strip().strip("`")
    if not raw:
        return None
    if RE_CA_EVM.fullmatch(raw):
        return raw.lower()
    if RE_CA_SOL.fullmatch(raw) and raw not in CA_SOL_BLOCKLIST:
        return raw
    return None


def _holder_addr_key(ev):
    return ev.wallet_addr or ev.wallet_name or "unknown"


def _event_sort_key(ev):
    return ev.timestamp or 0


# Holder-status inference now lives in wallet_aggregator so the in-window
# rollup, the cross-window deriver and this coin-page lookup all share one rule.
_holder_amount_value = wallet_aggregator.holder_amount_value
_wallet_holding_status = wallet_aggregator.wallet_holding_status


def _holder_event_payload(ev):
    return {
        "id": getattr(ev, "source_message_id", None),
        "date": ev.date,
        "time": to_taipei_str(ev.date),
        "action": ev.action,
        "usd_value": ev.usd_value,
        "sold_pct": ev.sold_pct,
        "holds_amount": ev.holds_amount,
        "holds_pct": ev.holds_pct,
        "pnl_usd": ev.pnl_usd,
        "pnl_pct": ev.pnl_pct,
        "has_pnl": ev.has_pnl,
        "mc_usd": ev.mc_usd,
        "price_usd": ev.price_usd,
        "chat_name": getattr(ev, "source_chat_name", ""),
        "snippet": (getattr(ev, "source_text", "") or "")[:260],
    }


def find_wallet_holders_for_ca(ca, days=180, limit=2500):
    """Infer current holders from archived wallet_log messages."""
    ca = normalize_holder_ca(ca)
    if not ca:
        return {
            "ca": "",
            "days": days,
            "holders": [],
            "exited": [],
            "unknown": [],
            "total_wallets": 0,
            "parsed_events": 0,
            "matched_messages": 0,
        }

    try:
        days = int(days) if days is not None else 180
    except (TypeError, ValueError):
        days = 180
    days = max(1, min(days, 3650))
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 2500
    limit = max(100, min(limit, 10000))

    text_expr = "lower(m.text)" if ca.startswith("0x") else "m.text"
    like = f"%{ca.lower() if ca.startswith('0x') else ca}%"
    params = [f"-{days} days", like, limit]
    with get_db_ctx() as conn:
        rows = conn.execute(f"""
            SELECT m.id, m.date, m.chat_id, m.chat_name, m.sender_name, m.text,
                   COALESCE(cc.prompt_profile, '') AS prompt_profile
            FROM messages m
            LEFT JOIN chat_category_map map ON map.chat_id = m.chat_id
            LEFT JOIN chat_categories cc ON cc.id = map.category_id
            WHERE COALESCE(cc.prompt_profile, '') IN ('wallet_log', 'wallet_log_priority')
              AND m.date >= datetime('now', 'localtime', ?)
              AND {text_expr} LIKE ?
            ORDER BY m.date DESC
            LIMIT ?
        """, params).fetchall()

    by_wallet = defaultdict(list)
    token_symbol = ""
    chain = ""
    parsed_events = 0
    for row in rows:
        text = row["text"] or ""
        msg = {"id": row["id"], "date": row["date"], "text": text}
        ev = wallet_aggregator.parse_message(msg)
        if ev is None:
            continue
        parsed_events += 1
        ev_ca = ev.token_ca or ""
        ev_ca_cmp = ev_ca.lower() if ev_ca.startswith("0x") else ev_ca
        if ev_ca_cmp != ca:
            continue
        ev.source_message_id = row["id"]
        ev.source_chat_name = row["chat_name"] or ""
        ev.source_text = text
        token_symbol = token_symbol or ev.token_symbol
        chain = chain or ev.chain
        by_wallet[_holder_addr_key(ev)].append(ev)

    buckets = {"holding": [], "exited": [], "unknown": []}
    for wallet_key, evs in by_wallet.items():
        evs.sort(key=_event_sort_key)
        latest = evs[-1]
        first = evs[0]
        status, status_reason = _wallet_holding_status(latest)
        buys = [e for e in evs if e.action == "BUY"]
        sells = [e for e in evs if e.action == "SELL"]
        latest_pnl = next((e for e in reversed(evs) if e.has_pnl), None)
        item = {
            "wallet_name": latest.wallet_name or first.wallet_name or wallet_key,
            "wallet_addr": latest.wallet_addr or first.wallet_addr or "",
            "status": status,
            "status_reason": status_reason,
            "first_seen": first.date,
            "last_seen": latest.date,
            "last_time": to_taipei_str(latest.date),
            "last_action": latest.action,
            "buy_count": len(buys),
            "sell_count": len(sells),
            "buy_usd": sum(e.usd_value for e in buys),
            "sell_usd": sum(e.usd_value for e in sells),
            "net_flow_usd": sum(e.usd_value for e in buys) - sum(e.usd_value for e in sells),
            "holds_amount": latest.holds_amount,
            "holds_pct": latest.holds_pct,
            "sold_pct": latest.sold_pct,
            "pnl_usd": latest_pnl.pnl_usd if latest_pnl else 0.0,
            "pnl_pct": latest_pnl.pnl_pct if latest_pnl else 0.0,
            "has_pnl": bool(latest_pnl),
            "events": [_holder_event_payload(e) for e in reversed(evs[-5:])],
        }
        buckets[status].append(item)

    for values in buckets.values():
        values.sort(key=lambda x: (x["last_seen"] or "", x["buy_usd"]), reverse=True)

    return {
        "ca": ca,
        "symbol": token_symbol,
        "chain": chain,
        "days": days,
        "matched_messages": len(rows),
        "parsed_events": parsed_events,
        "total_wallets": len(by_wallet),
        "holder_count": len(buckets["holding"]),
        "exited_count": len(buckets["exited"]),
        "unknown_count": len(buckets["unknown"]),
        "holders": buckets["holding"],
        "exited": buckets["exited"],
        "unknown": buckets["unknown"],
        "method": "wallet_log_latest_event",
    }
