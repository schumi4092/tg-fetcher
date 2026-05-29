"""SQLite memory store: schema, connection context, helpers, import routines."""

import base64
import gzip
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

from config import DB_PATH, logger


TAIPEI_TZ = timezone(timedelta(hours=8))


def to_taipei_str(value, fmt="%Y-%m-%d %H:%M"):
    """Render any ISO-ish datetime (or `datetime`) as UTC+8 text.

    Naive strings/datetimes are assumed to already be UTC+8 — they're returned
    formatted but unshifted. Aware values (e.g. Telethon's `+00:00` UTC) are
    converted to Asia/Taipei first. Returns "" if the value can't be parsed.
    """
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return text[:16]
    if dt.tzinfo is None:
        return dt.strftime(fmt)
    return dt.astimezone(TAIPEI_TZ).strftime(fmt)


def _connect_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")
    # 5s busy_timeout: short SQLite reader/writer waits don't immediately raise
    # OperationalError; long enough that genuinely-stuck callers still surface.
    conn.execute("PRAGMA busy_timeout = 5000")
    # synchronous=NORMAL is the recommended pairing with WAL: durable on
    # process crash, only loses last txn on full power loss. Big speedup
    # on the per-message inserts done by save_messages_for_summary.
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def get_db_ctx():
    """Context manager for DB connections — guarantees close on exit."""
    conn = _connect_db()
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _daily_summaries_create_sql(table_name="daily_summaries"):
    return f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            chat_name TEXT,
            hours REAL,
            message_count INTEGER,
            summary TEXT NOT NULL,
            raw_messages TEXT,
            summary_json TEXT,
            summary_slot TEXT NOT NULL DEFAULT '',
            period_start TEXT DEFAULT '',
            period_end TEXT DEFAULT '',
            source TEXT DEFAULT 'manual',
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            UNIQUE(date, chat_id, summary_slot)
        )
    """


def _unique_index_columns(c, table):
    uniques = []
    try:
        indexes = c.execute(f"PRAGMA index_list({table})").fetchall()
    except Exception:
        return uniques
    for idx in indexes:
        if not idx[2]:
            continue
        name = idx[1]
        cols = [r[2] for r in c.execute(f"PRAGMA index_info({name})").fetchall()]
        uniques.append(cols)
    return uniques


def _migrate_daily_summaries_slot_schema(c):
    """Rebuild daily_summaries so auto runs can store one row per slot.

    Older DBs have UNIQUE(date, chat_id), which forces the 22:00 run to append
    into the 10:00 row. SQLite cannot drop that unique constraint in place, so
    we copy into a replacement table with UNIQUE(date, chat_id, summary_slot).
    """
    cols = {row[1] for row in c.execute("PRAGMA table_info(daily_summaries)")}
    unique_cols = _unique_index_columns(c, "daily_summaries")
    desired = ["date", "chat_id", "summary_slot"]
    legacy = ["date", "chat_id"]
    missing = {"summary_slot", "period_start", "period_end"} - cols
    if not missing and desired in unique_cols and legacy not in unique_cols:
        return False

    logger.info("Migrating daily_summaries to per-slot rows...")
    c.execute("PRAGMA foreign_keys=OFF")
    for trig in ("daily_summaries_ai", "daily_summaries_ad", "daily_summaries_au"):
        c.execute(f"DROP TRIGGER IF EXISTS {trig}")
    c.execute("DROP TABLE IF EXISTS daily_summaries_new")
    c.execute(_daily_summaries_create_sql("daily_summaries_new"))

    def expr(col, default):
        return col if col in cols else default

    source_expr = expr("source", "'manual'")
    if "summary_slot" in cols:
        slot_expr = "COALESCE(summary_slot, '')"
    else:
        slot_expr = (
            "CASE WHEN COALESCE("
            f"{source_expr}, 'manual') = 'auto' "
            "AND length(COALESCE(created_at, '')) >= 16 "
            "THEN substr(created_at, 12, 5) ELSE '' END"
        )

    c.execute(f"""
        INSERT INTO daily_summaries_new
        (id, date, chat_id, chat_name, hours, message_count, summary,
         raw_messages, summary_json, summary_slot, period_start, period_end,
         source, created_at)
        SELECT
            id,
            date,
            chat_id,
            chat_name,
            hours,
            message_count,
            summary,
            {expr("raw_messages", "NULL")},
            {expr("summary_json", "NULL")},
            {slot_expr},
            {expr("period_start", "''")},
            {expr("period_end", "''")},
            {source_expr},
            {expr("created_at", "datetime('now', 'localtime')")}
        FROM daily_summaries
    """)
    c.execute("DROP TABLE daily_summaries")
    c.execute("ALTER TABLE daily_summaries_new RENAME TO daily_summaries")
    try:
        c.execute("DELETE FROM sqlite_sequence WHERE name = 'daily_summaries'")
        c.execute("""
            INSERT INTO sqlite_sequence(name, seq)
            SELECT 'daily_summaries', COALESCE(MAX(id), 0)
            FROM daily_summaries
        """)
    except Exception:
        pass
    c.execute("DROP TABLE IF EXISTS summaries_fts")
    c.execute("PRAGMA foreign_keys=ON")

    c.execute("CREATE INDEX IF NOT EXISTS idx_daily_summaries_date ON daily_summaries(date)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_daily_summaries_chat_date ON daily_summaries(chat_id, date)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_daily_summaries_source ON daily_summaries(source)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_daily_summaries_slot ON daily_summaries(date, summary_slot)")
    return True


def init_db():
    """Initialize database schema, triggers, and FTS indexes."""
    conn = _connect_db()
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    c = conn.cursor()

    # NOTE on raw_messages vs messages: raw_messages is the gzip+base64 blob
    # written at summarize-time (see encode_raw_messages) so we never lose the
    # source material even before it's indexed. The `messages` table (below)
    # is populated by /api/memory/archive and, on startup, a self-healing
    # migration (see "Self-healing migration" block) that decodes raw_messages
    # for any summaries whose messages haven't been imported yet. We keep both
    # on purpose: raw_messages is the durable source of truth during the brief
    # window between fetch and archive, `messages` is the query/FTS layer.
    c.execute(_daily_summaries_create_sql())
    # Self-healing: add summary_json to pre-existing daily_summaries tables.
    ds_cols = {row[1] for row in c.execute("PRAGMA table_info(daily_summaries)")}
    if "summary_json" not in ds_cols:
        c.execute("ALTER TABLE daily_summaries ADD COLUMN summary_json TEXT")
    # `source` distinguishes auto (background loop) from manual (UI button).
    # Default 'manual' for legacy rows since they predate auto-summarize.
    if "source" not in ds_cols:
        c.execute("ALTER TABLE daily_summaries ADD COLUMN source TEXT DEFAULT 'manual'")
    conn.commit()
    _migrate_daily_summaries_slot_schema(c)

    c.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            importance TEXT DEFAULT 'normal',
            tags TEXT,
            source_chat TEXT,
            source_summary_id INTEGER,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY (source_summary_id) REFERENCES daily_summaries(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            content TEXT NOT NULL,
            tags TEXT,
            related_event_id INTEGER,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY (related_event_id) REFERENCES events(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT NOT NULL UNIQUE,
            category TEXT DEFAULT '一般',
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS sentiment_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            chat_name TEXT,
            score REAL NOT NULL,
            label TEXT,
            chain_flow TEXT,
            meta_shift TEXT,
            risk_flag TEXT,
            summary_id INTEGER,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            UNIQUE(date, chat_id),
            FOREIGN KEY (summary_id) REFERENCES daily_summaries(id)
        )
    """)

    # Migration: add structural-signal columns to older sentiment_scores tables.
    sent_cols = {row[1] for row in c.execute("PRAGMA table_info(sentiment_scores)")}
    for col in ("chain_flow", "meta_shift", "risk_flag"):
        if col not in sent_cols:
            c.execute(f"ALTER TABLE sentiment_scores ADD COLUMN {col} TEXT")

    c.execute("""
        CREATE TABLE IF NOT EXISTS chat_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            color TEXT DEFAULT '#9a5b2a',
            sort_order INTEGER DEFAULT 0,
            prompt_profile TEXT DEFAULT 'group_chat',
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    # Self-healing: add prompt_profile to existing chat_categories tables.
    cat_cols = {row[1] for row in c.execute("PRAGMA table_info(chat_categories)")}
    if "prompt_profile" not in cat_cols:
        c.execute(
            "ALTER TABLE chat_categories ADD COLUMN prompt_profile TEXT DEFAULT 'group_chat'"
        )
        c.execute(
            "UPDATE chat_categories SET prompt_profile = 'group_chat' WHERE prompt_profile IS NULL"
        )

    c.execute("""
        CREATE TABLE IF NOT EXISTS chat_category_map (
            chat_id TEXT PRIMARY KEY,
            category_id INTEGER NOT NULL,
            updated_at TEXT DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY (category_id) REFERENCES chat_categories(id) ON DELETE CASCADE
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_chat_category_map_cat ON chat_category_map(category_id)")

    # Coin profile = long-form research dossier per coin (manual + AI-drafted).
    # Replaces the standalone watchlist as the durable per-asset memory: each
    # row aggregates auto-harvested context (narrative, timeline, KOL consensus,
    # smart-money flow) plus user-only fields (entry/exit, verdict, lesson,
    # archetype). See `coin_profiles` table — every coin you actually care about
    # gets one of these instead of being lost in daily summaries.
    c.execute("""
        CREATE TABLE IF NOT EXISTS coin_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            chain TEXT DEFAULT '',
            ca TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'tracking',
            narrative TEXT DEFAULT '',
            timeline_json TEXT DEFAULT '',
            kol_consensus TEXT DEFAULT '',
            smart_money_summary TEXT DEFAULT '',
            top_signal TEXT DEFAULT '',
            archetype TEXT DEFAULT '',
            my_entry_fdv TEXT DEFAULT '',
            my_entry_size TEXT DEFAULT '',
            my_exit_fdv TEXT DEFAULT '',
            my_exit_size TEXT DEFAULT '',
            my_pnl TEXT DEFAULT '',
            my_wallet TEXT DEFAULT '',
            my_verdict TEXT DEFAULT '',
            my_lesson TEXT DEFAULT '',
            my_raw_notes TEXT DEFAULT '',
            tags TEXT DEFAULT '',
            pinned INTEGER NOT NULL DEFAULT 0,
            first_seen_date TEXT DEFAULT '',
            last_updated TEXT DEFAULT (datetime('now', 'localtime')),
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_coin_profiles_symbol ON coin_profiles(symbol)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_coin_profiles_ca ON coin_profiles(ca)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_coin_profiles_status ON coin_profiles(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_coin_profiles_pinned ON coin_profiles(pinned)")

    # Trading rules = cross-coin guardrails distilled from individual recaps
    # (coin_profiles.my_lesson). One row per rule; injected into digest prompt
    # so the AI can flag ✓ 法則 #N or ⚠️ 違反 #N on each coin section. Hit
    # tracking via post-processing regex on the digest text.
    c.execute("""
        CREATE TABLE IF NOT EXISTS trading_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_text TEXT NOT NULL,
            reason TEXT DEFAULT '',
            scope TEXT NOT NULL DEFAULT 'general',
            status TEXT NOT NULL DEFAULT 'active',
            source_profile_id INTEGER DEFAULT NULL,
            hit_count INTEGER NOT NULL DEFAULT 0,
            last_hit_at TEXT DEFAULT '',
            pinned INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY (source_profile_id) REFERENCES coin_profiles(id) ON DELETE SET NULL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_trading_rules_status ON trading_rules(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_trading_rules_source ON trading_rules(source_profile_id)")

    # Cache for /api/watchtower/entity_brief — one row per entity. When the
    # user opens an entity in Watchtower, the frontend reads from here first
    # so the brief loads instantly; only regenerates on explicit refresh or
    # if the cache is missing. Saves the 30-60s LLM round-trip on revisits.
    c.execute("""
        CREATE TABLE IF NOT EXISTS entity_briefs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            value TEXT NOT NULL,
            brief_text TEXT NOT NULL,
            days_window INTEGER NOT NULL DEFAULT 14,
            summaries_count INTEGER DEFAULT 0,
            messages_count INTEGER DEFAULT 0,
            generated_at TEXT DEFAULT (datetime('now', 'localtime')),
            UNIQUE(kind, value)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_entity_briefs_lookup ON entity_briefs(kind, value)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS trusted_senders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER NOT NULL UNIQUE,
            name TEXT NOT NULL,
            username TEXT DEFAULT '',
            trust_level TEXT NOT NULL DEFAULT 'trusted',
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS app_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS embeddings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL,
            source_id INTEGER NOT NULL,
            chunk_text TEXT NOT NULL,
            embedding BLOB NOT NULL,
            norm REAL,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            UNIQUE(source_type, source_id)
        )
    """)

    # Migration: add `norm` to older embedding tables.
    cols = {row[1] for row in c.execute("PRAGMA table_info(embeddings)")}
    if "norm" not in cols:
        c.execute("ALTER TABLE embeddings ADD COLUMN norm REAL")

    # Per-message archive (populated by summarize + archive endpoints).
    # summary_id is nullable so archive-without-summarize still works.
    messages_table_existed = c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'"
    ).fetchone() is not None

    c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            summary_id INTEGER,
            msg_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            chat_name TEXT,
            sender_name TEXT,
            sender_username TEXT,
            sender_id INTEGER,
            topic_id INTEGER,
            text TEXT,
            media TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            UNIQUE(chat_id, msg_id),
            FOREIGN KEY (summary_id) REFERENCES daily_summaries(id) ON DELETE SET NULL
        )
    """)
    msg_cols = {row[1] for row in c.execute("PRAGMA table_info(messages)")}
    if "topic_id" not in msg_cols:
        c.execute("ALTER TABLE messages ADD COLUMN topic_id INTEGER")
    c.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_date ON messages(chat_id, date)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_topic_date ON messages(chat_id, topic_id, date)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_messages_summary ON messages(summary_id)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS message_summary_links (
            message_id INTEGER NOT NULL,
            summary_id INTEGER NOT NULL,
            link_type TEXT NOT NULL DEFAULT 'summary',
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            PRIMARY KEY (message_id, summary_id),
            FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE,
            FOREIGN KEY (summary_id) REFERENCES daily_summaries(id) ON DELETE CASCADE
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_msg_summary_links_summary ON message_summary_links(summary_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_msg_summary_links_message ON message_summary_links(message_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_msg_summary_links_summary_message ON message_summary_links(summary_id, message_id)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS auto_summary_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            slot TEXT NOT NULL,
            since_iso TEXT DEFAULT '',
            until_iso TEXT DEFAULT '',
            fetch_status TEXT DEFAULT 'pending',
            summary_status TEXT DEFAULT 'pending',
            started_at TEXT DEFAULT (datetime('now', 'localtime')),
            fetch_finished_at TEXT,
            summary_started_at TEXT,
            summary_finished_at TEXT,
            ok_count INTEGER DEFAULT 0,
            skip_existing_count INTEGER DEFAULT 0,
            skip_no_msgs_count INTEGER DEFAULT 0,
            failed_count INTEGER DEFAULT 0,
            error TEXT DEFAULT '',
            updated_at TEXT DEFAULT (datetime('now', 'localtime')),
            UNIQUE(date, slot)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_auto_summary_runs_status ON auto_summary_runs(date, slot)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS auto_summary_chat_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT NOT NULL,
            slot TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            chat_name TEXT DEFAULT '',
            profile TEXT DEFAULT '',
            status TEXT DEFAULT '',
            summary_id INTEGER,
            elapsed_secs REAL DEFAULT 0,
            prompt_len INTEGER DEFAULT 0,
            msg_text_len INTEGER DEFAULT 0,
            message_count INTEGER DEFAULT 0,
            fallback_used INTEGER DEFAULT 0,
            prep_mode TEXT DEFAULT '',
            stream_error TEXT DEFAULT '',
            started_at TEXT DEFAULT (datetime('now', 'localtime')),
            finished_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    chat_run_cols = {row[1] for row in c.execute("PRAGMA table_info(auto_summary_chat_runs)")}
    if "prep_mode" not in chat_run_cols:
        c.execute("ALTER TABLE auto_summary_chat_runs ADD COLUMN prep_mode TEXT DEFAULT ''")
    c.execute("CREATE INDEX IF NOT EXISTS idx_auto_summary_chat_runs_slot ON auto_summary_chat_runs(run_date, slot)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_auto_summary_chat_runs_chat ON auto_summary_chat_runs(chat_id, run_date)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_daily_summaries_source_date_slot ON daily_summaries(source, date, summary_slot)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_daily_summaries_chat_source_date_slot ON daily_summaries(chat_id, source, date, summary_slot)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS auto_topic_filters (
            chat_id TEXT NOT NULL,
            topic_id INTEGER NOT NULL,
            topic_title TEXT DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT DEFAULT (datetime('now', 'localtime')),
            PRIMARY KEY (chat_id, topic_id)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_auto_topic_filters_chat ON auto_topic_filters(chat_id, enabled)")

    # Migration: summaries_fts 原本只索引 summary 欄；若缺 chat_name 則 drop 重建。
    def _fts_has_column(table, col):
        try:
            return any(row[1] == col for row in c.execute(f"PRAGMA table_info({table})"))
        except Exception:
            return False

    summaries_fts_needs_rebuild = False
    if _fts_has_column("summaries_fts", "summary") and not _fts_has_column("summaries_fts", "chat_name"):
        c.execute("DROP TRIGGER IF EXISTS daily_summaries_ai")
        c.execute("DROP TRIGGER IF EXISTS daily_summaries_ad")
        c.execute("DROP TRIGGER IF EXISTS daily_summaries_au")
        c.execute("DROP TABLE IF EXISTS summaries_fts")
        summaries_fts_needs_rebuild = True
        logger.info("🔧 summaries_fts 加入 chat_name 欄位，重建中...")

    summaries_fts_exists = _fts_has_column("summaries_fts", "summary")
    events_fts_exists = _fts_has_column("events_fts", "title")
    notes_fts_exists = _fts_has_column("notes_fts", "content")
    messages_fts_exists = _fts_has_column("messages_fts", "text")

    c.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS summaries_fts USING fts5(
            summary, chat_name,
            content='daily_summaries',
            content_rowid='id'
        )
    """)
    c.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
            title, description, tags,
            content='events',
            content_rowid='id'
        )
    """)
    c.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
            content, tags,
            content='notes',
            content_rowid='id'
        )
    """)
    c.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            text, sender_name, chat_name,
            content='messages',
            content_rowid='id'
        )
    """)

    for tbl, fts, cols_csv in [
        ("daily_summaries", "summaries_fts", "summary, chat_name"),
        ("events", "events_fts", "title, description, tags"),
        ("notes", "notes_fts", "content, tags"),
        ("messages", "messages_fts", "text, sender_name, chat_name"),
    ]:
        col_list = cols_csv.split(", ")
        new_cols = ", ".join(f"new.{col}" for col in col_list)
        old_cols = ", ".join(f"old.{col}" for col in col_list)
        c.execute(f"""
            CREATE TRIGGER IF NOT EXISTS {tbl}_ai AFTER INSERT ON {tbl} BEGIN
                INSERT INTO {fts}(rowid, {cols_csv}) VALUES (new.id, {new_cols});
            END
        """)
        c.execute(f"""
            CREATE TRIGGER IF NOT EXISTS {tbl}_ad AFTER DELETE ON {tbl} BEGIN
                INSERT INTO {fts}({fts}, rowid, {cols_csv}) VALUES ('delete', old.id, {old_cols});
            END
        """)
        c.execute(f"""
            CREATE TRIGGER IF NOT EXISTS {tbl}_au AFTER UPDATE ON {tbl} BEGIN
                INSERT INTO {fts}({fts}, rowid, {cols_csv}) VALUES ('delete', old.id, {old_cols});
                INSERT INTO {fts}(rowid, {cols_csv}) VALUES (new.id, {new_cols});
            END
        """)

    # Rebuild only when FTS was just (re)created — triggers keep it in sync after that.
    if summaries_fts_needs_rebuild or not summaries_fts_exists:
        c.execute("INSERT INTO summaries_fts(summaries_fts) VALUES ('rebuild')")
    if not events_fts_exists:
        c.execute("INSERT INTO events_fts(events_fts) VALUES ('rebuild')")
    if not notes_fts_exists:
        c.execute("INSERT INTO notes_fts(notes_fts) VALUES ('rebuild')")
    # messages_fts is populated by the migration below (triggers do the work),
    # or by the trigger on new inserts once the table exists.
    if messages_table_existed and not messages_fts_exists:
        c.execute("INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')")

    # Self-healing migration: run when messages is empty AND there are summaries with raw_messages.
    # Covers both first-time creation and recovering from a failed prior migration.
    needs_migration = False
    try:
        if c.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0:
            has_raw = c.execute(
                "SELECT 1 FROM daily_summaries WHERE raw_messages IS NOT NULL LIMIT 1"
            ).fetchone()
            needs_migration = has_raw is not None
    except Exception:
        needs_migration = not messages_table_existed

    if needs_migration:
        logger.info("🔧 messages 表為空而 daily_summaries 有 raw,開始遷移歷史訊息...")
        summaries = c.execute(
            "SELECT id, date, chat_id, chat_name, raw_messages FROM daily_summaries"
        ).fetchall()
        migrated = 0
        for row in summaries:
            blob = row["raw_messages"]
            if not blob:
                continue
            msgs = decode_raw_messages(blob)
            for m in msgs:
                msg_id = m.get("id")
                if msg_id is None:
                    continue
                try:
                    cur = c.execute("""
                        INSERT OR IGNORE INTO messages
                        (summary_id, msg_id, date, chat_id, chat_name,
                         sender_name, sender_username, sender_id, topic_id, text, media)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (row["id"], msg_id, m.get("date") or row["date"],
                          str(row["chat_id"]), row["chat_name"],
                          m.get("from"), m.get("username"), m.get("sender_id"),
                          m.get("topic_id"),
                          m.get("text", "") or "", m.get("media", "") or ""))
                    migrated += cur.rowcount
                except Exception as e:
                    logger.warning("遷移訊息失敗 (summary=%s msg=%s): %s", row["id"], msg_id, e)
        logger.info("🔧 messages 遷移完成,共寫入 %d 則訊息。", migrated)

    _run_versioned_migrations(c)

    conn.commit()
    conn.close()
    logger.info("📦 資料庫已初始化: %s", DB_PATH)


