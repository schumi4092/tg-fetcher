"""Settings routes — chat categories, category map, watchlist, trusted senders."""

import re
import sqlite3

from flask import Blueprint, jsonify, request

from config import logger
from db import build_fts_query, get_db_ctx
import ai

from routes._shared import get_json_body


bp = Blueprint("settings", __name__)


# ============================================================
# Chat categories
# ============================================================

@bp.route("/api/chat_categories", methods=["GET"])
def api_chat_categories_list():
    with get_db_ctx() as conn:
        rows = conn.execute("""
            SELECT c.id, c.name, c.color, c.sort_order, c.prompt_profile,
                   COUNT(m.chat_id) AS chat_count
            FROM chat_categories c
            LEFT JOIN chat_category_map m ON m.category_id = c.id
            GROUP BY c.id
            ORDER BY c.sort_order, c.id
        """).fetchall()
    return jsonify({
        "categories": [dict(r) for r in rows],
        "profiles": [
            {"value": k, "label": v["label"]}
            for k, v in ai.PROFILES.items()
        ],
    })


@bp.route("/api/chat_categories", methods=["POST"])
def api_chat_categories_create():
    data = get_json_body()
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "請輸入分類名稱"}), 400
    color = data.get("color") or "#9a5b2a"
    sort_order = data.get("sort_order", 0)
    prompt_profile = (data.get("prompt_profile") or ai.DEFAULT_PROFILE).strip()
    if prompt_profile not in ai.PROFILES:
        return jsonify({"error": f"不支援的 prompt_profile: {prompt_profile}"}), 400
    try:
        with get_db_ctx() as conn:
            conn.execute(
                "INSERT INTO chat_categories (name, color, sort_order, prompt_profile) VALUES (?, ?, ?, ?)",
                (name, color, sort_order, prompt_profile)
            )
            conn.commit()
            new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return jsonify({"id": new_id, "status": "created"})
    except sqlite3.IntegrityError:
        return jsonify({"error": "分類名稱已存在"}), 400


@bp.route("/api/chat_categories/<int:cat_id>", methods=["PUT"])
def api_chat_category_update(cat_id):
    with get_db_ctx() as conn:
        exists = conn.execute("SELECT 1 FROM chat_categories WHERE id = ?", (cat_id,)).fetchone()
        if not exists:
            return jsonify({"error": "分類不存在"}), 404
        data = get_json_body()
        fields, params = [], []
        if "name" in data:
            fields.append("name = ?")
            params.append((data.get("name") or "").strip())
        if "color" in data:
            fields.append("color = ?")
            params.append(data.get("color") or "#9a5b2a")
        if "sort_order" in data:
            fields.append("sort_order = ?")
            params.append(int(data.get("sort_order") or 0))
        if "prompt_profile" in data:
            pp = (data.get("prompt_profile") or "").strip()
            if pp not in ai.PROFILES:
                return jsonify({"error": f"不支援的 prompt_profile: {pp}"}), 400
            fields.append("prompt_profile = ?")
            params.append(pp)
        if not fields:
            return jsonify({"error": "沒有要更新的欄位"}), 400
        params.append(cat_id)
        try:
            conn.execute(f"UPDATE chat_categories SET {', '.join(fields)} WHERE id = ?", params)
            conn.commit()
        except sqlite3.IntegrityError:
            return jsonify({"error": "分類名稱已存在"}), 400
    return jsonify({"status": "updated"})


@bp.route("/api/chat_categories/<int:cat_id>", methods=["DELETE"])
def api_chat_category_delete(cat_id):
    with get_db_ctx() as conn:
        exists = conn.execute("SELECT 1 FROM chat_categories WHERE id = ?", (cat_id,)).fetchone()
        if not exists:
            return jsonify({"error": "分類不存在"}), 404
        conn.execute("DELETE FROM chat_category_map WHERE category_id = ?", (cat_id,))
        conn.execute("DELETE FROM chat_categories WHERE id = ?", (cat_id,))
        conn.commit()
    return jsonify({"status": "deleted"})


