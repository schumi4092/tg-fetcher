"""
TG Fetcher Pro — Telegram 訊息擷取 + AI 總結 + 本地記憶

Flask + Telethon + Claude API + SQLite。模組拆分:
- config.py            設定、路徑、模型名稱、logger
- db.py                SQLite schema、連線、FTS 與匯入 helper
- embeddings.py        VoyageAI + 向量搜尋
- ai.py                Claude 總結／情緒／事件／記憶 Q&A
- telegram_service.py  Telethon 背景執行緒與 helper
- twitter_client.py    6551 Twitter REST API client
- wallet_aggregator.py 錢包事件聚合器
- routes/              Flask blueprints (per-feature route modules)
- server.py (本檔)     app 啟動、單例鎖、background loops

安裝:
    pip install flask telethon flask-cors anthropic numpy
    (可選) pip install voyageai
"""

import atexit
import os
import secrets
import threading
import time as _time
from datetime import date, datetime, timedelta, timezone

try:
    from flask import Flask, jsonify, request
    from flask_cors import CORS
except ImportError as e:
    print("=" * 60)
    print(f"  缺少依賴：{e}")
    print("  請執行：pip install flask telethon flask-cors anthropic")
    print("=" * 60)
    raise SystemExit(1)

from config import (
    AUTO_FETCH_HOURS,
    AUTO_FETCH_INTERVAL_HOURS,
    AUTO_SUMMARIZE_HOURS,
    AUTO_SUMMARIZE_INITIAL_DELAY_SECS,
    AUTO_SUMMARIZE_CATCHUP_MAX_HOURS,
    AUTO_SUMMARIZE_INTERVAL_HOURS,
    AUTO_SUMMARIZE_RUN_ON_START,
    AUTO_SUMMARIZE_SLOT_BUDGET_SECS,
    AUTO_SUMMARIZE_SLOT_FALLBACK_MIN_REMAINING_SECS,
    AUTO_SUMMARIZE_TIMES,
    API_ACCESS_TOKEN,
    CLAUDE_API_KEY,
    HOST,
    PORT,
    STATIC_DIR,
    logger,
)
from db import TAIPEI_TZ, get_db_ctx, init_db, save_messages_for_summary
import ai
import ai_backend
import embeddings
import telegram_service as tgs
from telegram_service import is_logged_in, run_async

from routes import register_blueprints
from routes.telegram import fetch_messages_for_entity