# ---------------------------------------------------------------------------
# Versioned migrations
# ---------------------------------------------------------------------------
# Add new entries by appending (version, [step, ...]) where each step is
# either a SQL string or a callable taking the cursor. Never reorder or
# rewrite previous versions — each version runs at most once per DB and the
# committed version lives in app_state(key='schema_version').
# Existing init_db blocks above stay because they're idempotent self-healers
# for users upgrading from before this versioning system existed.


def _add_column_if_missing(table, column, type_decl):
    """Return a callable that ALTERs `table` to add `column` only if missing.

    SQLite has no `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` so we read
    PRAGMA table_info and skip if the column is already present. Idempotent
    — safe to re-run, safe on fresh DBs where CREATE TABLE already declared
    the column.
    """
    def _step(c):
        cols = {row[1] for row in c.execute(f"PRAGMA table_info({table})")}
        if column in cols:
            return
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_decl}")
    return _step


_MIGRATIONS = [
    (1, [
        # Indexes that were missing — date-range scans on these tables are
        # used everywhere (timeline, watchtower, ai_memory_ask, etc.).
        "CREATE INDEX IF NOT EXISTS idx_daily_summaries_date ON daily_summaries(date)",
        "CREATE INDEX IF NOT EXISTS idx_daily_summaries_chat_date ON daily_summaries(chat_id, date)",
        "CREATE INDEX IF NOT EXISTS idx_daily_summaries_source ON daily_summaries(source)",
        "CREATE INDEX IF NOT EXISTS idx_events_date ON events(date)",
        "CREATE INDEX IF NOT EXISTS idx_events_source_summary ON events(source_summary_id)",
        "CREATE INDEX IF NOT EXISTS idx_notes_date ON notes(date)",
        "CREATE INDEX IF NOT EXISTS idx_sentiment_date ON sentiment_scores(date)",
    ]),
    (2, [
        # `my_raw_notes` was previously added by an inline self-healer in
        # init_db; folded into the versioned system. Idempotent because
        # CREATE TABLE for fresh DBs already declares the column.
        _add_column_if_missing("coin_profiles", "my_raw_notes", "TEXT DEFAULT ''"),
    ]),
    (3, [
        "CREATE INDEX IF NOT EXISTS idx_daily_summaries_slot ON daily_summaries(date, summary_slot)",
    ]),
    (4, [
        """
        CREATE TABLE IF NOT EXISTS message_summary_links (
            message_id INTEGER NOT NULL,
            summary_id INTEGER NOT NULL,
            link_type TEXT NOT NULL DEFAULT 'summary',
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            PRIMARY KEY (message_id, summary_id),
            FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE,
            FOREIGN KEY (summary_id) REFERENCES daily_summaries(id) ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_msg_summary_links_summary ON message_summary_links(summary_id)",
        "CREATE INDEX IF NOT EXISTS idx_msg_summary_links_message ON message_summary_links(message_id)",
        """
        INSERT OR IGNORE INTO message_summary_links (message_id, summary_id)
        SELECT id, summary_id
        FROM messages
        WHERE summary_id IS NOT NULL
        """,
        """
        CREATE TABLE IF NOT EXISTS auto_summary_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            slot TEXT NOT NULL,
            since_iso TEXT DEFAULT '',
            until_iso TEXT DEFAULT '',
            fetch_status TEXT DEFAULT 'pending',
            summary_status TEXT DEFAULT 'pending',
            started_at TEXT DEFAULT (datetime('now', 'localtime')),
            fetch_finished_at TEXT,
            summary_started_at TEXT,
            summary_finished_at TEXT,
            ok_count INTEGER DEFAULT 0,
            skip_existing_count INTEGER DEFAULT 0,
            skip_no_msgs_count INTEGER DEFAULT 0,
            failed_count INTEGER DEFAULT 0,
            error TEXT DEFAULT '',
            updated_at TEXT DEFAULT (datetime('now', 'localtime')),
            UNIQUE(date, slot)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_auto_summary_runs_status ON auto_summary_runs(date, slot)",
    ]),
    (5, [
        _add_column_if_missing("messages", "topic_id", "INTEGER"),
        "CREATE INDEX IF NOT EXISTS idx_messages_chat_topic_date ON messages(chat_id, topic_id, date)",
        """
        CREATE TABLE IF NOT EXISTS auto_topic_filters (
            chat_id TEXT NOT NULL,
            topic_id INTEGER NOT NULL,
            topic_title TEXT DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT DEFAULT (datetime('now', 'localtime')),
            PRIMARY KEY (chat_id, topic_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_auto_topic_filters_chat ON auto_topic_filters(chat_id, enabled)",
    ]),
    (6, [
        "CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date DESC)",
        "CREATE INDEX IF NOT EXISTS idx_daily_summaries_source_date_slot ON daily_summaries(source, date, summary_slot)",
        "CREATE INDEX IF NOT EXISTS idx_daily_summaries_chat_source_date_slot ON daily_summaries(chat_id, source, date, summary_slot)",
        "CREATE INDEX IF NOT EXISTS idx_msg_summary_links_summary_message ON message_summary_links(summary_id, message_id)",
        """
        CREATE TABLE IF NOT EXISTS auto_summary_chat_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT NOT NULL,
            slot TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            chat_name TEXT DEFAULT '',
            profile TEXT DEFAULT '',
            status TEXT DEFAULT '',
            summary_id INTEGER,
            elapsed_secs REAL DEFAULT 0,
            prompt_len INTEGER DEFAULT 0,
            msg_text_len INTEGER DEFAULT 0,
            message_count INTEGER DEFAULT 0,
            fallback_used INTEGER DEFAULT 0,
            prep_mode TEXT DEFAULT '',
            stream_error TEXT DEFAULT '',
            started_at TEXT DEFAULT (datetime('now', 'localtime')),
            finished_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_auto_summary_chat_runs_slot ON auto_summary_chat_runs(run_date, slot)",
        "CREATE INDEX IF NOT EXISTS idx_auto_summary_chat_runs_chat ON auto_summary_chat_runs(chat_id, run_date)",
    ]),
    (7, [
        _add_column_if_missing("auto_summary_chat_runs", "prep_mode", "TEXT DEFAULT ''"),
    ]),
]


