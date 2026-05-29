"""Deterministic aggregator for Ray Orange | Wallet Tracker messages.

Replaces the LLM-based SYS_COMPRESS_WALLET pass for the wallet_log profile.
Parses Ray Orange's markdown format with regex, groups events by
(wallet_addr, token_ca, action), and renders a compact rollup that feeds
straight into SYS_WALLET deep analysis — no Sonnet compression step, no
per-chunk CLI latency.

Unparseable messages fall through into an "unrecognized" bucket (capped).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


# ---------------------------------------------------------------------------
# Regex — match Ray Orange's markdown layout. See sample messages for format:
#   Header:   🔴 [SELL COPE](tx_url) (ETHEREUM)
#             🔴 [SELL HAMILTON](tx_url) on JUPITER
#             🆕🟢 [BUY KISHU](tx_url) (ETHEREUM)
#             💸 [TRANSFER](tx_url) (ETHEREUM)
#             🆗 [APPROVE PLINKS](tx_url) (BASE)
#   Wallet:   🔹[**wallet_name**](https://.../address/0x...) ...
#   Swap:     swapped **AMT** ($USD) [**SYM_A**]... for **AMT** ($USD) [**SYM_B**]...
#   PnL:      ➖Sold: N% / 📉PnL: **$-X** (-Y%) / 📈PnL: **$+X** (+Y%)
#   Holds:    ✊Holds: AMT (PCT%)
#   Footer:   🔗 **#SYM** | **MC**: $AMT | **Seen**: AGE: [...]
#   CA tail:  `0x...`
# ---------------------------------------------------------------------------

_HEADER_RE = re.compile(
    r"(?P<emoji>🔴|🆕🟢|🟢|💸|🆗|🔄)?\s*"
    r"\[(?P<action>BUY|SELL|TRANSFER|APPROVE|SWAP)"
    r"(?:\s+(?P<symbol>[^\]]+))?\]\([^)]+\)"
    r"(?:\s+\((?P<chain>[A-Z]+)\)|\s+on\s+(?P<venue>[A-Z0-9_ -]+))"
)
_TRACKED_WALLET_LINE_RE = re.compile(
    r"\[[^\]]+\]\("
    r"https?://[^)]+/(?:address|account)/(?P<addr>[A-Za-z0-9]{32,64})[^)]*\)"
    r"\s+\*\*(?P<name>[^*]+)\*\*"
)
_WALLET_RE = re.compile(
    r"\[\*\*(?P<name>[^*]+)\*\*\]\("
    r"https?://[^)]+/(?:address|account)/(?P<addr>[A-Za-z0-9]{32,64})"
)
_TRANSFER_COUNTERPARTY_RE = re.compile(
    r"\[\*\*(?P<from_name>[^*]+)\*\*\]\("
    r"https?://[^)]+/(?:address|account)/(?P<from_addr>[A-Za-z0-9]{32,64})[^)]*\)"
    r"\s+transferred to multiple accounts",
    re.I,
)
_CA_TAIL_RE = re.compile(r"`(0x[a-fA-F0-9]{40})`")
_FOOTER_SYMBOL_RE = re.compile(r"\*\*#(?P<symbol>[A-Za-z0-9_$.\-]+)\*\*")
_PRICE_RE = re.compile(r"@\$(?P<price>[\d,]+(?:\.\d+)?)")
_SWAP_USD_RE = re.compile(
    r"swapped\s+\*\*[\d,.]+\*\*\s+\(\$(?P<u1>[\d,]+(?:\.\d+)?)\)",
)
_TRANSFER_USD_RE = re.compile(
    r"transferred[\s\S]*?\*\*[\d,.]+\*\*\s+\(\$(?P<usd>[\d,]+(?:\.\d+)?)\)",
    re.I,
)
_SOLD_RE = re.compile(r"Sold:\s*(?P<pct>\d+(?:\.\d+)?)%")
_PNL_RE = re.compile(
    r"PnL[^$]*?\*\*\$(?P<sign>-?\+?)(?P<amt>[\d,]+(?:\.\d+)?)\*\*"
    r"\s*\((?P<psign>-?\+?)(?P<pct>[\d.]+)%\)"
)
_UPNL_RE = re.compile(r"uPnL:\s*\*\*\$(?P<sign>-?\+?)(?P<amt>[\d,]+(?:\.\d+)?)\*\*")
_HOLDS_RE = re.compile(r"Holds:\s*(?P<amt>[\d.,]+[KMBkmb]?)\s*\((?P<pct>[\d.]+)%\)")
_MC_RE = re.compile(r"\*\*MC\*\*:\s*\$(?P<amt>[\d,]+(?:\.\d+)?)(?P<unit>[KMB])?")
_LQ_RE = re.compile(r"\*\*LQ\*\*:\s*\$(?P<amt>[\d,]+(?:\.\d+)?)(?P<unit>[KMB])?")
_SEEN_RE = re.compile(r"\*\*Seen\*\*:\s*(?P<seen>[^:|\[]+?):\s*\[")
_SOL_CA_RE = re.compile(r"`(?P<ca>[1-9A-HJ-NP-Za-km-z]{32,44})`")  # sol base58


# ---------------------------------------------------------------------------
# Event model
# ---------------------------------------------------------------------------


TAIPEI_TZ = timezone(timedelta(hours=8))


@dataclass
class WalletEvent:
    timestamp: int = 0
    date: str = ""
    action: str = ""          # BUY / SELL / TRANSFER / APPROVE / SWAP
    chain: str = ""
    wallet_name: str = ""
    wallet_addr: str = ""
    token_symbol: str = ""
    token_ca: str = ""
    usd_value: float = 0.0
    sold_pct: float = 0.0
    pnl_usd: float = 0.0
    pnl_pct: float = 0.0
    has_pnl: bool = False
    upnl_usd: float = 0.0
    holds_amount: str = ""
    holds_pct: float = 0.0
    mc_usd: float = 0.0
    lq_usd: float = 0.0
    price_usd: float = 0.0
    seen_age: str = ""
    transfer_from_name: str = ""
    transfer_from_addr: str = ""
    transfer_direction: str = ""  # IN when tracked wallet receives, OUT otherwise
    is_first_buy: bool = False


def _usd_to_float(amt_str: str, unit: str = "") -> float:
    try:
        v = float(amt_str.replace(",", ""))
    except ValueError:
        return 0.0
    mult = {"K": 1e3, "M": 1e6, "B": 1e9}.get(unit or "", 1.0)
    return v * mult


def _msg_timestamp(msg: dict) -> int:
    """Prefer explicit timestamp, otherwise derive it from the stored ISO date."""
    raw_ts = msg.get("timestamp")
    if raw_ts not in (None, ""):
        try:
            return int(float(raw_ts))
        except (TypeError, ValueError):
            pass

    raw_date = msg.get("date")
    if not raw_date:
        return 0
    if isinstance(raw_date, datetime):
        dt = raw_date
    else:
        text = str(raw_date).strip()
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TAIPEI_TZ)
    return int(dt.timestamp())


def parse_message(msg: dict) -> WalletEvent | None:
    """Parse one Ray Orange message dict. Returns None if the format doesn't match."""
    text = (msg.get("text") or "").strip()
    if not text:
        return None

    hm = _HEADER_RE.search(text)
    if not hm:
        return None

    action = hm.group("action")
    chain = hm.group("chain")
    if not chain:
        venue = (hm.group("venue") or "").upper()
        if venue in {"JUPITER", "RAYDIUM", "ORCA", "PUMPFUN", "METEORA"}:
            chain = "SOLANA"
        else:
            chain = venue or "UNKNOWN"
    header_symbol = (hm.group("symbol") or "").strip()
    is_first_buy = hm.group("emoji") == "🆕🟢"

    wm = _TRACKED_WALLET_LINE_RE.search(text) or _WALLET_RE.search(text)
    if not wm:
        return None

    ev = WalletEvent(
        timestamp=_msg_timestamp(msg),
        date=msg.get("date") or "",
        action=action,
        chain=chain,
        wallet_name=wm.group("name").strip(),
        wallet_addr=wm.group("addr").lower() if wm.group("addr").startswith("0x") else wm.group("addr"),
        token_symbol=header_symbol,
        is_first_buy=is_first_buy,
    )

    if not ev.token_symbol:
        fm = _FOOTER_SYMBOL_RE.search(text)
        if fm:
            ev.token_symbol = fm.group("symbol").lstrip("$")

    # Token CA — last backtick block is usually the subject token.
    # Ray Orange puts the subject CA on its own line at the bottom.
    ca_matches = _CA_TAIL_RE.findall(text)
    if ca_matches:
        ev.token_ca = ca_matches[-1].lower()
    elif chain == "SOLANA":
        sol_matches = _SOL_CA_RE.findall(text)
        if sol_matches:
            ev.token_ca = sol_matches[-1]

    # USD value — SWAP/BUY/SELL carry two matching $ amounts; first = subject leg.
    if action in {"BUY", "SELL", "SWAP"}:
        um = _SWAP_USD_RE.search(text)
        if um:
            ev.usd_value = _usd_to_float(um.group("u1"))
    elif action == "TRANSFER":
        um = _TRANSFER_USD_RE.search(text)
        if um:
            ev.usd_value = _usd_to_float(um.group("usd"))
        tm = _TRANSFER_COUNTERPARTY_RE.search(text)
        if tm:
            ev.transfer_from_name = tm.group("from_name")
            from_addr = tm.group("from_addr")
            ev.transfer_from_addr = from_addr.lower() if from_addr.startswith("0x") else from_addr
            ev.transfer_direction = "IN"
    # APPROVE has no USD — leave 0.

    sm = _SOLD_RE.search(text)
    if sm:
        ev.sold_pct = float(sm.group("pct"))

    pm = _PNL_RE.search(text)
    if pm:
        sign = -1.0 if pm.group("sign") == "-" else 1.0
        ev.pnl_usd = sign * float(pm.group("amt").replace(",", ""))
        psign = -1.0 if pm.group("psign") == "-" else 1.0
        ev.pnl_pct = psign * float(pm.group("pct"))
        ev.has_pnl = True

    upm = _UPNL_RE.search(text)
    if upm:
        sign = -1.0 if upm.group("sign") == "-" else 1.0
        ev.upnl_usd = sign * float(upm.group("amt").replace(",", ""))

    hm2 = _HOLDS_RE.search(text)
    if hm2:
        ev.holds_amount = hm2.group("amt")
        ev.holds_pct = float(hm2.group("pct"))

    mm = _MC_RE.search(text)
    if mm:
        ev.mc_usd = _usd_to_float(mm.group("amt"), mm.group("unit") or "")

    lm = _LQ_RE.search(text)
    if lm:
        ev.lq_usd = _usd_to_float(lm.group("amt"), lm.group("unit") or "")

    pr = _PRICE_RE.search(text)
    if pr:
        ev.price_usd = _usd_to_float(pr.group("price"))

    senm = _SEEN_RE.search(text)
    if senm:
        ev.seen_age = senm.group("seen").strip()

    return ev