app = Flask(__name__, static_folder=str(STATIC_DIR))
_ALLOWED_ORIGINS = [
    f"http://{HOST}:{PORT}",
    f"http://127.0.0.1:{PORT}",
    f"http://localhost:{PORT}",
]
CORS(
    app,
    resources={
        r"/api/*": {
            "origins": _ALLOWED_ORIGINS,
            "allow_headers": ["Content-Type", "X-TG-Fetcher-Token"],
            "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        }
    },
)
register_blueprints(app)

_API_TOKEN_COOKIE = "tg_fetcher_api_token"
_API_TOKEN_HEADER = "X-TG-Fetcher-Token"


@app.before_request
def _require_local_api_token():
    """Block cross-site/localhost API reads and mutations.

    The first-party UI receives a SameSite cookie from `/`, reads it, and echoes
    the same value in a custom header. Third-party pages cannot read the cookie
    or pass the CORS preflight for that header.
    """
    if app.config.get("TESTING") or not request.path.startswith("/api/"):
        return None
    if request.method == "OPTIONS":
        return None
    header_token = request.headers.get(_API_TOKEN_HEADER, "")
    cookie_token = request.cookies.get(_API_TOKEN_COOKIE, "")
    if (
        secrets.compare_digest(header_token, API_ACCESS_TOKEN)
        and secrets.compare_digest(cookie_token, API_ACCESS_TOKEN)
    ):
        return None
    return jsonify({"error": "forbidden"}), 403


@app.after_request
def _set_local_api_token_cookie(response):
    if request.path == "/":
        response.set_cookie(
            _API_TOKEN_COOKIE,
            API_ACCESS_TOKEN,
            httponly=False,
            samesite="Strict",
        )
    return response


# ============================================================
# Single-instance lock — prevents two server.py processes from sharing
# the same Telethon session file (which corrupts auth state).
# ============================================================

_SERVER_LOCK_HANDLE = None


def _acquire_single_instance_lock():
    """Prevent two local server.py processes from sharing one Telethon session."""
    global _SERVER_LOCK_HANDLE
    lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tg_fetcher.server.lock")
    fh = open(lock_path, "a+", encoding="utf-8")
    fh.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False

    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    _SERVER_LOCK_HANDLE = fh
    return True


def _release_single_instance_lock():
    global _SERVER_LOCK_HANDLE
    fh = _SERVER_LOCK_HANDLE
    if fh is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        fh.close()
    finally:
        _SERVER_LOCK_HANDLE = None


# ============================================================
# Background loop — auto-fetch from categorized chats every N hours
# ------------------------------------------------------------
# Whitelist = chats with a chat_category_map entry. Categorizing a chat
# is the explicit signal that the user cares about its history; everything
# else stays uncategorized and is opted-out by default.
#
# Only archives raw messages (save_messages_for_summary with summary_id=None)
# — does NOT auto-summarize. Summary stays on-demand: it eats tokens, and
# auto-fetched archive is enough for backfill / FTS / coin-profile cross-ref
# without burning AI calls on chats the user may not read.
# ============================================================

_AUTO_FETCH_STOP = threading.Event()
_AUTO_FETCH_RUNNING = threading.Event()
_AUTO_FETCH_CYCLE_DONE = threading.Event()
_AUTO_FETCH_STATE_KEY = "auto_fetch.last_completed_at"


def _coerce_chat_target(chat_id):
    try:
        return int(chat_id)
    except (TypeError, ValueError):
        return chat_id


def _parse_state_datetime(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except Exception:
        return None
    if dt.tzinfo is None:
        return dt.astimezone()
    return dt


def _get_auto_fetch_last_completed():
    """Return the explicit auto-fetch checkpoint, if one has been written.

    No checkpoint means "bootstrap": run one normal AUTO_FETCH_HOURS cycle and
    then start the regular cadence. Do not infer a catch-up window from older
    archived rows; the first automatic fetch should only establish the recent
    12h baseline.
    """
    with get_db_ctx() as conn:
        row = conn.execute(
            "SELECT value FROM app_state WHERE key = ?",
            (_AUTO_FETCH_STATE_KEY,),
        ).fetchone()
        if row:
            return _parse_state_datetime(row["value"])
        return None


def _set_auto_fetch_last_completed(ts=None):
    ts = ts or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.astimezone()
    value = ts.astimezone(timezone.utc).isoformat()
    with get_db_ctx() as conn:
        conn.execute("""
            INSERT INTO app_state (key, value, updated_at)
            VALUES (?, ?, datetime('now', 'localtime'))
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
        """, (_AUTO_FETCH_STATE_KEY, value))
        conn.commit()


def _seconds_until_next_auto_fetch():
    if AUTO_FETCH_INTERVAL_HOURS <= 0:
        return None
    interval = AUTO_FETCH_INTERVAL_HOURS * 3600
    last = _get_auto_fetch_last_completed()
    if last is None:
        # Fresh DB: start after the normal boot grace, then persist from there.
        return 60
    now = datetime.now(last.tzinfo or timezone.utc)
    elapsed = (now - last).total_seconds()
    return max(0, int(interval - elapsed))


def _get_auto_fetch_chats():
    """Chats to auto-fetch — those with a category assignment.

    If auto_topic_filters has enabled rows for a chat, background fetch and
    summarize are constrained to those forum topics only.
    """
    with get_db_ctx() as conn:
        rows = conn.execute("""
            SELECT m.chat_id, c.prompt_profile,
                   GROUP_CONCAT(f.topic_id) AS topic_ids,
                   GROUP_CONCAT(COALESCE(f.topic_title, '')) AS topic_titles
            FROM chat_category_map m
            JOIN chat_categories c ON c.id = m.category_id
            LEFT JOIN auto_topic_filters f
              ON f.chat_id = m.chat_id AND f.enabled = 1
            GROUP BY m.chat_id, c.prompt_profile, c.sort_order
            ORDER BY c.sort_order, m.chat_id
        """).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["topic_ids"] = [
            int(x) for x in (item.get("topic_ids") or "").split(",")
            if x.strip().isdigit()
        ]
        item["topic_titles"] = [
            x for x in (item.get("topic_titles") or "").split(",") if x
        ]
        out.append(item)
    return out


def _auto_chat_display_name(chat_name, chat_cfg):
    titles = chat_cfg.get("topic_titles") or []
    if not titles:
        return chat_name
    return f"{chat_name} · {', '.join(titles[:3])}"


def _auto_summarize_chat_priority(chat_cfg):
    """Lower value runs earlier when the slot has a wall-clock budget."""
    profile = chat_cfg.get("prompt_profile") or ""
    profile_rank = {
        "wallet_log": 0,
        "wallet_log_priority": 1,
        "broadcast": 2,
        "group_chat": 3,
    }.get(profile, 4)
    return profile_rank, str(chat_cfg.get("chat_id") or "")


def _auto_summarize_order(chats):
    return sorted(chats, key=_auto_summarize_chat_priority)


def _auto_slot_force_fallback_reason(started_mono):
    """Return a deterministic-fallback reason when the cycle budget is spent."""
    if AUTO_SUMMARIZE_SLOT_BUDGET_SECS <= 0:
        return None
    elapsed = _time.monotonic() - started_mono
    remaining = AUTO_SUMMARIZE_SLOT_BUDGET_SECS - elapsed
    if remaining < AUTO_SUMMARIZE_SLOT_FALLBACK_MIN_REMAINING_SECS:
        return (
            f"slot budget nearly exhausted "
            f"(elapsed={elapsed:.0f}s, remaining={max(0, remaining):.0f}s)"
        )
    return None


def _record_auto_summary_chat_run(summary_date, summary_slot, chat_id,
                                  chat_name, status, summary_id, started_mono,
                                  started_wall,
                                  metrics=None):
    metrics = metrics or {}
    elapsed = _time.monotonic() - started_mono
    try:
        with get_db_ctx() as conn:
            conn.execute("""
                INSERT INTO auto_summary_chat_runs
                (run_date, slot, chat_id, chat_name, profile, status, summary_id,
                 elapsed_secs, prompt_len, msg_text_len, message_count,
                 fallback_used, prep_mode, stream_error, started_at, finished_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                summary_date or "",
                summary_slot or "",
                str(chat_id),
                chat_name or "",
                metrics.get("profile") or "",
                status or "",
                summary_id,
                elapsed,
                int(metrics.get("prompt_len") or 0),
                int(metrics.get("msg_text_len") or 0),
                int(metrics.get("message_count") or 0),
                1 if metrics.get("fallback_used") else 0,
                metrics.get("prep_mode") or "",
                str(metrics.get("stream_error") or "")[:1000],
                started_wall.isoformat(timespec="seconds"),
                datetime.now().isoformat(timespec="seconds"),
            ))
            conn.commit()
    except Exception:
        logger.exception(
            "auto-summarize: failed to record chat metrics for %s [%s]",
            chat_name, chat_id,
        )


def _run_auto_fetch_cycle(hours=None, cap_to_config=True, reason="scheduled"):
    """One cycle: fetch + archive last `hours` of messages for each whitelisted chat."""
    if hours is None:
        hours = AUTO_FETCH_HOURS
    else:
        try:
            hours = float(hours)
            if cap_to_config:
                hours = min(hours, float(AUTO_FETCH_HOURS))
        except (TypeError, ValueError):
            hours = AUTO_FETCH_HOURS
    hours = max(1, hours)

    _AUTO_FETCH_CYCLE_DONE.clear()
    _AUTO_FETCH_RUNNING.set()
    stats = {
        "status": "running",
        "new": 0,
        "failed": 0,
        "chats": 0,
        "hours": hours,
        "reason": reason,
    }
    try:
        if not tgs.telethon_ready() or not tgs.is_logged_in():
            logger.info("auto-fetch: skipped — Telegram not ready/logged-in")
            stats["status"] = "skipped"
            return stats

        chats = _get_auto_fetch_chats()
        if not chats:
            logger.info("auto-fetch: no categorized chats to fetch")
            stats["status"] = "empty"
            return stats

        started = datetime.now()
        stats["chats"] = len(chats)
        logger.info("auto-fetch: starting cycle for %d chat(s), %.1fh window (%s)",
                    len(chats), hours, reason)
        total_archived = 0
        failed = 0

        for c in chats:
            chat_id = c["chat_id"]
            topic_ids = set(c.get("topic_ids") or [])
            try:
                async def _do():
                    entity = await tgs.tg_client.get_entity(_coerce_chat_target(chat_id))
                    return await fetch_messages_for_entity(entity, hours, topic_ids or None)
                result = run_async(_do(), timeout=180)
                messages = (result or {}).get("messages") or []
                if not messages:
                    continue
                with get_db_ctx() as conn:
                    new_count, _total = save_messages_for_summary(
                        conn, messages, chat_id,
                        _auto_chat_display_name(result.get("chat_name") or chat_id, c),
                        summary_id=None,
                    )
                    conn.commit()
                total_archived += new_count
                if new_count:
                    logger.info("auto-fetch: %s [%s] +%d new",
                                result.get("chat_name") or chat_id, chat_id, new_count)
            except Exception as e:
                failed += 1
                logger.warning("auto-fetch failed for %s: %s", chat_id, e)

        elapsed = (datetime.now() - started).total_seconds()
        logger.info("auto-fetch: cycle done — %d new across %d chats (%d failed) in %.1fs",
                    total_archived, len(chats), failed, elapsed)
        _set_auto_fetch_last_completed()
        stats.update({
            "status": "partial" if failed else "done",
            "new": total_archived,
            "failed": failed,
        })
        return stats
    finally:
        _AUTO_FETCH_RUNNING.clear()
        _AUTO_FETCH_CYCLE_DONE.set()


def _auto_fetch_loop():
    """Run a fetch cycle every AUTO_FETCH_INTERVAL_HOURS, retrying on errors."""
    while not _AUTO_FETCH_STOP.is_set():
        wait_secs = _seconds_until_next_auto_fetch()
        if wait_secs is None:
            return
        logger.info("auto-fetch: next cycle in %ds", wait_secs)
        if _AUTO_FETCH_STOP.wait(wait_secs):
            return
        try:
            _run_auto_fetch_cycle(hours=AUTO_FETCH_HOURS)
        except Exception:
            logger.exception("auto-fetch loop iteration crashed")
        # If the cycle was due but skipped before recording completion
        # (for example Telegram is still logged out), avoid a tight retry loop.
        next_wait = _seconds_until_next_auto_fetch()
        if next_wait is not None and next_wait <= 0:
            if _AUTO_FETCH_STOP.wait(60):
                return


def _start_auto_fetch():
    if AUTO_FETCH_INTERVAL_HOURS <= 0:
        logger.info("auto-fetch: disabled (AUTO_FETCH_INTERVAL_HOURS=0)")
        return
    t = threading.Thread(target=_auto_fetch_loop, daemon=True, name="auto-fetch")
    t.start()
    logger.info("auto-fetch: started, interval=%dh, window=%dh, restart-safe schedule enabled",
                AUTO_FETCH_INTERVAL_HOURS, AUTO_FETCH_HOURS)


atexit.register(_AUTO_FETCH_STOP.set)


# ============================================================
# Background loop — auto-summarize whitelisted chats once per day
# ------------------------------------------------------------
# Per-chat/per-slot dedupe via daily_summaries UNIQUE(date, chat_id,
# summary_slot), so 10:00 and 22:00 are stored separately.
# wallet_log is included through its deterministic aggregator path.
# The summary itself is tagged source='auto' so the UI can badge it
# distinctly from manually-triggered summaries.
# ============================================================

_AUTO_SUMMARIZE_STOP = threading.Event()


def _normalize_chunk(chunk):
    since_iso, until_iso, summary_date = chunk[:3]
    summary_slot = chunk[3] if len(chunk) > 3 else ""
    if not summary_slot and until_iso:
        try:
            slot_dt = datetime.fromisoformat(until_iso).astimezone(TAIPEI_TZ)
            slot_date, slot = _slot_info_for_datetime(slot_dt)
            summary_slot = _slot_label(slot) if slot else slot_dt.strftime("%H:%M")
            if slot_date:
                summary_date = slot_date.isoformat()
        except Exception:
            pass
    return since_iso, until_iso, summary_date, summary_slot


def _upsert_auto_summary_run(summary_date, summary_slot, **fields):
    if not summary_date or not summary_slot:
        return
    allowed = {
        "since_iso", "until_iso", "fetch_status", "summary_status",
        "started_at", "fetch_finished_at", "summary_started_at",
        "summary_finished_at", "ok_count", "skip_existing_count",
        "skip_no_msgs_count", "failed_count", "error",
    }
    payload = {k: v for k, v in fields.items() if k in allowed}
    if not payload:
        payload = {}
    columns = ["date", "slot"] + list(payload.keys()) + ["updated_at"]
    values = [summary_date, summary_slot] + list(payload.values()) + [datetime.now().isoformat(timespec="seconds")]
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


def _prefetch_for_summary_chunks(chunks):
    if not chunks:
        return {"status": "not_needed", "failed": 0, "new": 0}
    _wait_for_initial_auto_fetch(max_wait=max(60, AUTO_SUMMARIZE_INITIAL_DELAY_SECS))
    normalized = [_normalize_chunk(c) for c in chunks]
    try:
        earliest = min(datetime.fromisoformat(c[0]) for c in normalized)
        latest = max(datetime.fromisoformat(c[1]) for c in normalized)
        needed_hours = ((latest - earliest).total_seconds() / 3600) + 1
    except Exception:
        needed_hours = AUTO_FETCH_HOURS
    needed_hours = min(max(needed_hours, AUTO_FETCH_HOURS), AUTO_SUMMARIZE_CATCHUP_MAX_HOURS)
    logger.info(
        "auto-summarize: prefetching %.1fh before slot summary catch-up",
        needed_hours,
    )
    stats = _run_auto_fetch_cycle(
        hours=needed_hours,
        cap_to_config=False,
        reason="summary-preflight",
    ) or {"status": "unknown", "failed": 0, "new": 0}
    fetch_status = stats.get("status") or "unknown"
    if stats.get("failed", 0):
        fetch_status = "partial"
    for since_iso, until_iso, summary_date, summary_slot in normalized:
        _upsert_auto_summary_run(
            summary_date, summary_slot,
            since_iso=since_iso,
            until_iso=until_iso,
            fetch_status=fetch_status,
            fetch_finished_at=datetime.now().isoformat(timespec="seconds"),
        )
    return stats


def _wait_for_initial_auto_fetch(max_wait=None):
    """Let the first auto-summary consume freshly archived messages."""
    if AUTO_FETCH_INTERVAL_HOURS <= 0 or _AUTO_FETCH_CYCLE_DONE.is_set():
        return
    if not _AUTO_FETCH_RUNNING.is_set():
        return
    if max_wait is None:
        max_wait = max(60, AUTO_SUMMARIZE_INITIAL_DELAY_SECS)
    deadline = _time.monotonic() + max_wait
    logger.info("auto-summarize: waiting up to %ds for first auto-fetch cycle", max_wait)
    while not _AUTO_FETCH_CYCLE_DONE.is_set():
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            logger.warning("auto-summarize: first auto-fetch wait timed out; using current DB state")
            return
        _AUTO_FETCH_CYCLE_DONE.wait(min(remaining, 5.0))


def _run_auto_summarize_cycle(chunks=None):
    """One cycle: summarize each whitelisted chat.

    `chunks`: optional list of `(since_iso_utc, until_iso_utc, summary_date,
    summary_slot)` tuples for slot-aligned catch-up. When set, runs each chat across all
    chunks in chronological order — chunks landing on the same date append
    via `summarize_chat_auto`'s existing cumulative-append path; chunks on
    different dates produce different daily_summaries rows.

    When `chunks` is None, runs the legacy rolling AUTO_SUMMARIZE_HOURS
    window. Slot mode passes slot chunks and filters already-linked messages
    so 10:00 and 22:00 stay separate without duplicating the buffer.
    """
    if not ai_backend.ai_available():
        logger.info("auto-summarize: skipped — AI backend unavailable")
        return

    _wait_for_initial_auto_fetch()
    if chunks:
        _prefetch_for_summary_chunks(chunks)

    chats = _get_auto_fetch_chats()
    if not chats:
        return

    name_hints = {}
    with get_db_ctx() as conn:
        for c in chats:
            row = conn.execute(
                "SELECT chat_name FROM messages WHERE chat_id = ? "
                "ORDER BY date DESC LIMIT 1",
                (c["chat_id"],),
            ).fetchone()
            base_name = (row["chat_name"] if row else None) or c["chat_id"]
            name_hints[c["chat_id"]] = _auto_chat_display_name(base_name, c)

    started = datetime.now()
    started_mono = _time.monotonic()
    summarize_chats = _auto_summarize_order(chats)
    counts = {"ok": 0, "skipped_existing": 0,
              "skipped_no_messages": 0, "skipped_ai_unavailable": 0,
              "failed": 0}

    if chunks:
        normalized_chunks = [_normalize_chunk(c) for c in chunks]
        chunk_counts = {
            i: {
                "ok": 0,
                "skipped_existing": 0,
                "skipped_no_messages": 0,
                "failed": 0,
                "error": "",
                "meta": chunk,
            }
            for i, chunk in enumerate(normalized_chunks, 1)
        }
        for since_iso, until_iso, summary_date, summary_slot in normalized_chunks:
            _upsert_auto_summary_run(
                summary_date, summary_slot,
                since_iso=since_iso,
                until_iso=until_iso,
                summary_status="running",
                summary_started_at=datetime.now().isoformat(timespec="seconds"),
            )
        logger.info(
            "auto-summarize: starting slot-aligned cycle for %d chat(s) across %d "
            "slot-aligned chunk(s), budget=%ds",
            len(summarize_chats), len(chunks), AUTO_SUMMARIZE_SLOT_BUDGET_SECS,
        )
        for c in summarize_chats:
            chat_id = c["chat_id"]
            chat_name = name_hints.get(chat_id) or chat_id
            for i, chunk in enumerate(normalized_chunks, 1):
                chat_started_mono = _time.monotonic()
                chat_started_wall = datetime.now()
                since_iso, until_iso, summary_date, summary_slot = chunk
                try:
                    window_hours = (
                        datetime.fromisoformat(until_iso)
                        - datetime.fromisoformat(since_iso)
                    ).total_seconds() / 3600
                except Exception:
                    window_hours = AUTO_SUMMARIZE_HOURS
                try:
                    force_fallback_reason = _auto_slot_force_fallback_reason(started_mono)
                    if force_fallback_reason:
                        logger.warning(
                            "auto-summarize: %s [%s] chunk %d/%d using deterministic fallback: %s",
                            chat_name, chat_id, i, len(chunks), force_fallback_reason,
                        )
                    summary_id, status, metrics = ai.summarize_chat_auto(
                        chat_id, chat_name,
                        hours=window_hours,
                        since_iso=since_iso,
                        until_iso=until_iso,
                        summary_date=summary_date,
                        summary_slot=summary_slot,
                        topic_ids=c.get("topic_ids") or None,
                        force_fallback_reason=force_fallback_reason,
                        collect_metrics=True,
                    )
                    _record_auto_summary_chat_run(
                        summary_date, summary_slot, chat_id, chat_name,
                        status, summary_id, chat_started_mono,
                        chat_started_wall, metrics,
                    )
                    counts[status] = counts.get(status, 0) + 1
                    if i in chunk_counts:
                        chunk_counts[i][status] = chunk_counts[i].get(status, 0) + 1
                    if status == "ok":
                        logger.info(
                            "auto-summarize: %s [%s] chunk %d/%d (date=%s slot=%s) ok (id=%s)",
                            chat_name, chat_id, i, len(chunks),
                            summary_date, summary_slot or "", summary_id,
                        )
                except Exception as e:
                    try:
                        _record_auto_summary_chat_run(
                            summary_date, summary_slot, chat_id, chat_name,
                            "failed", None, chat_started_mono,
                            chat_started_wall,
                            {"stream_error": str(e), "profile": c.get("prompt_profile") or ""},
                        )
                    except Exception:
                        pass
                    counts["failed"] += 1
                    if i in chunk_counts:
                        chunk_counts[i]["failed"] += 1
                        chunk_counts[i]["error"] = str(e)
                    logger.warning(
                        "auto-summarize failed for %s [%s] chunk %d/%d "
                        "(date=%s): %s",
                        chat_name, chat_id, i, len(chunks), summary_date, e,
                    )
        for i, c in chunk_counts.items():
            since_iso, until_iso, summary_date, summary_slot = c["meta"]
            failed = c.get("failed", 0)
            ok = c.get("ok", 0)
            skip_existing = c.get("skipped_existing", 0)
            skip_no_msgs = c.get("skipped_no_messages", 0)
            summary_status = "failed" if failed and not ok else "partial" if failed else "done"
            _upsert_auto_summary_run(
                summary_date, summary_slot,
                summary_status=summary_status,
                summary_finished_at=datetime.now().isoformat(timespec="seconds"),
                ok_count=ok,
                skip_existing_count=skip_existing,
                skip_no_msgs_count=skip_no_msgs,
                failed_count=failed,
                error=c.get("error", ""),
            )
    else:
        cycle_hours = AUTO_SUMMARIZE_HOURS
        logger.info("auto-summarize: starting cycle for %d chat(s), %dh window, budget=%ds",
                    len(summarize_chats), cycle_hours, AUTO_SUMMARIZE_SLOT_BUDGET_SECS)
        for c in summarize_chats:
            chat_id = c["chat_id"]
            chat_name = name_hints.get(chat_id) or chat_id
            chat_started_mono = _time.monotonic()
            chat_started_wall = datetime.now()
            try:
                force_fallback_reason = _auto_slot_force_fallback_reason(started_mono)
                if force_fallback_reason:
                    logger.warning(
                        "auto-summarize: %s [%s] using deterministic fallback: %s",
                        chat_name, chat_id, force_fallback_reason,
                    )
                summary_id, status, metrics = ai.summarize_chat_auto(
                    chat_id, chat_name, hours=cycle_hours,
                    topic_ids=c.get("topic_ids") or None,
                    force_fallback_reason=force_fallback_reason,
                    collect_metrics=True,
                )
                _record_auto_summary_chat_run(
                    date.today().isoformat(), "", chat_id, chat_name,
                    status, summary_id, chat_started_mono,
                    chat_started_wall, metrics,
                )
                counts[status] = counts.get(status, 0) + 1
                if status == "ok":
                    logger.info("auto-summarize: %s [%s] ok (id=%s)",
                                chat_name, chat_id, summary_id)
            except Exception as e:
                try:
                    _record_auto_summary_chat_run(
                        date.today().isoformat(), "", chat_id, chat_name,
                        "failed", None, chat_started_mono,
                        chat_started_wall,
                        {"stream_error": str(e), "profile": c.get("prompt_profile") or ""},
                    )
                except Exception:
                    pass
                counts["failed"] += 1
                logger.warning("auto-summarize failed for %s [%s]: %s",
                               chat_name, chat_id, e)

    elapsed = (datetime.now() - started).total_seconds()
    logger.info(
        "auto-summarize: cycle done — ok=%d, skip(existing)=%d, "
        "skip(no-msgs)=%d, failed=%d, %.1fs",
        counts["ok"], counts["skipped_existing"],
        counts["skipped_no_messages"], counts["failed"], elapsed,
    )


def _seconds_until_next_auto_summarize():
    """Return how many seconds to wait before the next auto-summarize cycle.

    Reads the most recent source='auto' row in daily_summaries and computes
    INTERVAL - elapsed. So restarting the server doesn't re-trigger a cycle
    that already ran today; the loop only fires once the configured interval
    has actually elapsed since the last run.
    """
    interval_secs = AUTO_SUMMARIZE_INTERVAL_HOURS * 3600
    try:
        with get_db_ctx() as conn:
            since = (datetime.now(timezone.utc) - timedelta(hours=AUTO_SUMMARIZE_HOURS)).isoformat()
            has_unlinked = conn.execute(
                """
                SELECT 1
                FROM messages m
                JOIN chat_category_map map ON map.chat_id = m.chat_id
                WHERE m.summary_id IS NULL
                  AND m.date >= ?
                LIMIT 1
                """,
                (since,),
            ).fetchone()
            if has_unlinked:
                logger.info("auto-summarize: recent unlinked messages found; scheduling catch-up")
                return AUTO_SUMMARIZE_INITIAL_DELAY_SECS

            row = conn.execute(
                "SELECT MAX(created_at) AS last FROM daily_summaries WHERE source = 'auto'"
            ).fetchone()
        last = row["last"] if row else None
        if not last:
            return AUTO_SUMMARIZE_INITIAL_DELAY_SECS
        last_dt = datetime.fromisoformat(last)
        elapsed = (datetime.now() - last_dt).total_seconds()
        remaining = interval_secs - elapsed
        return max(int(remaining), AUTO_SUMMARIZE_INITIAL_DELAY_SECS) if remaining > 0 else AUTO_SUMMARIZE_INITIAL_DELAY_SECS
    except Exception:
        logger.exception("auto-summarize: failed to read last run timestamp")
        return AUTO_SUMMARIZE_INITIAL_DELAY_SECS


def _slot_label(slot):
    h, m = slot
    return f"{h:02d}:{m:02d}"


def _slot_datetime(day, slot):
    """Return the real Asia/Taipei datetime for a logical slot date.

    A configured 24:00 slot belongs to `day` but fires at 00:00 the next day.
    """
    h, m = slot
    base = datetime(day.year, day.month, day.day, tzinfo=TAIPEI_TZ)
    if h == 24 and m == 0:
        return base + timedelta(days=1)
    return datetime(day.year, day.month, day.day, h, m, tzinfo=TAIPEI_TZ)


def _slot_info_for_datetime(slot_dt):
    """Map a real slot datetime back to (logical_date, configured_slot)."""
    if not AUTO_SUMMARIZE_TIMES or slot_dt is None:
        return None, None
    real_dt = slot_dt.astimezone(TAIPEI_TZ) if slot_dt.tzinfo else slot_dt.replace(tzinfo=TAIPEI_TZ)
    for day in (real_dt.date() - timedelta(days=1), real_dt.date()):
        for slot in AUTO_SUMMARIZE_TIMES:
            if _slot_datetime(day, slot) == real_dt:
                return day, slot
    return real_dt.date(), (real_dt.hour, real_dt.minute)


def _next_auto_summarize_slot(now=None):
    """Return next slot datetime (Asia/Taipei, tz-aware) strictly after `now`.
    Loops to first slot of next day if all today's slots have passed."""
    if not AUTO_SUMMARIZE_TIMES:
        return None
    now = now or datetime.now(TAIPEI_TZ)
    today = now.date()
    for slot in AUTO_SUMMARIZE_TIMES:
        cand = _slot_datetime(today, slot)
        if cand > now:
            return cand
    tomorrow = today + timedelta(days=1)
    return _slot_datetime(tomorrow, AUTO_SUMMARIZE_TIMES[0])


def _last_passed_slot(now=None):
    """Return the most recent slot datetime ≤ `now` (Asia/Taipei, tz-aware).
    Falls back to last slot of yesterday if all today's slots are still ahead."""
    if not AUTO_SUMMARIZE_TIMES:
        return None
    now = now or datetime.now(TAIPEI_TZ)
    today = now.date()
    candidates = [_slot_datetime(today, slot) for slot in AUTO_SUMMARIZE_TIMES]
    past = [c for c in candidates if c <= now]
    if past:
        return max(past)
    yesterday = today - timedelta(days=1)
    return _slot_datetime(yesterday, AUTO_SUMMARIZE_TIMES[-1])


def _chunk_for_slot(slot_dt):
    """Return the buffered message window ending at a configured slot."""
    if not AUTO_SUMMARIZE_TIMES or slot_dt is None:
        return None
    times = list(AUTO_SUMMARIZE_TIMES)
    slot_date, slot = _slot_info_for_datetime(slot_dt)
    try:
        idx = times.index(slot)
    except ValueError:
        idx = 0
    if idx > 0:
        prev_date = slot_date
        prev_slot = times[idx - 1]
    else:
        prev_date = slot_date - timedelta(days=1)
        prev_slot = times[-1]
    prev_slot_dt = _slot_datetime(prev_date, prev_slot)
    buffer_start = slot_dt - timedelta(hours=AUTO_SUMMARIZE_HOURS)
    window_start = min(prev_slot_dt, buffer_start)
    return (
        window_start.astimezone(timezone.utc).isoformat(),
        slot_dt.astimezone(timezone.utc).isoformat(),
        slot_date.isoformat(),
        _slot_label(slot),
    )


def _parse_summary_checkpoint(row):
    if not row:
        return None
    period_end = row["last_period_end"] if "last_period_end" in row.keys() else None
    if period_end:
        try:
            dt = datetime.fromisoformat(period_end)
            return dt.astimezone(TAIPEI_TZ) if dt.tzinfo else dt.replace(tzinfo=TAIPEI_TZ)
        except Exception:
            pass
    last_run = row["last"] if "last" in row.keys() else None
    if last_run:
        try:
            # created_at is written with datetime('now','localtime') on this
            # machine, so tag the naive value as UTC+8.
            return datetime.fromisoformat(last_run).replace(tzinfo=TAIPEI_TZ)
        except Exception:
            pass
    return None


def _slot_mode_coverage(conn, last_slot):
    """Return (covered, checkpoint_dt) for slot-mode startup recovery.

    A slot is covered only after its `auto_summary_runs` row reaches `done`.
    Merely having one `daily_summaries` row is not enough: if the process dies
    mid-cycle, several chats may still be pending even though one chat already
    wrote a summary.
    """
    slot_date, slot = _slot_info_for_datetime(last_slot)
    slot_label = _slot_label(slot) if slot else ""
    run = conn.execute("""
        SELECT summary_status
        FROM auto_summary_runs
        WHERE date = ? AND slot = ?
    """, (slot_date.isoformat(), slot_label)).fetchone()
    covered = bool(run and run["summary_status"] == "done")

    checkpoint = conn.execute("""
        SELECT MAX(NULLIF(until_iso, '')) AS last_period_end
        FROM auto_summary_runs
        WHERE summary_status = 'done'
    """).fetchone()
    return covered, _parse_summary_checkpoint(checkpoint)


def _enumerate_catchup_chunks(last_run_dt, now_taipei):
    """Build slot-aligned catch-up chunks.

    Returns list of (since_iso_utc, until_iso_utc, summary_date) in
    chronological order. Each chunk corresponds to a configured slot
    (`AUTO_SUMMARIZE_TIMES`) that fired between `last_run_dt` and `now_taipei`.

    Window for slot `s` is `[prev_slot, s)` in Asia/Taipei, where
    `prev_slot` is the slot immediately before `s` in the schedule. The
    first chunk's start is clamped to `last_run_dt` so we don't re-pull
    messages already covered by the last completed run. summary_date = the
    date `s` falls on in Asia/Taipei — matching what the live slot fire
    would have written.

    Honors AUTO_SUMMARIZE_CATCHUP_MAX_HOURS by trimming the oldest chunks
    whose end-time falls outside that window from now.
    """
    if not AUTO_SUMMARIZE_TIMES:
        return []

    # Build all schedule slot datetimes from a few days before last_run_dt
    # through now_taipei, then filter to the (last_run_dt, now_taipei] range.
    start_date = (last_run_dt - timedelta(days=1)).date()
    end_date = now_taipei.date()
    slots = []
    d = start_date
    while d <= end_date:
        for slot in AUTO_SUMMARIZE_TIMES:
            slots.append((_slot_datetime(d, slot), d, slot))
        d += timedelta(days=1)
    slots.sort(key=lambda x: x[0])

    missed = [s for s in slots if last_run_dt < s[0] <= now_taipei]
    if not missed:
        return []

    cap_dt = now_taipei - timedelta(hours=AUTO_SUMMARIZE_CATCHUP_MAX_HOURS)

    chunks = []
    first = missed[0]
    for s in missed:
        slot_dt, slot_date, slot = s
        idx = slots.index(s)
        prev_slot = slots[idx - 1][0] if idx > 0 else slot_dt - timedelta(hours=AUTO_SUMMARIZE_HOURS)
        # Only the first chunk's start gets clamped — subsequent chunks chain
        # from their predecessor missed slot, which is already > last_run_dt.
        chunk_start = max(prev_slot, last_run_dt) if s == first else prev_slot
        if slot_dt < cap_dt:
            # Past the catch-up cap; skip oldest chunks to bound work.
            continue
        if chunk_start >= slot_dt:
            # Degenerate window (last_run_dt past this slot's prev boundary).
            continue
        since_utc = chunk_start.astimezone(timezone.utc).isoformat()
        until_utc = slot_dt.astimezone(timezone.utc).isoformat()
        chunks.append((since_utc, until_utc, slot_date.isoformat(), _slot_label(slot)))
    return chunks


def _slot_mode_initial_wait():
    """Slot-mode startup wait. Returns (wait_secs, chunks_plan).

    If the most recent past slot has no auto summary written *after* it
    (= we missed that slot, e.g. machine was off), build a slot-aligned
    catch-up plan via `_enumerate_catchup_chunks` so each missed slot
    becomes its own chunk written to its native date. Otherwise wait until
    the next slot with an empty plan.
    """
    last_slot = _last_passed_slot()
    if last_slot is None:
        return AUTO_SUMMARIZE_INITIAL_DELAY_SECS, []

    last_run_dt = None
    covered = False
    try:
        with get_db_ctx() as conn:
            covered, last_run_dt = _slot_mode_coverage(conn, last_slot)
    except Exception:
        logger.exception("auto-summarize: slot-mode coverage check failed")

    now = datetime.now(TAIPEI_TZ)
    if covered:
        next_slot = _next_auto_summarize_slot(now)
        wait_secs = max(int((next_slot - now).total_seconds()), 1)
        logger.info(
            "auto-summarize: slot mode — last slot %s already covered; "
            "next slot %s in %ds",
            last_slot.strftime("%Y-%m-%d %H:%M"),
            next_slot.strftime("%Y-%m-%d %H:%M"),
            wait_secs,
        )
        return wait_secs, []

    # Not covered — build slot-aligned catch-up plan.
    if last_run_dt:
        chunks = _enumerate_catchup_chunks(last_run_dt, now)
    else:
        # No prior auto run: catch up only the most recent passed slot's
        # window so a fresh install doesn't trigger a multi-day backfill.
        chunks = _enumerate_catchup_chunks(last_slot - timedelta(hours=AUTO_SUMMARIZE_HOURS, seconds=1), now)

    if not chunks:
        logger.info(
            "auto-summarize: slot mode — last slot %s not covered but no "
            "chunks produced; catching up in %ds (default %dh window)",
            last_slot.strftime("%Y-%m-%d %H:%M"),
            AUTO_SUMMARIZE_INITIAL_DELAY_SECS, AUTO_SUMMARIZE_HOURS,
        )
        return AUTO_SUMMARIZE_INITIAL_DELAY_SECS, []

    chunk_summary = ", ".join(
        f"{chunk[2]} {chunk[3] if len(chunk) > 3 else ''}({chunk[0][11:16]}->{chunk[1][11:16]} UTC)"
        for chunk in chunks
    )
    logger.info(
        "auto-summarize: slot mode — last slot %s not covered; %d slot-aligned "
        "catch-up chunk(s) in %ds: %s",
        last_slot.strftime("%Y-%m-%d %H:%M"),
        len(chunks), AUTO_SUMMARIZE_INITIAL_DELAY_SECS, chunk_summary,
    )
    return AUTO_SUMMARIZE_INITIAL_DELAY_SECS, chunks


def _auto_summarize_loop():
    """Run auto-summarize cycles.

    Slot mode (AUTO_SUMMARIZE_TIMES set): fire at fixed Asia/Taipei wall-clock
    times. On startup, if the most recent past slot wasn't covered (machine
    was off), runs a catch-up immediately.

    Interval mode (legacy fallback): runs every AUTO_SUMMARIZE_INTERVAL_HOURS;
    AUTO_SUMMARIZE_RUN_ON_START controls whether to fire on startup.
    """
    pending_chunks = None
    if AUTO_SUMMARIZE_TIMES:
        wait_secs, pending_chunks = _slot_mode_initial_wait()
    elif AUTO_SUMMARIZE_RUN_ON_START:
        wait_secs = AUTO_SUMMARIZE_INITIAL_DELAY_SECS
        logger.info("auto-summarize: run-on-start enabled; first cycle in %ds", wait_secs)
    else:
        wait_secs = _seconds_until_next_auto_summarize()
        logger.info("auto-summarize: first cycle in %ds", wait_secs)

    if _AUTO_SUMMARIZE_STOP.wait(wait_secs):
        return
    while not _AUTO_SUMMARIZE_STOP.is_set():
        try:
            run_chunks = pending_chunks if pending_chunks else None
            if AUTO_SUMMARIZE_TIMES and run_chunks is None:
                current_slot = _last_passed_slot(datetime.now(TAIPEI_TZ))
                chunk = _chunk_for_slot(current_slot)
                run_chunks = [chunk] if chunk else None
            _run_auto_summarize_cycle(chunks=run_chunks)
        except Exception:
            logger.exception("auto-summarize loop iteration crashed")
        # Catch-up plan is one-shot; later slot-mode cycles build a fresh
        # buffered window for the slot that just fired.
        pending_chunks = None

        if AUTO_SUMMARIZE_TIMES:
            now = datetime.now(TAIPEI_TZ)
            # Cycle overrun guard: a long catch-up (e.g. Ray Orange backlog)
            # can run past one or more scheduled slots. Without this, the
            # post-cycle wait would jump straight to the next *future* slot
            # via _next_auto_summarize_slot, silently skipping any slots that
            # fired while we were busy. Re-enumerate from the slot we just
            # finished (max until_iso across run_chunks) up to now and queue
            # any newly-passed slots as the next iteration's chunks.
            last_processed_dt = None
            if run_chunks:
                last_until = max((chunk[1] for chunk in run_chunks), default=None)
                if last_until:
                    try:
                        dt = datetime.fromisoformat(last_until)
                        last_processed_dt = (
                            dt.astimezone(TAIPEI_TZ) if dt.tzinfo
                            else dt.replace(tzinfo=TAIPEI_TZ)
                        )
                    except Exception:
                        pass
            overrun_chunks = (
                _enumerate_catchup_chunks(last_processed_dt, now)
                if last_processed_dt else []
            )
            if overrun_chunks:
                pending_chunks = overrun_chunks
                wait_secs = 1
                chunk_summary = ", ".join(
                    f"{chunk[2]} {chunk[3] if len(chunk) > 3 else ''}({chunk[0][11:16]}->{chunk[1][11:16]} UTC)"
                    for chunk in overrun_chunks
                )
                logger.info(
                    "auto-summarize: cycle overran into %d new slot(s); "
                    "running catch-up immediately: %s",
                    len(overrun_chunks), chunk_summary,
                )
            else:
                next_slot = _next_auto_summarize_slot(now)
                wait_secs = max(int((next_slot - now).total_seconds()), 1)
                logger.info(
                    "auto-summarize: cycle done; next slot %s in %ds",
                    next_slot.strftime("%Y-%m-%d %H:%M"), wait_secs,
                )
        else:
            wait_secs = AUTO_SUMMARIZE_INTERVAL_HOURS * 3600

        if _AUTO_SUMMARIZE_STOP.wait(wait_secs):
            return


def _start_auto_summarize():
    if AUTO_SUMMARIZE_INTERVAL_HOURS <= 0 and not AUTO_SUMMARIZE_TIMES:
        logger.info("auto-summarize: disabled (AUTO_SUMMARIZE_INTERVAL_HOURS=0 and no AUTO_SUMMARIZE_TIMES)")
        return
    t = threading.Thread(target=_auto_summarize_loop, daemon=True, name="auto-summarize")
    t.start()
    if AUTO_SUMMARIZE_TIMES:
        slots = ", ".join(f"{h:02d}:{m:02d}" for h, m in AUTO_SUMMARIZE_TIMES)
        logger.info(
            "auto-summarize: started, slot mode (Asia/Taipei): [%s], window=%dh",
            slots, AUTO_SUMMARIZE_HOURS,
        )
    else:
        logger.info(
            "auto-summarize: started, interval mode: every %dh, window=%dh, "
            "initial_delay=%ds, run_on_start=%s",
            AUTO_SUMMARIZE_INTERVAL_HOURS, AUTO_SUMMARIZE_HOURS,
            AUTO_SUMMARIZE_INITIAL_DELAY_SECS, AUTO_SUMMARIZE_RUN_ON_START,
        )


atexit.register(_AUTO_SUMMARIZE_STOP.set)


# ============================================================
# Main entry
# ============================================================

if __name__ == "__main__":
    if not _acquire_single_instance_lock():
        print("Another tg-fetcher server.py is already running. Stop it before starting a new one.")
        raise SystemExit(1)
    atexit.register(_release_single_instance_lock)

    print("\n" + "=" * 60)
    print("  🚀 TG Fetcher Pro — Crypto 情報中心")
    print("=" * 60)

    init_db()
    embeddings.ensure_embedding_norms()

    if CLAUDE_API_KEY:
        print("  🤖 Claude API 已設定")
        print("  🔀 模型分級：Opus 4.7（深度總結/摘要差異）+ Sonnet 4.6（壓縮/情緒/事件/記憶問答）")
    else:
        print("  ⚠ 未設定 CLAUDE_API_KEY，AI 功能將無法使用")
        print("    設定方式：export CLAUDE_API_KEY=\"sk-ant-...\"")

    if embeddings.HAS_VOYAGE and embeddings.get_voyage_client():
        print("  🧠 VoyageAI Embedding 已啟用（RAG 向量搜尋）")
    else:
        print("  ℹ 未啟用 Embedding（安裝 voyageai + 設定 VOYAGE_API_KEY 可提升記憶問答品質）")

    tg_thread = threading.Thread(target=tgs.start_telethon_loop, daemon=True)
    tg_thread.start()
    if not tgs.wait_ready(timeout=10.0):
        print("  ⏳ Telegram 仍在連線中，Flask 先啟動（/api/* 會在 ready 後開始可用）")

    if AUTO_FETCH_INTERVAL_HOURS > 0:
        print(f"  🔁 自動 fetch 已啟用：每 {AUTO_FETCH_INTERVAL_HOURS}h 抓 {AUTO_FETCH_HOURS}h 視窗（重啟會延續上次排程,已分類的 chat,只 archive 不 summarize）")
    else:
        print("  ⏸  自動 fetch 已停用（AUTO_FETCH_INTERVAL_HOURS=0)")
    _start_auto_fetch()

    if AUTO_SUMMARIZE_TIMES:
        slots = "、".join(f"{h:02d}:{m:02d}" for h, m in AUTO_SUMMARIZE_TIMES)
        print(f"  🤖 自動 summarize 已啟用：固定時刻 {slots}（UTC+8）跑 {AUTO_SUMMARIZE_HOURS}h 視窗（錯過 slot 會在啟動時補跑;含 wallet_log,標記 source='auto'）")
    elif AUTO_SUMMARIZE_INTERVAL_HOURS > 0:
        startup_note = "，啟動後先跑一輪" if AUTO_SUMMARIZE_RUN_ON_START else ""
        print(f"  🤖 自動 summarize 已啟用：每 {AUTO_SUMMARIZE_INTERVAL_HOURS}h 跑一輪 {AUTO_SUMMARIZE_HOURS}h 視窗{startup_note}（含 wallet_log,走 deterministic 聚合,標記 source='auto'）")
    else:
        print("  ⏸  自動 summarize 已停用（AUTO_SUMMARIZE_INTERVAL_HOURS=0 且未設 AUTO_SUMMARIZE_TIMES）")
    _start_auto_summarize()

    print(f"\n  🌐 http://{HOST}:{PORT}")
    print("  按 Ctrl+C 停止\n")

    app.run(host=HOST, port=PORT, debug=False)
