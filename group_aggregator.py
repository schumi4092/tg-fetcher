"""Deterministic high-signal rollup for noisy group_chat auto summaries."""

from collections import Counter, defaultdict
import re

from db import to_taipei_str


RE_TICKER = re.compile(r"\$([A-Za-z][A-Za-z0-9_]{1,9})\b")
RE_CA_EVM = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
RE_CA_SOL = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
RE_URL = re.compile(r"https?://\S+|t\.me/\S+|x\.com/\S+|twitter\.com/\S+", re.I)

CA_SOL_BLOCKLIST = {
    "WindowsPowerShell", "ProgramFiles", "AppData", "LocalAppData",
}

HIGH_SIGNAL_RE = re.compile(
    r"\b("
    r"ca|contract|address|deploy|deployed|launch|launched|mint|airdrop|claim|"
    r"fdv|mcap|market\s*cap|mc|liq|liquidity|dex|dexscreener|pump|pumpfun|"
    r"buy|bought|ape|entry|entered|sell|sold|exit|holder|wallet|whale|"
    r"rug|scam|hack|exploit|partnership|listing|binance|coinbase|okx|bybit|"
    r"3x|5x|10x|100x|alpha|thesis|cabal|dev|team|founder|rebrand|migration"
    r")\b",
    re.I,
)
VALUE_RE = re.compile(
    r"(\$?\d+(?:\.\d+)?\s*[kmbKMB]\b|\d+(?:\.\d+)?x\b|\+\d+(?:\.\d+)?%)"
)


def _clean_text(text):
    return " ".join((text or "").replace("\n", " ").split())


def _message_time(message):
    return to_taipei_str(message.get("date"), fmt="%H:%M") or "??:??"


def _sender(message):
    return message.get("from") or message.get("sender_name") or "unknown"


def _trust_mark(message, trust_map):
    sender_id = message.get("sender_id")
    level = trust_map.get(sender_id) if sender_id else None
    if level == "trusted":
        return "⭐"
    if level == "noise":
        return "🔇"
    return ""


def _line(message, trust_map):
    mark = _trust_mark(message, trust_map)
    prefix = f"{mark}{_sender(message)}" if mark else _sender(message)
    media = f" [{message.get('media')}]" if message.get("media") else ""
    return f"[{_message_time(message)}] {prefix}: {_clean_text(message.get('text'))}{media}"


def _entities(text):
    text = text or ""
    tickers = {m.upper() for m in RE_TICKER.findall(text)}
    cas = set(RE_CA_EVM.findall(text))
    for m in RE_CA_SOL.findall(text):
        if m in CA_SOL_BLOCKLIST or m.isdigit() or m.isupper() or m.islower():
            continue
        cas.add(m)
    return tickers, cas


def _score_message(message, trust_map):
    text = message.get("text") or ""
    tickers, cas = _entities(text)
    score = 0
    reasons = []
    if tickers:
        score += 5 + min(len(tickers), 3)
        reasons.append("ticker")
    if cas:
        score += 10 + min(len(cas), 2) * 2
        reasons.append("ca")
    if RE_URL.search(text):
        score += 3
        reasons.append("url")
    if HIGH_SIGNAL_RE.search(text):
        score += 4
        reasons.append("keyword")
    if VALUE_RE.search(text):
        score += 2
        reasons.append("number")
    if trust_map.get(message.get("sender_id")) == "trusted":
        score += 6
        reasons.append("trusted")
    if message.get("media"):
        score += 1
    return score, reasons, tickers, cas


def _append_capped(out, line, target_chars):
    if sum(len(x) + 1 for x in out) + len(line) + 1 > target_chars:
        return False
    out.append(line)
    return True


