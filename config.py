"""Environment + configuration + logging setup.

Imported first by every other module; must remain dependency-free.
"""

import logging
import os
import secrets
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


def _load_env_file(path):
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            quote = value[0]
            value = value[1:-1]
            if quote == '"':
                value = value.encode("utf-8").decode("unicode_escape")
        else:
            hash_idx = value.find(" #")
            if hash_idx >= 0:
                value = value[:hash_idx].rstrip()
        os.environ.setdefault(key, value)


_load_env_file(BASE_DIR / ".env")

API_ID = os.getenv("TG_API_ID", "")
API_HASH = os.getenv("TG_API_HASH", "")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY", "")
VOYAGE_API_KEY = os.getenv("VOYAGE_API_KEY", "")
SESSION_NAME = str(BASE_DIR / "tg_web_session")
DB_PATH = str(BASE_DIR / "tg_memory.db")
HOST = "127.0.0.1"
PORT = 5151
API_ACCESS_TOKEN = os.getenv("TG_FETCHER_API_TOKEN") or secrets.token_urlsafe(32)
VOYAGE_MODEL = "voyage-multilingual-2"

MODEL_OPUS = "claude-opus-4-7"
MODEL_SONNET = "claude-sonnet-4-6"
MODEL_HAIKU = "claude-haiku-4-5"
MODEL_SHORT_NAMES = {MODEL_OPUS: "Opus", MODEL_SONNET: "Sonnet", MODEL_HAIKU: "Haiku"}
MODEL_CLI_ALIASES = {MODEL_OPUS: "opus", MODEL_SONNET: "sonnet", MODEL_HAIKU: "haiku"}

CHUNK_CHARS = 120000
DIRECT_LIMIT = 600000

# AI backend: "api" uses anthropic SDK (billed per token),
# "cli" shells out to Claude Code which uses your subscription quota.
AI_BACKEND = os.getenv("AI_BACKEND", "api").strip().lower()
CLAUDE_CLI_PATH = os.getenv("CLAUDE_CLI_PATH", "claude")
CLAUDE_CLI_TIMEOUT = int(os.getenv("CLAUDE_CLI_TIMEOUT", "300"))

# 6551 Twitter REST API — same endpoint opentwitter-mcp uses. Lets the
# Watchtower entity brief pull first-hand tweet content so the AI summary
# isn't limited to TG-transcribed second-hand mentions.
# Get token at https://6551.io/mcp.
TWITTER_API_BASE = os.getenv("TWITTER_API_BASE", "https://ai.6551.io").rstrip("/")
TWITTER_TOKEN = (os.getenv("TWITTER_TOKEN") or os.getenv("OPENNEWS_TOKEN") or "").strip()

# Auto-fetch background loop. Set INTERVAL=0 to disable. Whitelist =
# chats that have a category assignment (chat_category_map row); the
# loop archives their messages but does NOT auto-summarize — summary
# stays on-demand or via a separate schedule.
AUTO_FETCH_INTERVAL_HOURS = int(os.getenv("AUTO_FETCH_INTERVAL_HOURS", "12"))
AUTO_FETCH_HOURS = int(os.getenv("AUTO_FETCH_HOURS", "12"))

# Auto-summarize: per-day LLM summary on whitelisted chats (wallet_log uses
# its deterministic pre-aggregator before the final LLM summary).
# Runs once per cycle, dedupes via daily_summaries UNIQUE(date, chat_id)
# so restarts don't re-run today's summary. Set INTERVAL=0 to disable.
AUTO_SUMMARIZE_INTERVAL_HOURS = int(os.getenv("AUTO_SUMMARIZE_INTERVAL_HOURS", "12"))
AUTO_SUMMARIZE_HOURS = int(os.getenv("AUTO_SUMMARIZE_HOURS", "12"))
AUTO_SUMMARIZE_RUN_ON_START = (
    os.getenv("AUTO_SUMMARIZE_RUN_ON_START", "1").strip().lower()
    not in {"0", "false", "no", "off"}
)


