"""
QUORBIT Protocol — pgvector Pipeline (AGPL-3.0) — R7

Stores task embeddings and reputation history in PostgreSQL + pgvector.

Routing strategy:
  - top-5 candidates:   exact cosine similarity (ANN index)
  - >50 candidates:     average proxy (use mean embedding of agent's history)

The embed() method returns a deterministic 384-dim hash-based vector in
development/test mode.  Replace with a real sentence-transformer in production.

Schema: see docs/migrations/001_pgvector.sql
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

EMBEDDING_DIM = 384
TOP_K_EXACT = 5          # use exact cosine sim below this candidate count
PROXY_THRESHOLD = 50     # use avg-proxy cosine above this candidate count


class PgVectorStore:
    """
    pgvector-backed store for task embeddings and reputation history.

    Parameters
    ----------
    dsn : str | None
        PostgreSQL DSN (e.g. "postgresql://user:pw@host/db").
        If None, the store operates in dry-run / dev mode (no DB writes).
    """

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn
        self._conn: object = None
        self._available = False
        if dsn:
            self._connect()

    # ── Connection ────────────────────────────────────────────────────────

    def _connect(self) -> None:
        try:
            import psycopg2  # type: ignore[import]
            self._conn = psycopg2.connect(self._dsn)
            self._available = True
            logger.info("PgVectorStore: connected to %s", self._dsn)
        except Exception as exc:
            logger.warning("PgVectorStore: DB unavailable — %s", exc)
            self._available = False

    def _cursor(self) -> object:
        if not self._available or self._conn is None:
            raise RuntimeError("PgVectorStore: no active DB connection.")
        return self._conn.cursor()  # type: ignore[union-attr]

    # ── Embedding ─────────────────────────────────────────────────────────

    def embed(self, text: str) -> list[float]:
        """
        Generate a 384-dim unit-normalised embedding for text.

        Development mode: deterministic hash-based pseudo-embedding.
        Production: replace this method body with a sentence-transformer call,
        e.g. SentenceTransformer('all-MiniLM-L6-v2').encode(text).tolist()
        """
        import numpy as np

        seed = int(hashlib.sha256(text.encode()).hexdigest(), 16) % (2**31)
        rng = np.random.RandomState(seed)
        vec = rng.randn(EMBEDDING_DIM).astype(np.float64)
        norm = float(np.linalg.norm(vec))
        if norm > 1e-9:
            vec /= norm
        return vec.tolist()

    # ── task_history ──────────────────────────────────────────────────────

    def insert_task_history(
        self,
        agent_id: str,
        embedding: list[float],
        outcome: str,
        timestamp: Optional[float] = None,
    ) -> None:
        """Append one task result embedding to task_history."""
        if not self._available:
            return
        ts = timestamp if timestamp is not None else time.time()
        sql = """
            INSERT INTO task_history (agent_id, embedding, outcome, timestamp)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT DO NOTHING
        """
        try:
            cur = self._cursor()
            cur.execute(sql, (agent_id, embedding, outcome, ts))  # type: ignore[union-attr]
            self._conn.commit()  # type: ignore[union-attr]
        except Exception as exc:
            logger.error("PgVectorStore.insert_task_history failed: %s", exc)

    def task_fit_score(
        self,
        query_text: str,
        candidate_agent_ids: list[str],
    ) -> dict[str, float]:
        """
        Compute task-fit scores for candidates via cosine similarity.

        - ≤ TOP_K_EXACT candidates: exact cosine sim per candidate.
        - > PROXY_THRESHOLD candidates: average-proxy cosine (mean embedding).
        - Between: exact cosine sim (still manageable).

        Returns a dict {agent_id: similarity_score ∈ [0.0, 1.0]}.
        If the DB is unavailable, all scores default to 0.5.
        """
        if not self._available or not candidate_agent_ids:
            return {aid: 0.5 for aid in candidate_agent_ids}

        query_embedding = self.embed(query_text)
        n = len(candidate_agent_ids)

        if n > PROXY_THRESHOLD:
            return self._proxy_similarity(query_embedding, candidate_agent_ids)
        return self._exact_similarity(query_embedding, candidate_agent_ids)

    def _exact_similarity(
        self,
        query_embedding: list[float],
        agent_ids: list[str],
    ) -> dict[str, float]:
        """Exact cosine similarity from DB using ivfflat index."""
        results: dict[str, float] = {}
        sql = """
            SELECT 1 - (embedding <=> %s::vector) AS cosine_sim
            FROM task_history
            WHERE agent_id = %s
            ORDER BY embedding <=> %s::vector
            LIMIT 1
        """
        try:
            cur = self._cursor()
            for aid in agent_ids:
                cur.execute(sql, (query_embedding, aid, query_embedding))  # type: ignore[union-attr]
                row = cur.fetchone()  # type: ignore[union-attr]
                results[aid] = float(row[0]) if row else 0.5
        except Exception as exc:
            logger.error("PgVectorStore._exact_similarity failed: %s", exc)
            results = {aid: 0.5 for aid in agent_ids}
        return results

    def _proxy_similarity(
        self,
        query_embedding: list[float],
        agent_ids: list[str],
    ) -> dict[str, float]:
        """
        Average-proxy cosine similarity: compute mean embedding per agent
        from the last 50 tasks, then cosine-sim against query.
        """
        results: dict[str, float] = {}
        sql = """
            SELECT agent_id, AVG(embedding) AS mean_embedding
            FROM (
                SELECT agent_id, embedding
                FROM task_history
                WHERE agent_id = ANY(%s)
                ORDER BY timestamp DESC
                LIMIT 50
            ) recent
            GROUP BY agent_id
        """
        try:
            import numpy as np

            cur = self._cursor()
            cur.execute(sql, (agent_ids,))  # type: ignore[union-attr]
            rows = cur.fetchall()  # type: ignore[union-attr]

            q = np.array(query_embedding, dtype=np.float64)
            for agent_id, mean_emb in rows:
                m = np.array(mean_emb, dtype=np.float64)
                denom = np.linalg.norm(q) * np.linalg.norm(m)
                sim = float(np.dot(q, m) / denom) if denom > 1e-9 else 0.5
                results[agent_id] = max(0.0, min(1.0, (sim + 1.0) / 2.0))

            # Fill any missing agents with default
            for aid in agent_ids:
                if aid not in results:
                    results[aid] = 0.5
        except Exception as exc:
            logger.error("PgVectorStore._proxy_similarity failed: %s", exc)
            results = {aid: 0.5 for aid in agent_ids}
        return results

    # ── reputation_history ────────────────────────────────────────────────

    def insert_reputation_history(
        self,
        agent_id: str,
        score: float,
        delta: float,
        reason: str,
        timestamp: float,
        embedding: Optional[list[float]] = None,
    ) -> None:
        """Append one reputation event to the append-only history table."""
        if not self._available:
            return
        sql = """
            INSERT INTO reputation_history
                (agent_id, score, delta, reason, timestamp, embedding)
            VALUES (%s, %s, %s, %s, %s, %s)
        """
        try:
            cur = self._cursor()
            cur.execute(  # type: ignore[union-attr]
                sql, (agent_id, score, delta, reason, timestamp, embedding)
            )
            self._conn.commit()  # type: ignore[union-attr]
        except Exception as exc:
            logger.error("PgVectorStore.insert_reputation_history failed: %s", exc)

    # ── Cosine similarity helper (standalone) ─────────────────────────────

    @staticmethod
    def cosine_similarity(a: list[float], b: list[float]) -> float:
        """Compute cosine similarity between two equal-length embedding vectors."""
        import numpy as np

        av = np.array(a, dtype=np.float64)
        bv = np.array(b, dtype=np.float64)
        denom = float(np.linalg.norm(av) * np.linalg.norm(bv))
        return float(np.dot(av, bv) / denom) if denom > 1e-9 else 0.0

    def __repr__(self) -> str:
        return f"PgVectorStore(available={self._available}, dsn={self._dsn!r})"
