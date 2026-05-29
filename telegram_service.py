"""Telethon client lifecycle + thread-safe helpers for sync code."""

import asyncio
import sqlite3
import sys
import threading
from concurrent.futures import TimeoutError as FuturesTimeoutError

from config import API_HASH, API_ID, SESSION_NAME, logger

try:
    from telethon import TelegramClient
    from telethon.tl.types import (
        User, Chat, Channel,
        MessageMediaPhoto, MessageMediaDocument,
        MessageMediaGeo, MessageMediaPoll,
        MessageMediaWebPage, MessageMediaContact,
    )
    from telethon.errors import SessionPasswordNeededError  # re-exported
except ImportError as e:
    print("=" * 60)
    print(f"  缺少依賴：{e}")
    print("  請執行：pip install flask telethon flask-cors anthropic")
    print("=" * 60)
    sys.exit(1)

__all__ = [
    "tg_client", "tg_loop", "SessionPasswordNeededError",
    "is_logged_in", "set_logged_in",
    "login_state", "login_lock", "safe_login_state",
    "run_async", "run_async_cancellable", "telethon_ready", "wait_ready",
    "get_media_type", "format_sender",
    "start_telethon_loop",
]

# Mutable module-level state (accessed as telegram_service.tg_client etc.)
tg_client = None
tg_loop = None
login_state = {"phase": "idle"}
login_lock = threading.Lock()
_logged_in_evt = threading.Event()
_ready_evt = threading.Event()

# Keyed in-flight registry for cancellable requests. Only the most recent
# request per key stays alive — earlier ones get cancelled so rapid slider
# drags don't queue up and trip Telegram's GetHistoryRequest flood wait.
_inflight_keyed_lock = threading.Lock()
_inflight_keyed = {}


def is_logged_in():
    return _logged_in_evt.is_set()


def set_logged_in(value):
    if value:
        _logged_in_evt.set()
    else:
        _logged_in_evt.clear()


def safe_login_state():
    """Only expose `phase` and a masked phone; never leak phone_code_hash."""
    with login_lock:
        phase = login_state.get("phase", "idle")
        out = {"phase": phase}
        if phase == "error" and login_state.get("error"):
            out["error"] = login_state["error"]
        if phase in ("need_code", "need_password") and login_state.get("phone"):
            phone = login_state["phone"]
            out["phone_masked"] = phone[:3] + "****" + phone[-2:] if len(phone) > 5 else "****"
        return out


def telethon_ready():
    return tg_client is not None and tg_loop is not None


def wait_ready(timeout=10.0):
    """Block until the Telethon client is connected and the loop is running, or timeout."""
    return _ready_evt.wait(timeout=timeout)


def run_async(coro, timeout=30):
    """Submit a coroutine to the Telethon-owned loop and await the result."""
    if tg_loop is None or tg_client is None or not tg_loop.is_running():
        raise RuntimeError("Telegram client is still starting. Please try again in a few seconds.")
    future = asyncio.run_coroutine_threadsafe(coro, tg_loop)
    try:
        return future.result(timeout=timeout)
    except FuturesTimeoutError:
        future.cancel()
        raise RuntimeError(f"Telegram 操作逾時（>{timeout}s）")


def run_async_cancellable(coro, cancel_key, timeout=30):
    """Like run_async, but any prior in-flight call with the same cancel_key is
    cancelled first. Callers that get superseded see concurrent.futures.CancelledError."""
    if tg_loop is None or tg_client is None or not tg_loop.is_running():
        raise RuntimeError("Telegram client is still starting. Please try again in a few seconds.")

    with _inflight_keyed_lock:
        old = _inflight_keyed.pop(cancel_key, None)
    if old is not None and not old.done():
        old.cancel()

    future = asyncio.run_coroutine_threadsafe(coro, tg_loop)
    with _inflight_keyed_lock:
        _inflight_keyed[cancel_key] = future
    try:
        return future.result(timeout=timeout)
    except FuturesTimeoutError:
        future.cancel()
        raise RuntimeError(f"Telegram 操作逾時（>{timeout}s）")
    finally:
        with _inflight_keyed_lock:
            if _inflight_keyed.get(cancel_key) is future:
                _inflight_keyed.pop(cancel_key, None)


def get_media_type(msg):
    media = msg.media
    if media is None:
        return ""
    if isinstance(media, MessageMediaPhoto):
        return "photo"
    if isinstance(media, MessageMediaDocument):
        doc = media.document
        if doc:
            for attr in doc.attributes:
                name = type(attr).__name__
                if name == "DocumentAttributeVideo":
                    return "video_note" if getattr(attr, 'round_message', False) else "video"
                if name == "DocumentAttributeAudio":
                    return "voice" if getattr(attr, 'voice', False) else "audio"
                if name == "DocumentAttributeSticker":
                    return "sticker"
                if name == "DocumentAttributeAnimated":
                    return "gif"
            return "document"
    if isinstance(media, MessageMediaGeo):
        return "location"
    if isinstance(media, MessageMediaPoll):
        return "poll"
    if isinstance(media, MessageMediaWebPage):
        return "webpage"
    if isinstance(media, MessageMediaContact):
        return "contact"
    return "other"


def format_sender(msg):
    sender = msg.sender
    if sender is None:
        return "未知", "", None
    if isinstance(sender, User):
        name = sender.first_name or ""
        if sender.last_name:
            name += f" {sender.last_name}"
        return name.strip() or "未知", sender.username or "", sender.id
    if isinstance(sender, (Chat, Channel)):
        return sender.title or "未知", getattr(sender, 'username', '') or "", sender.id
    return "未知", "", None


def start_telethon_loop():
    """Run an asyncio event loop in a background thread, owning the Telethon client."""
    global tg_client, tg_loop

    if not API_ID or not API_HASH:
        logger.error("尚未設定 Telegram API，Telethon 不啟動；請在 .env 設定 TG_API_ID / TG_API_HASH")
        return

    try:
        api_id_int = int(API_ID)
    except ValueError:
        logger.error("TG_API_ID 不是合法整數：%r", API_ID)
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    client = TelegramClient(SESSION_NAME, api_id_int, API_HASH)

    async def _connect():
        await client.connect()
        authorized = await client.is_user_authorized()
        set_logged_in(authorized)
        if authorized:
            me = await client.get_me()
            logger.info("✅ Telegram 已登入：%s (@%s)", me.first_name, me.username or "N/A")
        else:
            logger.info("Telegram 尚未登入，請透過網頁介面登入")

    async def _connect_with_retries(max_attempts=12):
        for attempt in range(1, max_attempts + 1):
            try:
                await _connect()
                return True
            except sqlite3.OperationalError as e:
                if "database is locked" not in str(e).lower():
                    raise
                wait_secs = min(10, max(1, attempt * 2))
                logger.warning(
                    "Telegram session DB is locked; retrying in %ss (%d/%d). "
                    "Another tg-fetcher process may still be shutting down.",
                    wait_secs, attempt, max_attempts,
                )
                try:
                    await client.disconnect()
                except Exception:
                    pass
                await asyncio.sleep(wait_secs)
        logger.error("Telegram session DB stayed locked after %d attempts; Telegram is not ready.", max_attempts)
        return False

    try:
        connected = loop.run_until_complete(_connect_with_retries())
    except Exception:
        logger.exception("Telegram startup failed")
        connected = False
    if not connected:
        try:
            loop.run_until_complete(client.disconnect())
        except Exception:
            pass
        loop.close()
        return
    tg_loop = loop
    tg_client = client
    _ready_evt.set()
    loop.run_forever()
