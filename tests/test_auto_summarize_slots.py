from datetime import datetime


def test_slot_budget_zero_disables_forced_fallback(monkeypatch):
    import server

    monkeypatch.setattr(server, "AUTO_SUMMARIZE_SLOT_BUDGET_SECS", 0)
    monkeypatch.setattr(server, "AUTO_SUMMARIZE_SLOT_FALLBACK_MIN_REMAINING_SECS", 240)

    assert server._auto_slot_force_fallback_reason(0) is None


def test_end_of_day_slot_is_saved_on_logical_date(monkeypatch):
    import server

    monkeypatch.setattr(server, "AUTO_SUMMARIZE_TIMES", [(8, 0), (16, 0), (24, 0)])
    monkeypatch.setattr(server, "AUTO_SUMMARIZE_HOURS", 8)

    slot_dt = datetime(2026, 5, 7, 0, 0, tzinfo=server.TAIPEI_TZ)
    since, until, summary_date, summary_slot = server._chunk_for_slot(slot_dt)

    assert summary_date == "2026-05-06"
    assert summary_slot == "24:00"
    assert since == "2026-05-06T08:00:00+00:00"
    assert until == "2026-05-06T16:00:00+00:00"


def test_slot_mode_coverage_requires_done_run(fresh_db, monkeypatch):
    import db
    import server

    db.init_db()
    monkeypatch.setattr(server, "AUTO_SUMMARIZE_TIMES", [(8, 0), (16, 0), (24, 0)])

    last_slot = datetime(2026, 5, 15, 8, 0, tzinfo=server.TAIPEI_TZ)
    with db.get_db_ctx() as conn:
        conn.execute("""
            INSERT INTO auto_summary_runs
            (date, slot, since_iso, until_iso, summary_status)
            VALUES (?, ?, ?, ?, ?)
        """, (
            "2026-05-14",
            "24:00",
            "2026-05-14T08:00:00+00:00",
            "2026-05-14T16:00:00+00:00",
            "done",
        ))
        conn.execute("""
            INSERT INTO auto_summary_runs
            (date, slot, since_iso, until_iso, summary_status)
            VALUES (?, ?, ?, ?, ?)
        """, (
            "2026-05-15",
            "08:00",
            "2026-05-14T16:00:00+00:00",
            "2026-05-15T00:00:00+00:00",
            "running",
        ))
        conn.commit()

        covered, checkpoint = server._slot_mode_coverage(conn, last_slot)

    assert covered is False
    assert checkpoint == datetime(2026, 5, 15, 0, 0, tzinfo=server.TAIPEI_TZ)


def test_slot_mode_coverage_accepts_done_latest_slot(fresh_db, monkeypatch):
    import db
    import server

    db.init_db()
    monkeypatch.setattr(server, "AUTO_SUMMARIZE_TIMES", [(8, 0), (16, 0), (24, 0)])

    last_slot = datetime(2026, 5, 15, 8, 0, tzinfo=server.TAIPEI_TZ)
    with db.get_db_ctx() as conn:
        conn.execute("""
            INSERT INTO auto_summary_runs
            (date, slot, since_iso, until_iso, summary_status)
            VALUES (?, ?, ?, ?, ?)
        """, (
            "2026-05-15",
            "08:00",
            "2026-05-14T16:00:00+00:00",
            "2026-05-15T00:00:00+00:00",
            "done",
        ))
        conn.commit()

        covered, checkpoint = server._slot_mode_coverage(conn, last_slot)

    assert covered is True
    assert checkpoint == datetime(2026, 5, 15, 8, 0, tzinfo=server.TAIPEI_TZ)
