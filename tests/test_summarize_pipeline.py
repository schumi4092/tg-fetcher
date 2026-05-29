"""Tests for SummarizePipeline.

We never call the real AI here — instead we monkeypatch the ai_backend +
ai module bits the pipeline depends on so each phase can be exercised
in isolation. The point is to lock in the event order and the state
transitions, not to test Claude.
"""

import pytest

import db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_ai_backend(monkeypatch):
    """Patch ai_backend so .ai_available() = True and .ai_stream() yields a
    canned 2-token then done sequence."""
    import ai_backend

    monkeypatch.setattr(ai_backend, "ai_available", lambda: True)
    monkeypatch.setattr(ai_backend, "backend_name", lambda: "test")

    def fake_stream(*args, **kwargs):
        yield {"type": "token", "token": "hello "}
        yield {"type": "token", "token": "world"}
        yield {"type": "done", "text": "hello world", "usage": None}

    def fake_with_watchdog(source, **kwargs):
        # Pass the source through unchanged for tests.
        yield from source

    monkeypatch.setattr(ai_backend, "ai_stream", fake_stream)
    monkeypatch.setattr(ai_backend, "with_watchdog", fake_with_watchdog)
    return ai_backend


@pytest.fixture
def fake_ai(monkeypatch):
    """Patch the ai module so save / events / sentiment / embedding helpers
    are all no-ops returning predictable values."""
    import ai

    monkeypatch.setattr(ai, "_log_cost", lambda *a, **k: None)
    monkeypatch.setattr(ai, "build_coin_profile_context", lambda msgs, **k: "")
    monkeypatch.setattr(ai, "build_history_context", lambda chat_id, **k: "(none)")
    monkeypatch.setattr(ai, "save_daily_summary",
                        lambda *a, **k: (42, "2026-04-25"))
    monkeypatch.setattr(ai, "ai_extract_events", lambda summary, chat_name: [
        {"title": "test event", "description": "x"}
    ])
    monkeypatch.setattr(ai, "replace_summary_events",
                        lambda *a, **k: [{"title": "test event"}])
    monkeypatch.setattr(ai, "post_summarize",
                        lambda *a, **k: {"score": 5, "label": "中性"})
    monkeypatch.setattr(ai, "submit_summary_json_extract", lambda *a, **k: None)
    return ai


