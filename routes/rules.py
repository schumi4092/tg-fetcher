"""Trading rules routes — CRUD + distill candidate rules from a coin profile.

Trading rules are cross-coin guardrails (vs `coin_profiles.my_lesson` which is
per-coin). Active rules get injected into summarize/digest prompts via
`ai.build_trading_rules_block()`; the AI flags `✓ 法則 #N` / `⚠️ 違反 #N` and
post-processing in `ai.update_rule_hits()` bumps `hit_count` on cited rules.
"""

import sqlite3

from flask import Blueprint, jsonify, request

from config import MODEL_OPUS, MODEL_SHORT_NAMES, MODEL_SONNET, logger
from db import get_db_ctx
import ai
import ai_backend

from routes._shared import get_json_body, sse_event, sse_response
from routes._ai_stream import stream_ai_events


bp = Blueprint("rules", __name__)


# Editable columns. id / created_at / updated_at / hit_count / last_hit_at are
# server-managed; everything else is user-writable.
_RULE_FIELDS = ("rule_text", "reason", "scope", "status",
                "source_profile_id", "pinned")
_RULE_STATUSES = ("active", "archived", "draft")
_RULE_SCOPES = ("entry", "exit", "risk", "sizing", "general")


def _rule_payload(data, *, partial=False):
    """Coerce/validate payload for INSERT/UPDATE.

    `partial=True` allows missing fields (PUT path); when False (POST),
    `rule_text` is required.
    """
    out = {}
    for field in _RULE_FIELDS:
        if field not in data:
            continue
        val = data[field]
        if field == "pinned":
            out[field] = 1 if val else 0
        elif field == "status":
            if val not in _RULE_STATUSES:
                raise ValueError(f"status 必須是 {'/'.join(_RULE_STATUSES)}")
            out[field] = val
        elif field == "scope":
            v = (val or "general").strip().lower() or "general"
            # Free-form tag in principle, but coerce to known set when matched
            # — typos like "exits" get normalized; unknown scopes still allowed.
            out[field] = v
        elif field == "rule_text":
            v = (val or "").strip()
            if not v:
                raise ValueError("rule_text 不可為空")
            out[field] = v
        elif field == "source_profile_id":
            if val in (None, "", 0):
                out[field] = None
            else:
                try:
                    out[field] = int(val)
                except (TypeError, ValueError):
                    raise ValueError("source_profile_id 必須是整數")
        else:
            out[field] = "" if val is None else str(val)

    if not partial and "rule_text" not in out:
        raise ValueError("缺少 rule_text")
    return out


def _row_to_dict(row):
    return dict(row) if row else None


# ============================================================
# CRUD
# ============================================================

@bp.route("/api/trading_rules", methods=["GET"])
def api_rules_list():
    status = request.args.get("status")
    scope = request.args.get("scope")
    q = (request.args.get("q") or "").strip()
    source_profile_id = request.args.get("source_profile_id", type=int)

    sql = "SELECT * FROM trading_rules WHERE 1=1"
    params = []
    if status and status in _RULE_STATUSES:
        sql += " AND status = ?"
        params.append(status)
    if scope:
        sql += " AND scope = ?"
        params.append(scope)
    if source_profile_id:
        sql += " AND source_profile_id = ?"
        params.append(source_profile_id)
    if q:
        sql += " AND (rule_text LIKE ? OR reason LIKE ?)"
        like = f"%{q}%"
        params += [like, like]
    sql += (" ORDER BY pinned DESC, "
            "CASE status WHEN 'active' THEN 0 WHEN 'draft' THEN 1 ELSE 2 END, "
            "hit_count DESC, id ASC")

    with get_db_ctx() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    return jsonify({"rules": rows})


@bp.route("/api/trading_rules", methods=["POST"])
def api_rules_create():
    data = get_json_body()
    try:
        payload = _rule_payload(data, partial=False)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    cols = list(payload.keys())
    placeholders = ",".join("?" for _ in cols)
    try:
        with get_db_ctx() as conn:
            conn.execute(
                f"INSERT INTO trading_rules ({','.join(cols)}) VALUES ({placeholders})",
                [payload[c] for c in cols],
            )
            conn.commit()
            rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            row = _row_to_dict(conn.execute(
                "SELECT * FROM trading_rules WHERE id = ?", (rid,)
            ).fetchone())
        return jsonify({"id": rid, "rule": row})
    except sqlite3.IntegrityError as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/api/trading_rules/<int:rule_id>", methods=["GET"])
def api_rules_get(rule_id):
    with get_db_ctx() as conn:
        row = conn.execute(
            "SELECT * FROM trading_rules WHERE id = ?", (rule_id,)
        ).fetchone()
    if not row:
        return jsonify({"error": "找不到 rule"}), 404
    return jsonify({"rule": dict(row)})


@bp.route("/api/trading_rules/<int:rule_id>", methods=["PUT"])
def api_rules_update(rule_id):
    data = get_json_body()
    try:
        payload = _rule_payload(data, partial=True)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not payload:
        return jsonify({"error": "沒有可更新的欄位"}), 400

    with get_db_ctx() as conn:
        row = conn.execute(
            "SELECT 1 FROM trading_rules WHERE id = ?", (rule_id,)
        ).fetchone()
        if not row:
            return jsonify({"error": "找不到 rule"}), 404
        sets = ", ".join(f"{c} = ?" for c in payload.keys())
        params = list(payload.values()) + [rule_id]
        conn.execute(
            f"UPDATE trading_rules SET {sets}, "
            f"updated_at = datetime('now', 'localtime') WHERE id = ?",
            params,
        )
        conn.commit()
        updated = dict(conn.execute(
            "SELECT * FROM trading_rules WHERE id = ?", (rule_id,)
        ).fetchone())
    return jsonify({"rule": updated})