def _get_schema_version(c):
    try:
        row = c.execute(
            "SELECT value FROM app_state WHERE key = 'schema_version'"
        ).fetchone()
    except Exception:
        return 0
    if not row:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def _set_schema_version(c, version):
    c.execute(
        """
        INSERT INTO app_state (key, value, updated_at)
        VALUES ('schema_version', ?, datetime('now', 'localtime'))
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (str(version),),
    )


def _run_versioned_migrations(c):
    current = _get_schema_version(c)
    for version, steps in _MIGRATIONS:
        if version <= current:
            continue
        for step in steps:
            if callable(step):
                step(c)
            else:
                c.execute(step)
        _set_schema_version(c, version)
        current = version
        logger.info("🔧 schema migration v%d applied", version)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def clean_text(value):
    return (value or "").strip()


RAW_MESSAGES_CAP = 500
_RAW_MSG_FIELDS = ("id", "date", "from", "username", "sender_id", "text", "media")
_RAW_GZIP_PREFIX = "gz:"


def encode_raw_messages(messages):
    """Reduce to essential fields, cap count, gzip if oversized."""
    stripped = [
        {k: m.get(k) for k in _RAW_MSG_FIELDS if m.get(k) is not None}
        for m in messages[:RAW_MESSAGES_CAP]
    ]
    payload = json.dumps(stripped, ensure_ascii=False, separators=(",", ":"))
    if len(payload) < 2048:
        return payload
    compressed = gzip.compress(payload.encode("utf-8"))
    return _RAW_GZIP_PREFIX + base64.b64encode(compressed).decode("ascii")


def decode_raw_messages(text):
    """Inverse of encode_raw_messages; handles legacy plain JSON too."""
    if not text:
        return []
    if text.startswith(_RAW_GZIP_PREFIX):
        blob = base64.b64decode(text[len(_RAW_GZIP_PREFIX):])
        text = gzip.decompress(blob).decode("utf-8")
    try:
        return json.loads(text)
    except Exception:
        return []


_FTS_SAFE_STRIP = re.compile(r'[\s"*:()\[\]{}\-+~^=<>!?,.;/\\|&%#@$`\'\u3000]+')


def build_fts_query(text, joiner=" ", min_len=1):
    """Build a safe FTS5 prefix query from free-form user input."""
    if not text:
        return ""
    tokens = [t for t in _FTS_SAFE_STRIP.split(text) if t and len(t) >= min_len]
    if not tokens:
        return ""
    return joiner.join(f"{t}*" for t in tokens)


# ---------------------------------------------------------------------------
# Message archiving (populated by summarize + archive endpoints)
# ---------------------------------------------------------------------------

def save_messages_for_summary(conn, messages, chat_id, chat_name, summary_id=None):
    """Idempotent insert into messages; re-syncs summary_id + text on conflict.
    Returns (new_count, total_attempted)."""
    new_count = 0
    total = 0
    chat_id_str = str(chat_id)
    for m in messages:
        msg_id = m.get("id")
        if msg_id is None:
            continue
        total += 1
        try:
            existed = conn.execute(
                "SELECT 1 FROM messages WHERE chat_id = ? AND msg_id = ? LIMIT 1",
                (chat_id_str, msg_id),
            ).fetchone() is not None
            conn.execute("""
                INSERT INTO messages
                (summary_id, msg_id, date, chat_id, chat_name,
                 sender_name, sender_username, sender_id, topic_id, text, media)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, msg_id) DO UPDATE SET
                    summary_id = COALESCE(excluded.summary_id, messages.summary_id),
                    date = excluded.date,
                    chat_name = excluded.chat_name,
                    sender_name = excluded.sender_name,
                    sender_username = excluded.sender_username,
                    sender_id = excluded.sender_id,
                    topic_id = excluded.topic_id,
                    text = excluded.text,
                    media = excluded.media
            """, (summary_id, msg_id, m.get("date"), chat_id_str, chat_name,
                  m.get("from"), m.get("username"), m.get("sender_id"),
                  m.get("topic_id"),
                  m.get("text", "") or "", m.get("media", "") or ""))
            if not existed:
                new_count += 1
            if summary_id is not None:
                row = conn.execute(
                    "SELECT id FROM messages WHERE chat_id = ? AND msg_id = ?",
                    (chat_id_str, msg_id),
                ).fetchone()
                if row:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO message_summary_links
                        (message_id, summary_id, link_type)
                        VALUES (?, ?, 'summary')
                        """,
                        (row["id"] if hasattr(row, "keys") else row[0], summary_id),
                    )
        except Exception as e:
            logger.warning("insert message failed (chat=%s msg=%s): %s", chat_id, msg_id, e)
    return new_count, total