@bp.route("/api/chat_category_map", methods=["PUT"])
def api_chat_category_map():
    data = get_json_body()
    chat_id = str(data.get("chat_id") or "").strip()
    if not chat_id:
        return jsonify({"error": "chat_id 必填"}), 400
    cat_id = data.get("category_id")
    with get_db_ctx() as conn:
        if cat_id is None:
            conn.execute("DELETE FROM chat_category_map WHERE chat_id = ?", (chat_id,))
        else:
            exists = conn.execute("SELECT 1 FROM chat_categories WHERE id = ?", (int(cat_id),)).fetchone()
            if not exists:
                return jsonify({"error": "分類不存在"}), 404
            conn.execute("""
                INSERT INTO chat_category_map (chat_id, category_id, updated_at)
                VALUES (?, ?, datetime('now', 'localtime'))
                ON CONFLICT(chat_id) DO UPDATE SET
                    category_id = excluded.category_id,
                    updated_at = excluded.updated_at
            """, (chat_id, int(cat_id)))
        conn.commit()
    return jsonify({"status": "ok"})


# ============================================================
# Watchlist
# ============================================================

@bp.route("/api/watchlist", methods=["GET"])
def api_watchlist_list():
    with get_db_ctx() as conn:
        keywords = [dict(r) for r in conn.execute(
            "SELECT * FROM watchlist ORDER BY created_at DESC"
        ).fetchall()]
    return jsonify({"keywords": keywords})


@bp.route("/api/watchlist", methods=["POST"])
def api_watchlist_create():
    data = get_json_body()
    keyword = data.get("keyword", "").strip()
    if not keyword:
        return jsonify({"error": "請輸入關鍵字"}), 400
    try:
        with get_db_ctx() as conn:
            conn.execute(
                "INSERT INTO watchlist (keyword, category) VALUES (?, ?)",
                (keyword, data.get("category", "一般"))
            )
            conn.commit()
            kw_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return jsonify({"id": kw_id, "status": "created"})
    except sqlite3.IntegrityError:
        return jsonify({"error": "關鍵字已存在"}), 400


@bp.route("/api/watchlist", methods=["DELETE"])
def api_watchlist_delete():
    kw_id = request.args.get("id", type=int)
    if kw_id:
        with get_db_ctx() as conn:
            conn.execute("DELETE FROM watchlist WHERE id = ?", (kw_id,))
            conn.commit()
    return jsonify({"status": "deleted"})


_WATCH_STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "是否", "具體", "內容",
    "效果", "後續", "動向", "更新", "開發者", "活動", "是否持續", "群組",
}


def _watch_terms(keyword):
    """Extract high-signal tracking terms from a watchlist row.

    Watchlist rows are often full research questions imported from AI reports.
    Searching every word with OR creates garbage hits, so we track entities and
    quoted phrases first: $ticker, @handle, CA, and 「quoted phrase」.
    """
    raw = (keyword or "").strip()
    out = []
    seen = set()

    def add(value, kind, strong=True):
        value = (value or "").strip()
        if not value:
            return
        key = (kind, value.lower())
        if key in seen:
            return
        seen.add(key)
        out.append({"value": value, "kind": kind, "strong": bool(strong)})

    for ca in re.findall(r"\b0x[a-fA-F0-9]{40}\b|\b[1-9A-HJ-NP-Za-km-z]{32,44}\b", raw):
        if len(ca) >= 32 and (ca.startswith("0x") or any(ch.isdigit() for ch in ca)):
            add(ca, "ca", True)

    for ticker in re.findall(r"\$[A-Za-z][A-Za-z0-9_.-]{1,20}", raw):
        add(ticker, "ticker", True)

    for handle in re.findall(r"@[A-Za-z0-9_]{2,32}", raw):
        add(handle, "handle", True)

    for phrase in re.findall(r"[「“\"]([^」”\"]{3,80})[」”\"]", raw):
        phrase = phrase.strip()
        if len(phrase) >= 3:
            add(phrase, "phrase", True)

    # Fallback for manually-added plain keywords: keep meaningful chunks, but
    # treat them as weak terms so they need multiple matches to pass.
    if not any(t["strong"] for t in out):
        chunks = [
            t.strip()
            for t in re.split(r"[\s,，。:：;；/|()（）【】\[\]「」\"'`]+", raw)
            if t.strip()
        ]
        for token in chunks[:8]:
            if len(token) < 2 or token.lower() in _WATCH_STOPWORDS:
                continue
            add(token, "term", False)

    fts_seed = []
    for t in out:
        value = t["value"].lstrip("$@")
        if t["kind"] == "phrase":
            fts_seed.extend(
                p for p in re.split(r"\W+", value) if len(p) >= 3 and p.lower() not in _WATCH_STOPWORDS
            )
        elif len(value) >= 2:
            fts_seed.append(value)
    return {"raw": raw, "terms": out, "fts_query": build_fts_query(" ".join(fts_seed), joiner=" OR ", min_len=2)}