@bp.route("/api/trading_rules/<int:rule_id>", methods=["DELETE"])
def api_rules_delete(rule_id):
    with get_db_ctx() as conn:
        row = conn.execute(
            "SELECT 1 FROM trading_rules WHERE id = ?", (rule_id,)
        ).fetchone()
        if not row:
            return jsonify({"error": "找不到 rule"}), 404
        conn.execute("DELETE FROM trading_rules WHERE id = ?", (rule_id,))
        conn.commit()
    return jsonify({"status": "deleted"})


# ============================================================
# Distill candidate rules from a coin profile
# (POST /api/coin_profiles/<id>/distill_rules — namespaced under coin_profiles
# because semantically it's "this profile → some rules", but the SSE handler
# lives here next to the rule CRUD logic.)
# ============================================================

@bp.route("/api/coin_profiles/<int:profile_id>/distill_rules", methods=["POST"])
def api_distill_rules(profile_id):
    data = get_json_body() or {}
    model_key = data.get("model", "sonnet")
    target_model = MODEL_OPUS if model_key == "opus" else MODEL_SONNET
    model_label = MODEL_SHORT_NAMES.get(target_model, "Sonnet")

    with get_db_ctx() as conn:
        row = conn.execute(
            "SELECT id, symbol, chain, archetype, narrative, "
            "my_verdict, my_lesson, my_raw_notes "
            "FROM coin_profiles WHERE id = ?",
            (profile_id,),
        ).fetchone()
    if not row:
        return jsonify({"error": "找不到 profile"}), 404

    profile = dict(row)
    # Need at least lesson or verdict or notes — pure observational profiles
    # have nothing to distill.
    if not any((profile.get(k) or "").strip()
               for k in ("my_lesson", "my_verdict", "my_raw_notes")):
        return jsonify({
            "error": "這份 profile 沒有 my_lesson / my_verdict / my_raw_notes,"
                     "先補一些再試提煉"
        }), 400

    def generate():
        ev = sse_event
        try:
            yield ev({"type": "progress", "progress": 10,
                      "msg": f"📦 讀 ${profile.get('symbol') or '?'} 的 lesson + notes..."})
            if not ai_backend.ai_available():
                yield ev({"type": "error", "error": "AI backend 未就緒"})
                return

            prompt = ai.PROMPT_DISTILL_RULES.format(
                symbol=profile.get("symbol") or "?",
                chain=profile.get("chain") or "(未知)",
                archetype=(profile.get("archetype") or "(無)")[:600],
                narrative=(profile.get("narrative") or "(無)")[:600],
                my_verdict=(profile.get("my_verdict") or "(無)")[:600],
                my_lesson=(profile.get("my_lesson") or "(無)")[:600],
                my_raw_notes=(profile.get("my_raw_notes") or "(無)")[:1500],
            )

            yield ev({"type": "progress", "progress": 25,
                      "msg": f"🧠 {model_label} 提煉中..."})

            def heartbeat(has_text, elapsed, idle):
                if has_text:
                    return {"type": "progress",
                            "msg": f"🧠 提煉中…(已 {elapsed}s,上次 token {idle}s 前)"}
                return {"type": "progress", "progress": 25,
                        "msg": f"🧠 等待第一個 token…(已等 {elapsed}s)"}

            full_text, stream_error = yield from stream_ai_events(
                prompt, ai.SYS_DISTILL_RULES, target_model,
                max_tokens=800,
                idle_timeout=180,
                heartbeat_every=10,
                heartbeat_message=heartbeat,
                event_wrapper=ev,
            )
            if stream_error:
                yield ev({"type": "error",
                          "error": f"提煉失敗: {stream_error}"})
                return
            if not full_text:
                yield ev({"type": "error", "error": "AI 沒有輸出"})
                return

            cleaned = full_text
            if cleaned.startswith("```"):
                import re as _re
                cleaned = _re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
                cleaned = _re.sub(r"\n?```\s*$", "", cleaned)

            import json as _json
            try:
                arr = _json.loads(cleaned)
            except Exception as e:
                # Fallback: slice from first `[` to last `]`.
                start = cleaned.find("[")
                end = cleaned.rfind("]")
                if start != -1 and end > start:
                    try:
                        arr = _json.loads(cleaned[start:end + 1])
                    except Exception as e2:
                        yield ev({"type": "error",
                                  "error": f"AI 輸出不是合法 JSON: {e2}. 原始前 200 字: {full_text[:200]!r}"})
                        return
                else:
                    yield ev({"type": "error",
                              "error": f"AI 輸出不是合法 JSON: {e}. 原始前 200 字: {full_text[:200]!r}"})
                    return

            if not isinstance(arr, list):
                yield ev({"type": "error", "error": "AI 輸出不是 JSON 陣列"})
                return

            candidates = []
            for item in arr:
                if not isinstance(item, dict):
                    continue
                rule_text = (item.get("rule_text") or "").strip()
                if not rule_text:
                    continue
                scope = (item.get("scope") or "general").strip().lower()
                if scope not in _RULE_SCOPES:
                    scope = "general"
                candidates.append({
                    "rule_text": rule_text,
                    "reason": (item.get("reason") or "").strip(),
                    "scope": scope,
                    "source_profile_id": profile_id,
                })

            yield ev({
                "type": "done",
                "candidates": candidates,
                "raw_json": cleaned[:2000],
                "source_profile_id": profile_id,
                "source_symbol": profile.get("symbol"),
            })
        except Exception as e:
            logger.exception("distill_rules failed")
            yield ev({"type": "error", "error": str(e)})

    return sse_response(generate)
