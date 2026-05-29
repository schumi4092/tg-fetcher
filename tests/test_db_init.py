"""init_db must be idempotent and produce the expected schema."""

import sqlite3
import db


EXPECTED_TABLES = {
    "daily_summaries", "events", "notes", "watchlist",
    "sentiment_scores", "chat_categories", "chat_category_map",
    "coin_profiles", "entity_briefs", "trusted_senders",
    "app_state", "embeddings", "messages", "message_summary_links",
    "auto_summary_runs",
}

EXPECTED_FTS = {"summaries_fts", "events_fts", "notes_fts", "messages_fts"}


def _all_tables(path):
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def test_init_db_creates_all_tables(fresh_db):
    db.init_db()
    tables = _all_tables(fresh_db)
    missing = EXPECTED_TABLES - tables
    assert not missing, f"missing tables: {missing}"
    fts_missing = EXPECTED_FTS - tables
    assert not fts_missing, f"missing fts tables: {fts_missing}"


def test_init_db_idempotent(fresh_db):
    """Running init_db twice in a row must not error or duplicate data."""
    db.init_db()
    # Insert one row of state to verify it survives a second init
    with db.get_db_ctx() as conn:
        conn.execute(
            "INSERT INTO chat_categories (name, color) VALUES (?, ?)",
            ("test_cat", "#fff"),
        )
        conn.commit()
    db.init_db()  # should not crash, should not wipe
    with db.get_db_ctx() as conn:
        row = conn.execute(
            "SELECT name FROM chat_categories WHERE name = 'test_cat'"
        ).fetchone()
    assert row is not None, "second init_db wiped existing data"


def test_init_db_pragmas_applied(fresh_db):
    """Verify connections come back with the expected PRAGMAs set."""
    db.init_db()
    with db.get_db_ctx() as conn:
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1
        # busy_timeout should be > 0 after our PRAGMA fix
        bt = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert bt >= 1000, f"busy_timeout too low: {bt}"
        jm = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert jm.lower() == "wal"


def test_schema_version_advances_and_holds(fresh_db):
    """schema_version must persist across init_db calls and not double-apply."""
    db.init_db()
    with db.get_db_ctx() as conn:
        v1 = conn.execute(
            "SELECT value FROM app_state WHERE key = 'schema_version'"
        ).fetchone()[0]
    db.init_db()
    with db.get_db_ctx() as conn:
        v2 = conn.execute(
            "SELECT value FROM app_state WHERE key = 'schema_version'"
        ).fetchone()[0]
    assert v1 == v2
    assert int(v1) >= 1


def test_expected_indexes_exist(fresh_db):
    """All migration-added indexes should exist after init_db."""
    db.init_db()
    expected = {
        "idx_daily_summaries_date",
        "idx_daily_summaries_chat_date",
        "idx_daily_summaries_source",
        "idx_daily_summaries_slot",
        "idx_events_date",
        "idx_events_source_summary",
        "idx_msg_summary_links_summary",
        "idx_msg_summary_links_message",
        "idx_auto_summary_runs_status",
        "idx_notes_date",
        "idx_sentiment_date",
    }
    with db.get_db_ctx() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    actual = {r[0] for r in rows}
    missing = expected - actual
    assert not missing, f"missing indexes: {missing}"


def test_migration_v2_my_raw_notes_present(fresh_db):
    """v2 ensures coin_profiles.my_raw_notes exists (idempotent on fresh DB)."""
    db.init_db()
    with db.get_db_ctx() as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(coin_profiles)")}
    assert "my_raw_notes" in cols


def test_daily_summaries_supports_multiple_slots_per_day(fresh_db):
    """Auto summaries should store separate 10:00 and 22:00 rows."""
    db.init_db()
    with db.get_db_ctx() as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(daily_summaries)")}
        assert {"summary_slot", "period_start", "period_end"} <= cols
        conn.execute("""
            INSERT INTO daily_summaries
            (date, chat_id, chat_name, hours, message_count, summary, summary_slot)
            VALUES ('2026-05-05', 'chat_x', 'Test', 12, 1, 'morning', '10:00')
        """)
        conn.execute("""
            INSERT INTO daily_summaries
            (date, chat_id, chat_name, hours, message_count, summary, summary_slot)
            VALUES ('2026-05-05', 'chat_x', 'Test', 12, 1, 'evening', '22:00')
        """)
        conn.commit()
        count = conn.execute("""
            SELECT COUNT(*) FROM daily_summaries
            WHERE date = '2026-05-05' AND chat_id = 'chat_x'
        """).fetchone()[0]
    assert count == 2


