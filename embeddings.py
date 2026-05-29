"""VoyageAI embedding client + vectorized cosine similarity search."""

import threading

from config import VOYAGE_API_KEY, VOYAGE_MODEL, logger
from db import get_db_ctx

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

try:
    import voyageai
    HAS_VOYAGE = True
except ImportError:
    HAS_VOYAGE = False


_voyage_client = None

# In-memory matrix cache for vector search.
#
# Without it, every search rebuilds the matrix from SQLite (SELECT N rows + N
# np.frombuffer calls + matrix allocation). With ~5K embeddings that's ~80ms
# per query just for the build; with the cache it's ~3ms (just the matmul).
#
# Invalidated on any store_embedding() write. Lazy build on first search.
# Locked because background _bg_executor in ai.py can write while a UI
# request is reading.
_search_cache = None       # (mat: ndarray N×D, norms: ndarray N, meta: list[dict])
_search_cache_lock = threading.Lock()


def _invalidate_search_cache():
    """Drop the cached matrix. Called on every embedding write."""
    global _search_cache
    with _search_cache_lock:
        _search_cache = None


def get_voyage_client():
    global _voyage_client
    if _voyage_client is not None:
        return _voyage_client
    if not HAS_VOYAGE or not VOYAGE_API_KEY:
        return None
    _voyage_client = voyageai.Client(api_key=VOYAGE_API_KEY)
    return _voyage_client


def _serialize_embedding(vec):
    if HAS_NUMPY:
        return np.asarray(vec, dtype=np.float32).tobytes()
    import struct
    return struct.pack(f'{len(vec)}f', *vec)


def _deserialize_embedding(blob):
    if HAS_NUMPY:
        return np.frombuffer(blob, dtype=np.float32)
    import struct
    n = len(blob) // 4
    return struct.unpack(f'{n}f', blob)


def _vec_norm(vec):
    if HAS_NUMPY:
        return float(np.linalg.norm(vec))
    return sum(x * x for x in vec) ** 0.5


def ensure_embedding_norms():
    """Backfill `norm` column for legacy rows (called once at startup)."""
    with get_db_ctx() as conn:
        pending = conn.execute(
            "SELECT id, embedding FROM embeddings WHERE norm IS NULL OR norm = 0"
        ).fetchall()
        if not pending:
            return
        logger.info("Backfilling %d embedding norms...", len(pending))
        for row in pending:
            vec = _deserialize_embedding(row["embedding"])
            conn.execute("UPDATE embeddings SET norm = ? WHERE id = ?",
                         (_vec_norm(vec), row["id"]))
        conn.commit()
    # Norms changed under us — drop the cache so the next search uses the
    # fresh values instead of the lazily-computed (and potentially mismatched)
    # norms from the prior cache build.
    _invalidate_search_cache()


def embed_texts(texts):
    """Return a list[list[float]] or None."""
    vo = get_voyage_client()
    if not vo:
        return None
    try:
        result = vo.embed(texts, model=VOYAGE_MODEL)
        return result.embeddings
    except Exception as e:
        logger.warning("Embedding 失敗: %s", e)
        return None


def store_embedding(source_type, source_id, text):
    vecs = embed_texts([text[:8000]])
    if not vecs:
        return False
    vec = vecs[0]
    blob = _serialize_embedding(vec)
    norm = _vec_norm(vec)
    with get_db_ctx() as conn:
        # UPSERT (rather than INSERT OR REPLACE) so re-embedding an existing
        # row keeps its original `created_at` instead of resetting to now.
        conn.execute("""
            INSERT INTO embeddings (source_type, source_id, chunk_text, embedding, norm)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source_type, source_id) DO UPDATE SET
                chunk_text = excluded.chunk_text,
                embedding = excluded.embedding,
                norm = excluded.norm
        """, (source_type, source_id, text[:2000], blob, norm))
        conn.commit()
    # The new row changes search results; drop the cache so the next search
    # rebuilds. Cheap (just clears a ref) — rebuild only happens on next read.
    _invalidate_search_cache()
    return True


