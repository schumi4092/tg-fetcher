"""Memory routes — timeline, day, digest, events, notes, summaries, search,
archive, ask, sentiment, diff, import, export. Anything that reads/writes the
memory store but isn't AI summarize and isn't coin/watchtower-specific."""

import json
import threading
from datetime import date, datetime

from flask import Blueprint, jsonify, make_response, request

from config import logger
from db import (
    build_fts_query,
    get_db_ctx,
    import_event_if_missing,
    import_note_if_missing,
    import_watchlist_if_missing,
    normalize_memory_import_payload,
    save_messages_for_summary,
)
import ai

from routes._shared import get_json_body


bp = Blueprint("memory", __name__)
_retry_slots_lock = threading.Lock()
_retrying_slots = set()
AUTO_FALLBACK_PREFIX = "[AI auto-summary fallback:"


def _effective_summary_run_status(raw_status, completed_chats, expected_chats):
    """Prefer observed completion over a stale in-flight run marker.

    A recovery cycle may keep `auto_summary_runs.summary_status='running'`
    until every slot in the cycle finishes, even after one specific slot has
    already reached all tracked chats. The UI should treat that slot as done.
    """
    if expected_chats and completed_chats >= expected_chats:
        return "done"
    return raw_status or ""


def _upsert_auto_summary_run(summary_date, summary_slot, **fields):
    allowed = {
        "summary_status", "summary_started_at", "summary_finished_at",
        "ok_count", "skip_existing_count", "skip_no_msgs_count",
        "failed_count", "error",
    }
    payload = {k: v for k, v in fields.items() if k in allowed}
    columns = ["date", "slot"] + list(payload.keys()) + ["updated_at"]
    values = (
        [summary_date, summary_slot]
        + list(payload.values())
        + [datetime.now().isoformat(timespec="seconds")]
    )
    placeholders = ",".join("?" * len(columns))
    updates = ", ".join(
        f"{col} = excluded.{col}"
        for col in columns
        if col not in {"date", "slot"}
    )
    with get_db_ctx() as conn:
        conn.execute(
            f"""
            INSERT INTO auto_summary_runs ({", ".join(columns)})
            VALUES ({placeholders})
            ON CONFLICT(date, slot) DO UPDATE SET {updates}
            """,
            values,
        )
        conn.commit()


def _fallback_summary_rows(conn, date_str, slot):
    return [dict(r) for r in conn.execute("""
        SELECT id, chat_id, chat_name
        FROM daily_summaries
        WHERE date = ?
          AND COALESCE(summary_slot, '') = ?
          AND source = 'auto'
          AND summary LIKE ?
    """, (date_str, slot, AUTO_FALLBACK_PREFIX + "%")).fetchall()]


def _delete_summaries(conn, summary_ids):
    if not summary_ids:
        return
    placeholders = ",".join("?" for _ in summary_ids)
    conn.execute(
        f"DELETE FROM message_summary_links WHERE summary_id IN ({placeholders})",
        summary_ids,
    )
    conn.execute(
        f"UPDATE messages SET summary_id = NULL WHERE summary_id IN ({placeholders})",
        summary_ids,
    )
    conn.execute(
        f"DELETE FROM events WHERE source_summary_id IN ({placeholders})",
        summary_ids,
    )
    conn.execute(
        f"DELETE FROM sentiment_scores WHERE summary_id IN ({placeholders})",
        summary_ids,
    )
    conn.execute(
        f"DELETE FROM daily_summaries WHERE id IN ({placeholders})",
        summary_ids,
    )


