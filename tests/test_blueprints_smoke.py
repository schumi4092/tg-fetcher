"""Integration smoke tests: hit each blueprint's routes via Flask test_client.

These don't replace real e2e tests but they catch:
  - blueprint registration failures
  - import errors that pure-function tests miss
  - obvious 500s from missing helpers / typos in route handlers
  - status code regressions on auth-gated routes

We keep dependencies fake: no Telegram login, no real AI, no Twitter token.
That means most routes return 4xx / 5xx with a specific error message — we
assert on the SHAPE of the response (status + JSON keys), not the content.
"""

import json
import os
import sys
import asyncio
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def client(fresh_db, monkeypatch):
    """Flask test client backed by an empty test DB.

    Disables background loops and Telegram so server.py can import without
    starting threads / connecting to the real TG API.
    """
    import config
    monkeypatch.setattr(config, "AUTO_FETCH_INTERVAL_HOURS", 0)
    monkeypatch.setattr(config, "AUTO_SUMMARIZE_INTERVAL_HOURS", 0)
    monkeypatch.setattr(config, "API_ID", "")
    monkeypatch.setattr(config, "API_HASH", "")

    # init DB before importing server (server imports init_db at __main__,
    # but blueprints expect the schema to exist when test_client hits them)
    import db
    db.init_db()

    # Force fresh import of server so blueprints re-bind to our patched config
    for mod in list(sys.modules):
        if mod in {"server", "routes"} or mod.startswith("routes."):
            sys.modules.pop(mod, None)
    import server
    server.app.config["TESTING"] = True
    return server.app.test_client()


# ---------------------------------------------------------------------------
# Telegram blueprint
# ---------------------------------------------------------------------------

def test_index_serves_html(client):
    r = client.get("/")
    # Index file may or may not exist depending on test env; either 200 or 404
    assert r.status_code in (200, 404)


