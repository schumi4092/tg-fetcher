"""Sync HTTP wrapper for the 6551 Twitter REST API.

Mirrors the subset of opentwitter-mcp's TwitterAPIClient that the Watchtower
entity brief uses. Sync (httpx.Client) because Flask handlers are sync.

Public surface:
    available() -> bool
    search(keywords, ...) -> list[dict]      # tweets matching query
    user_tweets(username, ...) -> list[dict] # recent tweets from user
    tweet_by_id(tw_id) -> dict | None        # full tweet + nested context

All functions are best-effort: log + return empty on failure rather than
raise, so a missing/expired token won't break the brief endpoint.
"""

from __future__ import annotations

import atexit
import re
import threading
from typing import Any, Optional

import httpx

from config import TWITTER_API_BASE, TWITTER_TOKEN, logger

_REQUEST_TIMEOUT = 25.0  # 6551 search can be slow on cold queries

# Module-level Client so we reuse the TCP/TLS connection across calls.
# httpx.Client is thread-safe; one shared instance is the recommended pattern.
_client_lock = threading.Lock()
_shared_client: Optional[httpx.Client] = None


def available() -> bool:
    """True iff a token is configured. Cheap — used to short-circuit."""
    return bool(TWITTER_TOKEN)


def _get_client() -> Optional[httpx.Client]:
    """Lazily build the shared httpx.Client. None when token not configured."""
    global _shared_client
    if not TWITTER_TOKEN:
        return None
    with _client_lock:
        if _shared_client is None:
            _shared_client = httpx.Client(
                base_url=TWITTER_API_BASE,
                headers={
                    "Authorization": f"Bearer {TWITTER_TOKEN}",
                    "Content-Type": "application/json",
                },
                timeout=_REQUEST_TIMEOUT,
                transport=httpx.HTTPTransport(retries=1),
            )
        return _shared_client


def _close_shared_client():
    global _shared_client
    with _client_lock:
        if _shared_client is not None:
            try:
                _shared_client.close()
            except Exception:
                pass
            _shared_client = None


atexit.register(_close_shared_client)


def _post(path: str, body: dict) -> Optional[dict]:
    client = _get_client()
    if client is None:
        return None
    try:
        r = client.post(path, json=body)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        logger.warning("Twitter API %s -> %s: %s", path, e.response.status_code,
                       (e.response.text or "")[:200])
    except Exception as e:
        logger.warning("Twitter API %s failed: %s", path, e)
    return None


def search(
    keywords: str,
    *,
    max_results: int = 20,
    since_date: Optional[str] = None,
    until_date: Optional[str] = None,
    exclude_replies: bool = True,
    exclude_retweets: bool = False,
    min_likes: int = 0,
    product: str = "Top",
) -> list[dict]:
    """Search Twitter for tweets matching `keywords`.

    `since_date` / `until_date` are YYYY-MM-DD strings (not datetimes).
    Returns the `data` array from the response, or [] on error.
    """
    if not keywords:
        return []
    body: dict[str, Any] = {
        "keywords": keywords,
        "maxResults": max_results,
        "product": product,
        "excludeReplies": bool(exclude_replies),
        "excludeRetweets": bool(exclude_retweets),
    }
    if since_date:
        body["sinceDate"] = since_date
    if until_date:
        body["untilDate"] = until_date
    if min_likes > 0:
        body["minLikes"] = min_likes
    resp = _post("/open/twitter_search", body)
    if not resp:
        return []
    data = resp.get("data") or []
    if not isinstance(data, list):
        return []
    # 6551 ignores small `maxResults` and always returns ~20. Hard-slice
    # client-side so callers actually get what they asked for.
    return data[:max_results]


def user_tweets(
    username: str,
    *,
    max_results: int = 20,
    include_replies: bool = False,
    include_retweets: bool = False,
) -> list[dict]:
    """Get recent tweets from `username` (no @)."""
    if not username:
        return []
    body = {
        "username": username.lstrip("@"),
        "maxResults": max_results,
        "product": "Latest",
        "includeReplies": bool(include_replies),
        "includeRetweets": bool(include_retweets),
    }
    resp = _post("/open/twitter_user_tweets", body)
    if not resp:
        return []
    data = resp.get("data") or []
    if not isinstance(data, list):
        return []
    return data[:max_results]


def tweet_by_id(tw_id: str) -> Optional[dict]:
    """Fetch a single tweet (with its reply/quote context) by ID."""
    if not tw_id:
        return None
    resp = _post("/open/twitter_tweet_by_id", {"twId": str(tw_id)})
    if not resp:
        return None
    return resp.get("data")


# ---------------------------------------------------------------------------
# Tweet → text formatter
# ---------------------------------------------------------------------------

# 6551's response field naming isn't documented in the repo, so we accept
# multiple common shapes (camelCase, snake_case, nested under `user`/`author`).
def _tweet_field(t: dict, *keys: str, default: str = "") -> str:
    """Try a list of keys (supports dotted paths). Return first non-empty."""
    for key in keys:
        cur: Any = t
        for part in key.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                cur = None
                break
        if cur not in (None, "", [], {}):
            return str(cur)
    return default


