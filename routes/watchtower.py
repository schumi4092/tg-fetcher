"""Watchtower routes — entity discovery, mention drilldown, AI quick brief."""

from datetime import datetime

from flask import Blueprint, jsonify, request

from config import (
    FOREGROUND_AI_IDLE_TIMEOUT_SECS,
    MODEL_OPUS,
    MODEL_SHORT_NAMES,
    MODEL_SONNET,
    logger,
)
from db import TAIPEI_TZ, get_db_ctx, to_taipei_str
import ai
import ai_backend
import twitter_client

from routes._shared import get_json_body, sse_event, sse_response
from routes._ai_stream import stream_ai_events
from routes._entities import harvest_entities_from_text, harvest_from_summary_json
from routes._bots import is_bot_message, fetch_context_for_hits


bp = Blueprint("watchtower", __name__)


@bp.route("/api/watchtower/entities")
def api_watchtower_entities():
    """Aggregate auto-harvested entities from recent daily_summaries.

    Each row = one entity (symbol/handle/ca) with mention metrics:
      - days_seen        : distinct dates the entity appeared
      - chats_seen       : distinct chats
      - first_date / last_date
      - has_profile      : already have a coin_profiles row?
      - profile_id       : link if exists
    """
    days = request.args.get("days", 14, type=int)
    days = max(1, min(days, 90))
    kind_filter = request.args.get("kind")  # symbol / handle / ca / None

    with get_db_ctx() as conn:
        rows = conn.execute(
            """
            SELECT id, date, chat_id, chat_name, summary, summary_json
            FROM daily_summaries
            WHERE date >= date('now', 'localtime', ?)
            ORDER BY date DESC
            """,
            (f"-{days} days",),
        ).fetchall()

        agg = {}
        for r in rows:
            ents = {"symbol": set(), "handle": set(), "ca": set()}
            md = harvest_entities_from_text(r["summary"] or "")
            sj = harvest_from_summary_json(r["summary_json"])
            for k in ents:
                ents[k] = md[k] | sj[k]

            for kind, values in ents.items():
                if kind_filter and kind != kind_filter:
                    continue
                for v in values:
                    key = (kind, v)
                    bucket = agg.get(key)
                    if bucket is None:
                        bucket = {
                            "kind": kind, "value": v,
                            "days": set(), "chats": set(),
                            "first_date": r["date"], "last_date": r["date"],
                            "summary_refs": [],
                        }
                        agg[key] = bucket
                    bucket["days"].add(r["date"])
                    bucket["chats"].add(r["chat_name"] or r["chat_id"])
                    if r["date"] < bucket["first_date"]:
                        bucket["first_date"] = r["date"]
                    if r["date"] > bucket["last_date"]:
                        bucket["last_date"] = r["date"]
                    if len(bucket["summary_refs"]) < 20:
                        bucket["summary_refs"].append({
                            "id": r["id"],
                            "date": r["date"],
                            "chat_name": r["chat_name"] or "",
                        })

        profiles = {row["symbol"]: row["id"]
                    for row in conn.execute("SELECT id, symbol FROM coin_profiles").fetchall()}
        profiles_ca = {row["ca"]: row["id"]
                       for row in conn.execute(
                           "SELECT id, ca FROM coin_profiles WHERE ca != ''").fetchall()}
        cached_briefs = {(row["kind"], row["value"])
                         for row in conn.execute(
                             "SELECT kind, value FROM entity_briefs").fetchall()}

    out = []
    for bucket in agg.values():
        v = bucket["value"]
        profile_id = None
        if bucket["kind"] == "symbol":
            profile_id = profiles.get(v)
        elif bucket["kind"] == "ca":
            profile_id = profiles_ca.get(v)
        refs = sorted(bucket["summary_refs"], key=lambda x: x["date"], reverse=True)
        out.append({
            "kind": bucket["kind"],
            "value": v,
            "days_seen": len(bucket["days"]),
            "chats_seen": len(bucket["chats"]),
            "first_date": bucket["first_date"],
            "last_date": bucket["last_date"],
            "summary_refs": refs,
            "summary_ids": [r["id"] for r in refs],
            "has_profile": profile_id is not None,
            "profile_id": profile_id,
            "has_brief": (bucket["kind"], v) in cached_briefs,
        })
    out.sort(key=lambda r: (r["days_seen"], r["last_date"], r["chats_seen"]),
             reverse=True)
    return jsonify({"entities": out, "window_days": days})