def _retry_auto_summary_slot(date_str, slot, since_iso, until_iso, retry_fallbacks=False):
    key = (date_str, slot)
    counts = {"ok": 0, "skipped_existing": 0, "skipped_no_messages": 0, "failed": 0}
    last_error = ""
    try:
        with get_db_ctx() as conn:
            if retry_fallbacks:
                fallback_rows = _fallback_summary_rows(conn, date_str, slot)
                _delete_summaries(conn, [r["id"] for r in fallback_rows])
                conn.commit()
                logger.info(
                    "manual auto-summary retry: deleted %d fallback summaries date=%s slot=%s",
                    len(fallback_rows), date_str, slot,
                )
            chats = [dict(r) for r in conn.execute("""
                SELECT m.chat_id
                FROM chat_category_map m
                JOIN chat_categories c ON c.id = m.category_id
                ORDER BY c.sort_order, m.chat_id
            """).fetchall()]
            names = {}
            for c in chats:
                row = conn.execute(
                    "SELECT chat_name FROM messages WHERE chat_id = ? "
                    "ORDER BY date DESC LIMIT 1",
                    (c["chat_id"],),
                ).fetchone()
                names[c["chat_id"]] = (row["chat_name"] if row else None) or c["chat_id"]

        try:
            window_hours = (
                datetime.fromisoformat(until_iso) - datetime.fromisoformat(since_iso)
            ).total_seconds() / 3600
        except Exception:
            window_hours = 0

        logger.info(
            "manual auto-summary retry: starting date=%s slot=%s chats=%d retry_fallbacks=%s",
            date_str, slot, len(chats), retry_fallbacks,
        )
        for c in chats:
            chat_id = c["chat_id"]
            chat_name = names.get(chat_id) or chat_id
            try:
                _summary_id, status = ai.summarize_chat_auto(
                    chat_id,
                    chat_name,
                    hours=window_hours,
                    since_iso=since_iso,
                    until_iso=until_iso,
                    summary_date=date_str,
                    summary_slot=slot,
                )
                counts[status] = counts.get(status, 0) + 1
            except Exception as e:
                counts["failed"] += 1
                last_error = str(e)
                logger.warning(
                    "manual auto-summary retry failed for %s [%s] date=%s slot=%s: %s",
                    chat_name, chat_id, date_str, slot, e,
                )

        with get_db_ctx() as conn:
            summary_count = conn.execute(
                "SELECT COUNT(*) FROM daily_summaries "
                "WHERE date = ? AND COALESCE(summary_slot, '') = ?",
                (date_str, slot),
            ).fetchone()[0]
        failed = counts["failed"]
        summary_status = "failed" if failed and not summary_count else "partial" if failed else "done"
        _upsert_auto_summary_run(
            date_str,
            slot,
            summary_status=summary_status,
            summary_finished_at=datetime.now().isoformat(timespec="seconds"),
            ok_count=summary_count,
            skip_existing_count=counts.get("skipped_existing", 0),
            skip_no_msgs_count=counts.get("skipped_no_messages", 0),
            failed_count=failed,
            error=last_error,
        )
        logger.info(
            "manual auto-summary retry: done date=%s slot=%s ok_total=%d "
            "skip(no-msgs)=%d failed=%d",
            date_str, slot, summary_count, counts.get("skipped_no_messages", 0), failed,
        )
    finally:
        with _retry_slots_lock:
            _retrying_slots.discard(key)