def test_messages_can_link_to_multiple_summaries(fresh_db):
    db.init_db()
    msg = {"id": 1, "date": "2026-05-05T00:00:00+00:00",
           "from": "A", "text": "hello", "media": ""}
    with db.get_db_ctx() as conn:
        s1 = conn.execute("""
            INSERT INTO daily_summaries
            (date, chat_id, chat_name, hours, message_count, summary, summary_slot)
            VALUES ('2026-05-05', 'chat_x', 'Test', 8, 1, 's1', '08:00')
        """).lastrowid
        s2 = conn.execute("""
            INSERT INTO daily_summaries
            (date, chat_id, chat_name, hours, message_count, summary, summary_slot)
            VALUES ('2026-05-05', 'chat_x', 'Test', 8, 1, 's2', '16:00')
        """).lastrowid
        db.save_messages_for_summary(conn, [msg], "chat_x", "Test", summary_id=s1)
        db.save_messages_for_summary(conn, [msg], "chat_x", "Test", summary_id=s2)
        conn.commit()
        links = conn.execute("""
            SELECT summary_id FROM message_summary_links ORDER BY summary_id
        """).fetchall()
    assert [r["summary_id"] for r in links] == [s1, s2]


def test_daily_summaries_legacy_unique_migrates_to_slot_unique(fresh_db):
    """Upgrade DBs that still have UNIQUE(date, chat_id)."""
    conn = sqlite3.connect(fresh_db)
    conn.execute("""
        CREATE TABLE daily_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            chat_name TEXT,
            hours REAL,
            message_count INTEGER,
            summary TEXT NOT NULL,
            raw_messages TEXT,
            summary_json TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            source TEXT DEFAULT 'manual',
            UNIQUE(date, chat_id)
        )
    """)
    conn.execute("""
        INSERT INTO daily_summaries
        (date, chat_id, chat_name, hours, message_count, summary, source, created_at)
        VALUES ('2026-05-05', 'chat_x', 'Test', 12, 1, 'old', 'auto',
                '2026-05-05 10:12:00')
    """)
    conn.execute("CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT)")
    conn.execute("INSERT INTO app_state (key, value) VALUES ('schema_version', '2')")
    conn.commit()
    conn.close()

    db.init_db()
    with db.get_db_ctx() as conn:
        row = conn.execute("""
            SELECT summary_slot FROM daily_summaries
            WHERE date = '2026-05-05' AND chat_id = 'chat_x'
        """).fetchone()
        assert row["summary_slot"] == "10:12"
        conn.execute("""
            INSERT INTO daily_summaries
            (date, chat_id, chat_name, hours, message_count, summary, summary_slot)
            VALUES ('2026-05-05', 'chat_x', 'Test', 12, 1, 'new', '22:00')
        """)
        conn.commit()
        count = conn.execute("""
            SELECT COUNT(*) FROM daily_summaries
            WHERE date = '2026-05-05' AND chat_id = 'chat_x'
        """).fetchone()[0]
    assert count == 2


def test_add_column_if_missing_skips_existing(fresh_db):
    """The migration helper should not error when column already exists."""
    db.init_db()  # adds my_raw_notes
    # Re-running init_db simulates calling the migration step twice
    db.init_db()
    with db.get_db_ctx() as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(coin_profiles)")}
    assert "my_raw_notes" in cols


def test_add_column_if_missing_actually_adds(fresh_db, monkeypatch):
    """If a fresh DB hasn't had v2 yet, the migration adds the column."""
    import sqlite3
    # Build a coin_profiles table WITHOUT my_raw_notes (simulates pre-v2 DB)
    conn = sqlite3.connect(fresh_db)
    conn.execute("""
        CREATE TABLE coin_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            ca TEXT DEFAULT ''
        )
    """)
    conn.execute("CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT)")
    conn.commit()
    cur = conn.cursor()

    # Run the v2 step directly
    step = db._add_column_if_missing("coin_profiles", "my_raw_notes", "TEXT DEFAULT ''")
    step(cur)
    conn.commit()

    cols = {row[1] for row in cur.execute("PRAGMA table_info(coin_profiles)")}
    assert "my_raw_notes" in cols
    conn.close()


def test_save_messages_idempotent(fresh_db):
    """Re-importing same messages must not duplicate rows."""
    db.init_db()
    msgs = [
        {"id": 1, "date": "2026-04-25T10:00:00+00:00", "from": "Alice",
         "username": "alice", "sender_id": 100, "text": "hi", "media": ""},
        {"id": 2, "date": "2026-04-25T10:01:00+00:00", "from": "Bob",
         "username": "bob", "sender_id": 200, "text": "hey", "media": ""},
    ]
    with db.get_db_ctx() as conn:
        new1, total1 = db.save_messages_for_summary(conn, msgs, "chat_x", "TestChat")
        conn.commit()
        new2, total2 = db.save_messages_for_summary(conn, msgs, "chat_x", "TestChat")
        conn.commit()
    assert new1 == 2
    assert new2 == 0, "second insert should report 0 new rows"
    with db.get_db_ctx() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id = 'chat_x'"
        ).fetchone()[0]
    assert count == 2