# ---------------------------------------------------------------------------
# Aggregation + render
# ---------------------------------------------------------------------------


def _fmt_usd(v: float) -> str:
    """Format USD — $1.2M, $45.3K, $678."""
    if v >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v/1_000:.1f}K"
    return f"${v:,.0f}"


def _fmt_signed_usd(v: float) -> str:
    sign = "+" if v >= 0 else "-"
    return f"{sign}{_fmt_usd(abs(v))}"


def _fmt_mc_range(mcs: list[float]) -> str:
    mcs = [m for m in mcs if m > 0]
    if not mcs:
        return "?"
    lo, hi = min(mcs), max(mcs)
    if lo == hi:
        return _fmt_usd(lo)
    return f"{_fmt_usd(lo)}–{_fmt_usd(hi)}"


def _hm(ts: int) -> str:
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(TAIPEI_TZ).strftime("%H:%M")


def _addr_short(addr: str) -> str:
    if not addr or len(addr) < 10:
        return addr
    return f"{addr[:6]}..{addr[-4:]}"


def _limit_ranked(items: list, limit: int | None) -> tuple[list, int]:
    if limit is None:
        return items, 0
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return items, 0
    if n <= 0:
        return [], len(items)
    return items[:n], max(0, len(items) - n)


def _parse_messages(messages: list[dict]) -> tuple[list[WalletEvent], list[str]]:
    events: list[WalletEvent] = []
    unparsed: list[str] = []
    for m in messages:
        ev = parse_message(m)
        if ev is None:
            raw = (m.get("text") or "").strip()
            if raw:
                unparsed.append(f"[{(m.get('date') or '')[:19]}] {raw[:400]}")
            continue
        events.append(ev)
    return events, unparsed


