# Copyright (c) 2026 Quorbit Labs
# SPDX-License-Identifier: AGPL-3.0-only
"""
Sprint 9.5 — Embedding model configuration with graceful degradation.

PATCH TARGET: backend/app/reputation/pgvector_store.py
Add this as the embedding provider used by the R7 pipeline.

Design decisions:
- Default: sentence-transformers/all-MiniLM-L6-v2 (384-dim, local, free)
  NOT OpenAI text-embedding-3-small (1536-dim) — avoids external dependency
- Fallback: if embedding fails, scoring degrades to cold_start mode
- The arch spec mentions 1536-dim — updated to 384 for local model.
  If OpenAI embeddings are desired later, change EMBEDDING_DIM and model.
"""

import logging
import hashlib
from typing import Optional

import numpy as np

logger = logging.getLogger("quorbit.embedding")

# Configuration — change these to switch embedding provider
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384
EMBEDDING_TIMEOUT_S = 5.0

# Lazy-loaded model instance
_model = None
_model_available = None  # None = not checked, True/False = result


def _load_model() -> bool:
    """
    Attempt to load the embedding model.
    Returns True if successful, False otherwise.
    """
    global _model, _model_available

    if _model_available is not None:
        return _model_available

    try:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(EMBEDDING_MODEL)
        _model_available = True
        logger.info(
            "Embedding model loaded: %s (%d-dim)",
            EMBEDDING_MODEL, EMBEDDING_DIM,
        )
        return True
    except ImportError:
        _model_available = False
        logger.warning(
            "sentence-transformers not installed. "
            "Scoring will use cold_start mode (declarative only). "
            "Install: pip install sentence-transformers"
        )
        return False
    except Exception as e:
        _model_available = False
        logger.warning(
            "Failed to load embedding model %s: %s. "
            "Scoring will use cold_start mode.",
            EMBEDDING_MODEL, e,
        )
        return False


def embed_text(text: str) -> Optional[np.ndarray]:
    """
    Generate embedding vector for text.

    Returns:
        np.ndarray of shape (EMBEDDING_DIM,) on success
        None if embedding is unavailable (model not loaded, error, etc.)

    When None is returned, the caller (scoring formula) should
    fall back to cold_start scoring (R9, declarative-only branch).
    """
    if not _load_model():
        return None

    try:
        vector = _model.encode(text, normalize_embeddings=True)
        return np.array(vector, dtype=np.float32)
    except Exception as e:
        logger.warning("Embedding failed for text (len=%d): %s", len(text), e)
        return None


def embed_available() -> bool:
    """Check if embedding is available without loading the model."""
    if _model_available is not None:
        return _model_available
    return _load_model()


def deterministic_fallback_vector(text: str) -> np.ndarray:
    """
    Deterministic pseudo-embedding based on text hash.

    NOT a real embedding — no semantic similarity.
    Used ONLY for testing and development when the real model
    is not installed. Never use for production scoring.
    """
    h = hashlib.sha256(text.encode()).digest()
    # Expand hash to EMBEDDING_DIM floats
    rng = np.random.RandomState(
        int.from_bytes(h[:4], "big")
    )
    vec = rng.randn(EMBEDDING_DIM).astype(np.float32)
    # Normalize
    vec = vec / np.linalg.norm(vec)
    return vec


# pgvector table DDL (for reference / migrations)
TASK_HISTORY_DDL = f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS task_history (
    id              BIGSERIAL PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    task_id         TEXT NOT NULL UNIQUE,
    task_embedding  vector({EMBEDDING_DIM}),
    outcome         TEXT NOT NULL CHECK (outcome IN ('success', 'failure', 'timeout', 'error')),
    score_delta     REAL,
    tokens_used     INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_task_history_agent
    ON task_history (agent_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_task_history_embedding
    ON task_history USING ivfflat (task_embedding vector_cosine_ops)
    WITH (lists = 100);
"""