@bp.route("/api/watchtower/entity_mentions")
def api_watchtower_entity_mentions():
    """Return raw `messages` rows that mention a given entity."""
    value = (request.args.get("value") or "").strip()
    kind = (request.args.get("kind") or "symbol").strip()
    days = max(1, min(request.args.get("days", 14, type=int), 90))
    limit = max(1, min(request.args.get("limit", 50, type=int), 200))
    include_bots = (request.args.get("include_bots") or "").lower() in ("1", "true")
    context_n = max(0, min(request.args.get("context", 5, type=int), 10))
    if not value:
        return jsonify({"error": "缺少 value"}), 400

    patterns = []
    if kind == "ca":
        patterns.append(f"%{value}%")
    elif kind == "handle":
        patterns.append(f"%@{value}%")
    else:  # symbol
        patterns.append(f"%${value}%")
        patterns.append(f"% {value} %")
        patterns.append(f"%{value} %")

    where_clauses = " OR ".join(["text LIKE ?"] * len(patterns))
    fetch_n = limit * 4 if not include_bots else limit
    sql = (f"SELECT id, msg_id, date, chat_id, chat_name, sender_name, "
           f"sender_username, text, media FROM messages "
           f"WHERE ({where_clauses}) AND date >= datetime('now', 'localtime', ?) "
           f"ORDER BY date DESC LIMIT ?")
    params = patterns + [f"-{days} days", fetch_n]

    with get_db_ctx() as conn:
        candidates = [dict(r) for r in conn.execute(sql, params).fetchall()]
        bot_skipped = 0
        if include_bots:
            mentions = candidates[:limit]
        else:
            mentions = []
            for c in candidates:
                if is_bot_message(c.get("text") or ""):
                    bot_skipped += 1
                    continue
                mentions.append(c)
                if len(mentions) >= limit:
                    break

        ctx_map = fetch_context_for_hits(conn, mentions, before=context_n, after=context_n) if context_n else {}

    for m in mentions:
        key = (str(m["chat_id"]), m["msg_id"])
        ctx = ctx_map.get(key, {"before": [], "after": []})
        m["context_before"] = ctx["before"]
        m["context_after"] = ctx["after"]

    return jsonify({
        "mentions": mentions,
        "value": value,
        "kind": kind,
        "window_days": days,
        "count": len(mentions),
        "bot_skipped": bot_skipped,
        "include_bots": include_bots,
    })


# ============================================================
# Entity quick brief
# ============================================================