def _latest_pnl_event(evs: list[WalletEvent]) -> WalletEvent | None:
    """Ray PnL is a cumulative wallet/token snapshot, not a per-sell delta."""
    with_pnl = [e for e in evs if e.has_pnl]
    if not with_pnl:
        return None
    return max(with_pnl, key=lambda e: e.timestamp)


def _realized_pnl_snapshot_total(evs: list[WalletEvent]) -> float:
    """Sum final realized-PnL snapshots across wallets for one token."""
    by_wallet: dict[str, list[WalletEvent]] = defaultdict(list)
    for ev in evs:
        if ev.action != "SELL" or not ev.has_pnl:
            continue
        key = ev.wallet_addr or ev.wallet_name
        by_wallet[key].append(ev)
    return sum(
        latest.pnl_usd
        for group in by_wallet.values()
        if (latest := _latest_pnl_event(group)) is not None
    )


def _event_symbol(evs: list[WalletEvent]) -> str:
    return next((e.token_symbol for e in evs if e.token_symbol), "?")


def _event_ca(evs: list[WalletEvent]) -> str:
    return next((e.token_ca for e in evs if e.token_ca), "")


# ---------------------------------------------------------------------------
# Holdings status + in-window reconcile.
#
# Single source of truth for "does this wallet still hold this token after its
# latest event". Shared by:
#   - the CURRENT_HOLDINGS section below (in-window, deterministic)
#   - holdings.py (query-time cross-window deriver)
#   - routes/coin_wallet.py (coin-page holder inference)
# so a change in Ray Orange's Holds/Sold formatting is fixed in exactly one
# place. The window log only reports flows; the Holds snapshot on each message
# is the real post-trade position, so status is read off the *latest* event.
# ---------------------------------------------------------------------------

HOLDING = "holding"
EXITED = "exited"
UNKNOWN = "unknown"

_HOLD_AMOUNT_RE = re.compile(r"^([-+]?\d+(?:\.\d+)?)([KMBkmb]?)$")


def holder_amount_value(value) -> float:
    """Parse a Ray-style hold amount (0.00 / 500K / 1.2M) to a float for >0 checks."""
    raw = str(value or "").strip().replace(",", "")
    if not raw:
        return 0.0
    m = _HOLD_AMOUNT_RE.match(raw)
    if not m:
        return 0.0
    mult = {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9}
    return float(m.group(1)) * mult.get((m.group(2) or "").upper(), 1.0)


def wallet_holding_status(latest: "WalletEvent") -> tuple[str, str]:
    """Infer whether a wallet still holds a token after its latest event.

    `latest` must be the chronologically last BUY/SELL event for one
    (wallet, token) pair. Returns (status, reason) with status in
    HOLDING / EXITED / UNKNOWN.
    """
    if latest.action == "BUY":
        return HOLDING, "latest_buy"
    if latest.action == "SELL":
        holds_value = holder_amount_value(latest.holds_amount)
        if latest.sold_pct >= 99.5:
            return EXITED, "sold_nearly_all"
        if latest.holds_pct > 0 or holds_value > 0:
            return HOLDING, "sell_reports_holds"
        if latest.holds_amount and holds_value <= 0:
            return EXITED, "sell_reports_zero_holds"
        if 0 < latest.sold_pct < 99.5:
            return HOLDING, "partial_sell"
        return UNKNOWN, "sell_without_holds_snapshot"
    return UNKNOWN, (latest.action or "unknown").lower()


def wallet_key(ev: "WalletEvent") -> str:
    return ev.wallet_addr or ev.wallet_name or "unknown"


def token_key(ev: "WalletEvent") -> str:
    return ev.token_ca or f"symbol:{ev.token_symbol}"


@dataclass
class HoldingRecord:
    """Reconciled current position for one (wallet, token) pair."""
    wallet_name: str = ""
    wallet_addr: str = ""
    chain: str = ""
    token_symbol: str = ""
    token_ca: str = ""
    status: str = ""
    status_reason: str = ""
    holds_amount: str = ""
    holds_pct: float = 0.0
    sold_pct: float = 0.0
    realized_pnl: float = 0.0
    buy_usd: float = 0.0
    sell_usd: float = 0.0
    n_buys: int = 0
    n_sells: int = 0
    round_trip: bool = False   # both bought and sold within the span
    reentry: bool = False      # sold then bought again later (賣了又買回來)
    first_ts: int = 0
    last_ts: int = 0
    last_action: str = ""
    mc_usd: float = 0.0