def _parse_slot_times(raw):
    """Parse 'HH:MM,HH:MM,...' into a sorted list of (hour, minute) tuples
    in Asia/Taipei. Empty / invalid → []  (caller falls back to interval mode).
    """
    out = []
    for s in (raw or "").split(","):
        s = s.strip()
        if not s:
            continue
        try:
            h_raw, m_raw = s.split(":")
            h, m = int(h_raw), int(m_raw)
        except (ValueError, TypeError):
            continue
        if 0 <= h <= 23 and 0 <= m <= 59:
            out.append((h, m))
        elif h == 24 and m == 0:
            out.append((h, m))
    return sorted(set(out))


# Fixed-slot mode for auto-summarize. When set, the loop fires at these
# wall-clock times (Asia/Taipei) instead of running every INTERVAL_HOURS.
# Catch-up: if the most recent past slot has no auto summary written after it
# (e.g. computer was off when the slot passed), the loop runs immediately on
# startup. Format: "HH:MM,HH:MM,..." — empty disables slot mode.
AUTO_SUMMARIZE_TIMES = _parse_slot_times(os.getenv("AUTO_SUMMARIZE_TIMES", ""))

# Adaptive catch-up window cap (slot mode). When the machine has been off for
# longer than AUTO_SUMMARIZE_HOURS, the catch-up cycle widens its window to
# `(now - last_auto_run) + 1h buffer`, capped at this value. Stops the prompt
# from blowing up after a multi-day gap. 48h covers >99% of real outages.
AUTO_SUMMARIZE_CATCHUP_MAX_HOURS = int(os.getenv("AUTO_SUMMARIZE_CATCHUP_MAX_HOURS", "48"))
# Initial delay so auto-fetch (60s grace + cycle) finishes before summarize
# starts pulling from messages — otherwise the first auto-summary may run
# on stale data. 5 min covers ~30 chats safely.
AUTO_SUMMARIZE_INITIAL_DELAY_SECS = int(os.getenv("AUTO_SUMMARIZE_INITIAL_DELAY_SECS", "300"))
# Watchdog idle timeout for the main generation call. CLI's stdout buffering
# on heavy summaries (Green Garden 24h, wallet_log digest) can leave the
# subprocess silent for 5+ min before first token — 300s false-positives.
# Heartbeat cadence controls how often "still alive" gets logged so a long
# generation doesn't look like a hang.
AUTO_SUMMARIZE_IDLE_TIMEOUT_SECS = int(os.getenv("AUTO_SUMMARIZE_IDLE_TIMEOUT_SECS", "600"))
AUTO_SUMMARIZE_HEARTBEAT_SECS = int(os.getenv("AUTO_SUMMARIZE_HEARTBEAT_SECS", "60"))
# Wall-clock budget for a whole slot cycle. <=0 disables budget-based
# fallback, leaving the per-call idle watchdog as the guard against hangs.
AUTO_SUMMARIZE_SLOT_BUDGET_SECS = int(os.getenv("AUTO_SUMMARIZE_SLOT_BUDGET_SECS", "0"))
AUTO_SUMMARIZE_SLOT_FALLBACK_MIN_REMAINING_SECS = int(
    os.getenv("AUTO_SUMMARIZE_SLOT_FALLBACK_MIN_REMAINING_SECS", "240"))

# User-triggered AI jobs (coin profile draft/fill, watchtower brief, etc.)
# share the same external Claude CLI queue as background summaries when
# AI_BACKEND=cli. In that mode, a background run can make an otherwise healthy
# foreground request wait much longer for its first token, so inherit the
# wider auto-summary threshold by default instead of false-failing at 300s.
_foreground_ai_default_timeout = (
    AUTO_SUMMARIZE_IDLE_TIMEOUT_SECS if AI_BACKEND == "cli" else 300
)
FOREGROUND_AI_IDLE_TIMEOUT_SECS = int(os.getenv(
    "FOREGROUND_AI_IDLE_TIMEOUT_SECS",
    str(_foreground_ai_default_timeout),
))