@bp.route("/api/memory/timeline")
def memory_timeline():
    days = request.args.get("days", 30, type=int)
    with get_db_ctx() as conn:
        active_dates = [r["date"] for r in conn.execute("""
            SELECT date FROM (
                SELECT date FROM daily_summaries
                UNION
                SELECT date FROM events
                UNION
                SELECT date FROM notes
                UNION
                SELECT date FROM auto_summary_runs
            )
            ORDER BY date DESC LIMIT ?
        """, (days,)).fetchall()]

        if not active_dates:
            return jsonify({"timeline": []})

        placeholders = ",".join("?" * len(active_dates))
        summary_rows = conn.execute(f"""
            SELECT
                date,
                COALESCE(summary_slot, '') AS summary_slot,
                COUNT(*) AS chat_count,
                SUM(message_count) AS total_msgs,
                GROUP_CONCAT(chat_name) AS chats,
                MIN(NULLIF(period_start, '')) AS period_start,
                MAX(NULLIF(period_end, '')) AS period_end
            FROM daily_summaries
            WHERE date IN ({placeholders})
            GROUP BY date, COALESCE(summary_slot, '')
        """, active_dates).fetchall()

        event_rows = conn.execute(f"""
            SELECT
                e.date,
                COALESCE(ds.summary_slot, '') AS summary_slot,
                COUNT(*) AS event_count,
                GROUP_CONCAT(e.title, '||') AS titles,
                GROUP_CONCAT(e.importance, '||') AS importances
            FROM events e
            LEFT JOIN daily_summaries ds ON ds.id = e.source_summary_id
            WHERE e.date IN ({placeholders})
            GROUP BY e.date, COALESCE(ds.summary_slot, '')
        """, active_dates).fetchall()

        note_rows = conn.execute(f"""
            SELECT date, COUNT(*) AS note_count
            FROM notes
            WHERE date IN ({placeholders})
            GROUP BY date
        """, active_dates).fetchall()
        run_rows = conn.execute(f"""
            SELECT
                date,
                slot AS summary_slot,
                fetch_status,
                summary_status,
                ok_count,
                skip_existing_count,
                skip_no_msgs_count,
                failed_count,
                error,
                updated_at
            FROM auto_summary_runs
            WHERE date IN ({placeholders})
        """, active_dates).fetchall()
        expected_chat_count = conn.execute(
            "SELECT COUNT(*) FROM chat_category_map"
        ).fetchone()[0]

    rows_by_key = {}

    def get_row(date_str, slot):
        slot = slot or ""
        key = (date_str, slot)
        if key not in rows_by_key:
            rows_by_key[key] = {
                "date": date_str,
                "summary_slot": slot,
                "timeline_key": f"{date_str}::{slot}",
                "summaries": 0,
                "total_msgs": 0,
                "chats": "",
                "events": 0,
                "event_titles": [],
                "notes": 0,
                "period_start": "",
                "period_end": "",
                "run_status": "",
                "fetch_status": "",
                "expected_chats": expected_chat_count or 0,
                "completed_chats": 0,
                "processed_chats": 0,
                "failed_chats": 0,
                "skipped_existing_chats": 0,
                "skipped_no_msgs_chats": 0,
                "run_error": "",
                "run_updated_at": "",
            }
        return rows_by_key[key]

    for r in summary_rows:
        row = get_row(r["date"], r["summary_slot"])
        row["summaries"] = r["chat_count"] or 0
        row["total_msgs"] = r["total_msgs"] or 0
        row["chats"] = r["chats"] or ""
        row["period_start"] = r["period_start"] or ""
        row["period_end"] = r["period_end"] or ""

    for r in event_rows:
        row = get_row(r["date"], r["summary_slot"])
        event_titles = []
        if r["titles"]:
            titles = r["titles"].split("||")
            importances = (r["importances"] or "").split("||")
            event_titles = [
                {"title": t, "importance": importances[i] if i < len(importances) else "normal"}
                for i, t in enumerate(titles)
            ]
        row["events"] = r["event_count"] or 0
        row["event_titles"] = event_titles

    for r in note_rows:
        row = get_row(r["date"], "")
        row["notes"] = r["note_count"] or 0

    for r in run_rows:
        row = get_row(r["date"], r["summary_slot"])
        row["fetch_status"] = r["fetch_status"] or ""
        row["completed_chats"] = max(row["summaries"], r["ok_count"] or 0)
        row["run_status"] = _effective_summary_run_status(
            r["summary_status"],
            row["completed_chats"],
            row["expected_chats"],
        )
        is_effectively_done = row["run_status"] == "done"
        row["failed_chats"] = 0 if is_effectively_done else (r["failed_count"] or 0)
        row["skipped_existing_chats"] = r["skip_existing_count"] or 0
        row["skipped_no_msgs_chats"] = r["skip_no_msgs_count"] or 0
        row["run_error"] = "" if is_effectively_done else (r["error"] or "")
        row["run_updated_at"] = r["updated_at"] or ""
        row["processed_chats"] = (
            row["completed_chats"]
            + row["failed_chats"]
            + row["skipped_existing_chats"]
            + row["skipped_no_msgs_chats"]
        )

    timeline = sorted(
        rows_by_key.values(),
        key=lambda r: (r["date"], r["summary_slot"] or ""),
        reverse=True,
    )
    return jsonify({"timeline": timeline})