def test_status_endpoint(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    data = r.get_json()
    assert "connected" in data
    assert "telegram_ready" in data
    assert "has_ai" in data
    assert "has_embeddings" in data
    assert "auto_summary_runs" in data


def test_status_endpoint_masks_phone(client, monkeypatch):
    import routes.telegram as telegram_routes
    import telegram_service as tgs

    class DummyUser:
        first_name = "Ada"
        last_name = "Lovelace"
        username = "ada"
        phone = "+886912345678"
        id = 123

    class DummyClient:
        async def is_user_authorized(self):
            return True

        async def get_me(self):
            return DummyUser()

    telegram_routes._status_cache.update({"ts": 0.0, "connected": False, "me": None})
    monkeypatch.setattr(telegram_routes, "telethon_ready", lambda: True)
    monkeypatch.setattr(tgs, "tg_client", DummyClient())
    monkeypatch.setattr(telegram_routes, "run_async", lambda coro, timeout=None: asyncio.run(coro))

    r = client.get("/api/status")
    assert r.status_code == 200
    user = r.get_json()["user"]
    assert "phone" not in user
    assert user["phone_masked"] == "+88****78"


def test_dialogs_requires_login(client):
    r = client.get("/api/dialogs")
    # Telegram client never starts, so we expect 503
    assert r.status_code == 503


def test_messages_requires_chat(client):
    r = client.get("/api/messages")
    assert r.status_code in (400, 503)


# ---------------------------------------------------------------------------
# Memory blueprint
# ---------------------------------------------------------------------------

def test_memory_timeline_empty(client):
    r = client.get("/api/memory/timeline?days=7")
    assert r.status_code == 200
    data = r.get_json()
    assert "timeline" in data
    assert isinstance(data["timeline"], list)


def test_memory_day_empty(client):
    r = client.get("/api/memory/day/2026-04-25")
    assert r.status_code == 200
    data = r.get_json()
    assert data["date"] == "2026-04-25"
    assert data["summaries"] == []
    assert data["events"] == []
    assert data["notes"] == []
    assert data["summary_run"] is None


def test_memory_timeline_and_day_split_by_summary_slot(client):
    import db

    with db.get_db_ctx() as conn:
        early_id = conn.execute("""
            INSERT INTO daily_summaries
            (date, chat_id, chat_name, hours, message_count, summary, summary_slot, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, ("2026-05-05", "chat-a", "Morning", 12, 10, "early brief", "10:00", "auto")).lastrowid
        late_id = conn.execute("""
            INSERT INTO daily_summaries
            (date, chat_id, chat_name, hours, message_count, summary, summary_slot, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, ("2026-05-05", "chat-a", "Evening", 12, 20, "late brief", "22:00", "auto")).lastrowid
        conn.execute("""
            INSERT INTO events
            (date, title, description, importance, source_chat, source_summary_id)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("2026-05-05", "Morning event", "x", "normal", "Morning", early_id))
        conn.execute("""
            INSERT INTO events
            (date, title, description, importance, source_chat, source_summary_id)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("2026-05-05", "Evening event", "y", "high", "Evening", late_id))
        conn.commit()

    r = client.get("/api/memory/timeline?days=7")
    assert r.status_code == 200
    same_day = [t for t in r.get_json()["timeline"] if t["date"] == "2026-05-05"]
    assert [t["summary_slot"] for t in same_day] == ["22:00", "10:00"]
    assert same_day[0]["summaries"] == 1
    assert same_day[0]["events"] == 1
    assert same_day[0]["event_titles"][0]["title"] == "Evening event"

    r = client.get("/api/memory/day/2026-05-05?slot=22%3A00")
    assert r.status_code == 200
    data = r.get_json()
    assert data["summary_slot"] == "22:00"
    assert [s["chat_name"] for s in data["summaries"]] == ["Evening"]
    assert [e["title"] for e in data["events"]] == ["Evening event"]

    r = client.delete("/api/memory/summaries?date=2026-05-05&slot=22%3A00")
    assert r.status_code == 200
    assert r.get_json()["count"] == 1
    with db.get_db_ctx() as conn:
        remaining = [r["summary_slot"] for r in conn.execute(
            "SELECT summary_slot FROM daily_summaries ORDER BY summary_slot"
        ).fetchall()]
    assert remaining == ["10:00"]


def test_memory_slot_progress_exposes_running_auto_summary(client):
    import db

    with db.get_db_ctx() as conn:
        cat_id = conn.execute("""
            INSERT INTO chat_categories (name, sort_order)
            VALUES (?, ?)
        """, ("Tracked", 1)).lastrowid
        conn.executemany("""
            INSERT INTO chat_category_map (chat_id, category_id)
            VALUES (?, ?)
        """, [("chat-a", cat_id), ("chat-b", cat_id)])
        conn.execute("""
            INSERT INTO daily_summaries
            (date, chat_id, chat_name, hours, message_count, summary, summary_slot, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, ("2026-05-14", "chat-a", "Alpha", 8, 12, "done", "24:00", "auto"))
        conn.execute("""
            INSERT INTO auto_summary_runs
            (date, slot, fetch_status, summary_status, ok_count, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("2026-05-14", "24:00", "done", "running", 0, "2026-05-15T10:53:34"))
        conn.commit()

    r = client.get("/api/memory/timeline?days=7")
    assert r.status_code == 200
    row = next(t for t in r.get_json()["timeline"] if t["date"] == "2026-05-14")
    assert row["run_status"] == "running"
    assert row["expected_chats"] == 2
    assert row["completed_chats"] == 1
    assert row["processed_chats"] == 1

    r = client.get("/api/memory/day/2026-05-14?slot=24%3A00")
    assert r.status_code == 200
    run = r.get_json()["summary_run"]
    assert run["summary_status"] == "running"
    assert run["expected_chats"] == 2
    assert run["completed_chats"] == 1


def test_memory_slot_progress_hides_stale_running_after_all_chats_finish(client):
    import db

    with db.get_db_ctx() as conn:
        cat_id = conn.execute("""
            INSERT INTO chat_categories (name, sort_order)
            VALUES (?, ?)
        """, ("Tracked", 1)).lastrowid
        conn.executemany("""
            INSERT INTO chat_category_map (chat_id, category_id)
            VALUES (?, ?)
        """, [("chat-a", cat_id), ("chat-b", cat_id)])
        conn.executemany("""
            INSERT INTO daily_summaries
            (date, chat_id, chat_name, hours, message_count, summary, summary_slot, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            ("2026-05-14", "chat-a", "Alpha", 8, 12, "done", "08:00", "auto"),
            ("2026-05-14", "chat-b", "Beta", 8, 8, "done", "08:00", "auto"),
        ])
        conn.execute("""
            INSERT INTO auto_summary_runs
            (date, slot, fetch_status, summary_status, ok_count, failed_count, error, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "2026-05-14",
            "08:00",
            "done",
            "running",
            1,
            1,
            "old failure",
            "2026-05-15T10:53:34",
        ))
        conn.commit()

    r = client.get("/api/memory/timeline?days=7")
    row = next(t for t in r.get_json()["timeline"] if t["date"] == "2026-05-14")
    assert row["run_status"] == "done"
    assert row["completed_chats"] == 2
    assert row["failed_chats"] == 0
    assert row["run_error"] == ""

    r = client.get("/api/memory/day/2026-05-14?slot=08%3A00")
    run = r.get_json()["summary_run"]
    assert run["summary_status"] == "done"
    assert run["completed_chats"] == 2
    assert run["failed_chats"] == 0
    assert run["error"] == ""


def test_memory_events_get_empty(client):
    r = client.get("/api/memory/events")
    assert r.status_code == 200
    assert r.get_json()["events"] == []


def test_memory_events_post_then_get(client):
    r = client.post("/api/memory/events", json={"title": "Test event", "description": "x"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["status"] == "created"
    assert "id" in body

    r = client.get("/api/memory/events")
    events = r.get_json()["events"]
    assert any(e["title"] == "Test event" for e in events)


def test_memory_events_post_requires_title(client):
    r = client.post("/api/memory/events", json={"description": "no title"})
    assert r.status_code == 400


def test_memory_notes_post_requires_content(client):
    r = client.post("/api/memory/notes", json={"tags": "x"})
    assert r.status_code == 400


def test_memory_notes_roundtrip(client):
    r = client.post("/api/memory/notes", json={"content": "hello note"})
    assert r.status_code == 200
    note_id = r.get_json()["id"]

    r = client.get("/api/memory/notes")
    assert any(n["id"] == note_id for n in r.get_json()["notes"])


def test_memory_search_empty_query(client):
    r = client.get("/api/memory/search?q=")
    assert r.status_code == 200
    data = r.get_json()
    assert data == {"summaries": [], "events": [], "notes": []}


def test_memory_summaries_delete_requires_arg(client):
    r = client.delete("/api/memory/summaries")
    assert r.status_code == 400


def test_memory_export_returns_json(client):
    r = client.get("/api/memory/export")
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith("application/json")
    payload = json.loads(r.get_data(as_text=True))
    assert "summaries" in payload
    assert "events" in payload
    assert "notes" in payload


def test_memory_archive_requires_messages(client):
    r = client.post("/api/memory/archive", json={"chat_id": "x", "messages": []})
    assert r.status_code == 400


def test_memory_archive_writes_messages(client):
    msgs = [{"id": 1, "date": "2026-04-25T10:00:00+00:00",
             "from": "A", "text": "hi", "media": ""}]
    r = client.post("/api/memory/archive",
                    json={"chat_id": "test_chat", "chat_name": "Test",
                          "messages": msgs})
    assert r.status_code == 200
    assert r.get_json()["status"] == "ok"
    assert r.get_json()["archived"] >= 1


def test_memory_sentiment_empty(client):
    r = client.get("/api/memory/sentiment")
    assert r.status_code == 200
    data = r.get_json()
    assert "sentiment" in data
    assert "trend" in data


# ---------------------------------------------------------------------------
# Coin blueprint
# ---------------------------------------------------------------------------

def test_coin_search_requires_query(client):
    r = client.get("/api/coin/search")
    assert r.status_code == 400


def test_coin_search_empty_db(client):
    r = client.get("/api/coin/search?q=PEPE")
    assert r.status_code == 200
    data = r.get_json()
    assert data["query"] == "PEPE"


def test_coin_profiles_get_empty(client):
    r = client.get("/api/coin_profiles")
    assert r.status_code == 200
    assert r.get_json()["profiles"] == []


def test_coin_profiles_post_requires_symbol(client):
    r = client.post("/api/coin_profiles", json={"chain": "base"})
    assert r.status_code == 400


def test_coin_profile_create_get_delete(client):
    r = client.post("/api/coin_profiles", json={"symbol": "PEPE", "chain": "ETH"})
    assert r.status_code == 200
    pid = r.get_json()["id"]

    r = client.get(f"/api/coin_profiles/{pid}")
    assert r.status_code == 200
    assert r.get_json()["profile"]["symbol"] == "PEPE"

    r = client.put(f"/api/coin_profiles/{pid}", json={"narrative": "v2"})
    assert r.status_code == 200
    assert r.get_json()["profile"]["narrative"] == "v2"

    r = client.delete(f"/api/coin_profiles/{pid}")
    assert r.status_code == 200

    r = client.get(f"/api/coin_profiles/{pid}")
    assert r.status_code == 404


def test_coin_profile_status_validation(client):
    r = client.post("/api/coin_profiles", json={"symbol": "PEPE", "status": "invalid"})
    assert r.status_code == 400


def test_coin_profile_create_merges_exact_ca(client):
    ca = "0xAbC123"
    r = client.post("/api/coin_profiles", json={
        "symbol": "PEPE",
        "ca": ca.lower(),
        "narrative": "keep me",
        "my_raw_notes": "[old]\nold note",
    })
    assert r.status_code == 200
    first = r.get_json()
    assert first["merged"] is False

    r = client.post("/api/coin_profiles", json={
        "symbol": "PEPE",
        "ca": ca,
        "narrative": "",
        "my_lesson": "new lesson",
        "my_raw_notes": "[new]\nnew note",
    })
    assert r.status_code == 200
    second = r.get_json()
    assert second["merged"] is True
    assert second["id"] == first["id"]
    assert second["profile"]["narrative"] == "keep me"
    assert second["profile"]["my_lesson"] == "new lesson"
    assert second["profile"]["my_raw_notes"] == "[new]\nnew note\n\n[old]\nold note"

    r = client.get("/api/coin_profiles")
    assert len(r.get_json()["profiles"]) == 1


def test_coin_holders_infers_wallet_log_holders(client):
    ca = "0x1234567890abcdef1234567890abcdef12345678"
    wallet_a = "0x" + "d" * 40
    wallet_b = "0x" + "e" * 40
    wallet_c = "0x" + "f" * 40
    buy_text = f"""🆕🟢 [BUY HAMILTON](https://etherscan.io/tx/0xabc) (ETHEREUM)

🔹[**Whale.eth**](https://etherscan.io/address/{wallet_a}) **Smart Whale**

swapped **0.5** ($1,200) [**ETH**](https://etherscan.io/token/0xeee) for **1,000,000** ($1,200) [**HAMILTON**](https://etherscan.io/token/0xfff)

💰 **#HAMILTON** | **MC**: $1.2M | **LQ**: $45K | **Seen**: 2h: [link]

`{ca}`"""
    sell_zero_holds_text = f"""🔴 [SELL HAMILTON](https://etherscan.io/tx/0x123) (ETHEREUM)

🔹[**Zero.eth**](https://etherscan.io/address/{wallet_c}) **Zero Holder**

swapped **1,000,000** ($900) [**HAMILTON**](...) for **0.3** ($900) [**ETH**](...)

Sold: 100% | 📈PnL: **$-100** (-10.0%)
Holds: 0.00 (0.00%)

💰 **#HAMILTON** | **MC**: $2.0M | **Seen**: 3h: [link]

`{ca}`"""
    sell_text = f"""🔴 [SELL HAMILTON](https://etherscan.io/tx/0xdef) (ETHEREUM)

🔹[**Paper.eth**](https://etherscan.io/address/{wallet_b}) **Paper Wallet**

swapped **1,000,000** ($2,000) [**HAMILTON**](...) for **1.0** ($2,000) [**ETH**](...)

Sold: 100%

💰 **#HAMILTON** | **MC**: $2.0M | **Seen**: 3h: [link]

`{ca}`"""

    import db
    with db.get_db_ctx() as conn:
      cat_id = conn.execute("""
          INSERT INTO chat_categories (name, prompt_profile)
          VALUES ('Wallets', 'wallet_log')
      """).lastrowid
      conn.execute(
          "INSERT INTO chat_category_map (chat_id, category_id) VALUES (?, ?)",
          ("wallet-chat", cat_id),
      )
      conn.execute("""
          INSERT INTO messages
          (msg_id, date, chat_id, chat_name, sender_name, text)
          VALUES (?, ?, ?, ?, ?, ?)
      """, (1, "2026-05-18 10:00:00", "wallet-chat", "Ray Orange", "bot", buy_text))
      conn.execute("""
          INSERT INTO messages
          (msg_id, date, chat_id, chat_name, sender_name, text)
          VALUES (?, ?, ?, ?, ?, ?)
      """, (2, "2026-05-18 11:00:00", "wallet-chat", "Ray Orange", "bot", sell_text))
      conn.execute("""
          INSERT INTO messages
          (msg_id, date, chat_id, chat_name, sender_name, text)
          VALUES (?, ?, ?, ?, ?, ?)
      """, (3, "2026-05-18 12:00:00", "wallet-chat", "Ray Orange", "bot", sell_zero_holds_text))
      conn.commit()

    r = client.get(f"/api/coin/holders?ca={ca}&days=3650")
    assert r.status_code == 200
    data = r.get_json()
    assert data["holder_count"] == 1
    assert data["exited_count"] == 2
    assert data["holders"][0]["wallet_name"] == "Smart Whale"
    exited_names = {w["wallet_name"] for w in data["exited"]}
    assert "Paper Wallet" in exited_names
    assert "Zero Holder" in exited_names


def test_memory_auto_summary_retry_requires_date_and_slot(client):
    r = client.post("/api/memory/auto_summary/retry", json={})
    assert r.status_code == 400


def test_memory_auto_summary_retry_missing_slot_run(client, monkeypatch):
    monkeypatch.setattr("routes.memory.ai.ai_available", lambda: True)
    r = client.post("/api/memory/auto_summary/retry", json={
        "date": "2026-05-17",
        "slot": "16:00",
    })
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Settings blueprint
# ---------------------------------------------------------------------------

def test_chat_categories_get_empty(client):
    r = client.get("/api/chat_categories")
    assert r.status_code == 200
    data = r.get_json()
    assert data["categories"] == []
    # profiles list comes from ai.PROFILES — should be non-empty
    assert len(data["profiles"]) >= 1


def test_chat_categories_create_lifecycle(client):
    r = client.post("/api/chat_categories",
                    json={"name": "Alpha", "color": "#fff"})
    assert r.status_code == 200
    cat_id = r.get_json()["id"]

    r = client.put(f"/api/chat_categories/{cat_id}", json={"name": "Beta"})
    assert r.status_code == 200

    r = client.delete(f"/api/chat_categories/{cat_id}")
    assert r.status_code == 200


def test_watchlist_lifecycle(client):
    r = client.get("/api/watchlist")
    assert r.status_code == 200
    assert r.get_json()["keywords"] == []

    r = client.post("/api/watchlist", json={"keyword": "PEPE", "category": "test"})
    assert r.status_code == 200
    kw_id = r.get_json()["id"]

    r = client.get("/api/watchlist")
    assert any(k["id"] == kw_id for k in r.get_json()["keywords"])

    r = client.get(f"/api/watchlist/hits?id={kw_id}")
    assert r.status_code == 200

    r = client.delete(f"/api/watchlist?id={kw_id}")
    assert r.status_code == 200


def test_trusted_senders_lifecycle(client):
    r = client.get("/api/trusted_senders")
    assert r.status_code == 200
    assert r.get_json()["senders"] == []

    r = client.post("/api/trusted_senders",
                    json={"sender_id": 12345, "name": "TestKOL", "trust_level": "trusted"})
    assert r.status_code == 200

    r = client.put("/api/trusted_senders",
                   json={"sender_id": 12345, "trust_level": "noise"})
    assert r.status_code == 200

    r = client.delete("/api/trusted_senders?sender_id=12345")
    assert r.status_code == 200


def test_trusted_senders_invalid_trust_level(client):
    r = client.post("/api/trusted_senders",
                    json={"sender_id": 1, "name": "X", "trust_level": "bogus"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Watchtower blueprint
# ---------------------------------------------------------------------------

def test_watchtower_entities_empty(client):
    r = client.get("/api/watchtower/entities?days=7")
    assert r.status_code == 200
    data = r.get_json()
    assert data["entities"] == []
    assert data["window_days"] == 7


def test_watchtower_entity_mentions_requires_value(client):
    r = client.get("/api/watchtower/entity_mentions")
    assert r.status_code == 400


def test_watchtower_entity_brief_get_missing(client):
    r = client.get("/api/watchtower/entity_brief?value=NOPE&kind=symbol")
    assert r.status_code == 200
    assert r.get_json()["brief"] is None


def test_watchtower_entity_brief_get_requires_value(client):
    r = client.get("/api/watchtower/entity_brief")
    assert r.status_code == 400
