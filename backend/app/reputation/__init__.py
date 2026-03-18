"""
QUORBIT Protocol — reputation package (AGPL-3.0)

Reputation scoring and persistence:
- scoring:       EMA-based reputation engine, transparency scoring, divergence detection
- pgvector_store: pgvector task embeddings and append-only reputation history
"""

from .scoring import (
    INITIAL_SCORE,
    TASK_DELTAS,
    TASK_WEIGHT,
    TRANSPARENCY_DELTAS,
    TRANSPARENCY_WEIGHT,
    EMA_ALPHA,
    EMA_WINDOW,
    AgentReputation,
    ReputationEngine,
)
from .pgvector_store import PgVectorStore

__all__ = [
    "AgentReputation",
    "ReputationEngine",
    "PgVectorStore",
    "INITIAL_SCORE",
    "TASK_DELTAS",
    "TRANSPARENCY_DELTAS",
    "TASK_WEIGHT",
    "TRANSPARENCY_WEIGHT",
    "EMA_ALPHA",
    "EMA_WINDOW",
]