# ---------------------------------------------------------------------------
# Coin-centric cross-chat search (used by /api/coin/search)
# ---------------------------------------------------------------------------

# CA format detection: EVM (0x + 40 hex) or Solana mint (base58, 32-44 chars).
_CA_EVM_RE = re.compile(r'\b0x[a-fA-F0-9]{40}\b')
_CA_SOL_RE = re.compile(r'\b[1-9A-HJ-NP-Za-km-z]{32,44}\b')


# Addresses that show up in wallet-log / swap messages but never represent a
# tradable token by themselves — wrapped natives, zero/dead sinks, and the
# most-trafficked DEX routers across chains. Filtering these out of ticker
# coin-search candidates removes the bulk of the co-occurrence noise where a
# `$LFI` swap message mechanically lists `WETH9 + Router + LFI` and all three
# CAs were treated as equally-weighted candidates. All entries lowercase to
# match the lowercase-EVM normalization in `extract_cas`.
SYSTEM_CONTRACT_BLACKLIST = {
    # Sinks
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
    # Wrapped natives
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # WETH (Ethereum)
    "0x4200000000000000000000000000000000000006",  # WETH (Base / OP-stack predeploy)
    "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",  # WETH (Arbitrum)
    "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",  # WBNB (BSC)
    "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270",  # WMATIC (Polygon)
    "0xb31f66aa3c1e785363f0875a1b74e27b85fd66c7",  # WAVAX (Avalanche)
    # Common DEX routers seen in wallet logs
    "0xe592427a0aece92de3edee1f18e0157c05861564",  # Uniswap V3 Router
    "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45",  # Uniswap V3 Router 2
    "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad",  # Uniswap Universal Router (multi-chain)
    "0x66a9893cc07d91d95644aedd05d03f95e1dba8af",  # Uniswap Universal Router v2
    "0x2626664c2603336e57b271c5c0b26f421741e481",  # Uniswap V3 Swap Router 02 (Base)
    "0x10ed43c718714eb63d5aa57b78b54704e256024e",  # Pancake V2 Router (BSC)
    "0x13f4ea83d0bd40e75c8222255bc855a974568dd4",  # Pancake V3 Smart Router (BSC)
    "0x111111125421ca6dc452d289314280a0f8842a65",  # 1inch V6 Aggregation Router
}