def _build_search_matrix(dim_hint=None):
    """Pull every embedding row into (mat, norms, meta). Sets the cache."""
    global _search_cache
    rows = []
    with get_db_ctx() as conn:
        cursor = conn.execute(
            "SELECT source_type, source_id, chunk_text, embedding, norm FROM embeddings"
        )
        for row in cursor:
            rows.append(row)
    if not rows:
        with _search_cache_lock:
            _search_cache = (None, None, [])
        return _search_cache

    # Derive dim from first row when caller didn't pass one (covers "fresh
    # cache, dim unknown" path).
    if dim_hint is None:
        first_vec = np.frombuffer(rows[0]["embedding"], dtype=np.float32)
        dim_hint = first_vec.shape[0]

    mat = np.empty((len(rows), dim_hint), dtype=np.float32)
    norms = np.empty(len(rows), dtype=np.float32)
    meta = []
    for i, row in enumerate(rows):
        vec = np.frombuffer(row["embedding"], dtype=np.float32)
        if vec.shape[0] != dim_hint:
            # Mismatched-dim rows (model upgrade?) get zeroed so they never
            # match anything. They'll be rebuilt fresh next time embeddings
            # are written.
            mat[i] = 0.0
            norms[i] = 0.0
        else:
            mat[i] = vec
            stored = row["norm"]
            norms[i] = stored if stored else float(np.linalg.norm(vec))
        meta.append({
            "source_type": row["source_type"],
            "source_id": row["source_id"],
            "chunk_text": row["chunk_text"],
        })
    with _search_cache_lock:
        _search_cache = (mat, norms, meta)
    return _search_cache


def _get_or_build_cache(dim_hint=None):
    """Return the cached (mat, norms, meta), building from DB if missing."""
    with _search_cache_lock:
        cache = _search_cache
    if cache is None:
        cache = _build_search_matrix(dim_hint)
    return cache


def _search_numpy(q_vec, q_norm, limit):
    mat, norms, meta = _get_or_build_cache(dim_hint=q_vec.shape[0])
    if mat is None or not meta:
        return None

    # Dim mismatch between cached matrix and query vector — rebuild from
    # scratch with the new dim. Happens after model upgrade.
    if mat.shape[1] != q_vec.shape[0]:
        _invalidate_search_cache()
        mat, norms, meta = _build_search_matrix(dim_hint=q_vec.shape[0])
        if mat is None or not meta:
            return None

    dots = mat @ q_vec
    denom = norms * q_norm
    sims = np.where(denom > 0, dots / np.where(denom == 0, 1, denom), 0.0)

    k = min(limit, sims.shape[0])
    if k == 0:
        return None
    top_idx = np.argpartition(-sims, k - 1)[:k]
    top_idx = top_idx[np.argsort(-sims[top_idx])]

    return [
        {
            "source_type": meta[i]["source_type"],
            "source_id": meta[i]["source_id"],
            "chunk_text": meta[i]["chunk_text"],
            "similarity": float(sims[i]),
        }
        for i in top_idx
    ]


def _search_fallback(q_vec, q_norm, limit):
    import heapq
    heap = []
    counter = 0
    with get_db_ctx() as conn:
        cursor = conn.execute(
            "SELECT source_type, source_id, chunk_text, embedding, norm FROM embeddings"
        )
        for row in cursor:
            e_vec = _deserialize_embedding(row["embedding"])
            norm_e = row["norm"] or _vec_norm(e_vec)
            if not norm_e:
                continue
            dot = sum(a * b for a, b in zip(q_vec, e_vec))
            sim = dot / (q_norm * norm_e)
            counter += 1
            item = (sim, counter, row["source_type"], row["source_id"], row["chunk_text"])
            if len(heap) < limit:
                heapq.heappush(heap, item)
            elif sim > heap[0][0]:
                heapq.heapreplace(heap, item)

    if not heap:
        return None
    heap.sort(key=lambda x: x[0], reverse=True)
    return [
        {"source_type": st, "source_id": sid, "chunk_text": ct, "similarity": sim}
        for sim, _, st, sid, ct in heap
    ]


def search_by_embedding(query, limit=10):
    vecs = embed_texts([query])
    if not vecs:
        return None
    q_raw = vecs[0]
    if HAS_NUMPY:
        q_vec = np.asarray(q_raw, dtype=np.float32)
        q_norm = float(np.linalg.norm(q_vec))
        if q_norm == 0:
            return []
        return _search_numpy(q_vec, q_norm, limit)

    q_norm = _vec_norm(q_raw)
    if q_norm == 0:
        return []
    return _search_fallback(q_raw, q_norm, limit)