@bp.route("/api/memory/day/<date_str>")
def memory_day(date_str):
    slot = request.args.get("slot")
    slot_filter = slot is not None
    slot = (slot or "").strip()
    with get_db_ctx() as conn:
        # Order by category sort_order so chats from the same group sit
        # adjacent (Ray → Tier-1 wallets → …); uncategorised chats fall to
        # the end. Within a category we sort by chat_name for a stable layout.
        summary_where = "WHERE ds.date = ?"
        summary_params = [date_str]
        if slot_filter:
            summary_where += " AND COALESCE(ds.summary_slot, '') = ?"
            summary_params.append(slot)
        summaries = [dict(r) for r in conn.execute("""
            SELECT ds.*, cc.name AS category_name, cc.sort_order AS category_sort
            FROM daily_summaries ds
            LEFT JOIN chat_category_map ccm ON ccm.chat_id = ds.chat_id
            LEFT JOIN chat_categories cc ON cc.id = ccm.category_id
            """ + summary_where + """
            ORDER BY
                CASE WHEN COALESCE(ds.summary_slot, '') = '' THEN 1 ELSE 0 END,
                ds.summary_slot DESC,
                CASE WHEN cc.sort_order IS NULL THEN 1 ELSE 0 END,
                cc.sort_order,
                cc.id,
                ds.chat_name
        """, summary_params).fetchall()]
        if slot_filter:
            events = [dict(r) for r in conn.execute("""
                SELECT e.*
                FROM events e
                JOIN daily_summaries ds ON ds.id = e.source_summary_id
                WHERE e.date = ? AND COALESCE(ds.summary_slot, '') = ?
                ORDER BY e.importance DESC, e.created_at
            """, (date_str, slot)).fetchall()]
        else:
            events = [dict(r) for r in conn.execute(
                "SELECT * FROM events WHERE date = ? ORDER BY importance DESC, created_at", (date_str,)
            ).fetchall()]
        notes = [dict(r) for r in conn.execute(
            "SELECT * FROM notes WHERE date = ? ORDER BY created_at DESC", (date_str,)
        ).fetchall()]
        run_row = None
        expected_chat_count = 0
        retrying_slot = False
        if slot_filter:
            fallback_rows = _fallback_summary_rows(conn, date_str, slot)
            key = (date_str, slot)
            with _retry_slots_lock:
                retrying_slot = key in _retrying_slots
            run_row = conn.execute("""
                SELECT
                    date,
                    slot AS summary_slot,
                    fetch_status,
                    summary_status,
                    ok_count,
                    skip_existing_count,
                    skip_no_msgs_count,
                    failed_count,
                    error,
                    updated_at
                FROM auto_summary_runs
                WHERE date = ? AND slot = ?
            """, (date_str, slot)).fetchone()
            expected_chat_count = conn.execute(
                "SELECT COUNT(*) FROM chat_category_map"
            ).fetchone()[0]

    for s in summaries:
        s.pop("raw_messages", None)
        summary_text = s.get("summary") or ""
        s["is_auto_fallback"] = bool(summary_text.startswith(AUTO_FALLBACK_PREFIX))
        if s["is_auto_fallback"]:
            first_line = summary_text.splitlines()[0] if summary_text else ""
            s["fallback_reason"] = first_line.strip("[]")

    run = None
    if run_row is not None:
        completed_chats = max(len(summaries), run_row["ok_count"] or 0)
        if retrying_slot:
            summary_status = "running"
        else:
            summary_status = _effective_summary_run_status(
                run_row["summary_status"],
                completed_chats,
                expected_chat_count or 0,
            )
        is_effectively_done = summary_status == "done"
        failed_chats = 0 if is_effectively_done else (run_row["failed_count"] or 0)
        skipped_existing_chats = run_row["skip_existing_count"] or 0
        skipped_no_msgs_chats = run_row["skip_no_msgs_count"] or 0
        run = {
            "fetch_status": run_row["fetch_status"] or "",
            "summary_status": summary_status,
            "expected_chats": expected_chat_count or 0,
            "completed_chats": completed_chats,
            "processed_chats": (
                completed_chats
                + failed_chats
                + skipped_existing_chats
                + skipped_no_msgs_chats
            ),
            "failed_chats": failed_chats,
            "skipped_existing_chats": skipped_existing_chats,
            "skipped_no_msgs_chats": skipped_no_msgs_chats,
            "error": "" if is_effectively_done else (run_row["error"] or ""),
            "updated_at": run_row["updated_at"] or "",
            "fallback_chats": len(fallback_rows) if slot_filter else 0,
            "retrying": retrying_slot,
        }

    return jsonify({
        "date": date_str,
        "summary_slot": slot if slot_filter else "",
        "summaries": summaries,
        "events": events,
        "notes": notes,
        "summary_run": run,
    })