# How many candidate CAs to surface for a ticker query. The tail past 3 is
# almost always co-occurrence noise (multi-token wallet logs) — capping here
# keeps the UI focused on the actual token CA.
TICKER_CA_CANDIDATE_TOP_N = 3


def is_system_contract(ca):
    """Return True if `ca` is a wrapped-native / sink / common-router address.

    Used to drop obvious co-occurrence noise from ticker CA candidates.
    Lowercase comparison is safe even for case-sensitive Solana CAs — they
    simply won't match the EVM-only blacklist entries.
    """
    if not ca:
        return False
    return ca.lower() in SYSTEM_CONTRACT_BLACKLIST


def detect_ca_type(query):
    """Return 'evm' / 'solana' / None based on query format."""
    q = (query or "").strip()
    if re.fullmatch(r'0x[a-fA-F0-9]{40}', q):
        return "evm"
    if re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}', q):
        return "solana"
    return None


def extract_cas(text):
    """Pull all CA-like strings out of a blob of text. Returns a list of unique CAs.

    EVM addresses are normalized to lowercase so EIP-55 checksum casing and
    raw lowercase forms collapse into the same key downstream (otherwise the
    same token's CA splits into two candidates in coin search). Solana base58
    is case-sensitive — left as-is.
    """
    if not text:
        return []
    seen = set()
    out = []
    for m in _CA_EVM_RE.findall(text):
        key = m.lower()
        if key not in seen:
            seen.add(key)
            out.append(key)
    for m in _CA_SOL_RE.findall(text):
        # Skip strings that are already captured as EVM above (EVM has 0x prefix, so no overlap).
        # Also skip very generic-looking strings by requiring at least one digit.
        if not any(ch.isdigit() for ch in m):
            continue
        if m in seen:
            continue
        seen.add(m)
        out.append(m)
    return out


# Maximum char distance between a ticker mention and a CA position for the CA
# to count as "associated" with that ticker. Tuned for typical TG message
# formats: in a single-line wallet log, a CA right after `$LFI (...)` is ~6
# chars away; a CA at the end of a 200-char swap-summary line is ~50-100. 60
# captures the latter while excluding multi-token aggregate lines where each
# token+CA group is separated by 80+ chars.
TICKER_CA_PROXIMITY_CHARS = 60


def cas_for_ticker(text, ticker, max_distance=TICKER_CA_PROXIMITY_CHARS):
    """Return the subset of `extract_cas(text)` credibly associated with `ticker`.

    Two-rule heuristic:
    - Single-CA message → keep the CA (unambiguous association).
    - Multi-CA message → keep only CAs whose nearest occurrence is within
      `max_distance` chars of any ticker mention.

    Filters wallet-log noise where one TG message lists 5+ tokens as a
    transaction summary; pre-filter, every CA in the message was treated as
    an equal candidate for the searched ticker. Comparison is case-insensitive
    for both ticker matching and EVM CA position lookup; Solana CAs (case-
    sensitive base58) are matched literally.
    """
    cas = extract_cas(text)
    if not cas:
        return []
    if len(cas) == 1:
        return cas

    bare = (ticker or "").lstrip("$").strip()
    if not bare:
        return cas

    # Allow optional `$` prefix; word boundaries prevent matching `LFI` inside
    # a longer alphanumeric token (e.g. `INFLATION`). Case-insensitive so
    # `$lfi` / `$LFI` / `LFI` all hit.
    pat = re.compile(rf"(?<![A-Za-z0-9])\$?{re.escape(bare)}(?![A-Za-z0-9])", re.IGNORECASE)
    ticker_positions = [m.start() for m in pat.finditer(text)]
    if not ticker_positions:
        # FTS matched something the regex didn't (rare; tokenizer differences).
        # Fall back to permissive — better to over-include than drop a real hit.
        return cas

    text_lower = text.lower()
    near = []
    for ca in cas:
        search_in = text_lower if ca.startswith("0x") else text
        # Find every occurrence of the CA in the message.
        positions = []
        idx = 0
        while True:
            i = search_in.find(ca, idx)
            if i < 0:
                break
            positions.append(i)
            idx = i + 1
        if any(abs(p - t) <= max_distance for p in positions for t in ticker_positions):
            near.append(ca)
    return near


def search_coin(query, limit_per_chat=3, max_chats=30, max_events=20, scan_cap=300,
                msg_sample_per_chat=20, msg_total_cap=1000, days=None):
    """Route coin search based on query shape:
    - CA (evm/solana): scan messages_fts, return msg-level per_chat view
    - ticker / name: scan summaries_fts, return summary-level view + CA disambiguation
    `days`: optional int; if set, restrict matches to the last N days.
    """
    query = (query or "").strip()
    empty = {
        "query": query, "mode": "empty",
        "per_chat": [], "events": [], "total_hits": 0,
        "ca_candidates": [],
    }
    if not query:
        return empty

    ca_type = detect_ca_type(query)
    since = None
    if days:
        try:
            from datetime import date as _date, timedelta as _td
            since = (_date.today() - _td(days=int(days))).isoformat()
        except Exception:
            since = None

    with get_db_ctx() as conn:
        if ca_type:
            return _search_coin_by_ca(conn, query, ca_type,
                                       limit_per_chat=msg_sample_per_chat,
                                       max_chats=max_chats, msg_total_cap=msg_total_cap,
                                       since=since)
        return _search_coin_by_ticker(conn, query, limit_per_chat=limit_per_chat,
                                        max_chats=max_chats, max_events=max_events,
                                        scan_cap=scan_cap, msg_total_cap=msg_total_cap,
                                        since=since)


