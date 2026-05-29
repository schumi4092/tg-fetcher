"""Telegram-related routes: index/static, status, login, dialogs, topics, messages, CSV export."""

import asyncio
import csv
import io
import math
import re
import threading
import time as _time
from concurrent.futures import CancelledError as FuturesCancelledError
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from flask import Blueprint, jsonify, make_response, request, send_from_directory

from config import STATIC_DIR, logger
from db import get_db_ctx
import ai
import embeddings
import telegram_service as tgs
from telegram_service import (
    SessionPasswordNeededError,
    format_sender,
    get_media_type,
    is_logged_in,
    login_lock,
    run_async,
    safe_login_state,
    set_logged_in,
    telethon_ready,
)
from telethon.tl.types import Channel, Chat

from routes._shared import get_json_body


bp = Blueprint("telegram", __name__)


_status_cache = {"ts": 0.0, "connected": False, "me": None}
_status_cache_lock = threading.Lock()
STATUS_CACHE_TTL = 15.0


def _mask_phone(phone):
    phone = str(phone or "")
    if not phone:
        return ""
    return phone[:3] + "****" + phone[-2:] if len(phone) > 5 else "****"


def _parse_positive_float_arg(name, default):
    raw = request.args.get(name)
    if raw in (None, ""):
        return default, None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, (jsonify({"error": f"{name} must be a positive number"}), 400)
    if not math.isfinite(value) or value <= 0:
        return None, (jsonify({"error": f"{name} must be a positive number"}), 400)
    return value, None


def _parse_optional_positive_int_arg(name, max_value=5000):
    raw = request.args.get(name)
    if raw in (None, ""):
        return None, None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None, (jsonify({"error": f"{name} must be a positive integer"}), 400)
    if value <= 0:
        return None, (jsonify({"error": f"{name} must be a positive integer"}), 400)
    if value > max_value:
        return None, (jsonify({"error": f"{name} must be <= {max_value}"}), 400)
    return value, None


@bp.route("/")
def index():
    return send_from_directory(str(STATIC_DIR), "index.html")


@bp.route("/api/status")
def api_status():
    is_ready = telethon_ready()

    now = _time.monotonic()
    with _status_cache_lock:
        cache_fresh = (now - _status_cache["ts"]) < STATUS_CACHE_TTL
        if cache_fresh:
            is_connected = _status_cache["connected"]
            me = _status_cache["me"]
        else:
            is_connected = False
            me = None

    if not cache_fresh:
        if is_ready:
            async def _probe():
                connected = await tgs.tg_client.is_user_authorized()
                if not connected:
                    return False, None
                user = await tgs.tg_client.get_me()
                return True, {
                    "name": f"{user.first_name or ''} {user.last_name or ''}".strip(),
                    "username": user.username or "",
                    "phone_masked": _mask_phone(user.phone),
                    "id": user.id,
                }
            try:
                is_connected, me = run_async(_probe())
                set_logged_in(is_connected)
            except Exception:
                is_connected, me = False, None

        with _status_cache_lock:
            _status_cache["ts"] = now
            _status_cache["connected"] = is_connected
            _status_cache["me"] = me

    auto_summary_runs = []
    try:
        with get_db_ctx() as conn:
            auto_summary_runs = [dict(r) for r in conn.execute("""
                SELECT date, slot, fetch_status, summary_status,
                       ok_count, skip_existing_count, skip_no_msgs_count,
                       failed_count, error, updated_at
                FROM auto_summary_runs
                ORDER BY date DESC, slot DESC
                LIMIT 6
            """).fetchall()]
    except Exception:
        auto_summary_runs = []

    return jsonify({
        "connected": is_connected,
        "telegram_ready": is_ready,
        "login_state": safe_login_state(),
        "user": me,
        "has_ai": bool(ai.get_claude_client()),
        "has_embeddings": bool(embeddings.get_voyage_client()),
        "auto_summary_runs": auto_summary_runs,
    })


@bp.route("/api/login/send_code", methods=["POST"])
def send_code():
    if not telethon_ready():
        return jsonify({"error": "Telegram client is still starting. Please try again in a few seconds."}), 503
    data = get_json_body()
    phone = data.get("phone", "").strip()
    if not phone:
        return jsonify({"error": "請輸入手機號碼"}), 400
    try:
        result = run_async(tgs.tg_client.send_code_request(phone))
        with login_lock:
            tgs.login_state = {"phase": "need_code", "phone": phone, "phone_code_hash": result.phone_code_hash}
        return jsonify({"status": "code_sent", "phone": phone})
    except Exception as e:
        with login_lock:
            tgs.login_state = {"phase": "error", "error": str(e)}
        return jsonify({"error": str(e)}), 400