@bp.route("/api/memory/auto_summary/retry", methods=["POST"])
def memory_auto_summary_retry():
    data = get_json_body() or {}
    date_str = (data.get("date") or "").strip()
    slot = (data.get("slot") or "").strip()
    retry_fallbacks = bool(data.get("retry_fallbacks") or data.get("retryFallbacks"))
    if not date_str or not slot:
        return jsonify({"error": "date 與 slot 必填"}), 400
    if not ai.ai_available():
        return jsonify({"error": "AI backend 未就緒"}), 503

    with get_db_ctx() as conn:
        row = conn.execute("""
            SELECT since_iso, until_iso, summary_status
            FROM auto_summary_runs
            WHERE date = ? AND slot = ?
        """, (date_str, slot)).fetchone()
        fallback_rows = _fallback_summary_rows(conn, date_str, slot)
    if not row:
        return jsonify({"error": "找不到這個 auto summary slot"}), 404
    if fallback_rows:
        retry_fallbacks = True
    if row["summary_status"] == "done" and not retry_fallbacks:
        return jsonify({"error": "這個 slot 已完成,不需要 retry"}), 409
    if retry_fallbacks and not fallback_rows:
        return jsonify({"error": "這個 slot 沒有 fallback summary 可重試"}), 409
    since_iso = row["since_iso"] or ""
    until_iso = row["until_iso"] or ""
    if not since_iso or not until_iso:
        return jsonify({"error": "這個 slot 缺少時間窗,無法 retry"}), 400

    key = (date_str, slot)
    with _retry_slots_lock:
        if key in _retrying_slots:
            return jsonify({"status": "already_running"}), 202
        _retrying_slots.add(key)

    _upsert_auto_summary_run(
        date_str,
        slot,
        summary_status="running",
        summary_started_at=datetime.now().isoformat(timespec="seconds"),
        failed_count=0,
        error="",
    )
    thread = threading.Thread(
        target=_retry_auto_summary_slot,
        args=(date_str, slot, since_iso, until_iso, retry_fallbacks),
        daemon=True,
        name=f"auto-summary-retry-{date_str}-{slot}",
    )
    thread.start()
    return jsonify({"status": "started", "date": date_str, "slot": slot}), 202


@bp.route("/api/memory/digest", methods=["POST"])
def memory_digest():
    data = get_json_body()
    date_str = data.get("date", date.today().isoformat())
    digest, error = ai.ai_daily_digest(date_str)
    if error:
        return jsonify({"error": error}), 500
    return jsonify({"digest": digest, "date": date_str})


@bp.route("/api/memory/events", methods=["GET"])
def memory_events_list():
    days = request.args.get("days", 30, type=int)
    with get_db_ctx() as conn:
        events = [dict(r) for r in conn.execute(
            "SELECT * FROM events ORDER BY date DESC, importance DESC LIMIT ?", (days * 10,)
        ).fetchall()]
    return jsonify({"events": events})


