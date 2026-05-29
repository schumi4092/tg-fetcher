"""Coin routes — search, synthesis, profile CRUD, AI draft/fill/from_notes."""

import re
import sqlite3
from datetime import datetime

from flask import Blueprint, jsonify, request

from config import (
    FOREGROUND_AI_IDLE_TIMEOUT_SECS,
    MODEL_OPUS,
    MODEL_SHORT_NAMES,
    MODEL_SONNET,
    logger,
)
from db import TAIPEI_TZ, get_db_ctx, search_coin, to_taipei_str
import ai
import ai_backend
import twitter_client

from routes._shared import get_json_body, sse_event, sse_response, try_parse_json_object
from routes._ai_stream import stream_ai_events
from routes.coin_wallet import (
    find_wallet_holders_for_ca,
    normalize_holder_ca as _normalize_holder_ca,
)
from routes._entities import (
    CA_SOL_BLOCKLIST,
    RE_CA_EVM,
    RE_CA_SOL,
    RE_TICKER,
)


bp = Blueprint("coin", __name__)


# ============================================================
# /api/coin/search + /api/coin/synthesis
# ============================================================

@bp.route("/api/coin/search")
def api_coin_search():
    """Cross-chat aggregation for a coin / CA. Pure local FTS, no AI."""
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "請輸入幣種或 CA"}), 400
    days = request.args.get("days", type=int)
    result = search_coin(query, days=days)
    ca = _normalize_holder_ca(query)
    if ca:
        result["holders"] = find_wallet_holders_for_ca(
            ca,
            days=days if days is not None else 180,
        )
    return jsonify(result)


@bp.route("/api/coin/holders")
def api_coin_holders():
    """Infer current smart-money holders for a token CA from wallet_log messages."""
    ca = _normalize_holder_ca(request.args.get("ca") or request.args.get("q"))
    if not ca:
        return jsonify({"error": "請輸入 EVM / Solana CA"}), 400
    days = request.args.get("days", default=180, type=int)
    return jsonify(find_wallet_holders_for_ca(ca, days=days))


@bp.route("/api/coin/synthesis", methods=["POST"])
def api_coin_synthesis():
    """Opt-in Claude synthesis of the cross-chat hits for a coin."""
    data = get_json_body()
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "請輸入幣種或 CA"}), 400
    days = data.get("days")
    try:
        days = int(days) if days else None
    except (TypeError, ValueError):
        days = None
    model_key = data.get("model", "sonnet")
    result = search_coin(query, days=days)
    text, error = ai.ai_synthesize_coin(query, result, model=model_key)
    if error:
        return jsonify({"error": error}), 500
    return jsonify({
        "query": query,
        "synthesis": text,
        "total_hits": result.get("total_hits", 0),
        "chat_count": len(result.get("per_chat") or []),
        "mode": result.get("mode"),
    })


# ============================================================
# Coin profile CRUD
# ============================================================

# Fields the frontend can write to. `id`, `created_at`, `last_updated` are
# managed server-side; everything else is editable.
_COIN_PROFILE_FIELDS = (
    "symbol", "chain", "ca", "status",
    "narrative", "timeline_json", "kol_consensus",
    "smart_money_summary", "top_signal", "archetype",
    "my_entry_fdv", "my_entry_size", "my_exit_fdv", "my_exit_size",
    "my_pnl", "my_wallet", "my_verdict", "my_lesson",
    "my_raw_notes",
    "tags", "pinned", "first_seen_date",
)
_COIN_PROFILE_STATUSES = ("tracking", "held", "exited", "dropped")


def _coin_profile_payload(data):
    """Coerce/validate payload for INSERT/UPDATE. Returns dict of accepted columns."""
    out = {}
    for field in _COIN_PROFILE_FIELDS:
        if field not in data:
            continue
        val = data[field]
        if field == "pinned":
            out[field] = 1 if val else 0
        elif field == "status":
            if val not in _COIN_PROFILE_STATUSES:
                raise ValueError(f"status 必須是 {'/'.join(_COIN_PROFILE_STATUSES)}")
            out[field] = val
        elif field == "symbol":
            v = (val or "").strip().upper().lstrip("$")
            if not v:
                raise ValueError("symbol 不可為空")
            out[field] = v
        elif field == "chain":
            out[field] = (val or "").strip().lower()
        else:
            out[field] = "" if val is None else str(val)
    return out


def _find_profile_by_exact_ca(conn, ca):
    """Return the most recently updated profile with this exact CA, if any."""
    ca = (ca or "").strip()
    if not ca:
        return None
    where = "LOWER(ca) = LOWER(?)" if ca.lower().startswith("0x") else "ca = ?"
    row = conn.execute(
        f"""
        SELECT * FROM coin_profiles
        WHERE {where}
        ORDER BY pinned DESC, last_updated DESC, id DESC
        LIMIT 1
        """,
        (ca,),
    ).fetchone()
    return dict(row) if row else None


