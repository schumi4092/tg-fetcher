"""Entity (ticker / handle / CA) extraction shared by coin + watchtower blueprints."""

import json
import re


# Pull $ticker / @handle / CA from arbitrary text. Symbols max 10 chars to
# avoid catching long words, CAs are length-bounded by chain (Solana = base58
# 32-44, EVM = 0x + 40 hex). Conservative — better to miss than to fabricate.
RE_TICKER = re.compile(r"\$([A-Za-z][A-Za-z0-9_]{1,9})\b")
RE_HANDLE = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z0-9_]{3,15})\b")
RE_CA_EVM = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
RE_CA_SOL = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
# Common false positives for the Solana CA pattern (base58 is broad).
CA_SOL_BLOCKLIST = {
    "WindowsPowerShell", "ProgramFiles", "AppData", "LocalAppData",
}


def harvest_entities_from_text(text):
    """Extract {symbol|handle|ca}: set() from text. Conservative."""
    if not text:
        return {"symbol": set(), "handle": set(), "ca": set()}
    symbols = {m.upper() for m in RE_TICKER.findall(text)}
    handles = {m.lower() for m in RE_HANDLE.findall(text)}
    cas = set(RE_CA_EVM.findall(text))
    for m in RE_CA_SOL.findall(text):
        if m in CA_SOL_BLOCKLIST:
            continue
        # Solana CAs typically have mixed case — pure-uppercase or pure-digit
        # strings of this length are usually noise (URLs, hashes, etc).
        if m.isdigit() or m.isupper() or m.islower():
            continue
        cas.add(m)
    return {"symbol": symbols, "handle": handles, "ca": cas}


def harvest_from_summary_json(summary_json):
    """Pull entities out of the structured summary_json blob (broadcast/wallet)."""
    out = {"symbol": set(), "handle": set(), "ca": set()}
    if not summary_json:
        return out
    try:
        data = json.loads(summary_json) if isinstance(summary_json, str) else summary_json
    except Exception:
        return out
    if not isinstance(data, dict):
        return out

    def _walk(value):
        if isinstance(value, str):
            ents = harvest_entities_from_text(value)
            for k in out:
                out[k] |= ents[k]
        elif isinstance(value, dict):
            for v in value.values():
                _walk(v)
        elif isinstance(value, list):
            for v in value:
                _walk(v)

    # Concentrate on the high-signal sections; full-walk would catch
    # irrelevant noise from `report.platform` etc.
    for key in ("key_takeaways", "events", "kol_opinions", "actionable",
                "watchlist", "market_by_chain", "checklist", "radar",
                "immediate", "needs_context", "weak_signals", "expired",
                "stale_or_repeat", "updates", "follows"):
        if key in data:
            _walk(data[key])
    return out
