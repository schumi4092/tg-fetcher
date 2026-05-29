"""Phase 4 — cross-reference a TG sender's stated trades against on-chain
early-buyer data from chain-flow, to infer their wallet(s).

Algorithm:
  1. Find messages by the sender that contain both a CA and a "buy" verb.
  2. For each (CA, msg_ts) pick the chain from CA format (EVM/Solana).
  3. POST to chain-flow `/api/pump_attribution` with a tight window ending
     at msg_ts (default: 60 min before → 5 min after).
  4. Collect every early-buyer wallet returned.
  5. Tally wallets across all calls — a wallet that appears as an early buyer
     in N out of M of the sender's calls is a high-confidence match.

Bounded by `max_cas` (default 5) to keep RPC cost reasonable.
"""

from __future__ import annotations

import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from ._conn import get_conn
from ._fmt import wrap
from .tools_authors import _resolve_to_id

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import extract_cas  # noqa: E402


CHAIN_FLOW_URL = os.environ.get("CHAIN_FLOW_BASE_URL", "http://127.0.0.1:8787")

# Verbs that strongly suggest the sender is reporting their own trade.
# Avoid generic "bought" without context — too noisy.
BUY_VERBS = re.compile(
    r"\b(aped|aping|filled|sniped|long(?:ing)?|entered|in at|bought in|"
    r"my (?:entry|buy)|loaded|loading|added|added more|stack(?:ed|ing))\b",
    re.IGNORECASE,
)


def _msg_ts_unix(date_str: str) -> int | None:
    """Messages.date is Taipei-local 'YYYY-MM-DD HH:MM' or similar. We accept
    either ISO with or without seconds; return Unix seconds (UTC).

    Treats the stored time as Taipei (UTC+8) — same convention as db.py.
    """
    if not date_str:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(date_str, fmt)
            return int(dt.timestamp()) - 8 * 3600  # Taipei → UTC
        except ValueError:
            continue
    return None


def _default_chain_for(ca: str, hint: str | None = None) -> str:
    if hint:
        return hint
    return "base" if ca.startswith("0x") else "sol"


def _call_pump_attribution(
    client: httpx.Client,
    chain: str,
    ca: str,
    window_start_ts: int,
    window_end_ts: int,
    top_n_buyers: int,
) -> dict[str, Any] | None:
    try:
        resp = client.post(
            f"{CHAIN_FLOW_URL}/api/pump_attribution",
            json={
                "chain": chain,
                "ca": ca,
                "window_start_ts": window_start_ts,
                "window_end_ts": window_end_ts,
                "top_n_buyers": top_n_buyers,
            },
            timeout=60.0,
        )
        if resp.status_code != 200:
            return {"_error": f"http_{resp.status_code}", "body": resp.text[:200]}
        return resp.json()
    except httpx.HTTPError as exc:
        return {"_error": "network", "message": str(exc)}