@bp.route("/api/memory/events", methods=["POST"])
def memory_events_create():
    data = get_json_body()
    title = data.get("title", "").strip()
    if not title:
        return jsonify({"error": "title is required"}), 400
    with get_db_ctx() as conn:
        conn.execute("""
            INSERT INTO events (date, title, description, importance, tags, source_chat)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (data.get("date", date.today().isoformat()),
              title, data.get("description", ""),
              data.get("importance", "normal"),
              data.get("tags", ""), data.get("source_chat", "手動新增")))
        conn.commit()
        event_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return jsonify({"id": event_id, "status": "created"})


@bp.route("/api/memory/events", methods=["DELETE"])
def memory_events_delete():
    event_id = request.args.get("id", type=int)
    if event_id:
        with get_db_ctx() as conn:
            conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
            conn.commit()
    return jsonify({"status": "deleted"})


@bp.route("/api/memory/notes", methods=["GET"])
def memory_notes_list():
    days = request.args.get("days", 30, type=int)
    with get_db_ctx() as conn:
        notes = [dict(r) for r in conn.execute(
            "SELECT * FROM notes ORDER BY date DESC, created_at DESC LIMIT ?", (days * 20,)
        ).fetchall()]
    return jsonify({"notes": notes})


@bp.route("/api/memory/notes", methods=["POST"])
def memory_notes_create():
    data = get_json_body()
    content = data.get("content", "").strip()
    if not content:
        return jsonify({"error": "content is required"}), 400
    with get_db_ctx() as conn:
        conn.execute("""
            INSERT INTO notes (date, content, tags, related_event_id)
            VALUES (?, ?, ?, ?)
        """, (data.get("date", date.today().isoformat()),
              content, data.get("tags", ""),
              data.get("related_event_id")))
        conn.commit()
        note_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return jsonify({"id": note_id, "status": "created"})


@bp.route("/api/memory/notes", methods=["DELETE"])
def memory_notes_delete():
    note_id = request.args.get("id", type=int)
    if note_id:
        with get_db_ctx() as conn:
            conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
            conn.commit()
    return jsonify({"status": "deleted"})


@bp.route("/api/memory/summaries", methods=["DELETE"])
def memory_summaries_delete():
    """Delete one summary (?id=N) or all summaries on a date (?date=YYYY-MM-DD).

    Cascades to auto-extracted events (source_summary_id IS NOT NULL),
    sentiment_scores, and summary embeddings — all of which were derived
    from the deleted summary and would otherwise dangle. Manually-added
    events (no source_summary_id) and notes are NOT touched.
    """
    summary_id = request.args.get("id", type=int)
    date_str = (request.args.get("date") or "").strip()
    slot_arg = request.args.get("slot")
    slot_filter = slot_arg is not None
    slot_arg = (slot_arg or "").strip()
    if not summary_id and not date_str:
        return jsonify({"error": "需要 id 或 date 參數"}), 400

    with get_db_ctx() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            if summary_id:
                conn.execute("DELETE FROM events WHERE source_summary_id = ?", (summary_id,))
                conn.execute("DELETE FROM sentiment_scores WHERE summary_id = ?", (summary_id,))
                conn.execute("DELETE FROM embeddings WHERE source_type = 'summary' AND source_id = ?", (summary_id,))
                conn.execute("DELETE FROM daily_summaries WHERE id = ?", (summary_id,))
                conn.commit()
                return jsonify({"status": "deleted", "id": summary_id})
            if slot_filter:
                ids = [r[0] for r in conn.execute(
                    """
                    SELECT id FROM daily_summaries
                    WHERE date = ? AND COALESCE(summary_slot, '') = ?
                    """,
                    (date_str, slot_arg),
                ).fetchall()]
            else:
                ids = [r[0] for r in conn.execute(
                    "SELECT id FROM daily_summaries WHERE date = ?", (date_str,)
                ).fetchall()]
            if not ids:
                conn.commit()
                return jsonify({
                    "status": "deleted",
                    "date": date_str,
                    "slot": slot_arg if slot_filter else None,
                    "count": 0,
                })
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"DELETE FROM events WHERE source_summary_id IN ({placeholders})", ids,
            )
            conn.execute(
                f"DELETE FROM sentiment_scores WHERE summary_id IN ({placeholders})", ids,
            )
            conn.execute(
                f"DELETE FROM embeddings WHERE source_type = 'summary' "
                f"AND source_id IN ({placeholders})", ids,
            )
            if slot_filter:
                conn.execute(
                    "DELETE FROM daily_summaries WHERE date = ? AND COALESCE(summary_slot, '') = ?",
                    (date_str, slot_arg),
                )
            else:
                conn.execute("DELETE FROM daily_summaries WHERE date = ?", (date_str,))
            conn.commit()
            return jsonify({
                "status": "deleted",
                "date": date_str,
                "slot": slot_arg if slot_filter else None,
                "count": len(ids),
            })
        except Exception:
            conn.rollback()
            raise


@bp.route("/api/memory/export")
def memory_export():
    with get_db_ctx() as conn:
        summaries = [dict(r) for r in conn.execute(
            """
            SELECT date, chat_id, chat_name, hours, message_count, summary,
                   summary_slot, period_start, period_end
            FROM daily_summaries
            ORDER BY date DESC, summary_slot DESC
            """
        ).fetchall()]
        events = [dict(r) for r in conn.execute(
            "SELECT date, title, description, importance, tags, source_chat FROM events ORDER BY date DESC"
        ).fetchall()]
        notes = [dict(r) for r in conn.execute(
            "SELECT date, content, tags, created_at FROM notes ORDER BY date DESC"
        ).fetchall()]
    payload = json.dumps({
        "exported_at": date.today().isoformat(),
        "summaries": summaries,
        "events": events,
        "notes": notes,
    }, ensure_ascii=False, indent=2)
    resp = make_response(payload)
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    resp.headers["Content-Disposition"] = f'attachment; filename="tg_memory_{date.today().isoformat()}.json"'
    return resp


@bp.route("/api/memory/import", methods=["POST"])
def memory_import():
    data = get_json_body()
    normalized, source_format, error = normalize_memory_import_payload(data)
    if error:
        return jsonify({"error": error}), 400

    summaries = normalized.get("summaries", [])
    events = normalized.get("events", [])
    notes = normalized.get("notes", [])
    watchlist = normalized.get("watchlist", [])

    imported = {"summaries": 0, "events": 0, "notes": 0}
    skipped = {"summaries": 0, "events": 0, "notes": 0}
    failed = {"summaries": 0, "events": 0, "notes": 0}
    watchlist_stats = {"imported": 0, "skipped": 0, "failed": 0}

    with get_db_ctx() as conn:
        for s in summaries:
            try:
                cursor = conn.execute("""
                    INSERT OR IGNORE INTO daily_summaries
                    (date, chat_id, chat_name, hours, message_count, summary,
                     summary_slot, period_start, period_end)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (s.get("date"), s.get("chat_id", ""), s.get("chat_name", ""),
                      s.get("hours", 24), s.get("message_count", 0),
                      s.get("summary", ""), s.get("summary_slot", ""),
                      s.get("period_start", ""), s.get("period_end", "")))
                if cursor.rowcount > 0:
                    imported["summaries"] += 1
                else:
                    skipped["summaries"] += 1
            except Exception as e:
                failed["summaries"] += 1
                logger.warning("匯入摘要失敗: %s", e)

        for e in events:
            try:
                if import_event_if_missing(conn, e):
                    imported["events"] += 1
                else:
                    skipped["events"] += 1
            except Exception as err:
                failed["events"] += 1
                logger.warning("匯入事件失敗: %s", err)

        for n in notes:
            try:
                if import_note_if_missing(conn, n):
                    imported["notes"] += 1
                else:
                    skipped["notes"] += 1
            except Exception as err:
                failed["notes"] += 1
                logger.warning("匯入筆記失敗: %s", err)

        for keyword in watchlist:
            try:
                if import_watchlist_if_missing(conn, keyword):
                    watchlist_stats["imported"] += 1
                else:
                    watchlist_stats["skipped"] += 1
            except Exception as err:
                watchlist_stats["failed"] += 1
                logger.warning("匯入追蹤清單失敗: %s", err)

        conn.commit()

    return jsonify({
        "status": "ok",
        "source_format": source_format,
        "imported": imported,
        "skipped": skipped,
        "failed": failed,
        "watchlist": watchlist_stats,
    })


