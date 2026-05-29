"""Cross-window + on-chain holdings context for wallet auto-summaries.

The 8h summary window (wallet_aggregator) only shows what changed *this* window.
That can't answer "what does this wallet hold right now" when the position was
opened earlier and untouched since. This module fills that gap:

  Layer 2 (query-time, free): scan the archived wallet_log messages over a
    longer lookback and reconcile each (wallet, token) to its latest Holds
    snapshot — exactly the rule wallet_aggregator.derive_holdings uses, just
    over more history. Decouples "current position" from the window.

  Layer 3 (on-chain, Tier-1 only): for the small curated wallet_log_priority
    feed, cross-check the TG-derived standing positions against real on-chain
    balances via gmgn-cli. Catches positions Ray never reported and silent
    exits Ray missed. Gated by ENABLE_GMGN_RECONCILE; never runs for Ray.

`build_holdings_context()` returns a markdown block that ai.py appends to the
deterministic rollup before the final LLM pass. Everything is best-effort: any
DB or CLI failure is logged and degrades to an empty/partial block — a holdings
lookup must never break a summary run.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime

import wallet_aggregator as wa
from wallet_aggregator import HOLDING, EXITED
from db import get_db_ctx
from config import (
    HOLDINGS_LOOKBACK_DAYS,
    HOLDINGS_MAX_SCAN_ROWS,
    HOLDINGS_MAX_WALLETS,
    HOLDINGS_MAX_TOKENS_PER_WALLET,
    ENABLE_GMGN_RECONCILE,
    ENABLE_GMGN_RECONCILE_RAY,
    GMGN_RECONCILE_MAX_WALLETS,
    GMGN_RECONCILE_TIMEOUT_SECS,
    GMGN_RECONCILE_PAUSE_SECS,
)

logger = logging.getLogger("tg_fetcher")

_PRIORITY_PROFILE = "wallet_log_priority"


# ---------------------------------------------------------------------------
# Layer 2 — query-time archive scan
# ---------------------------------------------------------------------------

def _scan_archive_records(
    profiles: tuple[str, ...],
    days: int,
    max_rows: int,
    wallet_addrs: set[str] | None = None,
) -> tuple[list[wa.HoldingRecord], dict]:
    """Reconcile recent archived wallet_log messages into HoldingRecords.

    Pulls the newest `max_rows` messages for the given prompt profiles over the
    last `days`, parses them, and reconciles each (wallet, token) to its latest
    Holds snapshot. If `wallet_addrs` is given, keeps only those wallets.
    """
    placeholders = ",".join("?" for _ in profiles)
    params = [*profiles, f"-{days} days", max_rows]
    try:
        with get_db_ctx() as conn:
            rows = conn.execute(f"""
                SELECT m.id, m.date, m.text
                FROM messages m
                LEFT JOIN chat_category_map map ON map.chat_id = m.chat_id
                LEFT JOIN chat_categories cc ON cc.id = map.category_id
                WHERE COALESCE(cc.prompt_profile, '') IN ({placeholders})
                  AND m.date >= datetime('now', 'localtime', ?)
                ORDER BY m.date DESC
                LIMIT ?
            """, params).fetchall()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("holdings: archive scan failed: %r", exc)
        return [], {"scanned_rows": 0, "parsed_events": 0}

    events = []
    for r in rows:
        ev = wa.parse_message({"id": r["id"], "date": r["date"], "text": r["text"] or ""})
        if ev is not None:
            events.append(ev)

    records = wa.derive_holdings(events)
    if wallet_addrs:
        scope = {a for a in wallet_addrs if a}
        records = [r for r in records if (r.wallet_addr or r.wallet_name) in scope]
    return records, {"scanned_rows": len(rows), "parsed_events": len(events)}


def _window_active_keys(window_messages: list[dict]) -> tuple[set[tuple[str, str]], dict[str, tuple]]:
    """From the current window, return:
       - the set of (wallet_key, token_key) pairs that traded this window
       - a map wallet_key -> (name, addr, chain) for active wallets
    """
    active_pairs: set[tuple[str, str]] = set()
    wallets: dict[str, tuple] = {}
    for m in window_messages:
        ev = wa.parse_message(m)
        if ev is None or ev.action not in ("BUY", "SELL"):
            continue
        wk = wa.wallet_key(ev)
        active_pairs.add((wk, wa.token_key(ev)))
        wallets.setdefault(wk, (ev.wallet_name, ev.wallet_addr, ev.chain))
    return active_pairs, wallets


# ---------------------------------------------------------------------------
# Layer 3 — on-chain reconcile (Tier-1 only)
# ---------------------------------------------------------------------------

def _reconcile_wallet_onchain(addr: str, chain: str, tg_records: list[wa.HoldingRecord]) -> dict | None:
    """Compare one wallet's TG-derived standing positions to its on-chain
    holdings. Returns a dict of confirmed / extra / gone token notes, or None
    if the chain isn't supported or the CLI call failed.
    """
    import gmgn_client  # local import: keep module import-safe if CLI absent

    code = gmgn_client.to_chain_code(chain)
    if not code or not addr:
        return None
    try:
        onchain = gmgn_client.fetch_holdings(code, addr, limit=30, timeout=GMGN_RECONCILE_TIMEOUT_SECS)
    except Exception as exc:
        logger.info("holdings: gmgn reconcile failed for %s [%s]: %r", addr[:10], chain, exc)
        return None

    onchain_by_ca = {h["ca"]: h for h in onchain if h["ca"]}
    tg_holding = {r.token_ca: r for r in tg_records if r.status == HOLDING and r.token_ca}

    confirmed, extra, gone = [], [], []
    for ca, h in onchain_by_ca.items():
        if ca in tg_holding:
            confirmed.append((tg_holding[ca].token_symbol or h["symbol"], h))
        else:
            extra.append(h)
    for ca, r in tg_holding.items():
        if ca not in onchain_by_ca:
            gone.append(r)
    return {"confirmed": confirmed, "extra": extra, "gone": gone, "onchain_count": len(onchain_by_ca)}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _fmt_ts(ts: int) -> str:
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts, tz=wa.timezone.utc).astimezone(wa.TAIPEI_TZ).strftime("%m-%d %H:%M")


def _standing_line(r: wa.HoldingRecord, traded_this_window: bool) -> str:
    bits = [f"${r.token_symbol or '?'}", f"[{wa.holding_status_label(r)}]"]
    if r.holds_amount:
        bits.append(f"holds={r.holds_amount}({r.holds_pct:.2f}%)")
    if r.realized_pnl:
        bits.append(f"realized_pnl={wa._fmt_signed_usd(r.realized_pnl)}")
    bits.append("本窗口有動" if traded_this_window else f"本窗口未動·末次{_fmt_ts(r.last_ts)}")
    if r.token_ca:
        bits.append(f"CA=`{r.token_ca}`")
    return " ".join(bits)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_holdings_context(window_messages: list[dict], profile_name: str) -> str:
    """Build the cross-window (+ Tier-1 on-chain) holdings block appended to the
    wallet rollup. Returns "" when there's nothing useful to add.
    """
    is_priority = profile_name == _PRIORITY_PROFILE
    active_pairs, active_wallets = _window_active_keys(window_messages)

    # Scope: Tier-1 → the whole (small) curated feed; Ray → only wallets active
    # this window, so the archive scan stays bounded on the high-volume feed.
    if is_priority:
        scan_profiles = (_PRIORITY_PROFILE,)
        scope = None
    else:
        scan_profiles = ("wallet_log",)
        scope = {wk for wk in active_wallets}
        if not scope:
            return ""  # nothing active this window → nothing to enrich

    records, meta = _scan_archive_records(
        scan_profiles, HOLDINGS_LOOKBACK_DAYS, HOLDINGS_MAX_SCAN_ROWS, wallet_addrs=scope,
    )
    if not records:
        return ""

    # Annotate which positions also traded in the current window.
    def traded(r):
        return (r.wallet_addr or r.wallet_name, r.token_ca or f"symbol:{r.token_symbol}") in active_pairs

    sections: list[str] = []

    if is_priority:
        # Full standing picture for the curated feed.
        sections.append(
            f"## STANDING_HOLDINGS (Tier-1 跨窗口持倉 · 回看 {HOLDINGS_LOOKBACK_DAYS}d · "
            f"依各 wallet×token 最新事件)"
        )
        sections.append(
            "這是 Tier-1 錢包目前的完整在倉狀態(不限本窗口);「本窗口未動」代表更早建倉至今未操作。"
        )
        sections.extend(_render_lines(records, active_pairs, traded,
                                      max_wallets=HOLDINGS_MAX_WALLETS,
                                      max_tokens=HOLDINGS_MAX_TOKENS_PER_WALLET,
                                      include_exited=False))
        sections.append("")
    else:
        # Ray: only the delta — standing positions NOT touched this window.
        pre_window = [r for r in records if r.status == HOLDING and not traded(r)]
        if pre_window:
            sections.append(
                f"## PRE_WINDOW_HOLDINGS (本窗口活躍錢包的「更早建倉、本窗口未動」持倉 · 回看 {HOLDINGS_LOOKBACK_DAYS}d)"
            )
            sections.append(
                "這些部位本窗口沒有 buy/sell,但回看仍在倉 — 補上 8h 窗口看不到的既有持倉。"
            )
            sections.extend(_render_lines(pre_window, active_pairs, traded,
                                          max_wallets=HOLDINGS_MAX_WALLETS,
                                          max_tokens=HOLDINGS_MAX_TOKENS_PER_WALLET,
                                          include_exited=False))
            sections.append("")

    # Layer 3: on-chain reconcile. Tier-1 reconciles the full curated feed; Ray
    # reconciles only the top-N active-window wallets (cap-bounded) so the
    # high-volume feed can't blow the GMGN rate limit. Separate flags, both off
    # by default.
    run_reconcile = (
        (is_priority and ENABLE_GMGN_RECONCILE)
        or (not is_priority and ENABLE_GMGN_RECONCILE_RAY)
    )
    if run_reconcile:
        recon = _build_onchain_section(
            records, scope_label="Tier-1" if is_priority else "Ray 窗口 top-N",
        )
        if recon:
            sections.extend(recon)

    block = "\n".join(sections).strip()
    if block:
        logger.info(
            "holdings: %s context built (scanned=%d parsed=%d records=%d priority=%s)",
            profile_name, meta["scanned_rows"], meta["parsed_events"], len(records), is_priority,
        )
    return block + "\n" if block else ""


def _render_lines(records, active_pairs, traded_fn, *, max_wallets, max_tokens, include_exited):
    by_wallet: dict[str, list[wa.HoldingRecord]] = defaultdict(list)
    for r in records:
        if not include_exited and r.status == EXITED:
            continue
        by_wallet[r.wallet_addr or r.wallet_name].append(r)
    if not by_wallet:
        return []

    ranked = sorted(
        by_wallet.values(),
        key=lambda items: (sum(1 for r in items if r.status == HOLDING), len(items)),
        reverse=True,
    )
    shown, hidden = ranked[:max_wallets], max(0, len(ranked) - max_wallets)

    out = []
    for items in shown:
        items.sort(key=lambda r: (r.status != HOLDING, -r.last_ts))
        head = items[0]
        addr = wa._addr_short(head.wallet_addr) if head.wallet_addr else "?"
        holding_n = sum(1 for r in items if r.status == HOLDING)
        out.append(f"- {head.wallet_name or '?'} ({addr}) [{head.chain}] still_holding={holding_n}")
        for r in items[:max_tokens]:
            out.append(f"    · {_standing_line(r, traded_fn(r))}")
        if len(items) > max_tokens:
            out.append(f"    · …(其餘 {len(items) - max_tokens} 顆略)")
    if hidden:
        out.append(f"OMITTED_WALLETS lower_priority={hidden}")
    return out


def _build_onchain_section(records: list[wa.HoldingRecord], scope_label: str = "Tier-1") -> list[str]:
    """Reconcile up to GMGN_RECONCILE_MAX_WALLETS wallets against on-chain
    balances, ranked by # holding positions. Used for both the full Tier-1 feed
    and Ray's top-N active-window wallets (caller scopes `records`).
    """
    try:
        import gmgn_client
        if not gmgn_client.available():
            return []
    except Exception:
        return []

    # One (addr, chain) per wallet, ranked by # holding positions.
    by_wallet: dict[tuple[str, str], list[wa.HoldingRecord]] = defaultdict(list)
    for r in records:
        if r.wallet_addr and r.chain:
            by_wallet[(r.wallet_addr, r.chain)].append(r)
    eligible = sorted(
        by_wallet.items(),
        key=lambda kv: sum(1 for r in kv[1] if r.status == HOLDING),
        reverse=True,
    )
    ranked = eligible[:GMGN_RECONCILE_MAX_WALLETS]
    skipped = max(0, len(eligible) - len(ranked))
    if not ranked:
        return []

    out = [f"## ONCHAIN_RECONCILE ({scope_label} 鏈上對帳 · gmgn-cli 即時餘額)"]
    out.append(
        "用鏈上真實餘額校正 TG 推導:＋=鏈上持有但 TG 未見(更早/未推送);"
        "⚠=TG 顯示持有但鏈上已無(疑似漏報賣出);✓=雙方一致(附鏈上估值/未實現)。"
    )
    if skipped:
        out.append(
            f"NOTE 僅對持倉數最多的 top-{GMGN_RECONCILE_MAX_WALLETS} 錢包對帳,"
            f"其餘 {skipped} 個活躍錢包未對帳(成本上限)。"
        )
    reconciled = 0
    for (addr, chain), tg_recs in ranked:
        res = _reconcile_wallet_onchain(addr, chain, tg_recs)
        if GMGN_RECONCILE_PAUSE_SECS > 0:
            time.sleep(GMGN_RECONCILE_PAUSE_SECS)
        if res is None:
            continue
        reconciled += 1
        name = next((r.wallet_name for r in tg_recs if r.wallet_name), "?")
        out.append(f"- {name} ({wa._addr_short(addr)}) [{chain}] onchain_positions={res['onchain_count']}")
        for sym, h in res["confirmed"][:HOLDINGS_MAX_TOKENS_PER_WALLET]:
            out.append(
                f"    · ✓ ${sym} 鏈上 usd={wa._fmt_usd(h['usd_value'])} "
                f"uPnL={wa._fmt_signed_usd(h['unrealized_profit'])}"
            )
        for h in res["extra"][:HOLDINGS_MAX_TOKENS_PER_WALLET]:
            out.append(
                f"    · ＋ ${h['symbol'] or '?'} 鏈上持有 TG未見 usd={wa._fmt_usd(h['usd_value'])} "
                f"uPnL={wa._fmt_signed_usd(h['unrealized_profit'])} CA=`{h['ca']}`"
            )
        for r in res["gone"][:HOLDINGS_MAX_TOKENS_PER_WALLET]:
            out.append(
                f"    · ⚠ ${r.token_symbol or '?'} TG顯示持有·鏈上已無(疑似漏報賣出) CA=`{r.token_ca}`"
            )
    if reconciled == 0:
        return []
    out.append("")
    return out