@pytest.fixture
def sample_messages():
    return [
        {"id": 1, "date": "2026-04-25T10:00:00+00:00",
         "from": "Alice", "username": "alice", "sender_id": 1,
         "text": "PEPE looks good", "media": ""},
        {"id": 2, "date": "2026-04-25T10:01:00+00:00",
         "from": "Bob", "username": "bob", "sender_id": 2,
         "text": "agree", "media": ""},
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_pipeline_validate_halts_when_ai_unavailable(fresh_db, sample_messages, monkeypatch):
    db.init_db()
    import ai_backend
    monkeypatch.setattr(ai_backend, "ai_available", lambda: False)

    from routes._summarize_pipeline import SummarizePipeline
    pipe = SummarizePipeline(sample_messages, "chat_x", "TestChat",
                             hours=8, model_key="sonnet", save_to_memory=False)
    events = list(pipe.run())

    # First event: progress for "preparing messages"; then error from validate.
    assert events[0]["type"] == "progress"
    err = next(e for e in events if e["type"] == "error")
    assert "AI backend" in err["error"]


def test_pipeline_yields_tokens_in_order(fresh_db, sample_messages,
                                          fake_ai_backend, fake_ai):
    db.init_db()
    from routes._summarize_pipeline import SummarizePipeline
    pipe = SummarizePipeline(sample_messages, "chat_x", "TestChat",
                             hours=8, model_key="sonnet", save_to_memory=False)
    events = list(pipe.run())

    tokens = [e["token"] for e in events if e["type"] == "token"]
    assert tokens == ["hello ", "world"]
    final = [e for e in events if e["type"] == "done"]
    assert len(final) == 1
    assert final[0]["summary"] == "hello world"
    assert final[0]["saved"] is False


def test_pipeline_save_to_memory_runs_post_hooks(fresh_db, sample_messages,
                                                  fake_ai_backend, fake_ai):
    db.init_db()
    from routes._summarize_pipeline import SummarizePipeline
    pipe = SummarizePipeline(sample_messages, "chat_x", "TestChat",
                             hours=8, model_key="sonnet", save_to_memory=True)
    events = list(pipe.run())

    final = next(e for e in events if e["type"] == "done")
    assert final["saved"] is True
    assert final["summary_id"] == 42
    assert final["events"] == [{"title": "test event"}]
    assert final["sentiment"] == {"score": 5, "label": "中性"}
    # The phase progress msgs should appear in this order
    progress_msgs = [e["msg"] for e in events if e["type"] == "progress"]
    save_idx = next(i for i, m in enumerate(progress_msgs) if "存入記憶庫" in m)
    events_idx = next(i for i, m in enumerate(progress_msgs) if "提取關鍵事件" in m)
    sentiment_idx = next(i for i, m in enumerate(progress_msgs) if "情緒指標" in m)
    assert save_idx < events_idx < sentiment_idx


def test_pipeline_no_text_messages_returns_empty_done(fresh_db, fake_ai_backend, fake_ai):
    db.init_db()
    # Messages with no text — the early-exit path
    msgs = [{"id": 1, "date": "2026-04-25T10:00:00+00:00",
             "from": "Alice", "username": "", "sender_id": 1,
             "text": "", "media": ""}]
    from routes._summarize_pipeline import SummarizePipeline
    pipe = SummarizePipeline(msgs, "chat_x", "TestChat",
                             hours=8, model_key="sonnet", save_to_memory=True)
    events = list(pipe.run())
    final = events[-1]
    assert final["type"] == "done"
    assert final["saved"] is False
    assert "沒有文字訊息" in final["summary"]


def test_pipeline_stream_error_halts(fresh_db, sample_messages, fake_ai, monkeypatch):
    db.init_db()
    import ai_backend
    monkeypatch.setattr(ai_backend, "ai_available", lambda: True)
    monkeypatch.setattr(ai_backend, "backend_name", lambda: "test")

    def fake_stream_with_error(*args, **kwargs):
        yield {"type": "error", "error": "model rate limited"}

    monkeypatch.setattr(ai_backend, "ai_stream", fake_stream_with_error)
    monkeypatch.setattr(ai_backend, "with_watchdog", lambda src, **k: src)

    from routes._summarize_pipeline import SummarizePipeline
    pipe = SummarizePipeline(sample_messages, "chat_x", "TestChat",
                             hours=8, model_key="sonnet", save_to_memory=False)
    events = list(pipe.run())

    assert events[-1]["type"] == "error"
    assert "rate limited" in events[-1]["error"]


def test_pipeline_resolves_profile_from_chat_category(fresh_db, sample_messages,
                                                       fake_ai_backend, fake_ai):
    """When chat has a wallet_log category mapping, profile flips to wallet_log."""
    db.init_db()
    with db.get_db_ctx() as conn:
        conn.execute("""
            INSERT INTO chat_categories (name, color, prompt_profile)
            VALUES ('wallet', '#000', 'wallet_log')
        """)
        cat_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO chat_category_map (chat_id, category_id) VALUES (?, ?)",
            ("chat_wallet", cat_id),
        )
        conn.commit()

    from routes._summarize_pipeline import SummarizePipeline
    pipe = SummarizePipeline(sample_messages, "chat_wallet", "WalletLog",
                             hours=8, model_key="sonnet", save_to_memory=False)
    events = list(pipe.run())
    assert pipe.profile_name == "wallet_log"
    # wallet_log path emits "錢包事件聚合" progress msg
    progress_msgs = [e.get("msg", "") for e in events if e["type"] == "progress"]
    assert any("錢包事件聚合" in m for m in progress_msgs)
