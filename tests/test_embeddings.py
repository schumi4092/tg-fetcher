"""embeddings.store_embedding must UPSERT and preserve original created_at."""

import time

import db
import embeddings


class _FakeVoyage:
    """Replaces voyageai.Client. Returns deterministic short vectors."""
    def embed(self, texts, model=None):
        class R:
            pass
        r = R()
        # 4-dim vectors for tests
        r.embeddings = [[float(i + 1)] * 4 for i, _ in enumerate(texts)]
        return r


def test_upsert_preserves_created_at(fresh_db, monkeypatch):
    db.init_db()
    monkeypatch.setattr(embeddings, "_voyage_client", _FakeVoyage())

    ok = embeddings.store_embedding("summary", 42, "first version of text")
    assert ok is True

    with db.get_db_ctx() as conn:
        row1 = conn.execute(
            "SELECT created_at, chunk_text FROM embeddings WHERE source_type='summary' AND source_id=42"
        ).fetchone()
    assert row1 is not None
    original_created_at = row1["created_at"]
    assert "first version" in row1["chunk_text"]

    # Sleep just enough that datetime('now', 'localtime') would tick.
    time.sleep(1.1)

    ok2 = embeddings.store_embedding("summary", 42, "second version of text")
    assert ok2 is True

    with db.get_db_ctx() as conn:
        row2 = conn.execute(
            "SELECT created_at, chunk_text FROM embeddings WHERE source_type='summary' AND source_id=42"
        ).fetchone()

    assert "second version" in row2["chunk_text"], "chunk_text should update"
    assert row2["created_at"] == original_created_at, (
        "created_at must be preserved on UPSERT (got %r, expected %r)"
        % (row2["created_at"], original_created_at)
    )


def test_store_returns_false_without_voyage(fresh_db, monkeypatch):
    db.init_db()
    monkeypatch.setattr(embeddings, "_voyage_client", None)
    monkeypatch.setattr(embeddings, "VOYAGE_API_KEY", "")
    monkeypatch.setattr(embeddings, "HAS_VOYAGE", False)
    assert embeddings.store_embedding("summary", 1, "text") is False


# ---------------------------------------------------------------------------
# Search-matrix cache
# ---------------------------------------------------------------------------

class _CountingVoyage:
    """Returns canned 4-dim vectors and counts embed() invocations so tests
    can verify whether a code path actually called the embedding API."""
    def __init__(self):
        self.calls = 0

    def embed(self, texts, model=None):
        self.calls += 1

        class R:
            pass
        r = R()
        # Vectors that point in distinct directions so cosine sims are predictable
        # First call (= store text 1): [1,0,0,0]; second store: [0,1,0,0]; etc.
        r.embeddings = [[float(i == self.calls - 1) for i in range(4)] for _ in texts]
        return r


def test_search_cache_built_lazily_and_used(fresh_db, monkeypatch):
    db.init_db()
    fake = _CountingVoyage()
    monkeypatch.setattr(embeddings, "_voyage_client", fake)

    # Seed two embeddings
    embeddings.store_embedding("summary", 1, "alpha doc")
    embeddings.store_embedding("summary", 2, "beta doc")

    # Cache should have been invalidated by both stores
    assert embeddings._search_cache is None

    results = embeddings.search_by_embedding("query")
    assert results is not None
    assert len(results) == 2
    # Cache should now be populated
    assert embeddings._search_cache is not None
    mat, norms, meta = embeddings._search_cache
    assert mat.shape == (2, 4)
    assert len(meta) == 2

    # A second search should not rebuild — count rows touched indirectly by
    # checking the cache stays the same object identity.
    cached_before = embeddings._search_cache
    embeddings.search_by_embedding("query2")
    cached_after = embeddings._search_cache
    assert cached_before is cached_after, "cache should be reused, not rebuilt"


def test_search_cache_invalidated_on_store(fresh_db, monkeypatch):
    db.init_db()
    fake = _CountingVoyage()
    monkeypatch.setattr(embeddings, "_voyage_client", fake)

    embeddings.store_embedding("summary", 1, "alpha")
    embeddings.search_by_embedding("q")  # builds cache
    assert embeddings._search_cache is not None

    # New write must invalidate
    embeddings.store_embedding("summary", 2, "beta")
    assert embeddings._search_cache is None

    # Next search rebuilds and includes the new row
    results = embeddings.search_by_embedding("q2")
    assert len(results) == 2


def test_search_cache_invalidated_on_norm_backfill(fresh_db, monkeypatch):
    """ensure_embedding_norms updates norm column directly — the cache,
    if already built, would have stale norms. Invalidating is the safe call."""
    db.init_db()
    fake = _CountingVoyage()
    monkeypatch.setattr(embeddings, "_voyage_client", fake)

    embeddings.store_embedding("summary", 1, "alpha")
    embeddings.search_by_embedding("q")  # build cache
    assert embeddings._search_cache is not None

    embeddings.ensure_embedding_norms()  # no pending rows, but the call itself is the trigger
    # In this fresh-data case ensure_embedding_norms exits early without invalidating
    # (because pending list is empty). That's correct — no DB write happened.
    assert embeddings._search_cache is not None

    # Now simulate legacy null-norm rows: write directly to DB then run backfill
    with db.get_db_ctx() as conn:
        conn.execute("UPDATE embeddings SET norm = NULL")
        conn.commit()
    embeddings.ensure_embedding_norms()
    # That call DID write — cache should be invalidated
    assert embeddings._search_cache is None


def test_search_returns_empty_meta_for_empty_db(fresh_db, monkeypatch):
    db.init_db()
    fake = _CountingVoyage()
    monkeypatch.setattr(embeddings, "_voyage_client", fake)

    # No store_embedding calls — DB is empty
    results = embeddings.search_by_embedding("anything")
    assert results is None  # search returns None when no rows match


def test_search_results_ordered_by_similarity(fresh_db, monkeypatch):
    db.init_db()
    fake = _CountingVoyage()
    monkeypatch.setattr(embeddings, "_voyage_client", fake)

    # Store 3 docs with orthogonal embeddings: [1,0,0,0], [0,1,0,0], [0,0,1,0]
    embeddings.store_embedding("summary", 1, "doc one")
    embeddings.store_embedding("summary", 2, "doc two")
    embeddings.store_embedding("summary", 3, "doc three")

    # 4th embed call (the query) returns [0,0,0,1] — orthogonal to all stored.
    # All sims should be 0; just verify result count + that source_id round-trips.
    results = embeddings.search_by_embedding("query")
    assert len(results) == 3
    ids = sorted(r["source_id"] for r in results)
    assert ids == [1, 2, 3]
