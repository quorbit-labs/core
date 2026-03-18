"""
QUORBIT Protocol — Reputation Engine (AGPL-3.0) — D11, R8

Score: float [0.0–1.0], initial = 0.75
Formula: reputation = task_component * 0.70 + transparency_component * 0.30

Deltas represent direct weighted contributions to the combined score.
EMA (window=100) is maintained as a smoothed shadow score for:
  - divergence detection (rolling 20 observations, flag at >2σ)
  - stable reporting without reacting to single extreme events

Storage:
  Live:    Redis  bus:reputation:{agent_id}  (float, no TTL)
  History: pgvector  reputation_history table (append-only, via PgVectorStore)
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from statistics import mean, stdev
from typing import Callable, Deque, Optional

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

INITIAL_SCORE: float = 0.75
TASK_WEIGHT: float = 0.70
TRANSPARENCY_WEIGHT: float = 0.30

EMA_WINDOW: int = 100
EMA_ALPHA: float = 2 / (EMA_WINDOW + 1)   # ≈ 0.0198

DIVERGENCE_WINDOW: int = 20   # rolling observations for divergence detection
DIVERGENCE_SIGMA: float = 2.0  # standard deviations threshold

# ── Task-outcome deltas (pre-weighted: already account for TASK_WEIGHT=0.70) ─
TASK_DELTAS: dict[str, float] = {
    "completed_on_time": +0.05,
    "completed_late":    +0.01,
    "abandoned":         -0.10,
    "validated":         +0.05,
    "flagged":           -0.15,
    "heartbeat_missed":  -0.02,
}

# ── Transparency deltas (pre-weighted: account for TRANSPARENCY_WEIGHT=0.30) ─
TRANSPARENCY_DELTAS: dict[str, float] = {
    "structured_error_response": +0.02,
    "result_marked_incorrect":   -0.05,
    "confirmed_hallucination":   -0.15,
    "fabricated_result":         -0.30,
}


# ── Per-agent state ────────────────────────────────────────────────────────────


@dataclass
class AgentReputation:
    """
    In-memory reputation state for one agent.

    Attributes
    ----------
    score : float
        Current combined reputation [0.0–1.0].
    ema_score : float
        EMA-smoothed score (alpha = 2/(100+1)) — slower to react, used for
        divergence detection and stable reporting.
    """

    agent_id: str
    _score: float = field(default=INITIAL_SCORE, repr=False)
    _ema: float = field(default=INITIAL_SCORE, repr=False)
    _history: Deque[float] = field(
        default_factory=lambda: deque(maxlen=DIVERGENCE_WINDOW), repr=False
    )

    @property
    def score(self) -> float:
        return self._score

    @property
    def ema_score(self) -> float:
        return self._ema

    def _apply_delta(self, delta: float) -> float:
        """Apply a delta to the direct score and update the EMA shadow."""
        self._score = max(0.0, min(1.0, self._score + delta))
        # EMA update: smooth toward the new direct score
        self._ema = EMA_ALPHA * self._score + (1 - EMA_ALPHA) * self._ema
        self._history.append(self._score)
        return self._score

    def apply_task_event(self, event: str) -> float:
        """Apply a task-outcome event and return the updated score."""
        if event not in TASK_DELTAS:
            raise ValueError(f"Unknown task event: {event!r}")
        return self._apply_delta(TASK_DELTAS[event])

    def apply_transparency_event(self, event: str) -> float:
        """Apply a transparency event and return the updated score."""
        if event not in TRANSPARENCY_DELTAS:
            raise ValueError(f"Unknown transparency event: {event!r}")
        return self._apply_delta(TRANSPARENCY_DELTAS[event])

    def is_divergent(self) -> bool:
        """
        Return True if the current score deviates > DIVERGENCE_SIGMA·σ
        from the rolling mean of the last DIVERGENCE_WINDOW observations.

        Requires at least 3 observations for meaningful statistics.
        """
        if len(self._history) < 3:
            return False
        mu = mean(self._history)
        try:
            sigma = stdev(self._history)
        except Exception:
            return False
        if sigma < 1e-9:
            return False
        return abs(self._score - mu) > DIVERGENCE_SIGMA * sigma

    def __repr__(self) -> str:
        return (
            f"AgentReputation(agent_id={self.agent_id!r}, "
            f"score={self._score:.4f}, ema={self._ema:.4f})"
        )


# ── Reputation Engine ──────────────────────────────────────────────────────────


class ReputationEngine:
    """
    Manages reputation scores for all agents.

    Parameters
    ----------
    redis_client : optional
        Live Redis client.  If None, scores are only kept in memory (test mode).
    pg_store : optional
        PgVectorStore for append-only history.  If None, history is skipped.
    on_divergence : optional
        Callback invoked when divergence is detected:  f(agent_id: str) -> None.
        Use this to trigger an AgentRegistry.set_state(PROBATIONARY) call.
    """

    def __init__(
        self,
        redis_client: Optional[object] = None,
        pg_store: Optional[object] = None,
        on_divergence: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._redis = redis_client
        self._pg = pg_store
        self._on_divergence = on_divergence
        self._agents: dict[str, AgentReputation] = {}

    # ── Internal ──────────────────────────────────────────────────────────

    def _get_or_create(self, agent_id: str) -> AgentReputation:
        if agent_id not in self._agents:
            rep = AgentReputation(agent_id=agent_id)
            # Restore live score from Redis if available
            if self._redis is not None:
                try:
                    val = self._redis.get(f"bus:reputation:{agent_id}")  # type: ignore[union-attr]
                    if val is not None:
                        rep._score = float(val)
                        rep._ema = float(val)
                except Exception:
                    pass
            self._agents[agent_id] = rep
        return self._agents[agent_id]

    def _persist(self, rep: AgentReputation, delta: float, reason: str) -> None:
        if self._redis is not None:
            try:
                self._redis.set(  # type: ignore[union-attr]
                    f"bus:reputation:{rep.agent_id}", str(rep.score)
                )
            except Exception:
                pass

        if self._pg is not None:
            try:
                embedding = self._pg.embed(reason)  # type: ignore[union-attr]
                self._pg.insert_reputation_history(  # type: ignore[union-attr]
                    agent_id=rep.agent_id,
                    score=rep.score,
                    delta=delta,
                    reason=reason,
                    timestamp=time.time(),
                    embedding=embedding,
                )
            except Exception:
                pass

    def _check_and_signal_divergence(self, rep: AgentReputation) -> None:
        if rep.is_divergent():
            logger.warning(
                "ReputationEngine: divergence detected for agent %s "
                "(score=%.4f, ema=%.4f) → PROBATIONARY",
                rep.agent_id,
                rep.score,
                rep.ema_score,
            )
            if self._on_divergence:
                self._on_divergence(rep.agent_id)

    # ── Public API ────────────────────────────────────────────────────────

    def get_score(self, agent_id: str) -> float:
        """Return the current reputation score (0.75 for unknown agents)."""
        return self._get_or_create(agent_id).score

    def get_ema_score(self, agent_id: str) -> float:
        """Return the EMA-smoothed reputation score."""
        return self._get_or_create(agent_id).ema_score

    def apply_task_event(self, agent_id: str, event: str) -> float:
        """Apply a task-outcome event and return the new score."""
        rep = self._get_or_create(agent_id)
        delta = TASK_DELTAS[event]
        new_score = rep.apply_task_event(event)
        self._persist(rep, delta, event)
        self._check_and_signal_divergence(rep)
        logger.debug(
            "Reputation: %s %s → %.4f (Δ%+.2f)",
            agent_id, event, new_score, delta,
        )
        return new_score

    def apply_transparency_event(self, agent_id: str, event: str) -> float:
        """Apply a transparency event and return the new score."""
        rep = self._get_or_create(agent_id)
        delta = TRANSPARENCY_DELTAS[event]
        new_score = rep.apply_transparency_event(event)
        self._persist(rep, delta, event)
        self._check_and_signal_divergence(rep)
        logger.debug(
            "Reputation: %s %s → %.4f (Δ%+.2f)",
            agent_id, event, new_score, delta,
        )
        return new_score

    def check_divergence(self, agent_id: str) -> bool:
        """Return True if the agent's score shows statistically anomalous divergence."""
        return self._get_or_create(agent_id).is_divergent()

    def reset(self, agent_id: str, score: float = INITIAL_SCORE) -> None:
        """Reset an agent's score (e.g. after returning from QUARANTINE)."""
        rep = self._get_or_create(agent_id)
        rep._score = max(0.0, min(1.0, score))
        rep._ema = rep._score
        rep._history.clear()
        self._persist(rep, 0.0, "reset")