def _parse_tweet_date(raw: str) -> str:
    """Normalize 6551's `createdAt` to `YYYY-MM-DD HH:MM` in UTC+8.

    Accepts both Twitter's classic format ("Sat Apr 25 13:56:03 +0000 2026")
    and ISO-8601 ("2026-04-25T13:56:03+00:00"). Falls back to first 16 chars.
    """
    if not raw:
        return ""
    from db import to_taipei_str
    # Twitter classic: "Sat Apr 25 13:56:03 +0000 2026"
    if " " in raw and "+" in raw:
        try:
            from datetime import datetime
            dt = datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y")
            return to_taipei_str(dt)
        except Exception:
            pass
    converted = to_taipei_str(raw)
    if converted:
        return converted
    return raw[:16] if len(raw) >= 16 else raw


def format_tweet_line(t: dict, max_len: int = 320) -> str:
    """Render a single tweet as one compact line for the LLM prompt.

    Format: `[YYYY-MM-DD HH:MM] @handle (Nfo) ❤N 🔁M 👁N: body…`
    `Nfo` = follower count abbreviated (signal for KOL weight).
    """
    if not isinstance(t, dict):
        return ""
    text = _tweet_field(t, "text", "full_text", "fullText", "content")
    if not text:
        return ""
    # 6551's actual response uses userScreenName / userName at top level.
    # Keep nested fallbacks for robustness across alt API shapes.
    handle = _tweet_field(
        t,
        "userScreenName", "user.screen_name", "user.username", "user.handle",
        "author.screen_name", "author.username", "author.handle",
        "screen_name", "username", "handle",
    )
    display_name = _tweet_field(t, "userName", "user.name", "author.name")
    created = _parse_tweet_date(_tweet_field(
        t, "createdAt", "created_at", "timestamp", "createdTime", "date",
    ))
    likes = _tweet_field(t, "favoriteCount", "favorite_count", "likeCount",
                         "public_metrics.like_count", default="")
    rts = _tweet_field(t, "retweetCount", "retweet_count",
                       "public_metrics.retweet_count", default="")
    replies = _tweet_field(t, "replyCount", "reply_count",
                           "public_metrics.reply_count", default="")
    views = _tweet_field(t, "viewCount", "view_count", default="")
    followers = _tweet_field(t, "userFollowers", "user.followers_count",
                             "author.public_metrics.followers_count", default="")
    body = " ".join(text.split())  # collapse all whitespace incl. newlines
    if len(body) > max_len:
        body = body[:max_len] + "…"
    metrics = []
    if likes: metrics.append(f"❤{likes}")
    if rts: metrics.append(f"🔁{rts}")
    if replies: metrics.append(f"💬{replies}")
    if views: metrics.append(f"👁{_abbrev_n(views)}")
    metrics_str = " ".join(metrics)
    parts = [f"[{created}]" if created else "[—]"]
    if handle:
        author = "@" + handle.lstrip("@")
        if display_name and display_name != handle:
            author += f"({display_name}"
            if followers:
                author += f", {_abbrev_n(followers)}fo"
            author += ")"
        elif followers:
            author += f"({_abbrev_n(followers)}fo)"
        parts.append(author)
    if metrics_str:
        parts.append(metrics_str)
    return " ".join(parts) + ": " + body


# ---------------------------------------------------------------------------
# Low-signal tweet filter (airdrop spam, exchange marketing boilerplate)
# ---------------------------------------------------------------------------

# Phrases that almost always mean "this tweet is exchange/giveaway noise,
# not signal about the entity itself". Exchange listing announcements ARE
# kept (they're milestone events) — those are caught by a separate, looser
# pattern that allows the listing through.
_AIRDROP_PATTERNS = (
    "drop your uid", "drop uid", "register on weex", "register on mexc",
    "rt to win", "retweet to win", "retweet to enter", "tag friends",
    "tag a fren", "🎁 giveaway", "🎁giveaway", "win $", "winners 🔹",
    "winners ‣", "to enter:", "follow + rt",
)
_AIRDROP_DUAL_TOKENS = (
    # Need any 2 of these together to be confident it's a giveaway tweet.
    "airdrop", "winners", "register", "claim", "🎁", "💰",
    "giveaway", "lucky", "raffle",
)


def is_low_signal_tweet(t: dict, *, min_chars: int = 12) -> bool:
    """Heuristic: identify tweets that drown out signal about the entity.

    Returns True for airdrop/giveaway spam, link-only tweets, and tweets
    that are too short to carry a thesis. Preserves exchange listing tweets
    (milestone events) by NOT matching on "trading" / "listing" alone.
    """
    if not isinstance(t, dict):
        return True
    text = _tweet_field(t, "text", "full_text", "fullText", "content")
    if not text or len(text.strip()) < min_chars:
        return True
    lt = text.lower()

    # Direct giveaway phrases — high precision.
    if any(p in lt for p in _AIRDROP_PATTERNS):
        return True

    # Dual-token: "airdrop" alone is OK (could be discussing tokenomics);
    # but "airdrop" + "winners" or "claim" is almost always the noise.
    matches = sum(1 for tok in _AIRDROP_DUAL_TOKENS if tok in lt)
    if matches >= 2:
        return True

    # Mostly URLs — strip them and see what's left.
    body_no_url = _RE_URL.sub("", text).strip()
    if len(body_no_url) < min_chars:
        return True

    return False


_RE_URL = re.compile(r"https?://\S+")


def _abbrev_n(n) -> str:
    """1234 → 1.2K, 1234567 → 1.2M, etc. Keeps tweet lines compact."""
    try:
        n = float(str(n).replace(",", ""))
    except Exception:
        return str(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(int(n))