def _entity_brief_context(value, kind, days, max_summaries=10, max_messages=25,
                          max_tweets=20):
    """Aggregate input for the entity brief — summaries + filtered messages
    + first-hand tweets (when TWITTER_TOKEN is configured)."""
    if kind == "ca":
        like = [f"%{value}%"]
    elif kind == "handle":
        like = [f"%@{value}%"]
    else:  # symbol
        like = [f"%${value}%", f"% {value} %", f"%{value} %"]

    sum_clauses = " OR ".join(["summary LIKE ?"] * len(like))
    msg_clauses = " OR ".join(["text LIKE ?"] * len(like))

    summary_lines = []
    msg_lines = []
    counts = {
        "summaries": 0,
        "messages": 0,
        "messages_after_bot_filter": 0,
        "tweets": 0,
        "twitter_enabled": twitter_client.available(),
    }

    with get_db_ctx() as conn:
        sum_rows = conn.execute(
            f"SELECT date, chat_name, summary FROM daily_summaries "
            f"WHERE ({sum_clauses}) AND date >= date('now', 'localtime', ?) "
            f"ORDER BY date DESC LIMIT ?",
            like + [f"-{days} days", max_summaries],
        ).fetchall()
        counts["summaries"] = len(sum_rows)
        for r in sum_rows:
            snippet = (r["summary"] or "")[:1200]
            summary_lines.append(f"[{r['date']}] {r['chat_name']}\n{snippet}")

        msg_rows = conn.execute(
            f"SELECT date, chat_name, sender_name, sender_username, text FROM messages "
            f"WHERE ({msg_clauses}) AND date >= datetime('now', 'localtime', ?) "
            f"ORDER BY date DESC LIMIT ?",
            like + [f"-{days} days", max_messages * 4],
        ).fetchall()
        counts["messages"] = len(msg_rows)
        for r in msg_rows:
            if is_bot_message(r["text"] or ""):
                continue
            ts = to_taipei_str(r["date"])
            sender = r["sender_name"] or "?"
            if r["sender_username"]:
                sender += f" @{r['sender_username']}"
            snippet = (r["text"] or "").replace("\n", " ").strip()[:280]
            msg_lines.append(f"[{ts}] {r['chat_name']} · {sender}: {snippet}")
            if len(msg_lines) >= max_messages:
                break
        counts["messages_after_bot_filter"] = len(msg_lines)

    # ---- Twitter context (only if token configured) ----
    tweet_lines = []
    if twitter_client.available():
        from datetime import date as _date, timedelta as _timedelta
        since = (_date.today() - _timedelta(days=days)).isoformat()
        tweets = []
        try:
            if kind == "symbol":
                tweets = twitter_client.search(
                    keywords=f"${value}",
                    max_results=max_tweets,
                    since_date=since,
                    exclude_replies=True,
                    exclude_retweets=False,
                )
            elif kind == "handle":
                tweets = twitter_client.user_tweets(
                    username=value,
                    max_results=max_tweets,
                    include_replies=False,
                    include_retweets=False,
                )
            elif kind == "ca":
                tweets = twitter_client.search(
                    keywords=value,
                    max_results=max_tweets,
                    since_date=since,
                    exclude_replies=True,
                )
        except Exception as e:
            logger.warning("twitter fetch for %s:%s failed: %s", kind, value, e)
            tweets = []

        skipped_low = 0
        for t in tweets:
            if twitter_client.is_low_signal_tweet(t):
                skipped_low += 1
                continue
            line = twitter_client.format_tweet_line(t, max_len=320)
            if line:
                tweet_lines.append(line)
        counts["tweets"] = len(tweet_lines)
        counts["tweets_skipped_low_signal"] = skipped_low

    summary_blob = "\n\n".join(summary_lines) if summary_lines else "(無)"
    if msg_lines:
        message_blob = "(時間為 UTC+8)\n" + "\n".join(msg_lines)
    else:
        message_blob = "(無人類訊息,可能全被 bot 蓋掉)"
    if not twitter_client.available():
        twitter_blob = "(TWITTER_TOKEN 未設定 — 跳過 X 第一手資料)"
    elif tweet_lines:
        twitter_blob = "\n".join(tweet_lines)
    else:
        twitter_blob = "(已啟用 Twitter,但 6551 對此查詢回 0 則 — 可能 keyword 太冷,或 API 額度問題)"

    return summary_blob, message_blob, twitter_blob, counts


_ENTITY_BRIEF_TTL_HOURS = 24


def _brief_age_hours(generated_at: str) -> float:
    """Return how many hours ago the brief was generated; -1 on parse error."""
    if not generated_at:
        return -1
    try:
        dt = datetime.strptime(generated_at, "%Y-%m-%d %H:%M:%S")
        # generated_at 是 DB 用 datetime('now','localtime') 寫入的 naive UTC+8 字串,
        # 要拿 UTC+8 的 wall-clock now 比,才不會跟著 host TZ 偏移。
        now_taipei_naive = datetime.now(TAIPEI_TZ).replace(tzinfo=None)
        delta = now_taipei_naive - dt
        return delta.total_seconds() / 3600
    except Exception:
        return -1


@bp.route("/api/watchtower/entity_brief", methods=["GET"])
def api_watchtower_entity_brief_get():
    """Return cached brief if any. POST regenerates."""
    value = (request.args.get("value") or "").strip()
    kind = (request.args.get("kind") or "symbol").strip()
    if not value:
        return jsonify({"error": "缺少 value"}), 400
    with get_db_ctx() as conn:
        row = conn.execute(
            "SELECT * FROM entity_briefs WHERE kind = ? AND value = ?",
            (kind, value),
        ).fetchone()
    if not row:
        return jsonify({"brief": None})
    brief = dict(row)
    age = _brief_age_hours(brief.get("generated_at") or "")
    brief["age_hours"] = round(age, 1) if age >= 0 else None
    brief["is_stale"] = age >= _ENTITY_BRIEF_TTL_HOURS if age >= 0 else False
    brief["ttl_hours"] = _ENTITY_BRIEF_TTL_HOURS
    return jsonify({"brief": brief})