def build_group_chat_rollup(
    messages,
    trust_map=None,
    target_chars=70000,
    max_entity_samples=5,
    max_timeline_samples=80,
    max_high_signal_lines=260,
):
    """Build a bounded source rollup that preserves trading-relevant signals."""
    trust_map = trust_map or {}
    target_chars = max(12000, int(target_chars or 70000))
    max_entity_samples = max(1, int(max_entity_samples or 5))
    max_timeline_samples = max(0, int(max_timeline_samples or 80))
    max_high_signal_lines = max(1, int(max_high_signal_lines or 260))
    scored = []
    ticker_counts = Counter()
    ca_counts = Counter()
    ticker_samples = defaultdict(list)
    ca_samples = defaultdict(list)
    sender_counts = Counter()

    for idx, message in enumerate(messages):
        text = message.get("text") or ""
        if not text and not message.get("media"):
            continue
        score, reasons, tickers, cas = _score_message(message, trust_map)
        sender_counts[_sender(message)] += 1
        line = _line(message, trust_map)
        if score > 0:
            scored.append((score, idx, reasons, line, message, tickers, cas))
        for ticker in tickers:
            ticker_counts[ticker] += 1
            if len(ticker_samples[ticker]) < max_entity_samples:
                ticker_samples[ticker].append(line)
        for ca in cas:
            ca_counts[ca] += 1
            if len(ca_samples[ca]) < max_entity_samples:
                ca_samples[ca].append(line)

    out = []
    out.append(
        f"GROUP_CHAT_SIGNAL_ROLLUP raw_messages={len(messages)} "
        f"signal_messages={len(scored)} target_chars={target_chars}"
    )
    out.append(
        "Use this as compressed source data. It preserves CA/ticker clusters, "
        "trusted/high-signal lines, URLs, numeric market clues, and timeline samples."
    )
    out.append("")

    if ticker_counts:
        out.append("## TOP_TICKERS")
        for ticker, count in ticker_counts.most_common(20):
            out.append(f"### ${ticker} mentions={count}")
            for sample in ticker_samples[ticker][:max_entity_samples]:
                out.append(f"- {sample}")
        out.append("")

    if ca_counts:
        out.append("## CONTRACTS")
        for ca, count in ca_counts.most_common(20):
            out.append(f"### `{ca}` mentions={count}")
            for sample in ca_samples[ca][:max_entity_samples]:
                out.append(f"- {sample}")
        out.append("")

    scored.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    if scored:
        out.append("## HIGH_SIGNAL_LINES")
        per_sender = Counter()
        shown = 0
        for score, _idx, reasons, line, message, _tickers, _cas in scored:
            sender = _sender(message)
            if per_sender[sender] >= 35 and trust_map.get(message.get("sender_id")) != "trusted":
                continue
            per_sender[sender] += 1
            if not _append_capped(
                out,
                f"- score={score} reason={','.join(reasons)} {line}",
                max(target_chars - 12000, int(target_chars * 0.78)),
            ):
                break
            shown += 1
            if shown >= max_high_signal_lines:
                break
        out.append("")

    if messages and max_timeline_samples > 0:
        out.append("## TIMELINE_SAMPLE")
        step = max(1, len(messages) // max_timeline_samples)
        emitted = 0
        seen_idx = set()
        for idx in range(0, len(messages), step):
            if emitted >= max_timeline_samples:
                break
            msg = messages[idx]
            text = _clean_text(msg.get("text"))
            if not text and not msg.get("media"):
                continue
            seen_idx.add(idx)
            if not _append_capped(out, f"- {_line(msg, trust_map)}", target_chars):
                break
            emitted += 1
        out.append("")

    out.append("## SENDER_VOLUME_TOP")
    for sender, count in sender_counts.most_common(20):
        if not _append_capped(out, f"- {sender}: {count}", target_chars):
            break

    omitted = max(0, len(messages) - len(scored))
    out.append("")
    out.append(
        f"## OMITTED low_signal_or_uncategorized={omitted} "
        f"raw_messages={len(messages)}"
    )
    text = "\n".join(out).rstrip() + "\n"
    if len(text) > target_chars:
        suffix = f"\n\n[GROUP_ROLLUP_TRUNCATED to {target_chars:,} chars]\n"
        text = text[:max(0, target_chars - len(suffix))].rstrip() + suffix
    return text