def _merge_profile_payload(existing, incoming):
    """Conservatively merge a new payload into an existing exact-CA profile."""
    merged = {}
    for field, value in incoming.items():
        if field == "my_raw_notes":
            existing_raw = (existing.get("my_raw_notes") or "").strip()
            incoming_raw = (value or "").strip()
            if incoming_raw:
                merged[field] = (
                    incoming_raw + "\n\n" + existing_raw
                    if existing_raw else incoming_raw
                )
            continue

        if field == "pinned":
            merged[field] = 1 if existing.get(field) or value else 0
            continue

        incoming_text = "" if value is None else str(value).strip()
        existing_text = "" if existing.get(field) is None else str(existing.get(field)).strip()
        if not existing_text and incoming_text:
            merged[field] = value

    return merged


@bp.route("/api/coin_profiles", methods=["GET"])
def api_coin_profiles_list():
    status = request.args.get("status")
    pinned = request.args.get("pinned")
    q = request.args.get("q", "").strip()
    sql = "SELECT * FROM coin_profiles WHERE 1=1"
    params = []
    if status and status in _COIN_PROFILE_STATUSES:
        sql += " AND status = ?"
        params.append(status)
    if pinned == "1":
        sql += " AND pinned = 1"
    if q:
        sql += " AND (symbol LIKE ? OR ca LIKE ? OR narrative LIKE ?)"
        like = f"%{q}%"
        params += [like, like, like]
    sql += " ORDER BY pinned DESC, last_updated DESC"
    with get_db_ctx() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    return jsonify({"profiles": rows})