def derive_holdings(events: list[WalletEvent]) -> list[HoldingRecord]:
    """Collapse BUY/SELL events into one current-position record per
    (wallet, token), using the chronologically latest event's Holds snapshot.

    Turns the noisy buy/sell flow into "what does this wallet hold right now" —
    the single state the downstream LLM should trust over raw flow arithmetic.
    Reused for both the in-window window (this module) and the cross-window
    archive scan (holdings.py), so the rules stay identical.
    """
    groups: dict[tuple, list[WalletEvent]] = defaultdict(list)
    for ev in events:
        if ev.action not in {"BUY", "SELL"}:
            continue
        groups[(wallet_key(ev), token_key(ev), ev.chain)].append(ev)

    records: list[HoldingRecord] = []
    for (_wk, _tk, chain), evs in groups.items():
        evs.sort(key=lambda e: e.timestamp)
        latest = evs[-1]
        first = evs[0]
        buys = [e for e in evs if e.action == "BUY"]
        sells = [e for e in evs if e.action == "SELL"]
        status, reason = wallet_holding_status(latest)
        # reentry: any BUY landing after the first SELL → sold then re-bought.
        first_sell_ts = min((e.timestamp for e in sells), default=None)
        reentry = first_sell_ts is not None and any(
            e.timestamp > first_sell_ts for e in buys
        )
        latest_pnl = _latest_pnl_event(evs)
        records.append(HoldingRecord(
            wallet_name=latest.wallet_name or first.wallet_name,
            wallet_addr=latest.wallet_addr or first.wallet_addr,
            chain=chain,
            token_symbol=next((e.token_symbol for e in evs if e.token_symbol), ""),
            token_ca=next((e.token_ca for e in evs if e.token_ca), ""),
            status=status,
            status_reason=reason,
            holds_amount=latest.holds_amount,
            holds_pct=latest.holds_pct,
            sold_pct=max((e.sold_pct for e in sells), default=0.0),
            realized_pnl=latest_pnl.pnl_usd if latest_pnl else 0.0,
            buy_usd=sum(e.usd_value for e in buys),
            sell_usd=sum(e.usd_value for e in sells),
            n_buys=len(buys),
            n_sells=len(sells),
            round_trip=bool(buys and sells),
            reentry=reentry,
            first_ts=first.timestamp,
            last_ts=latest.timestamp,
            last_action=latest.action,
            mc_usd=latest.mc_usd or first.mc_usd,
        ))
    return records


def holding_status_label(r: HoldingRecord) -> str:
    """Human (zh-Hant) status tag for one reconciled position."""
    if r.status == HOLDING:
        if r.reentry:
            return "賣後回補仍持有"
        if r.round_trip:
            return "來回後仍持有"
        if r.last_action == "SELL":
            return "減倉仍持有"
        return "新進/加倉"
    if r.status == EXITED:
        return "已清倉"
    return "持倉不明"


def _holding_token_line(r: HoldingRecord) -> str:
    bits = [f"${r.token_symbol or '?'}", f"[{holding_status_label(r)}]"]
    if r.holds_amount:
        bits.append(f"holds={r.holds_amount}({r.holds_pct:.2f}%)")
    if r.sold_pct:
        bits.append(f"sold={r.sold_pct:.0f}%")
    if r.buy_usd:
        bits.append(f"buy={_fmt_usd(r.buy_usd)}")
    if r.sell_usd:
        bits.append(f"sell={_fmt_usd(r.sell_usd)}")
    if r.realized_pnl:
        bits.append(f"realized_pnl={_fmt_signed_usd(r.realized_pnl)}")
    bits.append(f"last={r.last_action}@{_hm(r.last_ts)}")
    if r.token_ca:
        bits.append(f"CA=`{r.token_ca}`")
    return " ".join(bits)


def render_current_holdings(
    events: list[WalletEvent],
    max_wallets: int = 24,
    max_tokens_per_wallet: int = 8,
) -> list[str]:
    """Wallet-centric CURRENT_HOLDINGS block: per active wallet, the tokens it
    currently holds (and which it exited) with a deterministic status tag.

    This is the authoritative "who still holds what" view — it disambiguates
    net≈0 round-trips (sold-then-rebought vs flat) that the flow rollup cannot.
    """
    records = derive_holdings(events)
    if not records:
        return []

    by_wallet: dict[str, list[HoldingRecord]] = defaultdict(list)
    for r in records:
        by_wallet[r.wallet_addr or r.wallet_name].append(r)

    def wallet_rank(items: list[HoldingRecord]) -> tuple[int, float]:
        holding = sum(1 for r in items if r.status == HOLDING)
        return holding, sum(r.buy_usd + r.sell_usd for r in items)

    ranked = sorted(by_wallet.values(), key=wallet_rank, reverse=True)
    shown = ranked[:max_wallets]
    hidden_wallets = len(ranked) - len(shown)

    total_holding = sum(1 for r in records if r.status == HOLDING)
    total_exited = sum(1 for r in records if r.status == EXITED)

    out = ["## CURRENT_HOLDINGS (本窗口活躍錢包 × token 的最終持倉狀態，已去重)"]
    out.append(
        "依各 (wallet, token) 最後一筆事件的 Holds 快照判定；這是「目前是否還持有」"
        "的權威來源，請優先採用,不要再用 buy/sell 流水自行推算淨倉。"
    )
    out.append(
        f"SUMMARY active_wallets={len(by_wallet)} positions={len(records)} "
        f"holding={total_holding} exited={total_exited}"
    )
    for items in shown:
        items.sort(key=lambda r: (r.status != HOLDING, -(r.buy_usd + r.sell_usd)))
        head = items[0]
        holding_n = sum(1 for r in items if r.status == HOLDING)
        addr = _addr_short(head.wallet_addr) if head.wallet_addr else "?"
        out.append(
            f"- {head.wallet_name or '?'} ({addr}) [{head.chain}] "
            f"holding={holding_n}/{len(items)}"
        )
        for r in items[:max_tokens_per_wallet]:
            out.append(f"    · {_holding_token_line(r)}")
        if len(items) > max_tokens_per_wallet:
            out.append(f"    · …(其餘 {len(items) - max_tokens_per_wallet} 顆略)")
    if hidden_wallets > 0:
        out.append(f"OMITTED_HOLDING_WALLETS lower_priority={hidden_wallets}")
    out.append("")
    return out


def _wallet_buy_summary(evs: list[WalletEvent]) -> str:
    total = sum(e.usd_value for e in evs)
    first = min(evs, key=lambda e: e.timestamp)
    last = max(evs, key=lambda e: e.timestamp)
    parts = [
        f"{first.wallet_name} total={_fmt_usd(total)}",
        f"tx={len(evs)}",
        f"first={_hm(first.timestamp)}",
    ]
    if first.mc_usd:
        parts.append(f"first_mc={_fmt_usd(first.mc_usd)}")
    if last.timestamp != first.timestamp:
        parts.append(f"last={_hm(last.timestamp)}")
        if last.mc_usd:
            parts.append(f"last_mc={_fmt_usd(last.mc_usd)}")
    if any(e.is_first_buy for e in evs):
        parts.append("first_entry=yes")
    if last.seen_age:
        parts.append(f"seen={last.seen_age}")
    if last.holds_amount:
        parts.append(f"holds={last.holds_amount}({last.holds_pct:.2f}%)")
    return "; ".join(parts)