@bp.route("/api/memory/search")
def memory_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"summaries": [], "events": [], "notes": []})

    fts_query = build_fts_query(q)
    if not fts_query:
        return jsonify({"summaries": [], "events": [], "notes": []})

    with get_db_ctx() as conn:
        try:
            summaries = [dict(r) for r in conn.execute("""
                SELECT ds.id, ds.date, ds.chat_name, ds.summary
                       , ds.summary_slot, ds.created_at
                FROM summaries_fts fts
                JOIN daily_summaries ds ON ds.id = fts.rowid
                WHERE summaries_fts MATCH ?
                ORDER BY ds.date DESC, ds.summary_slot DESC LIMIT 20
            """, (fts_query,)).fetchall()]
        except Exception:
            like = f"%{q}%"
            summaries = [dict(r) for r in conn.execute(
                """
                SELECT id, date, chat_name, summary, summary_slot, created_at
                FROM daily_summaries
                WHERE summary LIKE ?
                ORDER BY date DESC, summary_slot DESC LIMIT 20
                """,
                (like,)
            ).fetchall()]

        try:
            events = [dict(r) for r in conn.execute("""
                SELECT e.*
                FROM events_fts fts
                JOIN events e ON e.id = fts.rowid
                WHERE events_fts MATCH ?
                ORDER BY e.date DESC LIMIT 20
            """, (fts_query,)).fetchall()]
        except Exception:
            like = f"%{q}%"
            events = [dict(r) for r in conn.execute(
                "SELECT * FROM events WHERE title LIKE ? OR description LIKE ? OR tags LIKE ? ORDER BY date DESC LIMIT 20",
                (like, like, like)
            ).fetchall()]

        try:
            notes = [dict(r) for r in conn.execute("""
                SELECT n.*
                FROM notes_fts fts
                JOIN notes n ON n.id = fts.rowid
                WHERE notes_fts MATCH ?
                ORDER BY n.date DESC LIMIT 20
            """, (fts_query,)).fetchall()]
        except Exception:
            like = f"%{q}%"
            notes = [dict(r) for r in conn.execute(
                "SELECT * FROM notes WHERE content LIKE ? OR tags LIKE ? ORDER BY date DESC LIMIT 20",
                (like, like)
            ).fetchall()]

    return jsonify({"summaries": summaries, "events": events, "notes": notes})