def _fetch_notes_for_coin(conn, query, since, fts_query=None, max_notes=30, ca_match=None):
    """Return notes matching a ticker/CA query. ca_match: "evm"|"solana"|None.
    For tickers use FTS. For CAs we do exact substring (case-insensitive for EVM)."""
    rows = []
    if ca_match:
        q_lower = query.lower()
        sql = "SELECT id, date, content, tags, created_at FROM notes"
        clauses = []
        params = []
        if ca_match == "evm":
            clauses.append("(LOWER(content) LIKE ? OR LOWER(tags) LIKE ?)")
            like = f"%{q_lower}%"
            params.extend([like, like])
        else:
            clauses.append("(content LIKE ? OR tags LIKE ?)")
            like = f"%{query}%"
            params.extend([like, like])
        if since:
            clauses.append("date >= ?")
            params.append(since)
        sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY date DESC LIMIT ?"
        params.append(max_notes)
        try:
            rows = conn.execute(sql, params).fetchall()
        except Exception as e:
            logger.warning("note CA search failed: %s", e)
    else:
        if not fts_query:
            return []
        try:
            sql = """
                SELECT n.id, n.date, n.content, n.tags, n.created_at
                FROM notes_fts fts
                JOIN notes n ON n.id = fts.rowid
                WHERE notes_fts MATCH ?
            """
            params = [fts_query]
            if since:
                sql += " AND n.date >= ?"
                params.append(since)
            sql += " ORDER BY n.date DESC LIMIT ?"
            params.append(max_notes)
            rows = conn.execute(sql, params).fetchall()
        except Exception as e:
            logger.warning("note ticker search failed: %s", e)
    return [dict(r) for r in rows]


def _search_coin_by_ticker(conn, query, limit_per_chat, max_chats, max_events, scan_cap,
                            msg_total_cap, since):
    fts_query = build_fts_query(query, joiner=" OR ", min_len=1)
    if not fts_query:
        return {"query": query, "mode": "ticker", "per_chat": [], "events": [],
                "notes": [], "total_hits": 0, "ca_candidates": []}

    summary_rows = []
    try:
        sql = """
            SELECT ds.id, ds.date, ds.chat_id, ds.chat_name,
                   snippet(summaries_fts, 0, '«', '»', '...', 18) AS snip
            FROM summaries_fts
            JOIN daily_summaries ds ON ds.id = summaries_fts.rowid
            WHERE summaries_fts MATCH ?
        """
        params = [fts_query]
        if since:
            sql += " AND ds.date >= ?"
            params.append(since)
        sql += " ORDER BY ds.date DESC LIMIT ?"
        params.append(scan_cap)
        summary_rows = conn.execute(sql, params).fetchall()
    except Exception as e:
        logger.warning("ticker summary search failed: %s", e)

    event_rows = []
    try:
        ev_sql = """
            SELECT e.id, e.date, e.title, e.description, e.importance,
                   e.tags, e.source_chat
            FROM events_fts
            JOIN events e ON e.id = events_fts.rowid
            WHERE events_fts MATCH ?
        """
        ev_params = [fts_query]
        if since:
            ev_sql += " AND e.date >= ?"
            ev_params.append(since)
        ev_sql += " ORDER BY e.date DESC LIMIT ?"
        ev_params.append(max_events)
        event_rows = conn.execute(ev_sql, ev_params).fetchall()
    except Exception as e:
        logger.warning("ticker event search failed: %s", e)

    # Pull matching raw messages to extract CA candidates + per-chat msg_count.
    msg_rows = []
    try:
        msg_sql = """
            SELECT m.chat_id, m.chat_name, m.text, m.date
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            WHERE messages_fts MATCH ?
        """
        msg_params = [fts_query]
        if since:
            msg_sql += " AND m.date >= ?"
            msg_params.append(since)
        msg_sql += " ORDER BY m.date DESC LIMIT ?"
        msg_params.append(msg_total_cap)
        msg_rows = conn.execute(msg_sql, msg_params).fetchall()
    except Exception as e:
        logger.warning("ticker msg search failed: %s", e)

    by_chat = {}
    for r in summary_rows:
        key = r["chat_name"] or "(unknown)"
        bucket = by_chat.get(key)
        if bucket is None:
            bucket = {
                "chat_name": key, "chat_id": r["chat_id"],
                "hit_days": 0, "msg_count": 0,
                "first_date": r["date"], "last_date": r["date"],
                "samples": [], "cas": [],
            }
            by_chat[key] = bucket
        bucket["hit_days"] += 1
        if r["date"] < bucket["first_date"]:
            bucket["first_date"] = r["date"]
        if r["date"] > bucket["last_date"]:
            bucket["last_date"] = r["date"]
        if len(bucket["samples"]) < limit_per_chat:
            bucket["samples"].append({
                "summary_id": r["id"], "date": r["date"],
                "snippet": r["snip"] or "",
            })

    # Merge in msg-level counts + CA candidates per chat.
    # System contracts (wrapped natives, sinks, common routers) are dropped
    # here — they co-occur in every Base/BSC/etc. wallet-log message and would
    # otherwise crowd out the actual token CA in the candidate ranking.
    chat_ca_sets = {}
    ca_chat_sets = {}   # ca -> set of chat keys that mention it
    ca_msg_counts = {}  # ca -> total msg count that mentioned it
    for r in msg_rows:
        key = r["chat_name"] or "(unknown)"
        bucket = by_chat.get(key)
        if bucket is None:
            bucket = {
                "chat_name": key, "chat_id": r["chat_id"],
                "hit_days": 0, "msg_count": 0,
                "first_date": r["date"][:10], "last_date": r["date"][:10],
                "samples": [], "cas": [],
            }
            by_chat[key] = bucket
        bucket["msg_count"] += 1
        cas = [c for c in cas_for_ticker(r["text"], query) if not is_system_contract(c)]
        if cas:
            chat_set = chat_ca_sets.setdefault(key, set())
            for ca in cas:
                chat_set.add(ca)
                ca_chat_sets.setdefault(ca, set()).add(key)
                ca_msg_counts[ca] = ca_msg_counts.get(ca, 0) + 1

    for key, ca_set in chat_ca_sets.items():
        if key in by_chat:
            by_chat[key]["cas"] = sorted(ca_set)

    # Rank by msg_count first (one chat with 30 mentions of the real CA beats
    # a router that happens to be in 2 chats with 6 mentions), chat_count as
    # tiebreaker. Capped at TICKER_CA_CANDIDATE_TOP_N — the long tail past
    # rank 3 is almost always co-occurrence noise that survived the blacklist.
    global_cas = [
        {"ca": ca, "chat_count": len(chats), "msg_count": ca_msg_counts.get(ca, 0)}
        for ca, chats in sorted(ca_chat_sets.items(),
                                key=lambda kv: (ca_msg_counts.get(kv[0], 0), len(kv[1])),
                                reverse=True)
    ][:TICKER_CA_CANDIDATE_TOP_N]

    per_chat = sorted(
        by_chat.values(),
        key=lambda b: (b["hit_days"], b["msg_count"], b["last_date"]),
        reverse=True,
    )[:max_chats]

    notes = _fetch_notes_for_coin(conn, query, since, fts_query=fts_query)

    return {
        "query": query,
        "mode": "ticker",
        "per_chat": per_chat,
        "events": [dict(r) for r in event_rows],
        "notes": notes,
        "total_hits": sum(b["hit_days"] for b in per_chat),
        "total_msgs": sum(b["msg_count"] for b in per_chat),
        "ca_candidates": global_cas,
    }