def _wallet_sell_summary(evs: list[WalletEvent]) -> str:
    total = sum(e.usd_value for e in evs)
    latest_pnl = _latest_pnl_event(evs)
    pnl = latest_pnl.pnl_usd if latest_pnl else 0.0
    max_sold = max((e.sold_pct for e in evs), default=0.0)
    first = min(evs, key=lambda e: e.timestamp)
    last = max(evs, key=lambda e: e.timestamp)
    parts = [
        f"{first.wallet_name} total={_fmt_usd(total)}",
        f"tx={len(evs)}",
        f"time={_hm(first.timestamp) if first.timestamp == last.timestamp else _hm(first.timestamp) + '-' + _hm(last.timestamp)}",
    ]
    if pnl:
        parts.append(f"realized_pnl={_fmt_signed_usd(pnl)}")
    if max_sold:
        parts.append(f"max_sold={max_sold:.0f}%")
    if last.mc_usd:
        parts.append(f"mc={_fmt_usd(last.mc_usd)}")
    if last.holds_amount:
        parts.append(f"holds={last.holds_amount}({last.holds_pct:.2f}%)")
    return "; ".join(parts)


def aggregate_token_flows(
    messages: list[dict],
    hours: float = 0.0,
    max_tokens: int = 14,
    max_wallets_per_token: int = 6,
    max_wallet_pnl_items: int = 8,
    max_unparsed_items: int | None = 3,
    transfer_alert_usd: float = 0.0,
    max_transfer_alerts: int = 30,
    max_holding_wallets: int = 24,
    max_tokens_per_holding_wallet: int = 8,
) -> str:
    """Render a compact token-centric rollup for the final wallet_log LLM pass.

    This keeps Ray Orange auto summaries useful: the final model sees token
    level flows instead of a long wallet-by-wallet table, so it can produce
    the analytical report without hitting the first-token cliff.

    `transfer_alert_usd > 0`: append a TRANSFER_ALERTS section listing
    tracked-wallet outgoing transfers at or above this USD value (potential
    wallet-hop / CEX deposit / internal move signal). The aggregator does
    NOT classify intent — it just surfaces raw events for downstream judgment.
    """
    events, unparsed = _parse_messages(messages)
    if not events:
        return "\n".join(unparsed) if unparsed else "(no events)"

    token_groups: dict[tuple, list[WalletEvent]] = defaultdict(list)
    wallet_token_sells: dict[tuple, list[WalletEvent]] = defaultdict(list)
    for ev in events:
        if ev.action in {"BUY", "SELL"}:
            key = (ev.token_ca or f"symbol:{ev.token_symbol}", ev.chain)
            token_groups[key].append(ev)
        if ev.action == "SELL" and ev.has_pnl:
            key = (
                ev.wallet_name,
                ev.wallet_addr or ev.wallet_name,
                ev.token_ca or f"symbol:{ev.token_symbol}",
                ev.chain,
            )
            wallet_token_sells[key].append(ev)

    wallet_pnl: dict[str, float] = defaultdict(float)
    for (wallet_name, _wallet_key, _token_key, _chain), sell_group in wallet_token_sells.items():
        latest = _latest_pnl_event(sell_group)
        if latest is not None:
            wallet_pnl[wallet_name] += latest.pnl_usd

    ranked = []
    for key, evs in token_groups.items():
        buys = [e for e in evs if e.action == "BUY"]
        sells = [e for e in evs if e.action == "SELL"]
        buy_total = sum(e.usd_value for e in buys)
        sell_total = sum(e.usd_value for e in sells)
        wallet_count = len({e.wallet_addr for e in evs if e.wallet_addr})
        score = buy_total + sell_total + wallet_count * 750
        ranked.append((score, key, evs, buys, sells, buy_total, sell_total))
    ranked.sort(key=lambda row: row[0], reverse=True)
    shown, hidden = _limit_ranked(ranked, max_tokens)

    out: list[str] = []
    out.append(
        f"WALLET_TOKEN_FLOW_ROLLUP hours={hours} raw_messages={len(messages)} "
        f"parsed={len(events)} unparsed={len(unparsed)}"
    )
    out.append(
        "Use this as compressed source data. Write the final answer in Chinese, "
        "with sections like: core takeaways, token flow ranking, wallet PnL, "
        "3x+ opportunities, timeline, and watchlist."
    )
    out.append("")

    core_candidates = []
    for score, (_ca_key, chain), evs, buys, sells, buy_total, sell_total in shown[:8]:
        sym = _event_symbol(evs)
        ca = _event_ca(evs)
        net = buy_total - sell_total
        buy_wallets = len({e.wallet_addr for e in buys if e.wallet_addr})
        sell_wallets = len({e.wallet_addr for e in sells if e.wallet_addr})
        first_buy = min(buys, key=lambda e: e.timestamp) if buys else None
        first_any = min(evs, key=lambda e: e.timestamp)
        last_any = max(evs, key=lambda e: e.timestamp)
        pnl = _realized_pnl_snapshot_total(sells)
        mcs = [e.mc_usd for e in evs if e.mc_usd]
        peak_mc = max(mcs) if mcs else 0.0
        first_buy_mc = first_buy.mc_usd if first_buy and first_buy.mc_usd else 0.0
        multiple = peak_mc / first_buy_mc if first_buy_mc and peak_mc else 0.0
        if buy_total and sell_total:
            label = "多空分歧" if abs(net) < max(buy_total, sell_total) * 0.35 else (
                "一致買入" if net > 0 else "出貨壓力"
            )
        elif buy_total:
            label = "一致買入" if buy_wallets >= 2 or buy_total >= 5000 else "留倉觀察"
        elif sell_total:
            label = "出貨壓力"
        else:
            label = "低信號"
        reasons = []
        if buy_total:
            reasons.append(f"buy={_fmt_usd(buy_total)} by {buy_wallets}w")
        if sell_total:
            reasons.append(f"sell={_fmt_usd(sell_total)} by {sell_wallets}w")
        if net:
            reasons.append(f"net={_fmt_signed_usd(net)}")
        if pnl:
            reasons.append(f"realized_pnl={_fmt_signed_usd(pnl)}")
        if multiple >= 3:
            reasons.append(f"peak_from_first_buy={multiple:.2f}x")
        if first_buy:
            reasons.append(
                f"earliest_buy={first_buy.wallet_name}@{_hm(first_buy.timestamp)}"
            )
        time_range = (
            _hm(first_any.timestamp)
            if first_any.timestamp == last_any.timestamp
            else f"{_hm(first_any.timestamp)}-{_hm(last_any.timestamp)}"
        )
        core_candidates.append(
            f"- ${sym} [{chain}] label={label}; score={score:.0f}; "
            f"time={time_range}; {', '.join(reasons)}"
            + (f"; CA=`{ca}`" if ca else "")
        )
    if core_candidates:
        out.append("## CORE_CANDIDATES")
        out.extend(core_candidates)
        out.append("")

    # Reconciled current-position view — keep it high in the rollup so the LLM
    # leads with "who still holds what" instead of inferring it from flows.
    out.extend(render_current_holdings(
        events,
        max_wallets=max_holding_wallets,
        max_tokens_per_wallet=max_tokens_per_holding_wallet,
    ))

    for _score, (_ca_key, chain), evs, buys, sells, buy_total, sell_total in shown:
        sym = _event_symbol(evs)
        ca = _event_ca(evs)
        net = buy_total - sell_total
        pnl = _realized_pnl_snapshot_total(sells)
        first_buy = min(buys, key=lambda e: e.timestamp) if buys else None
        first_any = min(evs, key=lambda e: e.timestamp)
        last_any = max(evs, key=lambda e: e.timestamp)
        mcs = [e.mc_usd for e in evs if e.mc_usd]
        peak_mc = max(mcs) if mcs else 0.0
        first_buy_mc = first_buy.mc_usd if first_buy and first_buy.mc_usd else 0.0
        multiple = peak_mc / first_buy_mc if first_buy_mc and peak_mc else 0.0
        buy_wallets = len({e.wallet_addr for e in buys if e.wallet_addr})
        sell_wallets = len({e.wallet_addr for e in sells if e.wallet_addr})

        out.append(f"## TOKEN ${sym} [{chain}]")
        if ca:
            out.append(f"CA: `{ca}`")
        out.append(
            f"FLOW buy={_fmt_usd(buy_total)} wallets={buy_wallets}; "
            f"sell={_fmt_usd(sell_total)} wallets={sell_wallets}; "
            f"net={_fmt_signed_usd(net)}; realized_pnl={_fmt_signed_usd(pnl)}"
        )
        out.append(f"TIME first={_hm(first_any.timestamp)} last={_hm(last_any.timestamp)}")
        if first_buy:
            out.append(
                f"EARLIEST_BUY wallet={first_buy.wallet_name}; time={_hm(first_buy.timestamp)}; "
                f"mc={_fmt_usd(first_buy.mc_usd) if first_buy.mc_usd else '?'}; "
                f"seen={first_buy.seen_age or '?'}"
            )
        if peak_mc:
            multiple_part = f"; multiple_from_first_buy={multiple:.2f}x" if multiple else ""
            out.append(f"VALUATION peak_seen_mc={_fmt_usd(peak_mc)}{multiple_part}")

        buy_by_wallet: dict[str, list[WalletEvent]] = defaultdict(list)
        sell_by_wallet: dict[str, list[WalletEvent]] = defaultdict(list)
        for ev in buys:
            buy_by_wallet[ev.wallet_addr].append(ev)
        for ev in sells:
            sell_by_wallet[ev.wallet_addr].append(ev)

        top_buys = sorted(
            buy_by_wallet.values(),
            key=lambda group: sum(e.usd_value for e in group),
            reverse=True,
        )
        top_sells = sorted(
            sell_by_wallet.values(),
            key=lambda group: sum(e.usd_value for e in group),
            reverse=True,
        )

        for idx, group in enumerate(top_buys[:max_wallets_per_token], start=1):
            out.append(f"TOP_BUY {idx}: {_wallet_buy_summary(group)}")
        if len(top_buys) > max_wallets_per_token:
            out.append(f"TOP_BUY_OMITTED wallets={len(top_buys) - max_wallets_per_token}")

        for idx, group in enumerate(top_sells[:max_wallets_per_token], start=1):
            out.append(f"TOP_SELL {idx}: {_wallet_sell_summary(group)}")
        if len(top_sells) > max_wallets_per_token:
            out.append(f"TOP_SELL_OMITTED wallets={len(top_sells) - max_wallets_per_token}")
        out.append("")

    if hidden:
        out.append(f"OMITTED_TOKENS lower_priority={hidden} total_tokens={len(ranked)}")
        out.append("")

    if transfer_alert_usd > 0:
        # Outgoing transfers from tracked wallets (direction != "IN" rules out
        # the "X transferred to multiple accounts" pattern where the tracked
        # wallet is among the recipients). Sort biggest first — large hops
        # are the actionable signal; the long tail of $200-$500 moves is
        # filtered by `max_transfer_alerts`.
        alerts = [
            ev for ev in events
            if ev.action == "TRANSFER"
            and ev.transfer_direction != "IN"
            and ev.usd_value >= transfer_alert_usd
        ]
        alerts.sort(key=lambda e: e.usd_value, reverse=True)
        alert_total = len(alerts)
        alerts = alerts[:max_transfer_alerts] if max_transfer_alerts > 0 else alerts
        if alerts:
            out.append(
                f"## TRANSFER_ALERTS threshold={_fmt_usd(transfer_alert_usd)} "
                f"shown={len(alerts)} total={alert_total}"
            )
            out.append(
                "Tracked-wallet outgoing transfers — possible wallet hop / "
                "CEX deposit / internal move. Surface these in the final "
                "report verbatim; do not infer intent."
            )
            for ev in alerts:
                bits = [
                    f"[{_hm(ev.timestamp)}]",
                    f"{ev.wallet_name or '?'}",
                    f"({_addr_short(ev.wallet_addr)})",
                    f"out {_fmt_usd(ev.usd_value)}",
                ]
                if ev.token_symbol:
                    bits.append(f"of ${ev.token_symbol}")
                if ev.chain:
                    bits.append(f"[{ev.chain}]")
                out.append("- " + " ".join(bits))
            if alert_total > len(alerts):
                out.append(f"- omitted_smaller_transfers={alert_total - len(alerts)}")
            out.append("")

    pnl_items = sorted(wallet_pnl.items(), key=lambda item: abs(item[1]), reverse=True)
    pnl_items, pnl_hidden = _limit_ranked(pnl_items, max_wallet_pnl_items)
    if pnl_items:
        out.append("## WALLET_REALIZED_PNL")
        for wallet, pnl in pnl_items:
            out.append(f"- {wallet}: {_fmt_signed_usd(pnl)}")
        if pnl_hidden:
            out.append(f"- omitted_wallet_pnl={pnl_hidden}")
        out.append("")

    if unparsed:
        shown_unparsed, hidden_unparsed = _limit_ranked(unparsed, max_unparsed_items)
        out.append(f"## UNPARSED count={len(unparsed)} shown={len(shown_unparsed)}")
        for line in shown_unparsed:
            out.append(f"- {line}")
        if hidden_unparsed:
            out.append(f"- omitted_unparsed={hidden_unparsed}")
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def aggregate_events(
    messages: list[dict],
    hours: float = 0.0,
    max_buy_items: int | None = None,
    max_sell_items: int | None = None,
    max_multi_items: int | None = None,
    max_transfer_items: int | None = None,
    max_unparsed_items: int | None = None,
) -> str:
    """Parse all messages, group by (wallet, token, action), render compact text.

    Output is plain markdown — fed as `msg_text` into PROMPT_WALLET_TEMPLATE.
    """
    events, unparsed = _parse_messages(messages)

    if not events:
        return "\n".join(unparsed) if unparsed else "(no events)"

    # Bucket by action
    buckets: dict[str, dict[tuple, list[WalletEvent]]] = {
        "BUY": defaultdict(list),
        "SELL": defaultdict(list),
        "SWAP": defaultdict(list),
        "TRANSFER": defaultdict(list),
        "APPROVE": defaultdict(list),
    }
    for ev in events:
        if ev.action in buckets:
            key = (ev.wallet_addr, ev.token_ca, ev.chain)
            buckets[ev.action][key].append(ev)

    out: list[str] = []
    out.append(
        f"【錢包活動聚合】時間範圍 {hours}h · "
        f"原始 {len(messages)} 則 · 解析 {len(events)} 則"
        + (f" · 未解析 {len(unparsed)} 則" if unparsed else "")
    )
    out.append("")

    # ----- BUY 聚合 ---------------------------------------------------------
    buy_items = list(buckets["BUY"].items())
    if buy_items:
        buy_items.sort(key=lambda kv: sum(e.usd_value for e in kv[1]), reverse=True)
        buy_total = len(buy_items)
        buy_items, hidden = _limit_ranked(buy_items, max_buy_items)
        out.append(f"## 🟢 買入聚合({len(buy_items)} 組,按總金額排序)")
        for (waddr, ca, chain), evs in buy_items:
            total = sum(e.usd_value for e in evs)
            first = min(evs, key=lambda e: e.timestamp)
            last = max(evs, key=lambda e: e.timestamp)
            sym = next((e.token_symbol for e in evs if e.token_symbol), "?")
            seen = first.seen_age or "?"
            tag = " ★首進" if any(e.is_first_buy for e in evs) else ""
            # Per-event MC at the earliest / latest buy — gives the analysis
            # model the "誰最早進場 @ 多少 MC" signal it needs for ranking.
            first_mc = _fmt_usd(first.mc_usd) if first.mc_usd else "?"
            if first.timestamp == last.timestamp:
                timing = f"首買 {_hm(first.timestamp)} @ MC {first_mc}"
            else:
                last_mc = _fmt_usd(last.mc_usd) if last.mc_usd else "?"
                timing = (f"首買 {_hm(first.timestamp)} @ MC {first_mc} · "
                          f"末買 {_hm(last.timestamp)} @ MC {last_mc}")
            extras = []
            if first.price_usd:
                extras.append(f"price ${first.price_usd:g}")
            if first.lq_usd:
                extras.append(f"LQ {_fmt_usd(first.lq_usd)}")
            if last.holds_amount:
                extras.append(f"holds {last.holds_amount} ({last.holds_pct:.2f}%)")
            out.append(
                f"- **{first.wallet_name}** ({_addr_short(waddr)}) × "
                f"${sym} [{chain}]{tag}: {len(evs)} 筆 · 合計 {_fmt_usd(total)} · "
                f"{timing} · Seen {seen}"
                + (f" · {' · '.join(extras)}" if extras else "")
            )
            if ca:
                out.append(f"  CA: `{ca}`")
        if hidden:
            out.append(f"- Omitted {hidden} lower-value BUY groups by auto cap (total groups: {buy_total}).")
        out.append("")

    # ----- SELL 聚合 --------------------------------------------------------
    sell_items = list(buckets["SELL"].items())
    sell_hidden = 0
    if sell_items:
        sell_items.sort(key=lambda kv: sum(e.usd_value for e in kv[1]), reverse=True)
        sell_total = len(sell_items)
        sell_items, sell_hidden = _limit_ranked(sell_items, max_sell_items)
        out.append(f"## 🔴 賣出聚合({len(sell_items)} 組,按總金額排序)")
        for (waddr, ca, chain), evs in sell_items:
            total = sum(e.usd_value for e in evs)
            latest_pnl = _latest_pnl_event(evs)
            pnl = latest_pnl.pnl_usd if latest_pnl else 0.0
            pnl_pct = latest_pnl.pnl_pct if latest_pnl else 0.0
            max_sold = max((e.sold_pct for e in evs), default=0.0)
            first = min(evs, key=lambda e: e.timestamp)
            last = max(evs, key=lambda e: e.timestamp)
            sym = next((e.token_symbol for e in evs if e.token_symbol), "?")
            pnl_str = _fmt_signed_usd(pnl)
            time_range = _hm(first.timestamp) if first.timestamp == last.timestamp \
                else f"{_hm(first.timestamp)}–{_hm(last.timestamp)}"
            extras = []
            if last.holds_amount:
                extras.append(f"holds {last.holds_amount} ({last.holds_pct:.2f}%)")
            if last.upnl_usd:
                extras.append(f"uPnL {_fmt_signed_usd(last.upnl_usd)}")
            if last.mc_usd:
                extras.append(f"MC {_fmt_usd(last.mc_usd)}")
            if last.lq_usd:
                extras.append(f"LQ {_fmt_usd(last.lq_usd)}")
            out.append(
                f"- **{first.wallet_name}** ({_addr_short(waddr)}) × "
                f"${sym} [{chain}]: {len(evs)} 筆 · 合計 {_fmt_usd(total)} · "
                f"PnL {pnl_str} (latest {pnl_pct:+.1f}%) · max sold {max_sold:.0f}% · "
                f"{time_range}"
                + (f" · {' · '.join(extras)}" if extras else "")
            )
            if ca:
                out.append(f"  CA: `{ca}`")
        out.append("")

    # ----- 多錢包連動(相同 token 有 ≥2 個不同錢包同方向)----------------------
    if sell_hidden:
        out.append(f"- Omitted {sell_hidden} lower-value SELL groups by auto cap (total groups: {sell_total}).")
        out.append("")

    cross_counts: dict[tuple, set] = defaultdict(set)
    for action in ("BUY", "SELL"):
        for (waddr, ca, chain), _evs in buckets[action].items():
            if ca:
                cross_counts[(ca, chain, action)].add(waddr)
    multi = [(k, w) for k, w in cross_counts.items() if len(w) >= 2]
    multi_hidden = 0
    if multi:
        multi.sort(key=lambda kw: -len(kw[1]))
        multi_total = len(multi)
        multi, multi_hidden = _limit_ranked(multi, max_multi_items)
        out.append(f"## 🎯 多錢包連動(≥2 錢包相同動作,{len(multi)} 組)")
        for (ca, chain, action), wallets in multi:
            evs = [e for e in events
                   if e.token_ca == ca and e.chain == chain and e.action == action]
            sym = next((e.token_symbol for e in evs if e.token_symbol), "?")
            names = sorted({e.wallet_name for e in evs})
            total = sum(e.usd_value for e in evs)
            first = min(evs, key=lambda e: e.timestamp)
            last = max(evs, key=lambda e: e.timestamp)
            time_range = _hm(first.timestamp) if first.timestamp == last.timestamp \
                else f"{_hm(first.timestamp)}–{_hm(last.timestamp)}"
            out.append(
                f"- ${sym} [{chain}] {action}: {len(wallets)} 錢包 "
                f"({', '.join(names)}) · 合計 {_fmt_usd(total)} · {time_range}"
            )
            out.append(f"  CA: `{ca}`")
        out.append("")

    # ----- TRANSFER ---------------------------------------------------------
    if multi_hidden:
        out.append(f"- Omitted {multi_hidden} lower-priority multi-wallet groups by auto cap (total groups: {multi_total}).")
        out.append("")

    tx_items = list(buckets["TRANSFER"].items())
    if tx_items:
        tx_items.sort(key=lambda kv: sum(e.usd_value for e in kv[1]), reverse=True)
        transfer_limit = max_transfer_items if max_transfer_items is not None else 20
        shown, _transfer_hidden = _limit_ranked(tx_items, transfer_limit)
        out.append(f"## 💸 轉帳聚合({len(tx_items)} 組,顯示前 {len(shown)} 組)")
        for (waddr, ca, chain), evs in shown:
            total = sum(e.usd_value for e in evs)
            first = min(evs, key=lambda e: e.timestamp)
            sym = next((e.token_symbol for e in evs if e.token_symbol), "?")
            direction = "轉入追蹤錢包" if first.transfer_direction == "IN" else "轉帳"
            from_part = ""
            if first.transfer_from_name or first.transfer_from_addr:
                from_part = f" · from {first.transfer_from_name or _addr_short(first.transfer_from_addr)}"
            out.append(
                f"- **{first.wallet_name}** ({_addr_short(waddr)}) {direction} ${sym} [{chain}]: "
                f"{_fmt_usd(total)} · {len(evs)} 筆 · 首 {_hm(first.timestamp)}"
                f"{from_part}"
            )
            if ca:
                out.append(f"  CA: `{ca}`")
        if len(tx_items) > len(shown):
            out.append(f"- 另有 {len(tx_items) - len(shown)} 組較小轉帳未展開")
        out.append("")

    # ----- APPROVE 摺疊 ----------------------------------------------------
    approve_items = list(buckets["APPROVE"].items())
    if approve_items:
        out.append(
            f"## 🆗 APPROVE 摺疊:{len(approve_items)} 組,"
            f"approval 不等於成交,不展開"
        )
        out.append("")

    # ----- 未識別格式(parser miss)------------------------------------------
    if unparsed:
        unparsed_limit = max_unparsed_items if max_unparsed_items is not None else 20
        shown, hidden = _limit_ranked(unparsed, unparsed_limit)
        out.append(f"## ⚠️ 未識別格式({len(unparsed)} 則,顯示前 {len(shown)})")
        for line in shown:
            out.append(f"- {line}")
        if hidden:
            out.append(f"- Omitted {hidden} unparsed messages by display cap.")
        out.append("")

    return "\n".join(out).rstrip() + "\n"