@bp.route("/api/memory/archive", methods=["POST"])
def memory_archive():
    """Archive fetched messages WITHOUT running AI summarize. Populates `messages` table only."""
    data = get_json_body()
    messages = data.get("messages") or []
    chat_name = (data.get("chat_name") or "").strip() or "未知"
    chat_id = str(data.get("chat_id") or "").strip()
    if not messages:
        return jsonify({"error": "沒有訊息可歸檔"}), 400
    if not chat_id:
        return jsonify({"error": "缺少 chat_id"}), 400

    with get_db_ctx() as conn:
        new_count, total = save_messages_for_summary(
            conn, messages, chat_id, chat_name, summary_id=None
        )
        conn.commit()

    return jsonify({
        "status": "ok",
        "archived": new_count,
        "total": total,
        "chat_id": chat_id,
        "chat_name": chat_name,
    })


@bp.route("/api/memory/ask", methods=["POST"])
def memory_ask():
    data = get_json_body()
    question = data.get("question", "").strip()
    if not question:
        return jsonify({"error": "請輸入問題"}), 400
    answer, error = ai.ai_memory_ask(question)
    if error:
        return jsonify({"error": error}), 500
    return jsonify({"answer": answer, "question": question})


@bp.route("/api/memory/sentiment")
def memory_sentiment():
    days = request.args.get("days", 30, type=int)
    chat_id = request.args.get("chat_id", "")

    with get_db_ctx() as conn:
        if chat_id:
            rows = conn.execute("""
                SELECT date, chat_id, chat_name, score, label
                FROM sentiment_scores
                WHERE chat_id = ?
                ORDER BY date DESC LIMIT ?
            """, (chat_id, days)).fetchall()
        else:
            rows = conn.execute("""
                SELECT date, chat_id, chat_name, score, label
                FROM sentiment_scores
                ORDER BY date DESC LIMIT ?
            """, (days * 10,)).fetchall()

    sentiment = [dict(r) for r in rows]

    daily_avg = {}
    for s in sentiment:
        d = s["date"]
        if d not in daily_avg:
            daily_avg[d] = {"date": d, "scores": [], "labels": []}
        daily_avg[d]["scores"].append(s["score"])
        daily_avg[d]["labels"].append(s["label"])

    trend = []
    for d in sorted(daily_avg.keys()):
        scores = daily_avg[d]["scores"]
        avg = sum(scores) / len(scores)
        trend.append({
            "date": d,
            "avg_score": round(avg, 1),
            "min_score": min(scores),
            "max_score": max(scores),
            "count": len(scores),
            "labels": daily_avg[d]["labels"],
        })

    return jsonify({"sentiment": sentiment, "trend": trend})


@bp.route("/api/memory/diff", methods=["POST"])
def memory_diff():
    data = get_json_body()
    old_id = data.get("old_id")
    new_id = data.get("new_id")
    chat_id = data.get("chat_id", "")
    old_date = data.get("old_date", "")
    new_date = data.get("new_date", "")

    with get_db_ctx() as conn:
        if old_id and new_id:
            old_row = conn.execute(
                "SELECT chat_name, summary, date FROM daily_summaries WHERE id = ?", (old_id,)
            ).fetchone()
            new_row = conn.execute(
                "SELECT chat_name, summary, date FROM daily_summaries WHERE id = ?", (new_id,)
            ).fetchone()
        elif chat_id and old_date and new_date:
            old_row = conn.execute(
                """
                SELECT chat_name, summary, date
                FROM daily_summaries
                WHERE chat_id = ? AND date = ?
                ORDER BY summary_slot DESC
                LIMIT 1
                """,
                (chat_id, old_date)
            ).fetchone()
            new_row = conn.execute(
                """
                SELECT chat_name, summary, date
                FROM daily_summaries
                WHERE chat_id = ? AND date = ?
                ORDER BY summary_slot DESC
                LIMIT 1
                """,
                (chat_id, new_date)
            ).fetchone()
        else:
            return jsonify({"error": "需要 (old_id + new_id) 或 (chat_id + old_date + new_date)"}), 400

    if not old_row or not new_row:
        return jsonify({"error": "找不到指定的摘要"}), 404

    chat_name = new_row["chat_name"] or old_row["chat_name"] or "未知"
    diff_text, error = ai.ai_diff_summaries(old_row["summary"], new_row["summary"], chat_name)
    if error:
        return jsonify({"error": error}), 500

    return jsonify({
        "diff": diff_text,
        "old_date": old_row["date"],
        "new_date": new_row["date"],
        "chat_name": chat_name,
    })