def _score_watch_text(text, spec):
    hay = (text or "").lower()
    if not hay:
        return 0, []
    strong_terms = [t for t in spec["terms"] if t["strong"]]
    weak_terms = [t for t in spec["terms"] if not t["strong"]]
    score = 0
    matches = []

    for t in strong_terms:
        value = t["value"]
        probe = value.lower()
        if t["kind"] == "ticker":
            probes = {probe, probe.lstrip("$")}
            hit = any((p and (p in hay or f"${p}" in hay)) for p in probes)
            weight = 8
        elif t["kind"] == "handle":
            probes = {probe, probe.lstrip("@")}
            hit = any((p and (p in hay or f"@{p}" in hay)) for p in probes)
            weight = 7
        else:
            hit = probe in hay
            weight = 6 if t["kind"] == "ca" else 5
        if hit:
            score += weight
            label = value if value.startswith(("$", "@")) else value
            if label not in matches:
                matches.append(label)

    if strong_terms:
        return score, matches

    weak_hits = 0
    for t in weak_terms:
        probe = t["value"].lower()
        if probe and probe in hay:
            weak_hits += 1
            score += 1
            matches.append(t["value"])
    if spec["raw"].lower() in hay:
        score += 4
        matches.insert(0, spec["raw"])
    if weak_hits >= 2 or score >= 4:
        return score, matches[:6]
    return 0, []


def _filter_watch_rows(rows, fields, spec, limit):
    ranked = []
    for row in rows:
        d = dict(row)
        text = "\n".join(str(d.get(field) or "") for field in fields)
        score, matches = _score_watch_text(text, spec)
        if score <= 0:
            continue
        d["_score"] = score
        d["_matches"] = matches
        ranked.append(d)
    ranked.sort(key=lambda r: (r.get("_score", 0), str(r.get("date") or "")), reverse=True)
    return ranked[:limit]


@bp.route("/api/watchlist/hits")
def api_watchlist_hits():
    kw_id = request.args.get("id", type=int)
    if not kw_id:
        return jsonify({"error": "缺少 id"}), 400

    with get_db_ctx() as conn:
        kw = conn.execute("SELECT * FROM watchlist WHERE id = ?", (kw_id,)).fetchone()
        if not kw:
            return jsonify({"error": "找不到關鍵字"}), 404

        keyword = kw["keyword"]
        spec = _watch_terms(keyword)
        fts_query = spec["fts_query"]
        exact_terms = [t["value"] for t in spec["terms"] if t["strong"]] or [keyword]
        like = f"%{exact_terms[0]}%"

        def _candidate_rows(sql, params, fallback_sql, fallback_params):
            try:
                if fts_query:
                    return [dict(r) for r in conn.execute(sql, params).fetchall()]
            except Exception as e:
                logger.warning("watchlist FTS query failed (id=%s): %s", kw_id, e)
            return [dict(r) for r in conn.execute(fallback_sql, fallback_params).fetchall()]

        summaries = _candidate_rows(
            """
            SELECT ds.id, ds.date, ds.chat_name, ds.summary AS snippet
            FROM summaries_fts fts
            JOIN daily_summaries ds ON ds.id = fts.rowid
            WHERE summaries_fts MATCH ?
            ORDER BY ds.date DESC LIMIT 80
            """,
            (fts_query,),
            """
            SELECT id, date, chat_name, summary AS snippet
            FROM daily_summaries
            WHERE summary LIKE ?
            ORDER BY date DESC LIMIT 80
            """,
            (like,),
        )
        summaries = _filter_watch_rows(summaries, ("chat_name", "snippet"), spec, 8)
        for s in summaries:
            s["snippet"] = (s.get("snippet") or "")[:520]

        events = _candidate_rows(
            """
            SELECT e.id, e.date, e.title, e.description, e.importance, e.tags
            FROM events_fts fts
            JOIN events e ON e.id = fts.rowid
            WHERE events_fts MATCH ?
            ORDER BY e.date DESC LIMIT 80
            """,
            (fts_query,),
            """
            SELECT id, date, title, description, importance, tags
            FROM events
            WHERE title LIKE ? OR description LIKE ? OR tags LIKE ?
            ORDER BY date DESC LIMIT 80
            """,
            (like, like, like),
        )
        events = _filter_watch_rows(events, ("title", "description", "tags"), spec, 8)

        notes = _candidate_rows(
            """
            SELECT n.id, n.date, n.content, n.tags
            FROM notes_fts fts
            JOIN notes n ON n.id = fts.rowid
            WHERE notes_fts MATCH ?
            ORDER BY n.date DESC LIMIT 80
            """,
            (fts_query,),
            """
            SELECT id, date, content, tags
            FROM notes
            WHERE content LIKE ? OR tags LIKE ?
            ORDER BY date DESC LIMIT 80
            """,
            (like, like),
        )
        notes = _filter_watch_rows(notes, ("content", "tags"), spec, 8)

        messages = _candidate_rows(
            """
            SELECT m.id, m.date, m.chat_name, m.sender_name, m.sender_username,
                   m.text
            FROM messages_fts fts
            JOIN messages m ON m.id = fts.rowid
            WHERE messages_fts MATCH ?
            ORDER BY m.date DESC LIMIT 200
            """,
            (fts_query,),
            """
            SELECT id, date, chat_name, sender_name, sender_username,
                   text
            FROM messages
            WHERE text LIKE ? OR sender_name LIKE ? OR chat_name LIKE ?
            ORDER BY date DESC LIMIT 200
            """,
            (like, like, like),
        )
        messages = _filter_watch_rows(messages, ("chat_name", "sender_name", "sender_username", "text"), spec, 10)
        for m in messages:
            m["text"] = (m.get("text") or "")[:520]

    return jsonify({
        "keyword": dict(kw),
        "terms": spec["terms"],
        "summaries": summaries,
        "events": events,
        "notes": notes,
        "messages": messages,
        "counts": {
            "summaries": len(summaries),
            "events": len(events),
            "notes": len(notes),
            "messages": len(messages),
        },
    })