@bp.route("/api/coin_profiles", methods=["POST"])
def api_coin_profiles_create():
    data = get_json_body()
    try:
        payload = _coin_profile_payload(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if "symbol" not in payload:
        return jsonify({"error": "缺少 symbol"}), 400
    try:
        with get_db_ctx() as conn:
            existing = _find_profile_by_exact_ca(conn, payload.get("ca"))
            if existing:
                updates = _merge_profile_payload(existing, payload)
                if updates:
                    sets = ", ".join(f"{c} = ?" for c in updates.keys())
                    params = list(updates.values()) + [existing["id"]]
                    conn.execute(
                        f"UPDATE coin_profiles SET {sets}, "
                        f"last_updated = datetime('now', 'localtime') WHERE id = ?",
                        params,
                    )
                    conn.commit()
                row = dict(conn.execute(
                    "SELECT * FROM coin_profiles WHERE id = ?", (existing["id"],)
                ).fetchone())
                return jsonify({"id": existing["id"], "profile": row, "merged": True})

            cols = list(payload.keys())
            placeholders = ",".join("?" for _ in cols)
            conn.execute(
                f"INSERT INTO coin_profiles ({','.join(cols)}) VALUES ({placeholders})",
                [payload[c] for c in cols],
            )
            conn.commit()
            row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            row = dict(conn.execute("SELECT * FROM coin_profiles WHERE id = ?", (row_id,)).fetchone())
        return jsonify({"id": row_id, "profile": row, "merged": False})
    except sqlite3.IntegrityError as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/api/coin_profiles/<int:profile_id>", methods=["GET"])
def api_coin_profile_get(profile_id):
    with get_db_ctx() as conn:
        row = conn.execute("SELECT * FROM coin_profiles WHERE id = ?", (profile_id,)).fetchone()
    if not row:
        return jsonify({"error": "找不到 profile"}), 404
    return jsonify({"profile": dict(row)})


@bp.route("/api/coin_profiles/<int:profile_id>", methods=["PUT"])
def api_coin_profile_update(profile_id):
    data = get_json_body()
    try:
        payload = _coin_profile_payload(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not payload:
        return jsonify({"error": "沒有可更新的欄位"}), 400
    with get_db_ctx() as conn:
        row = conn.execute("SELECT 1 FROM coin_profiles WHERE id = ?", (profile_id,)).fetchone()
        if not row:
            return jsonify({"error": "找不到 profile"}), 404
        sets = ", ".join(f"{c} = ?" for c in payload.keys())
        params = list(payload.values()) + [profile_id]
        conn.execute(
            f"UPDATE coin_profiles SET {sets}, last_updated = datetime('now', 'localtime') WHERE id = ?",
            params,
        )
        conn.commit()
        updated = dict(conn.execute("SELECT * FROM coin_profiles WHERE id = ?", (profile_id,)).fetchone())
    return jsonify({"profile": updated})


@bp.route("/api/coin_profiles/<int:profile_id>", methods=["DELETE"])
def api_coin_profile_delete(profile_id):
    with get_db_ctx() as conn:
        row = conn.execute("SELECT 1 FROM coin_profiles WHERE id = ?", (profile_id,)).fetchone()
        if not row:
            return jsonify({"error": "找不到 profile"}), 404
        conn.execute("DELETE FROM coin_profiles WHERE id = ?", (profile_id,))
        conn.commit()
    return jsonify({"status": "deleted"})


# ============================================================
# Coin profile context aggregators (for AI draft + smart money draft)
# ============================================================

def _coin_draft_context(symbol, ca, days=30):
    """Pull message / summary / event rows mentioning this symbol or CA."""
    parts = []
    counts = {"messages": 0, "summaries": 0, "events": 0}
    sym_dollar = f"%${symbol}%"
    sym_bare = f"% {symbol}%"
    sym_word = f"%{symbol} %"
    ca_pat = f"%{ca}%" if ca else None

    def _like_clause(cols):
        clauses = []
        params = []
        for col in cols:
            clauses += [f"{col} LIKE ?"] * 3
            params += [sym_dollar, sym_bare, sym_word]
            if ca_pat:
                clauses.append(f"{col} LIKE ?")
                params.append(ca_pat)
        return "(" + " OR ".join(clauses) + ")", params

    with get_db_ctx() as conn:
        where, params = _like_clause(["text"])
        sql = (f"SELECT date, chat_name, sender_name, text FROM messages "
               f"WHERE {where} AND date >= datetime('now', 'localtime', ?) "
               f"ORDER BY date LIMIT 200")
        params.append(f"-{days} days")
        msgs = conn.execute(sql, params).fetchall()
        if msgs:
            parts.append("--- TG MESSAGES (時間為 UTC+8) ---")
            for m in msgs:
                ts = to_taipei_str(m["date"])
                snippet = (m["text"] or "").replace("\n", " ").strip()[:300]
                parts.append(f"[{ts}] {m['chat_name'] or '?'} · {m['sender_name'] or '?'}: {snippet}")
            counts["messages"] = len(msgs)

        where, params = _like_clause(["summary"])
        sql = (f"SELECT date, chat_name, summary FROM daily_summaries "
               f"WHERE {where} AND date >= date('now', 'localtime', ?) "
               f"ORDER BY date LIMIT 30")
        params.append(f"-{days} days")
        sums = conn.execute(sql, params).fetchall()
        if sums:
            parts.append("\n--- DAILY SUMMARIES ---")
            for s in sums:
                snippet = (s["summary"] or "")[:1500]
                parts.append(f"[{s['date']}] {s['chat_name']}\n{snippet}")
            counts["summaries"] = len(sums)

        where, params = _like_clause(["title", "description", "tags"])
        sql = (f"SELECT date, title, description, tags, importance FROM events "
               f"WHERE {where} AND date >= date('now', 'localtime', ?) "
               f"ORDER BY date LIMIT 30")
        params.append(f"-{days} days")
        events = conn.execute(sql, params).fetchall()
        if events:
            parts.append("\n--- EVENTS ---")
            for e in events:
                desc = (e["description"] or "").replace("\n", " ")[:400]
                parts.append(
                    f"[{e['date']}] ({e['importance'] or 'normal'}) {e['title']}\n"
                    f"  {desc}\n  tags: {e['tags'] or '—'}"
                )
            counts["events"] = len(events)

    return "\n".join(parts), counts


def _coin_smart_money_context(symbol, ca, days=30):
    """Pull a smaller, wallet-biased context for the smart_money_summary field."""
    parts = []
    counts = {"messages": 0, "summaries": 0}
    sym_dollar = f"%${symbol}%"
    sym_bare = f"% {symbol}%"
    sym_word = f"%{symbol} %"
    ca_pat = f"%{ca}%" if ca else None

    def _like_clause(cols):
        clauses = []
        params = []
        for col in cols:
            clauses += [f"{col} LIKE ?"] * 3
            params += [sym_dollar, sym_bare, sym_word]
            if ca_pat:
                clauses.append(f"{col} LIKE ?")
                params.append(ca_pat)
        return "(" + " OR ".join(clauses) + ")", params

    with get_db_ctx() as conn:
        where, params = _like_clause(["m.text"])
        wallet_hint = """
            (
                cc.prompt_profile = 'wallet_log'
                OR lower(m.chat_name) LIKE '%wallet%'
                OR lower(m.chat_name) LIKE '%cielo%'
                OR lower(m.sender_name) LIKE '%wallet%'
                OR lower(m.sender_username) LIKE '%ray_orange%'
                OR lower(m.text) LIKE '%smart money%'
                OR lower(m.text) LIKE '%realized pnl%'
                OR lower(m.text) LIKE '%swapped%'
                OR lower(m.text) LIKE '%transferred%'
                OR lower(m.text) LIKE '%pnl:%'
                OR lower(m.text) LIKE '%holds:%'
                OR lower(m.text) LIKE '% bought %'
                OR lower(m.text) LIKE '% sold %'
                OR lower(m.text) LIKE '%[buy %'
                OR lower(m.text) LIKE '%[sell %'
                OR lower(m.text) LIKE '% buy %'
                OR lower(m.text) LIKE '% sell %'
            )
        """
        sql = f"""
            SELECT m.date, m.chat_name, m.sender_name, m.text,
                   COALESCE(cc.prompt_profile, '') AS prompt_profile
            FROM messages m
            LEFT JOIN chat_category_map map ON map.chat_id = m.chat_id
            LEFT JOIN chat_categories cc ON cc.id = map.category_id
            WHERE {where}
              AND m.date >= datetime('now', 'localtime', ?)
              AND {wallet_hint}
            ORDER BY
              CASE WHEN cc.prompt_profile = 'wallet_log' THEN 0 ELSE 1 END,
              m.date DESC
            LIMIT 120
        """
        params.append(f"-{days} days")
        msgs = conn.execute(sql, params).fetchall()
        if msgs:
            parts.append("--- WALLET / ON-CHAIN MESSAGES (時間為 UTC+8) ---")
            for m in msgs:
                ts = to_taipei_str(m["date"])
                snippet = (m["text"] or "").replace("\n", " ").strip()[:420]
                source = m["chat_name"] or m["sender_name"] or "?"
                profile = f" [{m['prompt_profile']}]" if m["prompt_profile"] else ""
                parts.append(f"[{ts}] {source}{profile}: {snippet}")
            counts["messages"] = len(msgs)

        where, params = _like_clause(["ds.summary"])
        sql = f"""
            SELECT ds.date, ds.chat_name, ds.summary,
                   COALESCE(cc.prompt_profile, '') AS prompt_profile
            FROM daily_summaries ds
            LEFT JOIN chat_category_map map ON map.chat_id = ds.chat_id
            LEFT JOIN chat_categories cc ON cc.id = map.category_id
            WHERE {where}
              AND ds.date >= date('now', 'localtime', ?)
              AND (
                cc.prompt_profile = 'wallet_log'
                OR lower(ds.chat_name) LIKE '%wallet%'
                OR lower(ds.chat_name) LIKE '%cielo%'
                OR lower(ds.summary) LIKE '%smart money%'
                OR lower(ds.summary) LIKE '%wallet%'
              )
            ORDER BY
              CASE WHEN cc.prompt_profile = 'wallet_log' THEN 0 ELSE 1 END,
              ds.date DESC
            LIMIT 12
        """
        params.append(f"-{days} days")
        sums = conn.execute(sql, params).fetchall()
        if sums:
            parts.append("\n--- WALLET / ON-CHAIN SUMMARIES ---")
            for s in sums:
                profile = f" [{s['prompt_profile']}]" if s["prompt_profile"] else ""
                snippet = (s["summary"] or "").strip()[:1200]
                parts.append(f"[{s['date']}] {s['chat_name']}{profile}\n{snippet}")
            counts["summaries"] = len(sums)

    if not parts:
        return "(沒有找到 wallet_log / on-chain 候選資料)", counts
    return "\n".join(parts), counts


# ============================================================
# AI draft (full 6-section profile)
# ============================================================

@bp.route("/api/coin_profiles/<int:profile_id>/draft", methods=["POST"])
def api_coin_profile_draft(profile_id):
    data = get_json_body() or {}
    model_key = data.get("model", "sonnet")
    days = int(data.get("days", 30))
    target_model = MODEL_OPUS if model_key == "opus" else MODEL_SONNET
    model_label = MODEL_SHORT_NAMES.get(target_model, "Sonnet")

    with get_db_ctx() as conn:
        prof_row = conn.execute(
            "SELECT * FROM coin_profiles WHERE id = ?", (profile_id,)
        ).fetchone()
    if not prof_row:
        return jsonify({"error": "找不到 profile"}), 404
    prof = dict(prof_row)
    symbol = prof["symbol"]
    ca = prof["ca"] or ""
    chain = prof["chain"] or ""

    def generate():
        ev = sse_event
        try:
            yield ev({"type": "progress", "progress": 5,
                      "msg": f"📦 聚合 ${symbol} 過去 {days} 天資料..."})

            if not ai_backend.ai_available():
                yield ev({"type": "error", "error": "AI backend 未就緒"})
                return

            context_blob, counts = _coin_draft_context(symbol, ca, days=days)
            total = sum(counts.values())
            if total == 0:
                yield ev({"type": "error",
                          "error": f"記憶庫沒有 ${symbol} 的相關資料(過去 {days} 天 messages / summaries / events 都查無)"})
                return

            yield ev({"type": "progress", "progress": 25,
                      "msg": (f"🧠 {model_label} 起草中 — "
                              f"{counts['messages']} msgs · {counts['summaries']} summaries · {counts['events']} events")})

            ca_line = f"\nCA: {ca}" if ca else ""
            chain_line = f"\nChain: {chain}" if chain else ""
            prompt = ai.PROMPT_COIN_DRAFT.format(
                symbol=symbol, ca_line=ca_line, chain_line=chain_line,
                days=days, context_blob=context_blob,
            )

            def heartbeat(has_text, elapsed, idle):
                if has_text:
                    return {"type": "progress",
                            "msg": f"🧠 生成中…(已 {elapsed}s,上次 token {idle}s 前)"}
                return {"type": "progress", "progress": 25,
                        "msg": f"🧠 等待第一個 token…(已等 {elapsed}s)"}

            full_text, stream_error = yield from stream_ai_events(
                prompt, ai.SYS_COIN_DRAFT, target_model,
                max_tokens=4000,
                idle_timeout=FOREGROUND_AI_IDLE_TIMEOUT_SECS,
                heartbeat_every=10,
                heartbeat_message=heartbeat,
                event_wrapper=ev,
            )
            if stream_error or not full_text:
                yield ev({"type": "error",
                          "error": f"AI draft 失敗: {stream_error or 'no output'}"})
                return

            sections = ai.parse_coin_draft(full_text)
            section_to_field = {
                "NARRATIVE": "narrative",
                "TIMELINE": "timeline_json",
                "KOL_CONSENSUS": "kol_consensus",
                "SMART_MONEY": "smart_money_summary",
                "TOP_SIGNAL": "top_signal",
                "ARCHETYPE": "archetype",
            }
            updates = {}
            for sec, field in section_to_field.items():
                if sections.get(sec):
                    updates[field] = sections[sec]

            yield ev({"type": "progress", "progress": 92,
                      "msg": f"✏️ 寫入 {len(updates)} 個區塊..."})

            saved_profile = prof
            if updates:
                with get_db_ctx() as conn:
                    sets = ", ".join(f"{c} = ?" for c in updates.keys())
                    params = list(updates.values()) + [profile_id]
                    conn.execute(
                        f"UPDATE coin_profiles SET {sets}, "
                        f"last_updated = datetime('now', 'localtime') WHERE id = ?",
                        params,
                    )
                    conn.commit()
                    saved_profile = dict(conn.execute(
                        "SELECT * FROM coin_profiles WHERE id = ?", (profile_id,)
                    ).fetchone())

            yield ev({
                "type": "done",
                "profile": saved_profile,
                "sections_written": list(updates.keys()),
                "context_counts": counts,
            })
        except Exception as e:
            logger.exception("coin draft failed")
            yield ev({"type": "error", "error": str(e)})

    return sse_response(generate)


@bp.route("/api/coin_profiles/<int:profile_id>/draft_smart_money", methods=["POST"])
def api_coin_profile_draft_smart_money(profile_id):
    data = get_json_body() or {}
    model_key = data.get("model", "sonnet")
    days = int(data.get("days", 30))
    target_model = MODEL_OPUS if model_key == "opus" else MODEL_SONNET
    model_label = MODEL_SHORT_NAMES.get(target_model, "Sonnet")

    with get_db_ctx() as conn:
        prof_row = conn.execute(
            "SELECT * FROM coin_profiles WHERE id = ?", (profile_id,)
        ).fetchone()
    if not prof_row:
        return jsonify({"error": "找不到 profile"}), 404
    prof = dict(prof_row)
    symbol = prof["symbol"]
    ca = prof["ca"] or ""
    chain = prof["chain"] or ""

    def generate():
        ev = sse_event
        try:
            yield ev({"type": "progress", "progress": 8,
                      "msg": f"聚合 ${symbol} 的 wallet / on-chain 資料..."})
            if not ai_backend.ai_available():
                yield ev({"type": "error", "error": "AI backend 不可用"})
                return

            context_blob, counts = _coin_smart_money_context(symbol, ca, days=days)
            yield ev({"type": "progress", "progress": 25,
                      "msg": (f"{model_label} 只刷新 Smart money — "
                              f"{counts['messages']} msgs · {counts['summaries']} summaries")})

            ca_line = f"\nCA: {ca}" if ca else ""
            chain_line = f"\nChain: {chain}" if chain else ""
            prompt = ai.PROMPT_COIN_SMART_MONEY.format(
                symbol=symbol, ca_line=ca_line, chain_line=chain_line,
                days=days, context_blob=context_blob,
            )

            smart_text, stream_error = yield from stream_ai_events(
                prompt, ai.SYS_COIN_SMART_MONEY, target_model,
                max_tokens=900,
                idle_timeout=FOREGROUND_AI_IDLE_TIMEOUT_SECS,
                heartbeat_every=8,
                heartbeat_message=lambda _has, elapsed, idle: {
                    "type": "progress",
                    "msg": f"{model_label} 還在整理 smart flow — {elapsed}s, {idle}s 無新 token",
                },
                event_wrapper=ev,
            )
            if stream_error or not smart_text:
                yield ev({"type": "error",
                          "error": f"Smart money draft 失敗: {stream_error or 'no output'}"})
                return

            with get_db_ctx() as conn:
                conn.execute(
                    "UPDATE coin_profiles SET smart_money_summary = ?, "
                    "last_updated = datetime('now', 'localtime') WHERE id = ?",
                    (smart_text, profile_id),
                )
                conn.commit()
                saved_profile = dict(conn.execute(
                    "SELECT * FROM coin_profiles WHERE id = ?", (profile_id,)
                ).fetchone())

            yield ev({
                "type": "done",
                "profile": saved_profile,
                "smart_money_summary": smart_text,
                "context_counts": counts,
            })
        except Exception as e:
            logger.exception("coin smart money draft failed")
            yield ev({"type": "error", "error": str(e)})

    return sse_response(generate)


# ============================================================
# Profile fill (extract fields from user notes + Twitter)
# ============================================================

def _resolve_search_target(notes: str, prof: dict):
    """Pick the best Twitter search query from user notes + existing profile.

    Returns (query, kind) where kind in {"ca", "symbol"} or (None, None) if
    nothing usable. CA is preferred because it's globally unique — symbol
    searches surface unrelated tickers with the same letters.
    """
    for r in (RE_CA_EVM, RE_CA_SOL):
        for m in r.findall(notes or ""):
            if r is RE_CA_SOL and (m in CA_SOL_BLOCKLIST
                                    or m.isdigit() or m.isupper() or m.islower()):
                continue
            return m, "ca"
    if prof.get("ca"):
        return prof["ca"], "ca"
    for m in RE_TICKER.findall(notes or ""):
        return m.upper(), "symbol"
    if prof.get("symbol"):
        return prof["symbol"].upper().lstrip("$"), "symbol"
    return None, None


def _clean_user_notes(notes: str, primary_ca: str = None) -> str:
    """Mechanical cleanup of user-pasted notes before they go into my_raw_notes."""
    text = notes or ""
    if primary_ca:
        ca_esc = re.escape(primary_ca)
        text = re.sub(rf"\(\s*CA\s*[::\s]*{ca_esc}\s*\)",
                      "", text, flags=re.IGNORECASE)
        text = re.sub(rf"CA\s*[::\s]*{ca_esc}", "", text, flags=re.IGNORECASE)
        text = re.sub(ca_esc, "", text)
    lines = [ln.rstrip() for ln in text.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    out, blank = [], 0
    for ln in lines:
        if not ln.strip():
            blank += 1
            if blank <= 1:
                out.append("")
        else:
            blank = 0
            out.append(ln)
    return "\n".join(out)


@bp.route("/api/coin_profiles/<int:profile_id>/fill", methods=["POST"])
def api_coin_profile_fill(profile_id):
    """Extract profile fields from user-supplied free-text notes + Twitter."""
    data = get_json_body() or {}
    notes = (data.get("notes") or "").strip()
    use_twitter = data.get("use_twitter") if "use_twitter" in data else True
    model_key = data.get("model", "sonnet")
    target_model = MODEL_OPUS if model_key == "opus" else MODEL_SONNET
    model_label = MODEL_SHORT_NAMES.get(target_model, "Sonnet")
    if not notes:
        return jsonify({"error": "請貼一些 notes 內容"}), 400
    if len(notes) > 30000:
        return jsonify({"error": f"notes 太長(>{30000} 字),建議拆段送"}), 400

    with get_db_ctx() as conn:
        prof_row = conn.execute(
            "SELECT * FROM coin_profiles WHERE id = ?", (profile_id,)
        ).fetchone()
    if not prof_row:
        return jsonify({"error": "找不到 profile"}), 404
    prof = dict(prof_row)

    section_keys = (
        "narrative", "timeline_json", "kol_consensus", "smart_money_summary",
        "top_signal", "archetype", "my_entry_fdv", "my_entry_size",
        "my_exit_fdv", "my_exit_size", "my_pnl", "my_wallet",
        "my_verdict", "my_lesson", "tags",
    )
    filled = [k for k in section_keys if (prof.get(k) or "").strip()]
    filled_str = ", ".join(filled) if filled else "(全部空)"

    def generate():
        ev = sse_event
        try:
            yield ev({"type": "progress", "progress": 5,
                      "msg": f"📦 解析 {len(notes)} 字筆記..."})
            if not ai_backend.ai_available():
                yield ev({"type": "error", "error": "AI backend 未就緒"})
                return

            tweet_blob = "(未啟用)"
            tweet_count = 0
            search_target = None
            if use_twitter and twitter_client.available():
                target, target_kind = _resolve_search_target(notes, prof)
                if target:
                    search_target = f"{target_kind}:{target}"
                    yield ev({"type": "progress", "progress": 15,
                              "msg": f"🐦 用 {target_kind} 「{target[:40]}」搜 X..."})
                    try:
                        if target_kind == "ca":
                            tweets = twitter_client.search(
                                keywords=target, max_results=10,
                                exclude_replies=True,
                            )
                        else:
                            tweets = twitter_client.search(
                                keywords=f"${target}", max_results=10,
                                exclude_replies=True,
                            )
                    except Exception as e:
                        logger.warning("twitter search failed for fill: %s", e)
                        tweets = []
                    tweet_lines = []
                    for t in tweets:
                        if twitter_client.is_low_signal_tweet(t):
                            continue
                        line = twitter_client.format_tweet_line(t, max_len=320)
                        if line:
                            tweet_lines.append(line)
                    tweet_count = len(tweet_lines)
                    tweet_blob = ("\n".join(tweet_lines) if tweet_lines
                                  else "(搜到 0 則或全部低訊號)")
            elif not twitter_client.available():
                tweet_blob = "(TWITTER_TOKEN 未設,跳過)"

            prompt = ai.PROMPT_PROFILE_FILL.format(
                current_symbol=prof.get("symbol", ""),
                current_chain=prof.get("chain") or "(無)",
                current_ca=prof.get("ca") or "(無)",
                current_status=prof.get("status") or "tracking",
                filled_sections=filled_str,
                user_notes=notes,
                twitter_blob=tweet_blob,
            )

            tw_part = (f" · {tweet_count} tweets" if tweet_count
                       else (" · X 0" if twitter_client.available() else " · X off"))
            yield ev({"type": "progress", "progress": 25,
                      "msg": f"🧠 {model_label} 抽取欄位中{tw_part}..."})

            def heartbeat(has_text, elapsed, idle):
                if has_text:
                    return {"type": "progress",
                            "msg": f"🧠 抽取中…(已 {elapsed}s,上次 token {idle}s 前)"}
                return {"type": "progress", "progress": 25,
                        "msg": f"🧠 等待第一個 token…(已等 {elapsed}s)"}

            full_text, stream_error = yield from stream_ai_events(
                prompt, ai.SYS_PROFILE_FILL, target_model,
                max_tokens=2000,
                idle_timeout=FOREGROUND_AI_IDLE_TIMEOUT_SECS,
                heartbeat_every=10,
                heartbeat_message=heartbeat,
                event_wrapper=ev,
            )
            if stream_error or not full_text:
                yield ev({"type": "error",
                          "error": f"擷取失敗: {stream_error or 'no output'}"})
                return

            cleaned = full_text
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
                cleaned = re.sub(r"\n?```\s*$", "", cleaned)
            patch_raw = try_parse_json_object(cleaned)
            if patch_raw is None:
                yield ev({"type": "error",
                          "error": f"AI 輸出不是合法 JSON. 原始輸出前 200 字: {full_text[:200]!r}"})
                return

            if not isinstance(patch_raw, dict):
                yield ev({"type": "error", "error": "AI 輸出不是 JSON object"})
                return

            try:
                patch = _coin_profile_payload(patch_raw)
            except ValueError as e:
                yield ev({"type": "error", "error": f"欄位驗證失敗: {e}"})
                return

            yield ev({"type": "progress", "progress": 92,
                      "msg": f"✏️ 寫入 {len(patch)} 個欄位..."})

            existing_raw = prof.get("my_raw_notes") or ""
            ts = datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M")
            final_ca = (patch.get("ca") or prof.get("ca") or "").strip()
            cleaned_notes = _clean_user_notes(notes, primary_ca=final_ca or None)
            new_raw_block = f"[{ts}]\n{cleaned_notes}"
            if existing_raw.strip():
                merged_raw = new_raw_block + "\n\n" + existing_raw
            else:
                merged_raw = new_raw_block
            patch["my_raw_notes"] = merged_raw

            with get_db_ctx() as conn:
                sets = ", ".join(f"{c} = ?" for c in patch.keys())
                params = list(patch.values()) + [profile_id]
                conn.execute(
                    f"UPDATE coin_profiles SET {sets}, "
                    f"last_updated = datetime('now', 'localtime') WHERE id = ?",
                    params,
                )
                conn.commit()
                saved = dict(conn.execute(
                    "SELECT * FROM coin_profiles WHERE id = ?", (profile_id,)
                ).fetchone())

            fields_written = [k for k in patch.keys() if k != "my_raw_notes"]

            yield ev({
                "type": "done",
                "profile": saved,
                "fields_written": fields_written,
                "raw_json": cleaned[:2000],
                "tweet_count": tweet_count,
                "search_target": search_target,
            })
        except Exception as e:
            logger.exception("profile fill failed")
            yield ev({"type": "error", "error": str(e)})

    return sse_response(generate)


# ============================================================
# Create coin profile from a single review/post-mortem paste
# ============================================================

@bp.route("/api/coin_profiles/from_notes", methods=["POST"])
def api_coin_profile_from_notes():
    data = get_json_body() or {}
    notes = (data.get("notes") or "").strip()
    use_twitter = data.get("use_twitter") if "use_twitter" in data else True
    model_key = data.get("model", "sonnet")
    target_model = MODEL_OPUS if model_key == "opus" else MODEL_SONNET
    model_label = MODEL_SHORT_NAMES.get(target_model, "Sonnet")

    if not notes:
        return jsonify({"error": "請貼一些 notes 內容"}), 400
    if len(notes) > 30000:
        return jsonify({"error": f"notes 太長(>{30000} 字),建議拆段送"}), 400

    target, target_kind = _resolve_search_target(notes, {})
    if not target:
        return jsonify({"error": "在筆記裡找不到 CA 或 $TICKER。請在內文加上 CA(0x… / base58 32-44 字)或 $SYMBOL,或用「+ New」手動建檔"}), 400

    chain_hint = ""
    if target_kind == "ca":
        if target.startswith("0x") and len(target) == 42:
            chain_hint = "evm"
        elif 32 <= len(target) <= 44 and not target.startswith("0x"):
            chain_hint = "solana"

    def generate():
        ev = sse_event
        try:
            yield ev({"type": "progress", "progress": 5,
                      "msg": f"📦 解析 {len(notes)} 字筆記..."})
            if not ai_backend.ai_available():
                yield ev({"type": "error", "error": "AI backend 未就緒"})
                return

            tweet_blob = "(未啟用)"
            tweet_count = 0
            search_target = f"{target_kind}:{target}"
            if use_twitter and twitter_client.available():
                yield ev({"type": "progress", "progress": 15,
                          "msg": f"🐦 用 {target_kind} 「{target[:40]}」搜 X..."})
                try:
                    if target_kind == "ca":
                        tweets = twitter_client.search(
                            keywords=target, max_results=10, exclude_replies=True,
                        )
                    else:
                        tweets = twitter_client.search(
                            keywords=f"${target}", max_results=10, exclude_replies=True,
                        )
                except Exception as e:
                    logger.warning("twitter search failed for from_notes: %s", e)
                    tweets = []
                tweet_lines = []
                for t in tweets:
                    if twitter_client.is_low_signal_tweet(t):
                        continue
                    line = twitter_client.format_tweet_line(t, max_len=320)
                    if line:
                        tweet_lines.append(line)
                tweet_count = len(tweet_lines)
                tweet_blob = ("\n".join(tweet_lines) if tweet_lines
                              else "(搜到 0 則或全部低訊號)")
            elif not twitter_client.available():
                tweet_blob = "(TWITTER_TOKEN 未設,跳過)"

            ca_hint = target if target_kind == "ca" else "(無)"
            symbol_hint = target if target_kind == "symbol" else "(請從筆記/推文判斷)"
            prompt = ai.PROMPT_PROFILE_FROM_NOTES.format(
                ca_hint=ca_hint,
                symbol_hint=symbol_hint,
                chain_hint=chain_hint or "(請判斷)",
                user_notes=notes,
                twitter_blob=tweet_blob,
            )

            tw_part = (f" · {tweet_count} tweets" if tweet_count
                       else (" · X 0" if twitter_client.available() else " · X off"))
            yield ev({"type": "progress", "progress": 25,
                      "msg": f"🧠 {model_label} 建檔中{tw_part}..."})

            def heartbeat(has_text, elapsed, idle):
                if has_text:
                    return {"type": "progress",
                            "msg": f"🧠 抽取中…(已 {elapsed}s,上次 token {idle}s 前)"}
                return {"type": "progress", "progress": 25,
                        "msg": f"🧠 等待第一個 token…(已等 {elapsed}s)"}

            full_text, stream_error = yield from stream_ai_events(
                prompt, ai.SYS_PROFILE_FROM_NOTES, target_model,
                max_tokens=2000,
                idle_timeout=FOREGROUND_AI_IDLE_TIMEOUT_SECS,
                heartbeat_every=10,
                heartbeat_message=heartbeat,
                event_wrapper=ev,
            )
            if stream_error or not full_text:
                yield ev({"type": "error",
                          "error": f"建檔失敗: {stream_error or 'no output'}"})
                return

            cleaned = full_text
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
                cleaned = re.sub(r"\n?```\s*$", "", cleaned)
            patch_raw = try_parse_json_object(cleaned)
            if patch_raw is None:
                yield ev({"type": "error",
                          "error": f"AI 輸出不是合法 JSON. 原始前 200 字: {full_text[:200]!r}"})
                return

            if not isinstance(patch_raw, dict):
                yield ev({"type": "error", "error": "AI 輸出不是 JSON object"})
                return

            if target_kind == "ca" and not (patch_raw.get("ca") or "").strip():
                patch_raw["ca"] = target
            if not (patch_raw.get("symbol") or "").strip() and target_kind == "symbol":
                patch_raw["symbol"] = target
            if (chain_hint == "solana" and not (patch_raw.get("chain") or "").strip()):
                patch_raw["chain"] = "solana"

            try:
                payload = _coin_profile_payload(patch_raw)
            except ValueError as e:
                yield ev({"type": "error", "error": f"欄位驗證失敗: {e}"})
                return

            if "symbol" not in payload or not payload["symbol"]:
                yield ev({"type": "error",
                          "error": "AI 找不出 symbol — 請在筆記內明確寫 $TICKER,或用「+ New」手動建檔"})
                return

            ts = datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M")
            primary_ca = target if target_kind == "ca" else None
            cleaned_notes = _clean_user_notes(notes, primary_ca=primary_ca)
            payload["my_raw_notes"] = f"[{ts}]\n{cleaned_notes}"

            yield ev({"type": "progress", "progress": 92,
                      "msg": f"✏️ 建立 / 合併 ${payload['symbol']} profile..."})

            with get_db_ctx() as conn:
                existing = _find_profile_by_exact_ca(conn, payload.get("ca"))
                merged = bool(existing)
                if existing:
                    updates = _merge_profile_payload(existing, payload)
                    if updates:
                        sets = ", ".join(f"{c} = ?" for c in updates.keys())
                        params = list(updates.values()) + [existing["id"]]
                        conn.execute(
                            f"UPDATE coin_profiles SET {sets}, "
                            f"last_updated = datetime('now', 'localtime') WHERE id = ?",
                            params,
                        )
                        conn.commit()
                    row_id = existing["id"]
                else:
                    cols = list(payload.keys())
                    placeholders = ",".join("?" for _ in cols)
                    try:
                        conn.execute(
                            f"INSERT INTO coin_profiles ({','.join(cols)}) VALUES ({placeholders})",
                            [payload[c] for c in cols],
                        )
                        conn.commit()
                    except sqlite3.IntegrityError as e:
                        yield ev({"type": "error", "error": f"DB 寫入失敗: {e}"})
                        return
                    row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                saved = dict(conn.execute(
                    "SELECT * FROM coin_profiles WHERE id = ?", (row_id,)
                ).fetchone())

            fields_written = [k for k in payload.keys() if k != "my_raw_notes"]
            yield ev({
                "type": "done",
                "profile": saved,
                "id": row_id,
                "fields_written": fields_written,
                "raw_json": cleaned[:2000],
                "tweet_count": tweet_count,
                "search_target": search_target,
                "merged": merged,
            })
        except Exception as e:
            logger.exception("profile from_notes failed")
            yield ev({"type": "error", "error": str(e)})

    return sse_response(generate)
