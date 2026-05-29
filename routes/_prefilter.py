"""Per-chat noise pre-filter, applied to messages before they reach the
summarize pipeline. Currently scoped to the high-volume `有的沒的關注群`
broadcast chat where the forwarder bot mixes high-signal posts with low-signal
follow/reply events.

Filter rules (Tier-3 broadcast):
  - `回复了推文` replies: drop when body has no CA/$TICKER AND is shorter
    than MIN_REPLY_BODY_LEN — these are obvious Q&A chatter ("@x 谢谢").
    This rule runs even for PRIORITY_TAGS; we still want alpha KOL signal,
    but not their banter. Replies with a CA/$TICKER, or longer substantive
    replies, are kept.
  - if the leading bracket tag matches PRIORITY_TAGS, keep unconditionally
    for non-follow events (e.g. `[alpha]` is curated upstream, every tweet /
    reply event is worth surfacing)
  - keep `发布新` originals unconditionally
  - for `关注了` follows, keep project / token / NFT / onchain accounts even
    at low convergence; drop personal-profile and generic-tool accounts; keep
    the rest only when the convergence count `你关注的N个用户也关注了` reaches
    MIN_CONVERGENCE_N
  - default-keep anything that matches no rule (conservative — rather pay
    LLM tokens than silently drop a future event format)
"""

import os
import re

from routes._entities import harvest_entities_from_text


NOISY_CHATS = {"2423905766"}

MIN_CONVERGENCE_N = int(os.getenv("BROADCAST_PREFILTER_MIN_N", "5"))

# Reply body shorter than this with no CA/$TICKER is treated as Q&A chatter.
# Empirical: real chatter replies max ~20 chars ("听我说谢谢你", "NAT CODED");
# a substantive CJK project mention is typically ≥30 chars. 30 is the floor —
# raise via env if too lenient, lower if it cuts real signal.
MIN_REPLY_BODY_LEN = int(os.getenv("BROADCAST_REPLY_MIN_BODY", "30"))

# Leading bracket tags whose messages bypass the noise filter entirely.
# `[alpha]` is the user's curated upstream alpha-KOL bucket — every event
# (even a 1-convergence follow with no token mention) is worth surfacing.
PRIORITY_TAGS = {
    t.strip() for t in os.getenv("BROADCAST_PRIORITY_TAGS", "alpha").split(",")
    if t.strip()
}

_RE_CONVERGENCE = re.compile(r"你关注的(\d+)个用户也关注了")
_RE_LEADING_TAG = re.compile(r"^\s*\[([^\]]+)\]")
_RE_TRAILING_URLS = re.compile(r"(?:\s*https?://\S+\s*)+$")
_RE_BIO = re.compile(r"用户简介:(.*?)(?:\n你关注的|\nhttps?://|\Z)", re.S)
_RE_FOLLOW_TARGET = re.compile(r"关注了\s+(.+?)\s*$")

PROJECT_FOLLOW_KEYWORDS = (
    "airdrop", "ai agent", "agent", "base", "bitcoin", "bridge", "chain",
    "collection", "crypto", "dao", "defi", "dex", "ethereum", "evm",
    "fair launch", "farcaster", "fdv", "freemint", "gamefi", "infra",
    "launch", "liquidity", "lore", "l2", "mainnet", "marketplace",
    "memecoin", "mint", "monad", "nft", "onchain", "on-chain", "ordinals",
    "perp", "perps", "privacy", "protocol", "solana", "staking", "swap",
    "token", "virtuals", "web3", "whitelist",
    "代币", "項目", "项目", "鏈上", "链上", "協議", "协议", "空投", "鑄造", "铸造",
)

STRONG_PROJECT_FOLLOW_KEYWORDS = (
    "airdrop", "ai agent", "agent", "bridge", "collection", "dex",
    "fair launch", "fdv", "freemint", "hooks", "infra", "infrastructure",
    "launch", "liquidity", "mainnet", "marketplace", "mint", "network",
    "nft", "onchain app", "on-chain app", "protocol", "staking", "swap",
    "testnet", "token", "uniswap", "whitelist",
    "代币", "項目", "项目", "協議", "协议", "空投", "鑄造", "铸造",
)

PERSONAL_FOLLOW_KEYWORDS = (
    "advisor", "artist", "builder", "collector", "creator", "curator",
    "designer", "engineer", "ex ", "founder", "investor", "lawyer",
    "loyalist", "marketing", "my posts are my own", "no financial advice",
    "not financial advice", "not here for", "opinions are my own", "researcher",
    "storyteller", "student", "trader", "writer",
    "不构成投资建议", "非投资建议",
)