def tg_match_wallet_for_author(
    sender_id: int | None = None,
    name: str | None = None,
    cas: list[str] | None = None,
    default_chain: str = "base",
    max_cas: int = 5,
    window_before_secs: int = 3600,
    window_after_secs: int = 300,
    top_n_buyers: int = 50,
    min_call_matches: int = 2,
    scan_limit: int = 1000,
) -> dict[str, Any]:
    """Infer wallet(s) belonging to a TG sender by cross-referencing their
    stated trades against on-chain early-buyer data.

    Parameters
    ----------
    sender_id, name : pick one — resolves to a sender.
    cas : optional explicit list of CAs the sender supposedly called. If
        omitted, the tool auto-picks the `max_cas` most-mentioned CAs from the
        sender's messages where a buy verb appears in the same message.
    default_chain : chain string passed to chain-flow for EVM CAs (default
        "base"; override with "eth", "bsc", etc.). Solana CAs always use "sol".
    window_before_secs / window_after_secs : window around each msg timestamp.
    top_n_buyers : how many early buyers to fetch per CA.
    min_call_matches : a candidate must appear as an early buyer in at least
        this many of the sender's calls to be returned.
    """
    conn = get_conn()
    sid = _resolve_to_id(conn, sender_id, name)
    if sid is None:
        return wrap(None, note="not_found", sender_id=sender_id, name=name)

    # 1. Build (CA, msg_ts) call list.
    calls: list[dict[str, Any]] = []
    if cas:
        # User-supplied CAs: find any message by this sender that mentions
        # each CA, use the latest msg_ts as the window anchor.
        for ca in cas:
            ca_norm = ca.lower() if ca.startswith("0x") else ca
            row = conn.execute(
                """
                SELECT date, text FROM messages
                WHERE sender_id = ? AND (text LIKE ? OR text LIKE ?)
                ORDER BY date DESC LIMIT 1
                """,
                (sid, f"%{ca}%", f"%{ca_norm}%"),
            ).fetchone()
            if row:
                ts = _msg_ts_unix(row["date"])
                if ts:
                    calls.append({"ca": ca_norm, "msg_ts": ts, "msg_date": row["date"]})
    else:
        # Auto: scan recent messages with a buy verb, extract CAs.
        rows = conn.execute(
            """
            SELECT date, text FROM messages
            WHERE sender_id = ? AND text IS NOT NULL AND text != ''
            ORDER BY date DESC LIMIT ?
            """,
            (sid, scan_limit),
        ).fetchall()
        ca_first_call: dict[str, dict[str, Any]] = {}
        for r in rows:
            text = r["text"] or ""
            if not BUY_VERBS.search(text):
                continue
            for ca in extract_cas(text):
                if ca not in ca_first_call:
                    ts = _msg_ts_unix(r["date"])
                    if ts:
                        ca_first_call[ca] = {"ca": ca, "msg_ts": ts, "msg_date": r["date"]}
        calls = list(ca_first_call.values())[:max_cas]

    if not calls:
        return wrap({"wallet_candidates": [], "calls_examined": []},
                    note="no_calls_found", sender_id=sid)

    # 2. Query chain-flow for each call.
    wallet_hits: dict[str, dict[str, Any]] = {}
    call_details: list[dict[str, Any]] = []
    t0 = time.time()

    with httpx.Client() as client:
        for call in calls:
            ca = call["ca"]
            ts = call["msg_ts"]
            chain = _default_chain_for(ca, default_chain if ca.startswith("0x") else None)
            window_start = ts - window_before_secs
            window_end = ts + window_after_secs

            result = _call_pump_attribution(
                client, chain, ca, window_start, window_end, top_n_buyers
            )
            call_info = {
                "ca": ca,
                "chain": chain,
                "msg_date": call.get("msg_date"),
                "window_start_ts": window_start,
                "window_end_ts": window_end,
            }
            if not result or "_error" in (result or {}):
                call_info["error"] = (result or {}).get("_error", "no_response")
                call_details.append(call_info)
                continue

            buyers = result.get("early_buyers") or []
            call_info["buyer_count"] = len(buyers)
            call_details.append(call_info)

            for b in buyers:
                addr = b.get("address")
                if not addr:
                    continue
                hit = wallet_hits.setdefault(addr, {
                    "address": addr,
                    "match_count": 0,
                    "matched_cas": [],
                    "total_usd_est": 0.0,
                    "evidence": [],
                })
                hit["match_count"] += 1
                hit["matched_cas"].append(ca)
                hit["total_usd_est"] += float(b.get("amount_usd_est") or 0)
                hit["evidence"].append({
                    "ca": ca,
                    "bought_at_ts": b.get("bought_at_ts"),
                    "amount_usd_est": b.get("amount_usd_est"),
                    "insider": b.get("insider"),
                    "funder": b.get("funder", {}).get("addr") if b.get("funder") else None,
                })

    # 3. Filter + rank.
    candidates = [
        h for h in wallet_hits.values()
        if h["match_count"] >= min_call_matches
    ]
    candidates.sort(key=lambda h: (h["match_count"], h["total_usd_est"]), reverse=True)

    payload = {
        "sender_id": sid,
        "calls_examined": call_details,
        "wallet_candidates": candidates,
        "candidate_count": len(candidates),
        "elapsed_secs": round(time.time() - t0, 2),
    }
    return wrap(payload, sender_id=sid, min_call_matches=min_call_matches,
                chain_flow_url=CHAIN_FLOW_URL)
