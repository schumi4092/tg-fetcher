"""GitHub trending proxy used by the Compare view.

The browser cannot reliably fetch github.com/trending because of CORS, so the
local Flask app fetches and trims the public HTML into a small JSON payload.
"""

import html
import re
import time
from urllib.parse import quote

import httpx
from flask import Blueprint, jsonify, request

from config import logger


bp = Blueprint("github_trending", __name__)

GITHUB_TRENDING_BASE = "https://github.com/trending"
TRENDING_CACHE_TTL_SECS = 15 * 60
_cache = {}


def _clean_html(value):
    value = re.sub(r"<svg\b.*?</svg>", " ", value or "", flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def _parse_count(value):
    value = (value or "").replace(",", "").strip()
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_trending_html(source, limit=20):
    """Parse GitHub's trending repository HTML into compact repo dictionaries."""
    repos = []
    articles = re.findall(
        r'<article\b[^>]*class="[^"]*\bBox-row\b[^"]*"[^>]*>(.*?)</article>',
        source or "",
        flags=re.I | re.S,
    )
    for article in articles:
        link = re.search(
            r'<h2\b.*?<a\b[^>]*href="(?P<href>/[^"/]+/[^"/]+)"[^>]*>(?P<body>.*?)</a>',
            article,
            flags=re.I | re.S,
        )
        if not link:
            continue
        full_name = _clean_html(link.group("body")).replace(" / ", "/")
        if "/" not in full_name:
            continue
        owner, name = [part.strip() for part in full_name.split("/", 1)]
        href = link.group("href").strip()

        desc_match = re.search(
            r'<p\b[^>]*class="[^"]*\bcolor-fg-muted\b[^"]*"[^>]*>(?P<body>.*?)</p>',
            article,
            flags=re.I | re.S,
        )
        lang_match = re.search(
            r'itemprop="programmingLanguage"[^>]*>(?P<lang>.*?)</span>',
            article,
            flags=re.I | re.S,
        )
        star_match = re.search(
            rf'href="{re.escape(href)}/stargazers"[^>]*>(?P<body>.*?)</a>',
            article,
            flags=re.I | re.S,
        )
        fork_match = re.search(
            rf'href="{re.escape(href)}/forks"[^>]*>(?P<body>.*?)</a>',
            article,
            flags=re.I | re.S,
        )
        period_match = re.search(
            r'([\d,]+)\s+stars?\s+(today|this week|this month)',
            _clean_html(article),
            flags=re.I,
        )

        repos.append({
            "rank": len(repos) + 1,
            "owner": owner,
            "name": name,
            "full_name": f"{owner}/{name}",
            "url": f"https://github.com{href}",
            "description": _clean_html(desc_match.group("body")) if desc_match else "",
            "language": _clean_html(lang_match.group("lang")) if lang_match else "",
            "stars": _parse_count(_clean_html(star_match.group("body"))) if star_match else 0,
            "forks": _parse_count(_clean_html(fork_match.group("body"))) if fork_match else 0,
            "period_stars": _parse_count(period_match.group(1)) if period_match else 0,
            "period_label": period_match.group(2).lower() if period_match else "",
        })
        if len(repos) >= limit:
            break
    return repos


def _fetch_trending(since, language, limit):
    lang_path = f"/{quote(language.strip())}" if language else ""
    url = f"{GITHUB_TRENDING_BASE}{lang_path}?since={quote(since)}"
    headers = {
        "Accept": "text/html,application/xhtml+xml",
        "User-Agent": "tg-fetcher compare panel",
    }
    response = httpx.get(url, headers=headers, follow_redirects=True, timeout=10)
    response.raise_for_status()
    return parse_trending_html(response.text, limit=limit)


@bp.route("/api/github/trending")
def api_github_trending():
    since = (request.args.get("since") or "daily").strip().lower()
    if since not in {"daily", "weekly", "monthly"}:
        return jsonify({"error": "since must be daily, weekly, or monthly"}), 400
    language = (request.args.get("language") or "").strip()
    limit = request.args.get("limit", 12, type=int)
    limit = max(1, min(limit or 12, 25))

    cache_key = (since, language.lower(), limit)
    now = time.time()
    refresh = request.args.get("refresh") in {"1", "true", "yes"} or "_" in request.args
    cached = _cache.get(cache_key)
    if cached and not refresh and now - cached["ts"] < TRENDING_CACHE_TTL_SECS:
        return jsonify({**cached["payload"], "cached": True})

    try:
        repos = _fetch_trending(since, language, limit)
    except Exception as exc:
        logger.warning("github trending fetch failed: %s", exc)
        if cached:
            return jsonify({**cached["payload"], "cached": True, "stale": True})
        return jsonify({"error": "github_trending_unavailable", "repos": []}), 502

    payload = {
        "source": "github_trending",
        "url": f"{GITHUB_TRENDING_BASE}{('/' + quote(language)) if language else ''}?since={since}",
        "since": since,
        "language": language,
        "repos": repos,
        "fetched_at": int(now),
    }
    _cache[cache_key] = {"ts": now, "payload": payload}
    return jsonify({**payload, "cached": False})
