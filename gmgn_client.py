"""Thin synchronous wrapper around the `gmgn-cli` binary for on-chain reads.

Used by holdings.py to reconcile Tier-1 wallets' TG-derived positions against
their real on-chain balances. We shell out to `gmgn-cli portfolio holdings
--raw` and parse the JSON — no network code lives here. gmgn-cli owns auth
(GMGN_API_KEY in ~/.config/gmgn/.env) and the leaky-bucket rate limiter.

The auto-summary pipeline is synchronous, so this uses subprocess.run rather
than asyncio. Every call is best-effort: on any failure it raises GmgnError,
and callers are expected to swallow it (a holdings lookup must never break a
summary run).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)

_GMGN_BIN = (
    shutil.which("gmgn-cli")
    or shutil.which("gmgn-cli.cmd")
    or os.environ.get("GMGN_CLI_PATH")
)

# Ray Orange chain tokens -> gmgn-cli --chain codes.
CHAIN_MAP = {
    "ETHEREUM": "eth", "ETH": "eth",
    "SOLANA": "sol", "SOL": "sol",
    "BASE": "base",
    "BSC": "bsc", "BNB": "bsc",
}


class GmgnError(RuntimeError):
    pass


def available() -> bool:
    """True if a gmgn-cli binary was found at import time."""
    return bool(_GMGN_BIN)


def to_chain_code(chain: str) -> str | None:
    """Map a Ray Orange chain label (ETHEREUM/SOLANA/BASE/BSC) to a CLI code."""
    return CHAIN_MAP.get((chain or "").strip().upper())


def _popen_args(args: list[str]) -> list[str]:
    # npm installs gmgn-cli as a .cmd shim on Windows; CreateProcess can't run
    # a .cmd directly, so route it through cmd.exe.
    if os.name == "nt" and str(_GMGN_BIN).lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", _GMGN_BIN, *args]
    return [_GMGN_BIN, *args]


def _run(args: list[str], timeout: float = 30.0):
    if not _GMGN_BIN:
        raise GmgnError(
            "gmgn-cli not found on PATH (npm i -g gmgn-cli) or set GMGN_CLI_PATH"
        )
    try:
        proc = subprocess.run(
            _popen_args(args),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise GmgnError(f"gmgn-cli timeout after {timeout}s: {' '.join(args)}") from exc
    except OSError as exc:
        raise GmgnError(f"gmgn-cli spawn failed: {exc}") from exc
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        raise GmgnError(f"gmgn-cli exit {proc.returncode}: {msg[:300]}")
    out = (proc.stdout or "").strip()
    if not out:
        return {}
    # --raw emits a single JSON line, but be defensive: take the last JSON-ish
    # line in case the CLI prints a banner/log line first.
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        for line in reversed(out.splitlines()):
            line = line.strip()
            if line[:1] in ("{", "["):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        raise GmgnError(f"gmgn-cli non-JSON output: {out[:200]}")


def _extract_holdings(data) -> list[dict]:
    """Pull the holdings array out of whatever envelope gmgn-cli returns.

    The `--raw` wallet_holdings response is `{"list": [...], "next": ...}`.
    Older docs call it `holdings`; accept both plus a bare list, defensively.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("list", "holdings"):
            if isinstance(data.get(key), list):
                return data[key]
        inner = data.get("data")
        if isinstance(inner, dict):
            for key in ("list", "holdings"):
                if isinstance(inner.get(key), list):
                    return inner[key]
        if isinstance(inner, list):
            return inner
    return []


def _to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def normalize_holding(item: dict) -> dict:
    """Flatten one raw gmgn holdings item to the fields the reconcile needs.

    Note the CA lives at token.token_address (not token.address) in the raw
    response, and EVM addresses are lower-cased to match the TG-derived keys.
    """
    token = item.get("token") or {}
    ca = (token.get("token_address") or token.get("address") or "")
    if ca.startswith("0x"):
        ca = ca.lower()
    return {
        "ca": ca,
        "symbol": token.get("symbol") or "",
        "balance": _to_float(item.get("balance")),
        "usd_value": _to_float(item.get("usd_value")),
        "unrealized_profit": _to_float(item.get("unrealized_profit")),
        "realized_profit": _to_float(item.get("realized_profit")),
        "total_profit": _to_float(item.get("total_profit")),
        "price": _to_float(token.get("price")),
        "last_active_ts": int(_to_float(item.get("last_active_timestamp"))),
    }


def fetch_holdings(
    chain_code: str,
    wallet: str,
    limit: int = 30,
    timeout: float = 30.0,
) -> list[dict]:
    """Return the wallet's open on-chain holdings, sorted by USD value desc.

    Each item is a raw gmgn holdings dict (see gmgn-portfolio SKILL.md): keys of
    interest are token.{address,symbol}, balance, usd_value, unrealized_profit,
    total_profit, last_active_timestamp.
    """
    data = _run([
        "portfolio", "holdings",
        "--chain", chain_code,
        "--wallet", wallet,
        "--order-by", "usd_value",
        "--direction", "desc",
        "--limit", str(limit),
        "--raw",
    ], timeout=timeout)
    out = []
    for raw in _extract_holdings(data):
        if not isinstance(raw, dict):
            continue
        h = normalize_holding(raw)
        if h["ca"] and (h["balance"] > 0 or h["usd_value"] > 0):
            out.append(h)
    return out