# wallet_log auto-summary safeguards. Dense Ray Orange days can contain
# thousands of unique wallet/token groups; cap each section before the final
# LLM pass so auto-summary does not retry the same oversized backlog forever.
# - section caps keep BUY/SELL/multi-wallet/transfer lists bounded
# - HARD_CAP: even after compression, never send more than this — last
#   resort to dodge CLI buffer cliffs. Truncation drops the lowest-value
#   tail (small approves / micro-transfers) which is rarely actionable.
AUTO_SUMMARIZE_WALLET_HARD_CAP = int(os.getenv("AUTO_SUMMARIZE_WALLET_HARD_CAP", "24000"))
AUTO_SUMMARIZE_WALLET_LLM_PROMPT_CAP = int(os.getenv("AUTO_SUMMARIZE_WALLET_LLM_PROMPT_CAP", "30000"))
AUTO_SUMMARIZE_FALLBACK_CHARS = int(os.getenv("AUTO_SUMMARIZE_FALLBACK_CHARS", "12000"))
AUTO_SUMMARIZE_GROUP_ROLLUP_TRIGGER_CHARS = int(
    os.getenv("AUTO_SUMMARIZE_GROUP_ROLLUP_TRIGGER_CHARS", "120000"))
AUTO_SUMMARIZE_GROUP_ROLLUP_TARGET_CHARS = int(
    os.getenv("AUTO_SUMMARIZE_GROUP_ROLLUP_TARGET_CHARS", "70000"))
AUTO_SUMMARIZE_GROUP_MAX_ENTITY_SAMPLES = int(
    os.getenv("AUTO_SUMMARIZE_GROUP_MAX_ENTITY_SAMPLES", "5"))
AUTO_SUMMARIZE_GROUP_MAX_TIMELINE_SAMPLES = int(
    os.getenv("AUTO_SUMMARIZE_GROUP_MAX_TIMELINE_SAMPLES", "80"))
AUTO_SUMMARIZE_GROUP_MAX_HIGH_SIGNAL_LINES = int(
    os.getenv("AUTO_SUMMARIZE_GROUP_MAX_HIGH_SIGNAL_LINES", "260"))
# Final safety cap for group_chat msg_text — even after rollup/compress, never
# send more than this to the LLM. >100k prompts can leave the CLI subprocess
# silent (no first token) for 10+ min on Windows pipe + heavy Sonnet
# preprocessing. Truncated tail is the lowest-value portion (oldest noise).
AUTO_SUMMARIZE_GROUP_CHAT_HARD_CAP = int(
    os.getenv("AUTO_SUMMARIZE_GROUP_CHAT_HARD_CAP", "80000"))

# Model for chunk compression (the LLM pass that distills oversized chats
# down before the final Sonnet summary). Default Haiku 4.5 — 3-5x faster
# and frees up Sonnet quota for the actual summary. Set to "sonnet" to
# revert if compression quality regresses on edge cases (subtle alpha,
# heavy implication). Accepts: "haiku" | "sonnet" | "opus".
_compress_model_alias = os.getenv("AUTO_SUMMARIZE_COMPRESS_MODEL", "haiku").strip().lower()
AUTO_SUMMARIZE_COMPRESS_MODEL = {
    "haiku": MODEL_HAIKU,
    "sonnet": MODEL_SONNET,
    "opus": MODEL_OPUS,
}.get(_compress_model_alias, MODEL_HAIKU)
# Safety net: if compressed output is suspiciously short relative to input
# (Haiku gave up early / refused / hit some limit), retry once on Sonnet.
# Ratio of compressed/input chars. 0.0 disables the fallback.
AUTO_SUMMARIZE_COMPRESS_MIN_RATIO = float(
    os.getenv("AUTO_SUMMARIZE_COMPRESS_MIN_RATIO", "0.04"))