@bp.route("/api/login/verify_code", methods=["POST"])
def verify_code():
    if not telethon_ready():
        return jsonify({"error": "Telegram client is still starting. Please try again in a few seconds."}), 503
    data = get_json_body()
    code = data.get("code", "").strip()
    with login_lock:
        if tgs.login_state.get("phase") != "need_code":
            return jsonify({"error": "請先發送驗證碼"}), 400
        phone = tgs.login_state["phone"]
        phone_code_hash = tgs.login_state["phone_code_hash"]
    try:
        run_async(tgs.tg_client.sign_in(phone, code, phone_code_hash=phone_code_hash))
        with login_lock:
            tgs.login_state = {"phase": "done"}
        set_logged_in(True)
        return jsonify({"status": "logged_in"})
    except SessionPasswordNeededError:
        with login_lock:
            tgs.login_state["phase"] = "need_password"
        return jsonify({"status": "need_password"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/api/login/verify_password", methods=["POST"])
def verify_password():
    if not telethon_ready():
        return jsonify({"error": "Telegram client is still starting. Please try again in a few seconds."}), 503
    data = get_json_body()
    password = data.get("password", "")
    try:
        run_async(tgs.tg_client.sign_in(password=password))
        with login_lock:
            tgs.login_state = {"phase": "done"}
        set_logged_in(True)
        return jsonify({"status": "logged_in"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/api/dialogs")
def get_dialogs():
    if not telethon_ready():
        return jsonify({"error": "Telegram client is still starting. Please try again in a few seconds."}), 503
    if not is_logged_in():
        return jsonify({"error": "未登入"}), 401
    limit = request.args.get("limit", 100, type=int)

    async def _fetch():
        dialogs = []
        async for d in tgs.tg_client.iter_dialogs(limit=limit):
            entity = d.entity
            dtype = "private"
            is_forum = False
            if isinstance(entity, Channel):
                dtype = "channel" if entity.broadcast else "supergroup"
                is_forum = getattr(entity, 'forum', False)
            elif isinstance(entity, Chat):
                dtype = "group"
            dialogs.append({
                "id": d.entity.id,
                "name": d.name or "(無名稱)",
                "type": dtype,
                "username": getattr(entity, 'username', '') or '',
                "unread": d.unread_count,
                "is_forum": is_forum,
            })
        return dialogs

    try:
        dialogs = run_async(_fetch())
        with get_db_ctx() as conn:
            mapping = {
                str(r["chat_id"]): {"category_id": r["category_id"],
                                     "category_name": r["name"],
                                     "category_color": r["color"],
                                     "prompt_profile": r["prompt_profile"]}
                for r in conn.execute("""
                    SELECT m.chat_id, m.category_id, c.name, c.color, c.prompt_profile
                    FROM chat_category_map m
                    JOIN chat_categories c ON c.id = m.category_id
                """).fetchall()
            }
        for d in dialogs:
            info = mapping.get(str(d["id"]))
            if info:
                d.update(info)
            else:
                d["category_id"] = None
                d["category_name"] = None
                d["category_color"] = None
                d["prompt_profile"] = None
        return jsonify({"dialogs": dialogs})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/topics")
def get_topics():
    if not telethon_ready():
        return jsonify({"error": "Telegram client is still starting. Please try again in a few seconds."}), 503
    if not is_logged_in():
        return jsonify({"error": "未登入"}), 401
    chat = request.args.get("chat", "")
    if not chat:
        return jsonify({"error": "請指定聊天室"}), 400

    async def _fetch():
        try:
            target = int(chat)
        except ValueError:
            target = chat

        entity = await tgs.tg_client.get_entity(target)

        try:
            from telethon.tl.functions.messages import GetForumTopicsRequest
            result = await tgs.tg_client(GetForumTopicsRequest(
                peer=entity,
                offset_date=None, offset_id=0, offset_topic=0,
                limit=100,
            ))
            topics = []
            for t in result.topics:
                topics.append({
                    "id": t.id, "title": t.title,
                    "icon_emoji": getattr(t, 'icon_emoji_id', None) or '',
                    "unread": getattr(t, 'unread_count', 0),
                    "top_message": getattr(t, 'top_message', 0),
                })
            topics.sort(key=lambda x: x["top_message"], reverse=True)
            return {"is_forum": True, "topics": topics}
        except ImportError:
            logger.warning("GetForumTopicsRequest 不可用，改用訊息掃描")
        except Exception as e:
            logger.warning("GetForumTopicsRequest 失敗: %s，改用訊息掃描", e)

        topic_last = {}
        async for msg in tgs.tg_client.iter_messages(entity, limit=5000):
            if not msg.reply_to:
                continue
            if getattr(msg.reply_to, 'forum_topic', False):
                tid = msg.reply_to.reply_to_top_id or msg.reply_to.reply_to_msg_id
                if tid and tid not in topic_last:
                    topic_last[tid] = msg.id

        if not topic_last:
            return {"is_forum": False, "topics": [],
                    "error": "找不到子頻道資料，請升級 Telethon: pip install --upgrade telethon"}

        topics = []
        header_ids = list(topic_last.keys())
        try:
            headers = await tgs.tg_client.get_messages(entity, ids=header_ids)
            if not isinstance(headers, list):
                headers = [headers]
            for msg in headers:
                if msg is None:
                    continue
                title = ""
                if hasattr(msg, 'action') and msg.action:
                    title = getattr(msg.action, 'title', '') or ''
                if not title:
                    title = (msg.text or '')[:30] or f"Topic #{msg.id}"
                topics.append({
                    "id": msg.id, "title": title,
                    "icon_emoji": '', "unread": 0,
                    "top_message": topic_last.get(msg.id, 0),
                })
        except Exception as e:
            logger.warning("取得 topic header 失敗: %s", e)
            for tid, last in topic_last.items():
                topics.append({"id": tid, "title": f"Topic #{tid}",
                               "icon_emoji": '', "unread": 0, "top_message": last})

        topics.sort(key=lambda x: x["top_message"], reverse=True)
        return {"is_forum": True, "topics": topics}

    try:
        return jsonify(run_async(_fetch()))
    except Exception as e:
        return jsonify({"error": str(e), "is_forum": False, "topics": []}), 500


async def fetch_messages_for_entity(entity, hours, filter_topics=None, limit=None):
    """Pull recent messages for a Telethon entity. Runs inside the Telethon loop."""
    chat_name = getattr(entity, 'title', None) or getattr(entity, 'first_name', '') or str(getattr(entity, 'id', ''))
    chat_type = "channel" if isinstance(entity, Channel) and entity.broadcast else \
                "supergroup" if isinstance(entity, Channel) else \
                "group" if isinstance(entity, Chat) else "private"
    is_forum = getattr(entity, 'forum', False)

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    messages = []

    if is_forum and filter_topics:
        for topic_id in filter_topics:
            try:
                async for msg in tgs.tg_client.iter_messages(entity, reply_to=topic_id, limit=limit):
                    if msg.date < since:
                        break
                    sender_name, sender_username, sender_id = format_sender(msg)
                    messages.append({
                        "id": msg.id, "date": msg.date.isoformat(),
                        "timestamp": int(msg.date.timestamp()),
                        "from": sender_name, "username": sender_username,
                        "sender_id": sender_id,
                        "text": msg.text or msg.message or "",
                        "media": get_media_type(msg),
                        "reply_to": msg.reply_to_msg_id if msg.reply_to else None,
                        "views": getattr(msg, 'views', None),
                        "forwards": getattr(msg, 'forwards', None),
                        "topic_id": topic_id,
                    })
            except Exception as e:
                logger.warning("Topic %s 擷取失敗: %s", topic_id, e)
        messages.sort(key=lambda m: m["timestamp"])
    else:
        async for msg in tgs.tg_client.iter_messages(entity, offset_date=now, reverse=False, limit=limit):
            if msg.date < since:
                break
            sender_name, sender_username, sender_id = format_sender(msg)
            msg_topic = None
            if is_forum and msg.reply_to and getattr(msg.reply_to, 'forum_topic', False):
                msg_topic = msg.reply_to.reply_to_top_id or msg.reply_to.reply_to_msg_id
            messages.append({
                "id": msg.id, "date": msg.date.isoformat(),
                "timestamp": int(msg.date.timestamp()),
                "from": sender_name, "username": sender_username,
                "sender_id": sender_id,
                "text": msg.text or msg.message or "",
                "media": get_media_type(msg),
                "reply_to": msg.reply_to_msg_id if msg.reply_to else None,
                "views": getattr(msg, 'views', None),
                "forwards": getattr(msg, 'forwards', None),
                "topic_id": msg_topic,
            })
        messages.reverse()

    return {
        "chat_name": chat_name, "chat_type": chat_type,
        "is_forum": is_forum,
        "hours": hours, "count": len(messages), "messages": messages,
    }


@bp.route("/api/messages")
def get_messages():
    if not telethon_ready():
        return jsonify({"error": "Telegram client is still starting. Please try again in a few seconds."}), 503
    if not is_logged_in():
        return jsonify({"error": "未登入"}), 401
    chat = request.args.get("chat", "")
    hours, error = _parse_positive_float_arg("hours", 8)
    if error:
        return error
    topic_ids = request.args.get("topics", "")
    limit, error = _parse_optional_positive_int_arg("limit")
    if error:
        return error
    # Scope for the cancel key — "ui" (default) isolates interactive browsing
    # from "brief" so Run Brief's fetch doesn't get cancelled when the user
    # opens the same chat mid-batch.
    ctx = request.args.get("ctx", "ui").strip() or "ui"
    if not chat:
        return jsonify({"error": "請指定聊天室"}), 400

    filter_topics = set()
    if topic_ids:
        for tid in topic_ids.split(","):
            tid = tid.strip()
            if tid:
                try:
                    filter_topics.add(int(tid))
                except ValueError:
                    pass

    async def _fetch():
        try:
            target = int(chat)
        except ValueError:
            target = chat
        entity = await tgs.tg_client.get_entity(target)
        return await fetch_messages_for_entity(entity, hours, filter_topics, limit)

    # Scale timeout with hours — wallet trackers (high-volume whale alert bots)
    # routinely blow past 30s on 8h+ windows while Telethon resolves senders.
    fetch_timeout = min(180, max(60, int(hours * 10)))
    try:
        result = tgs.run_async_cancellable(_fetch(), f"msgs:{ctx}:{chat}", timeout=fetch_timeout)
        with get_db_ctx() as conn:
            keywords = [dict(r) for r in conn.execute("SELECT id, keyword FROM watchlist").fetchall()]
        if keywords:
            alert_count = 0
            for msg in result["messages"]:
                text = (msg.get("text") or "").lower()
                matched = [k["keyword"] for k in keywords if k["keyword"].lower() in text]
                if matched:
                    msg["alerts"] = matched
                    alert_count += 1
            result["alert_count"] = alert_count
        return jsonify(result)
    except FuturesCancelledError:
        return jsonify({"error": "superseded"}), 409
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/messages/export_csv")
def export_messages_csv():
    if not telethon_ready():
        return jsonify({"error": "Telegram client is still starting. Please try again in a few seconds."}), 503
    if not is_logged_in():
        return jsonify({"error": "未登入"}), 401
    chats_raw = request.args.get("chats", "")
    hours, error = _parse_positive_float_arg("hours", 24)
    if error:
        return error
    limit, error = _parse_optional_positive_int_arg("limit")
    if error:
        return error
    chat_ids = [c.strip() for c in chats_raw.split(",") if c.strip()]
    if not chat_ids:
        return jsonify({"error": "請至少選一個聊天室"}), 400
    if len(chat_ids) > 50:
        return jsonify({"error": "一次最多匯出 50 個聊天室"}), 400

    async def _fetch_one(sem, chat):
        async with sem:
            try:
                target = int(chat)
            except ValueError:
                target = chat
            try:
                entity = await tgs.tg_client.get_entity(target)
                return await fetch_messages_for_entity(entity, hours, None, limit)
            except Exception as e:
                logger.warning("匯出 chat %s 失敗: %s", chat, e)
                return {"chat_name": str(chat), "error": str(e), "messages": []}

    async def _fetch_all():
        sem = asyncio.Semaphore(5)
        return await asyncio.gather(*[_fetch_one(sem, c) for c in chat_ids])

    try:
        results = run_async(_fetch_all(), timeout=min(300, max(60, 15 * len(chat_ids))))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    buf = io.StringIO()
    buf.write("﻿")  # UTF-8 BOM for Excel
    writer = csv.writer(buf, quoting=csv.QUOTE_ALL)
    writer.writerow(["chat_name", "chat_type", "date", "from", "username", "text", "media", "views", "forwards", "msg_id", "topic_id"])
    total = 0
    for r in results:
        chat_name = r.get("chat_name", "")
        chat_type = r.get("chat_type", "")
        for m in r.get("messages", []):
            text = (m.get("text") or "").replace("\r\n", "\n").replace("\r", "\n")
            writer.writerow([
                chat_name, chat_type, m.get("date", ""),
                m.get("from", ""), m.get("username", ""),
                text, m.get("media", ""),
                m.get("views") if m.get("views") is not None else "",
                m.get("forwards") if m.get("forwards") is not None else "",
                m.get("id", ""), m.get("topic_id") if m.get("topic_id") is not None else "",
            ])
            total += 1

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw_first = (results[0].get("chat_name") or "export") if results else "export"
    # ASCII fallback for `filename=`（latin-1 header 限制）；原名走 RFC 5987 `filename*`
    ascii_first = re.sub(r"[^A-Za-z0-9\-]", "_", raw_first)[:20].strip("_") or "export"
    fname_ascii = f"tg_{ascii_first}_{len(chat_ids)}chats_{stamp}.csv"
    utf8_first = re.sub(r'[\\/:*?"<>|\r\n]', "_", raw_first)[:20]
    fname_utf8 = f"tg_{utf8_first}_{len(chat_ids)}chats_{stamp}.csv"
    resp = make_response(buf.getvalue().encode("utf-8"))
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="{fname_ascii}"; '
        f"filename*=UTF-8''{quote(fname_utf8)}"
    )
    resp.headers["X-Export-Count"] = str(total)
    resp.headers["X-Export-Chats"] = str(len(chat_ids))
    return resp