TOOL_FOLLOW_KEYWORDS = (
    "analytics", "analyze", "api", "bot", "dashboard", "data analytics",
    "execution", "extension", "institutional grade", "screener", "terminal",
    "tool", "track", "tracker",
    "儀表板", "仪表盘", "分析", "工具", "終端", "终端", "追蹤", "追踪",
)


def _extract_bio(text):
    m = _RE_BIO.search(text or "")
    if not m:
        return ""
    return " ".join(m.group(1).split())


def _extract_follow_target(text):
    first_line = (text or "").splitlines()[0] if text else ""
    m = _RE_FOLLOW_TARGET.search(first_line)
    if not m:
        return ""
    return m.group(1).strip()


def _follow_target_blob(text):
    bio = _extract_bio(text)
    target = _extract_follow_target(text)
    return f"{target}\n{bio}"


def _contains_any(text, keywords):
    low = (text or "").lower()
    return any(k.lower() in low for k in keywords)


def _follow_noise_kind(text):
    """Classify low-convergence follow targets that should not reach the LLM.

    Explicit ticker / CA wins because the message points to a concrete tradable
    entity. Otherwise tool/dashboard language is dropped before broad crypto
    words like "crypto" are considered project signal.
    """
    bio = _extract_bio(text)
    target_blob = _follow_target_blob(text)
    ents = harvest_entities_from_text(text)
    if ents["ca"] or ents["symbol"]:
        return None
    if _contains_any(bio, TOOL_FOLLOW_KEYWORDS):
        return "follow-tool-account"
    if (
        _contains_any(bio, PERSONAL_FOLLOW_KEYWORDS)
        and not _contains_any(target_blob, STRONG_PROJECT_FOLLOW_KEYWORDS)
    ):
        return "follow-personal-account"
    if _contains_any(target_blob, PROJECT_FOLLOW_KEYWORDS):
        return None
    return None


def _extract_reply_body(text):
    """Pull the reply body out of a `回复了推文` message — the lines between
    the `回复了推文` marker and the trailing tweet URL."""
    idx = text.find("回复了推文")
    if idx < 0:
        return ""
    body = text[idx + len("回复了推文"):].strip()
    return _RE_TRAILING_URLS.sub("", body).strip()


def _classify(text):
    """Return ('keep'|'drop', reason) for a single message body."""
    if not text:
        return ("keep", "empty")
    # Reply Q&A filter runs first — applies even under PRIORITY_TAGS so we
    # don't surface alpha-KOL banter as if it were signal.
    if "回复了推文" in text:
        body = _extract_reply_body(text)
        ents = harvest_entities_from_text(body)
        if ents["ca"] or ents["symbol"]:
            return ("keep", "reply+token")
        if len(body) >= MIN_REPLY_BODY_LEN:
            return ("keep", "reply+long")
        return ("drop", "reply-chatter")
    if "关注了" in text:
        ents = harvest_entities_from_text(text)
        if ents["ca"] or ents["symbol"]:
            return ("keep", "follow+token")
        noise_kind = _follow_noise_kind(text)
        if noise_kind:
            return ("drop", noise_kind)
        if _contains_any(_follow_target_blob(text), PROJECT_FOLLOW_KEYWORDS):
            return ("keep", "follow+project")
        m = _RE_CONVERGENCE.search(text)
        if m and int(m.group(1)) >= MIN_CONVERGENCE_N:
            return ("keep", f"follow+convergence({m.group(1)})")
        return ("drop", "follow-noise")
    tag_m = _RE_LEADING_TAG.match(text)
    if tag_m and tag_m.group(1) in PRIORITY_TAGS:
        return ("keep", f"priority[{tag_m.group(1)}]")
    if "发布新" in text:
        return ("keep", "original")
    return ("keep", "fallback")


def apply_noise_prefilter(messages, chat_id):
    """Filter `messages` for chats listed in NOISY_CHATS. No-op for other chats.

    Returns (filtered_messages, stats) where stats is None for no-op runs and
    otherwise a dict with keys: kept, dropped, total, drop_reasons.
    """
    if str(chat_id or "") not in NOISY_CHATS:
        return messages, None

    kept = []
    drop_reasons = {}
    for m in messages:
        decision, reason = _classify(m.get("text", "") or "")
        if decision == "keep":
            kept.append(m)
        else:
            drop_reasons[reason] = drop_reasons.get(reason, 0) + 1

    stats = {
        "kept": len(kept),
        "dropped": len(messages) - len(kept),
        "total": len(messages),
        "drop_reasons": drop_reasons,
    }
    return kept, stats