# Max workers for parallel chunk compression. Subscription Max-5x easily
# handles 3 concurrent CLI calls (Haiku especially). Earlier code forced
# this to 1 on CLI backend — comment claimed "fine at 6" but kept the
# choke. Now configurable; 3 is a safe default that still gives ~3x
# throughput vs serial.
AUTO_SUMMARIZE_COMPRESS_WORKERS = int(
    os.getenv("AUTO_SUMMARIZE_COMPRESS_WORKERS", "3"))

# Per-sender pre-summarize for group_chat: when one author dumps more than this
# many chars of raw text in a slot (alpha-caller dominance — Altcoinist /
# big-poster pattern), run a Haiku structured-extraction pass on just their
# messages BEFORE the deterministic group rollup. Their messages are then
# replaced by a single synthetic "summary" message holding the structured
# {ticker, thesis, action, target, risk} table. Keeps Sonnet's final prompt
# off the 70k hard cap on group_chat slots dominated by one long-form poster.
# Set 0 to disable.
AUTO_SUMMARIZE_PER_SENDER_TRIGGER_CHARS = int(
    os.getenv("AUTO_SUMMARIZE_PER_SENDER_TRIGGER_CHARS", "30000"))
# Per-extract chunk size — Haiku handles ~16k cleanly without first-token lag.
AUTO_SUMMARIZE_PER_SENDER_CHUNK_CHARS = int(
    os.getenv("AUTO_SUMMARIZE_PER_SENDER_CHUNK_CHARS", "16000"))
# Hard cap on per-sender pre-extract output to keep structured-table rewrite
# from itself bloating msg_text.
AUTO_SUMMARIZE_PER_SENDER_MAX_OUTPUT_CHARS = int(
    os.getenv("AUTO_SUMMARIZE_PER_SENDER_MAX_OUTPUT_CHARS", "8000"))
AUTO_SUMMARIZE_WALLET_FALLBACK_CHARS = int(os.getenv("AUTO_SUMMARIZE_WALLET_FALLBACK_CHARS", "24000"))
AUTO_SUMMARIZE_WALLET_MAX_TOKEN_ITEMS = int(os.getenv("AUTO_SUMMARIZE_WALLET_MAX_TOKEN_ITEMS", "10"))
AUTO_SUMMARIZE_WALLET_MAX_WALLETS_PER_TOKEN = int(
    os.getenv("AUTO_SUMMARIZE_WALLET_MAX_WALLETS_PER_TOKEN", "4"))
AUTO_SUMMARIZE_WALLET_AUTO_MAX_TOKENS = int(
    os.getenv("AUTO_SUMMARIZE_WALLET_AUTO_MAX_TOKENS", "3200"))
AUTO_SUMMARIZE_WALLET_IDLE_TIMEOUT_SECS = int(
    os.getenv("AUTO_SUMMARIZE_WALLET_IDLE_TIMEOUT_SECS", "480"))
AUTO_SUMMARIZE_WALLET_MAX_BUY_ITEMS = int(os.getenv("AUTO_SUMMARIZE_WALLET_MAX_BUY_ITEMS", "35"))
AUTO_SUMMARIZE_WALLET_MAX_SELL_ITEMS = int(os.getenv("AUTO_SUMMARIZE_WALLET_MAX_SELL_ITEMS", "35"))
AUTO_SUMMARIZE_WALLET_MAX_MULTI_ITEMS = int(os.getenv("AUTO_SUMMARIZE_WALLET_MAX_MULTI_ITEMS", "20"))
AUTO_SUMMARIZE_WALLET_MAX_TRANSFER_ITEMS = int(os.getenv("AUTO_SUMMARIZE_WALLET_MAX_TRANSFER_ITEMS", "10"))
AUTO_SUMMARIZE_WALLET_MAX_UNPARSED_ITEMS = int(os.getenv("AUTO_SUMMARIZE_WALLET_MAX_UNPARSED_ITEMS", "5"))