def _search_coin_by_ca(conn, query, ca_type, limit_per_chat, max_chats, msg_total_cap, since):
    # Exact-match path: FTS gives us candidates, but we filter by literal substring
    # to eliminate any tokenizer false positives. Case-insensitive for EVM.
    fts_query = build_fts_query(query, joiner=" OR ", min_len=4)

    msg_rows = []
    try:
        sql = """
            SELECT m.id, m.msg_id, m.date, m.chat_id, m.chat_name,
                   m.sender_name, m.sender_username, m.sender_id,
                   m.text, m.media
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            WHERE messages_fts MATCH ?
        """
        params = [fts_query or query]
        if since:
            sql += " AND m.date >= ?"
            params.append(since)
        sql += " ORDER BY m.date ASC LIMIT ?"
        params.append(msg_total_cap)
        msg_rows = conn.execute(sql, params).fetchall()
    except Exception as e:
        logger.warning("CA message search failed: %s", e)

    trust = {}
    try:
        trust = {row["sender_id"]: row["trust_level"]
                 for row in conn.execute("SELECT sender_id, trust_level FROM trusted_senders").fetchall()}
    except Exception:
        pass

    # Filter by literal match (case-insensitive for EVM) to drop tokenizer noise.
    q_lower = query.lower()
    filtered = []
    for r in msg_rows:
        text = r["text"] or ""
        if ca_type == "evm":
            if q_lower in text.lower():
                filtered.append(r)
        else:  # solana — case-sensitive
            if query in text:
                filtered.append(r)

    by_chat = {}
    for r in filtered:
        key = r["chat_name"] or "(unknown)"
        bucket = by_chat.get(key)
        if bucket is None:
            bucket = {
                "chat_name": key, "chat_id": r["chat_id"],
                "msg_count": 0,
                "first_date": r["date"], "last_date": r["date"],
                "samples": [],
            }
            by_chat[key] = bucket
        bucket["msg_count"] += 1
        if r["date"] < bucket["first_date"]:
            bucket["first_date"] = r["date"]
        if r["date"] > bucket["last_date"]:
            bucket["last_date"] = r["date"]
        if len(bucket["samples"]) < limit_per_chat:
            bucket["samples"].append({
                "msg_id": r["msg_id"],
                "date": r["date"],
                "sender_name": r["sender_name"] or "",
                "sender_username": r["sender_username"] or "",
                "sender_id": r["sender_id"],
                "trust": trust.get(r["sender_id"]),
                "text": r["text"] or "",
                "media": r["media"] or "",
            })

    # For CA: sort by earliest-mention first (who posted it first matters).
    per_chat = sorted(by_chat.values(), key=lambda b: b["first_date"])[:max_chats]

    notes = _fetch_notes_for_coin(conn, query, since, ca_match=ca_type)

    return {
        "query": query,
        "mode": "ca",
        "ca_type": ca_type,
        "per_chat": per_chat,
        "events": [],
        "notes": notes,
        "total_hits": sum(b["msg_count"] for b in per_chat),
        "ca_candidates": [query],
    }


# ---------------------------------------------------------------------------
# Import helpers — used by /api/memory/import
# ---------------------------------------------------------------------------

