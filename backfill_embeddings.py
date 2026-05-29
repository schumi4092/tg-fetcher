"""Backfill Voyage embeddings for summaries / events / notes that predate the key.

用法：設好 VOYAGE_API_KEY 後，在 tg-fetcher 目錄執行
    python backfill_embeddings.py
"""

from db import get_db_ctx
from embeddings import get_voyage_client, store_embedding


def main():
    if not get_voyage_client():
        print("❌ VOYAGE_API_KEY 未設定或 voyageai 套件未安裝，結束。")
        return

    with get_db_ctx() as conn:
        summaries = conn.execute("""
            SELECT id, summary FROM daily_summaries
            WHERE summary IS NOT NULL AND summary != ''
              AND id NOT IN (SELECT source_id FROM embeddings WHERE source_type='summary')
        """).fetchall()
        events = conn.execute("""
            SELECT id, title, description FROM events
            WHERE id NOT IN (SELECT source_id FROM embeddings WHERE source_type='event')
        """).fetchall()
        notes = conn.execute("""
            SELECT id, content FROM notes
            WHERE content IS NOT NULL AND content != ''
              AND id NOT IN (SELECT source_id FROM embeddings WHERE source_type='note')
        """).fetchall()

    print(f"待補：summaries={len(summaries)}  events={len(events)}  notes={len(notes)}")

    ok = fail = 0
    for r in summaries:
        if store_embedding("summary", r["id"], r["summary"]):
            ok += 1; print(f"  [summary {r['id']}] ok")
        else:
            fail += 1; print(f"  [summary {r['id']}] FAIL")

    for r in events:
        text = f"{r['title']}\n{r['description'] or ''}".strip()
        if text and store_embedding("event", r["id"], text):
            ok += 1; print(f"  [event {r['id']}] ok")
        else:
            fail += 1; print(f"  [event {r['id']}] FAIL")

    for r in notes:
        if store_embedding("note", r["id"], r["content"]):
            ok += 1; print(f"  [note {r['id']}] ok")
        else:
            fail += 1; print(f"  [note {r['id']}] FAIL")

    print(f"\n✓ 完成。成功 {ok} 筆，失敗 {fail} 筆。")


if __name__ == "__main__":
    main()