# Surface tracked-wallet outgoing TRANSFERs (= potential wallet-hop / CEX
# deposit / internal move signal) when usd_value >= this threshold. Default
# $200 catches most "operator switching wallets" patterns while filtering
# dust/approval transactions. Set 0 to disable the section entirely.
AUTO_SUMMARIZE_WALLET_TRANSFER_ALERT_USD = float(
    os.getenv("AUTO_SUMMARIZE_WALLET_TRANSFER_ALERT_USD", "200"))
# Cap on entries shown in the transfer-alert section — at $200 threshold a
# busy 12h window can produce 100+ alerts; sort by USD desc and trim.
AUTO_SUMMARIZE_WALLET_MAX_TRANSFER_ALERTS = int(
    os.getenv("AUTO_SUMMARIZE_WALLET_MAX_TRANSFER_ALERTS", "30"))

# ---------------------------------------------------------------------------
# Wallet holdings context (cross-window + on-chain reconcile).
#
# The 8h auto-summary window only shows what changed; it can't show what a
# wallet *currently* holds if the position was opened earlier. holdings.py
# scans the archived wallet_log messages over a longer lookback and reconciles
# each (wallet, token) to its latest Holds snapshot, then (Tier-1 only)
# cross-checks against real on-chain balances via gmgn-cli.
# ---------------------------------------------------------------------------
# How far back to scan archived messages when deriving standing positions.
HOLDINGS_LOOKBACK_DAYS = int(os.getenv("HOLDINGS_LOOKBACK_DAYS", "14"))
# Row cap on the archive scan — bounds cost on the high-volume wallet_log (Ray)
# feed. Newest-first, so the cap drops the oldest tail. The curated Tier-1
# feed is small and never hits this.
HOLDINGS_MAX_SCAN_ROWS = int(os.getenv("HOLDINGS_MAX_SCAN_ROWS", "6000"))
# Max wallets to render in the cross-window holdings block.
HOLDINGS_MAX_WALLETS = int(os.getenv("HOLDINGS_MAX_WALLETS", "30"))
HOLDINGS_MAX_TOKENS_PER_WALLET = int(os.getenv("HOLDINGS_MAX_TOKENS_PER_WALLET", "10"))

# Tier-1 on-chain reconcile via gmgn-cli. Off by default — it adds external
# API calls (rate-limited, uses the user's GMGN key) and is meant for the small
# curated wallet_log_priority feed. Enable with ENABLE_GMGN_RECONCILE=1.
ENABLE_GMGN_RECONCILE = os.getenv("ENABLE_GMGN_RECONCILE", "0").strip().lower() in ("1", "true", "yes", "on")
# Same reconcile for the high-volume Ray wallet_log feed — kept on a SEPARATE
# flag so enabling Tier-1 doesn't silently drag Ray's hundreds of wallets onto
# the rate limiter. When on, only the top-N active-window wallets (capped by
# GMGN_RECONCILE_MAX_WALLETS) get an on-chain lookup, never the whole feed.
ENABLE_GMGN_RECONCILE_RAY = os.getenv("ENABLE_GMGN_RECONCILE_RAY", "0").strip().lower() in ("1", "true", "yes", "on")
# Hard ceiling on how many Tier-1 wallets get an on-chain lookup per run, so a
# growing priority list can't blow the GMGN rate limit (holdings weight=2).
GMGN_RECONCILE_MAX_WALLETS = int(os.getenv("GMGN_RECONCILE_MAX_WALLETS", "12"))
# Per-call gmgn-cli timeout (seconds) and small inter-call pause to stay under
# the leaky-bucket limiter (rate=10, holdings weight=2 → ~5 req/s sustained).
GMGN_RECONCILE_TIMEOUT_SECS = float(os.getenv("GMGN_RECONCILE_TIMEOUT_SECS", "30"))
GMGN_RECONCILE_PAUSE_SECS = float(os.getenv("GMGN_RECONCILE_PAUSE_SECS", "0.25"))

# Make stdout/stderr tolerate emoji on legacy Windows code pages.
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("tg_fetcher")
