"""Auto-summary message linkage behavior."""

import db


def test_auto_summary_can_reuse_manually_linked_message_but_not_auto_link(fresh_db, monkeypatch):
    db.init_db()
    import ai

    monkeypatch.setattr(ai, "ai_available", lambda: True)
    monkeypatch.setattr(ai, "build_coin_profile_context", lambda msgs, **k: "")
    monkeypatch.setattr(ai, "build_history_context", lambda chat_id, **k: "")
    monkeypatch.setattr(ai, "update_rule_hits", lambda *a, **k: None)
    monkeypatch.setattr(ai, "_run_auto_summary_post_hooks", lambda *a, **k: None)

    def fake_stream(*args, **kwargs):
        yield {"type": "token", "token": "auto summary"}
        yield {"type": "done", "text": "auto summary", "usage": None}

    monkeypatch.setattr(ai, "ai_stream", fake_stream)
    monkeypatch.setattr(ai, "with_watchdog", lambda source, **kwargs: source)

    msg = {
        "id": 101,
        "date": "2026-05-05T01:00:00+00:00",
        "from": "Alice",
        "username": "alice",
        "sender_id": 1,
        "text": "$TEST looks active",
        "media": "",
    }

    with db.get_db_ctx() as conn:
        manual_id = conn.execute("""
            INSERT INTO daily_summaries
            (date, chat_id, chat_name, hours, message_count, summary, summary_slot, source)
            VALUES ('2026-05-05', 'chat_x', 'Chat X', 8, 1, 'manual', '', 'manual')
        """).lastrowid
        db.save_messages_for_summary(conn, [msg], "chat_x", "Chat X", summary_id=manual_id)
        conn.commit()

    summary_id, status = ai.summarize_chat_auto(
        "chat_x", "Chat X", hours=8,
        since_iso="2026-05-05T00:00:00+00:00",
        until_iso="2026-05-05T08:00:00+00:00",
        summary_date="2026-05-05",
        summary_slot="08:00",
    )
    assert status == "ok"
    assert summary_id

    # The same message is allowed to belong to the manual summary and the
    # scheduled auto slot.
    with db.get_db_ctx() as conn:
        links = conn.execute("""
            SELECT l.summary_id, ds.source
            FROM message_summary_links l
            JOIN daily_summaries ds ON ds.id = l.summary_id
            ORDER BY l.summary_id
        """).fetchall()
    assert [r["source"] for r in links] == ["manual", "auto"]

    # A second auto slot over the same source window should skip the message
    # because it is already covered by an auto summary.
    summary_id2, status2 = ai.summarize_chat_auto(
        "chat_x", "Chat X", hours=8,
        since_iso="2026-05-05T00:00:00+00:00",
        until_iso="2026-05-05T08:00:00+00:00",
        summary_date="2026-05-05",
        summary_slot="16:00",
    )
    assert summary_id2 is None
    assert status2 == "skipped_no_messages"