def import_event_if_missing(conn, event):
    date_value = clean_text(event.get("date"))
    title = clean_text(event.get("title"))
    description = clean_text(event.get("description"))
    importance = clean_text(event.get("importance")) or "normal"
    tags = clean_text(event.get("tags"))
    source_chat = clean_text(event.get("source_chat"))

    if not date_value or not title:
        return False

    existing = conn.execute("""
        SELECT 1 FROM events
        WHERE date = ? AND title = ?
          AND COALESCE(description, '') = ?
          AND COALESCE(importance, 'normal') = ?
          AND COALESCE(tags, '') = ?
          AND COALESCE(source_chat, '') = ?
        LIMIT 1
    """, (date_value, title, description, importance, tags, source_chat)).fetchone()
    if existing:
        return False

    conn.execute("""
        INSERT INTO events (date, title, description, importance, tags, source_chat)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (date_value, title, description, importance, tags, source_chat))
    return True


def import_note_if_missing(conn, note):
    date_value = clean_text(note.get("date"))
    content = clean_text(note.get("content"))
    tags = clean_text(note.get("tags"))

    if not date_value or not content:
        return False

    existing = conn.execute("""
        SELECT 1 FROM notes
        WHERE date = ? AND content = ? AND COALESCE(tags, '') = ?
        LIMIT 1
    """, (date_value, content, tags)).fetchone()
    if existing:
        return False

    conn.execute(
        "INSERT INTO notes (date, content, tags) VALUES (?, ?, ?)",
        (date_value, content, tags),
    )
    return True


def import_watchlist_if_missing(conn, keyword, category="匯入分析"):
    keyword = clean_text(keyword)
    category = clean_text(category) or "匯入分析"
    if not keyword:
        return False

    existing = conn.execute(
        "SELECT 1 FROM watchlist WHERE keyword = ? LIMIT 1", (keyword,)
    ).fetchone()
    if existing:
        return False

    conn.execute(
        "INSERT INTO watchlist (keyword, category) VALUES (?, ?)",
        (keyword, category),
    )
    return True


def _pick_import_date(data, report):
    for value in (
        (report.get("time_range") or {}).get("end"),
        data.get("generated_at"),
        (report.get("time_range") or {}).get("start"),
    ):
        value = clean_text(value)
        if len(value) >= 10:
            return value[:10]
    return date.today().isoformat()


def _build_analysis_summary(data):
    report = data.get("report") or {}
    lines = []

    title = clean_text(report.get("title")) or "匯入分析報告"
    channel = clean_text(report.get("channel")) or "未命名來源"
    platform = clean_text(report.get("platform")) or "未知平台"
    time_range = report.get("time_range") or {}
    duration_hours = report.get("time_range", {}).get("duration_hours")
    message_count = report.get("message_count")
    unique_senders = report.get("unique_senders")
    top_users = report.get("top_active_users") or []

    lines.append(f"【{title}】")
    lines.append(f"來源：{channel} / {platform}")
    if time_range.get("start") or time_range.get("end"):
        lines.append(f"時段：{clean_text(time_range.get('start'))} -> {clean_text(time_range.get('end'))}")
    if duration_hours is not None or message_count is not None or unique_senders is not None:
        lines.append(
            f"統計：{duration_hours or '?'} 小時，{message_count or '?'} 則訊息，{unique_senders or '?'} 位活躍用戶"
        )
    if top_users:
        lines.append(f"活躍成員：{', '.join(str(x) for x in top_users[:8])}")

    checklist = data.get("checklist") or []
    if checklist:
        lines.append("")
        lines.append("【今日待查】")
        for item in checklist[:8]:
            if not isinstance(item, dict):
                continue
            priority = clean_text(item.get("priority"))
            target = clean_text(item.get("target"))
            action = clean_text(item.get("action"))
            why_now = clean_text(item.get("why_now"))
            line = f"- [{priority}] {target}: {action}".strip(": ")
            if why_now:
                line += f" — {why_now}"
            lines.append(line)

    radar = data.get("radar") or []
    if radar:
        lines.append("")
        lines.append("【新項目雷達】")
        for item in radar[:12]:
            if not isinstance(item, dict):
                continue
            target = clean_text(item.get("target"))
            status = clean_text(item.get("status"))
            strength = clean_text(item.get("strength"))
            why_now = clean_text(item.get("why_now")) or clean_text(item.get("signal"))
            next_step = clean_text(item.get("next_step"))
            if target:
                line = f"- {target} [{status}/{strength}]"
                if why_now:
                    line += f"：{why_now}"
                if next_step:
                    line += f"；下一步：{next_step}"
                lines.append(line)

    needs_context = data.get("needs_context") or []
    if needs_context:
        lines.append("")
        lines.append("【裸 CA / 上下文缺口】")
        for item in needs_context[:8]:
            if not isinstance(item, dict):
                continue
            clue = clean_text(item.get("clue"))
            missing = clean_text(item.get("missing"))
            next_step = clean_text(item.get("next_step"))
            if clue:
                line = f"- {clue}"
                if missing:
                    line += f"；缺：{missing}"
                if next_step:
                    line += f"；下一步：{next_step}"
                lines.append(line)

    expired = data.get("expired") or []
    if expired:
        lines.append("")
        lines.append("【過期 / 已追高】")
        for item in expired[:8]:
            if not isinstance(item, dict):
                continue
            target = clean_text(item.get("target"))
            reason = clean_text(item.get("reason"))
            next_step = clean_text(item.get("next_step"))
            if target:
                line = f"- {target}"
                if reason:
                    line += f"：{reason}"
                if next_step:
                    line += f"；下一步：{next_step}"
                lines.append(line)

    takeaways = data.get("key_takeaways") or []
    if takeaways:
        lines.append("")
        lines.append("【核心重點】")
        for item in takeaways:
            t_title = clean_text(item.get("title"))
            t_summary = clean_text(item.get("summary"))
            if t_title or t_summary:
                lines.append(f"- {t_title}: {t_summary}".strip(": "))

    market = data.get("market_by_chain") or {}
    if market:
        lines.append("")
        lines.append("【各鏈動態】")
        for chain_key, chain_data in market.items():
            if not isinstance(chain_data, dict):
                continue
            chain_label = clean_text(chain_data.get("label")) or chain_key
            tokens = chain_data.get("tokens") or []
            lines.append(f"- {chain_label}")
            if not tokens:
                lines.append("  無明顯動態")
                continue
            for token in tokens[:6]:
                symbol = clean_text(token.get("symbol")) or clean_text(token.get("name")) or "未知標的"
                fdv = clean_text(token.get("fdv_range"))
                gain = clean_text(token.get("gain_pct"))
                narrative = clean_text(token.get("narrative"))
                risk = clean_text(token.get("risk"))
                parts = [symbol]
                if fdv:
                    parts.append(f"FDV {fdv}")
                if gain:
                    parts.append(f"漲跌 {gain}")
                if narrative:
                    parts.append(f"敘事 {narrative}")
                if risk:
                    parts.append(f"風險 {risk}")
                lines.append("  - " + "；".join(parts))

    events = data.get("events") or []
    if events:
        lines.append("")
        lines.append("【重要事件】")
        for event in events:
            e_title = clean_text(event.get("title"))
            what = clean_text(event.get("what"))
            impact = clean_text(event.get("impact"))
            timeframe = clean_text(event.get("timeframe"))
            lines.append(f"- {e_title}")
            if what:
                lines.append(f"  發生了什麼：{what}")
            if impact:
                lines.append(f"  潛在影響：{impact}")
            if timeframe:
                lines.append(f"  時間範圍：{timeframe}")

    kols = data.get("kol_opinions") or []
    if kols:
        lines.append("")
        lines.append("【KOL 觀點】")
        for item in kols[:8]:
            name = clean_text(item.get("name"))
            role = clean_text(item.get("role"))
            stance = clean_text(item.get("stance"))
            lines.append(f"- {name} {f'({role})' if role else ''}: {stance}".strip())

    sentiment = data.get("sentiment") or {}
    if sentiment:
        lines.append("")
        lines.append("【整體情緒】")
        overall = clean_text(sentiment.get("overall"))
        consensus = clean_text(sentiment.get("consensus"))
        if overall:
            lines.append(f"- 整體：{overall}")
        if consensus:
            lines.append(f"- 共識：{consensus}")
        for label, items in (("偏空因素", sentiment.get("bearish_factors") or []),
                             ("偏多因素", sentiment.get("bullish_factors") or [])):
            if items:
                lines.append(f"- {label}：")
                for item in items[:5]:
                    lines.append(f"  - {item}")

    actions = data.get("actionable") or []
    if actions:
        lines.append("")
        lines.append("【可操作建議】")
        for item in actions:
            action = clean_text(item.get("action"))
            condition = clean_text(item.get("condition"))
            stop_loss = clean_text(item.get("stop_loss"))
            lines.append(f"- {action}")
            if condition:
                lines.append(f"  條件：{condition}")
            if stop_loss:
                lines.append(f"  風控：{stop_loss}")

    watchlist = data.get("watchlist") or []
    if watchlist:
        lines.append("")
        lines.append("【後續追蹤】")
        for item in watchlist[:10]:
            text = clean_text(item)
            if text:
                lines.append(f"- {text}")

    return "\n".join(lines).strip() or "匯入的分析報告沒有可用內容。"


def normalize_memory_import_payload(data):
    """Accept either the memory_export format or an analysis report; return a canonical dict."""
    if not isinstance(data, dict):
        return None, None, "匯入內容必須是 JSON 物件"

    report = data.get("report")
    if isinstance(report, dict):
        chat_name = clean_text(report.get("channel")) or clean_text(report.get("title")) or "匯入分析"
        summary_date = _pick_import_date(data, report)
        summary_text = _build_analysis_summary(data)
        hours = (report.get("time_range") or {}).get("duration_hours") or 24
        message_count = report.get("message_count") or 0
        chat_id = f"import::{chat_name}"

        summaries = [{
            "date": summary_date,
            "chat_id": chat_id,
            "chat_name": chat_name,
            "hours": hours,
            "message_count": message_count,
            "summary": summary_text,
        }]

        events = []
        for item in data.get("radar") or []:
            if not isinstance(item, dict):
                continue
            target = clean_text(item.get("target"))
            why_now = clean_text(item.get("why_now")) or clean_text(item.get("signal"))
            status = clean_text(item.get("status"))
            strength = clean_text(item.get("strength"))
            if target and why_now:
                events.append({
                    "date": summary_date,
                    "title": f"{target} [{status}/{strength}]",
                    "description": why_now,
                    "importance": "high" if strength == "A" else "normal",
                    "tags": "analysis-import,radar",
                    "source_chat": chat_name,
                })
        for event in data.get("events") or []:
            if not isinstance(event, dict):
                continue
            title = clean_text(event.get("title"))
            what = clean_text(event.get("what"))
            impact = clean_text(event.get("impact"))
            timeframe = clean_text(event.get("timeframe"))
            description_parts = []
            if what:
                description_parts.append(f"發生了什麼：{what}")
            if impact:
                description_parts.append(f"潛在影響：{impact}")
            if timeframe:
                description_parts.append(f"時間範圍：{timeframe}")
            if title:
                events.append({
                    "date": summary_date,
                    "title": title,
                    "description": " | ".join(description_parts),
                    "importance": "normal",
                    "tags": "analysis-import",
                    "source_chat": chat_name,
                })

        notes = []
        for item in data.get("checklist") or []:
            if not isinstance(item, dict):
                continue
            priority = clean_text(item.get("priority"))
            target = clean_text(item.get("target"))
            action = clean_text(item.get("action"))
            why_now = clean_text(item.get("why_now"))
            if target or action:
                content = f"[待查:{priority}] {target}: {action}".strip(": ")
                if why_now:
                    content += f"；原因：{why_now}"
                notes.append({"date": summary_date, "content": content, "tags": "analysis,checklist"})
        for item in data.get("needs_context") or []:
            if not isinstance(item, dict):
                continue
            clue = clean_text(item.get("clue"))
            missing = clean_text(item.get("missing"))
            next_step = clean_text(item.get("next_step"))
            if clue:
                content = f"[缺口] {clue}"
                if missing:
                    content += f"；缺：{missing}"
                if next_step:
                    content += f"；下一步：{next_step}"
                notes.append({"date": summary_date, "content": content, "tags": "analysis,needs-context"})
        for item in data.get("expired") or []:
            if not isinstance(item, dict):
                continue
            target = clean_text(item.get("target"))
            reason = clean_text(item.get("reason"))
            if target:
                content = f"[過期] {target}"
                if reason:
                    content += f"；原因：{reason}"
                notes.append({"date": summary_date, "content": content, "tags": "analysis,expired"})
        for item in data.get("actionable") or []:
            if not isinstance(item, dict):
                continue
            action = clean_text(item.get("action"))
            condition = clean_text(item.get("condition"))
            stop_loss = clean_text(item.get("stop_loss"))
            if action:
                content = f"[操作] {action}"
                if condition:
                    content += f"；條件：{condition}"
                if stop_loss:
                    content += f"；風控：{stop_loss}"
                notes.append({"date": summary_date, "content": content, "tags": "analysis,actionable"})

        sentiment = data.get("sentiment") or {}
        consensus = clean_text(sentiment.get("consensus"))
        overall = clean_text(sentiment.get("overall"))
        if overall or consensus:
            content = f"[情緒] {overall}" if overall else "[情緒]"
            if consensus:
                content += f"；共識：{consensus}"
            notes.append({"date": summary_date, "content": content, "tags": "analysis,sentiment"})

        for item in data.get("kol_opinions") or []:
            if not isinstance(item, dict):
                continue
            name = clean_text(item.get("name"))
            stance = clean_text(item.get("stance"))
            if name and stance:
                notes.append({"date": summary_date, "content": f"[KOL] {name}: {stance}", "tags": "analysis,kol"})

        watchlist = [item for item in (data.get("watchlist") or []) if clean_text(item)]
        for section in ("radar", "weak_signals"):
            for item in data.get(section) or []:
                if not isinstance(item, dict):
                    continue
                target = clean_text(item.get("target"))
                if target and target not in watchlist:
                    watchlist.append(target)
        for item in data.get("checklist") or []:
            if not isinstance(item, dict):
                continue
            target = clean_text(item.get("target"))
            if target and target not in watchlist:
                watchlist.append(target)

        return {
            "summaries": summaries,
            "events": events,
            "notes": notes,
            "watchlist": watchlist,
        }, "analysis_report", None

    if any(key in data for key in ("summaries", "events", "notes")):
        return {
            "summaries": data.get("summaries", []),
            "events": data.get("events", []),
            "notes": data.get("notes", []),
            "watchlist": data.get("watchlist", []),
        }, "memory_export", None

    return None, None, "格式不正確，找不到 summaries/events/notes，也不是支援的分析報告格式"