# ============================================================
# Trusted senders
# ============================================================

@bp.route("/api/trusted_senders", methods=["GET"])
def api_trusted_senders_list():
    with get_db_ctx() as conn:
        senders = [dict(r) for r in conn.execute(
            "SELECT * FROM trusted_senders ORDER BY trust_level, name"
        ).fetchall()]
    return jsonify({"senders": senders})


@bp.route("/api/trusted_senders", methods=["POST"])
def api_trusted_senders_create():
    data = get_json_body()
    sender_id = data.get("sender_id")
    name = data.get("name", "").strip()
    trust_level = data.get("trust_level", "trusted")
    if not sender_id or not name:
        return jsonify({"error": "需要 sender_id 和 name"}), 400
    if trust_level not in ("trusted", "neutral", "noise"):
        return jsonify({"error": "trust_level 必須是 trusted/neutral/noise"}), 400
    try:
        with get_db_ctx() as conn:
            conn.execute("""
                INSERT INTO trusted_senders (sender_id, name, username, trust_level, notes)
                VALUES (?, ?, ?, ?, ?)
            """, (int(sender_id), name,
                  data.get("username", ""), trust_level,
                  data.get("notes", "")))
            conn.commit()
            row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        ai.invalidate_trust_map()
        return jsonify({"id": row_id, "status": "created"})
    except sqlite3.IntegrityError:
        return jsonify({"error": "該 sender_id 已存在，請用 PUT 更新"}), 400


@bp.route("/api/trusted_senders", methods=["PUT"])
def api_trusted_senders_update():
    data = get_json_body()
    sender_id = data.get("sender_id")
    if not sender_id:
        return jsonify({"error": "需要 sender_id"}), 400
    updates = []
    params = []
    for field in ("name", "username", "trust_level", "notes"):
        if field in data:
            updates.append(f"{field} = ?")
            params.append(data[field])
    if not updates:
        return jsonify({"error": "沒有要更新的欄位"}), 400
    params.append(int(sender_id))
    with get_db_ctx() as conn:
        conn.execute(
            f"UPDATE trusted_senders SET {', '.join(updates)} WHERE sender_id = ?",
            params
        )
        conn.commit()
    ai.invalidate_trust_map()
    return jsonify({"status": "updated"})


@bp.route("/api/trusted_senders", methods=["DELETE"])
def api_trusted_senders_delete():
    sender_id = request.args.get("sender_id", type=int)
    row_id = request.args.get("id", type=int)
    with get_db_ctx() as conn:
        if sender_id:
            conn.execute("DELETE FROM trusted_senders WHERE sender_id = ?", (sender_id,))
        elif row_id:
            conn.execute("DELETE FROM trusted_senders WHERE id = ?", (row_id,))
        conn.commit()
    ai.invalidate_trust_map()
    return jsonify({"status": "deleted"})