@bp.route("/api/watchtower/entity_brief", methods=["POST"])
def api_watchtower_entity_brief_create():
    """Generate (and cache) a fresh brief — streams Sonnet output as SSE."""
    data = get_json_body() or {}
    value = (data.get("value") or "").strip()
    kind = (data.get("kind") or "symbol").strip()
    days = max(1, min(int(data.get("days") or 14), 90))
    model_key = data.get("model", "sonnet")
    target_model = MODEL_OPUS if model_key == "opus" else MODEL_SONNET
    model_label = MODEL_SHORT_NAMES.get(target_model, "Sonnet")
    if not value:
        return jsonify({"error": "缺少 value"}), 400

    if kind == "symbol":
        entity_label = f"${value}"
    elif kind == "handle":
        entity_label = f"@{value}"
    else:
        entity_label = value

    def generate():
        ev = sse_event
        try:
            yield ev({"type": "progress", "progress": 5,
                      "msg": f"📦 聚合 {entity_label} 過去 {days} 天資料..."})

            if not ai_backend.ai_available():
                yield ev({"type": "error", "error": "AI backend 未就緒"})
                return

            summary_blob, message_blob, twitter_blob, counts = _entity_brief_context(value, kind, days)
            if (counts["summaries"] == 0 and counts["messages_after_bot_filter"] == 0
                    and counts["tweets"] == 0):
                yield ev({"type": "error",
                          "error": f"{entity_label} 在過去 {days} 天的記憶庫 + X 都沒有可分析的訊號"})
                return

            tw_part = (f" · {counts['tweets']} tweets" if counts["twitter_enabled"]
                       else " · X off")
            yield ev({"type": "progress", "progress": 30,
                      "msg": (f"🧠 {model_label} 起草中 — "
                              f"{counts['summaries']} summaries · "
                              f"{counts['messages_after_bot_filter']} msgs"
                              f"{tw_part}")})

            prompt = ai.PROMPT_ENTITY_BRIEF.format(
                entity_label=entity_label, kind=kind, days=days,
                summary_blob=summary_blob, message_blob=message_blob,
                twitter_blob=twitter_blob,
            )

            def heartbeat(has_text, elapsed, idle):
                if has_text:
                    return {"type": "progress",
                            "msg": f"🧠 生成中…(已 {elapsed}s,上次 token {idle}s 前)"}
                return {"type": "progress", "progress": 30,
                        "msg": f"🧠 等待第一個 token…(已等 {elapsed}s)"}

            full_text, stream_error = yield from stream_ai_events(
                prompt, ai.SYS_ENTITY_BRIEF, target_model,
                max_tokens=800,
                idle_timeout=FOREGROUND_AI_IDLE_TIMEOUT_SECS,
                heartbeat_every=10,
                heartbeat_message=heartbeat,
                event_wrapper=ev,
            )
            if stream_error or not full_text:
                yield ev({"type": "error",
                          "error": f"Brief 失敗: {stream_error or 'no output'}"})
                return

            # Persist to entity_briefs cache so the next visit is instant.
            generated_at = None
            try:
                with get_db_ctx() as conn:
                    conn.execute("""
                        INSERT INTO entity_briefs
                            (kind, value, brief_text, days_window, summaries_count, messages_count, generated_at)
                        VALUES (?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
                        ON CONFLICT(kind, value) DO UPDATE SET
                            brief_text = excluded.brief_text,
                            days_window = excluded.days_window,
                            summaries_count = excluded.summaries_count,
                            messages_count = excluded.messages_count,
                            generated_at = datetime('now', 'localtime')
                    """, (kind, value, full_text, days,
                          counts["summaries"], counts["messages_after_bot_filter"]))
                    conn.commit()
                    generated_at = conn.execute(
                        "SELECT generated_at FROM entity_briefs WHERE kind = ? AND value = ?",
                        (kind, value),
                    ).fetchone()[0]
            except Exception:
                logger.exception("entity_briefs cache write failed (kind=%s value=%s)",
                                 kind, value)

            yield ev({
                "type": "done",
                "brief": full_text,
                "context_counts": counts,
                "entity_label": entity_label,
                "days": days,
                "generated_at": generated_at,
            })
        except Exception as e:
            logger.exception("entity brief failed")
            yield ev({"type": "error", "error": str(e)})

    return sse_response(generate)
