"""
QUORBIT Protocol — Anti-gaming Detectors (AGPL-3.0) — D12, D17

Three complementary detectors guard against reputation manipulation:

  collusion_graph  (weight=50, window=20)
    Detects agents that systematically validate each other in mutual cycles.
    Score = mutual_validation_ratio within the rolling window.

  graph_symmetry   (weight=30, window=50)
    Detects symmetric rating patterns (A rates B high and B rates A high,
    repeatedly, suggesting coordinated score inflation).
    Score = symmetry_coefficient over the rating history.

  sla_cliff        (weight=20, window=30)
    Detects agents that consistently complete tasks at exactly T-1 seconds
    (just under the SLA deadline), suggesting strategic sandbagging.
    Score = near_cliff_ratio over the last window completions.

Combined score: weighted_sum(scores) / 100  ∈ [0.0, 1.0]

Weights are managed by AdaptiveWeights and updated based on observed threats.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

from .adaptive_weights import AdaptiveWeights
from .graph_store import GraphStore

logger = logging.getLogger(__name__)

# ── Detector thresholds ───────────────────────────────────────────────────────

COLLUSION_THRESHOLD = 0.40   # mutual_ratio > 40% → suspicious
SYMMETRY_THRESHOLD = 0.50    # symmetry_coeff > 50% → suspicious
SLA_CLIFF_THRESHOLD = 0.60   # near_cliff_ratio > 60% → suspicious

COMBINED_SUSPICIOUS = 0.55   # combined score above this → flag agent


# ── Collusion Graph Detector ──────────────────────────────────────────────────


class CollusionGraphDetector:
    """
    Detects mutual-validation rings.

    add_validation(validator, validated, round_id) records that validator
    vouched for validated in a given consensus round.  If the relationship
    is frequently reciprocated within the window → suspicious.
    """

    def __init__(self, window: int = 20, graph_store: Optional[GraphStore] = None) -> None:
        self._window = window
        self._graph = graph_store or GraphStore()
        # (validator, validated) → deque of round_ids
        self._events: Dict[Tuple[str, str], Deque[str]] = defaultdict(
            lambda: deque(maxlen=window)
        )

    def add_validation(self, validator: str, validated: str, round_id: str) -> None:
        """Record a validation event."""
        key = (validator, validated)
        self._events[key].append(round_id)
        self._graph.add_edge(validator, validated, weight=1.0)

    def score(self, agent_id: str) -> float:
        """
        Suspicion score [0.0–1.0] for agent_id.

        Computes the fraction of outgoing validations in the window that have
        a reciprocal (mutual) validation from the validated agent.
        """
        # Count outgoing validations in the window
        out_pairs = [(v, d) for (v, d) in self._events if v == agent_id]
        if not out_pairs:
            return self._graph.suspicion_score(agent_id) * 0.5

        mutual_count = 0
        for _, validated in out_pairs:
            reverse_key = (validated, agent_id)
            if reverse_key in self._events and len(self._events[reverse_key]) > 0:
                mutual_count += 1

        mutual_ratio = mutual_count / len(out_pairs)
        # Blend with graph clustering coefficient
        cc = self._graph.clustering_coefficient(agent_id)
        return min(1.0, mutual_ratio * 0.7 + cc * 0.3)

    def detect(self, agent_id: str) -> bool:
        return self.score(agent_id) > COLLUSION_THRESHOLD


# ── Graph Symmetry Detector ───────────────────────────────────────────────────


@dataclass
class RatingEvent:
    rater: str
    rated: str
    rating: float     # normalised [0.0–1.0]
    timestamp: float = field(default_factory=time.time)


class GraphSymmetryDetector:
    """
    Detects symmetric rating inflation.

    Observes rating events and computes how often (A rates B high) ↔ (B rates A high).
    A symmetric pattern beyond chance indicates coordinated gaming.
    """

    def __init__(self, window: int = 50, high_rating_threshold: float = 0.80) -> None:
        self._window = window
        self._threshold = high_rating_threshold
        # agent_id → deque of RatingEvents (as rater)
        self._ratings: Dict[str, Deque[RatingEvent]] = defaultdict(
            lambda: deque(maxlen=window)
        )

    def add_rating(self, rater: str, rated: str, rating: float) -> None:
        """Record a rating event."""
        evt = RatingEvent(rater=rater, rated=rated, rating=rating)
        self._ratings[rater].append(evt)

    def score(self, agent_id: str) -> float:
        """
        Symmetry coefficient for agent_id [0.0–1.0].

        For each high-rating (agent → peer), check if peer also gave a high rating
        back (peer → agent).  Score = fraction of mutual high-ratings.
        """
        events = list(self._ratings.get(agent_id, []))
        high_out = [e for e in events if e.rating >= self._threshold]
        if not high_out:
            return 0.0

        symmetric = 0
        for evt in high_out:
            peer_events = list(self._ratings.get(evt.rated, []))
            for pe in peer_events:
                if pe.rated == agent_id and pe.rating >= self._threshold:
                    symmetric += 1
                    break

        return symmetric / len(high_out)

    def detect(self, agent_id: str) -> bool:
        return self.score(agent_id) > SYMMETRY_THRESHOLD


# ── SLA Cliff Detector ────────────────────────────────────────────────────────


@dataclass
class TaskCompletion:
    agent_id: str
    sla_limit: float       # seconds
    actual_time: float     # seconds
    timestamp: float = field(default_factory=time.time)

    @property
    def cliff_ratio(self) -> float:
        """How close to the SLA limit (1.0 = exactly at limit, 0.0 = instant)."""
        if self.sla_limit <= 0:
            return 0.0
        return min(1.0, self.actual_time / self.sla_limit)


class SlaCliffDetector:
    """
    Detects agents that consistently finish tasks just before the SLA deadline.

    A cliff ratio > 0.95 (finished in > 95% of allotted time) counts as
    a near-cliff completion.  If > 60% of completions are near-cliff → suspicious.
    """

    NEAR_CLIFF = 0.95   # completed in > 95% of SLA time → near-cliff

    def __init__(self, window: int = 30) -> None:
        self._window = window
        # agent_id → deque of TaskCompletion
        self._completions: Dict[str, Deque[TaskCompletion]] = defaultdict(
            lambda: deque(maxlen=window)
        )

    def add_completion(
        self,
        agent_id: str,
        sla_limit: float,
        actual_time: float,
    ) -> None:
        """Record a task completion time."""
        self._completions[agent_id].append(
            TaskCompletion(agent_id=agent_id, sla_limit=sla_limit, actual_time=actual_time)
        )

    def score(self, agent_id: str) -> float:
        """
        Near-cliff ratio for agent_id [0.0–1.0].

        Fraction of completions where cliff_ratio > NEAR_CLIFF.
        """
        completions = list(self._completions.get(agent_id, []))
        if not completions:
            return 0.0
        near_cliff = sum(1 for c in completions if c.cliff_ratio > self.NEAR_CLIFF)
        return near_cliff / len(completions)

    def detect(self, agent_id: str) -> bool:
        return self.score(agent_id) > SLA_CLIFF_THRESHOLD


# ── Combined Anti-gaming Detector ─────────────────────────────────────────────


class AntiGamingDetector:
    """
    Combines all three detectors with adaptive weights.

    Parameters
    ----------
    weights : AdaptiveWeights | None
        Weight manager.  If None, uses default BASE_WEIGHTS.
    graph_store : GraphStore | None
        Shared graph store for collusion detection.
    """

    def __init__(
        self,
        weights: Optional[AdaptiveWeights] = None,
        graph_store: Optional[GraphStore] = None,
    ) -> None:
        self._weights = weights or AdaptiveWeights()
        self._graph = graph_store or GraphStore()

        self.collusion = CollusionGraphDetector(graph_store=self._graph)
        self.symmetry = GraphSymmetryDetector()
        self.sla = SlaCliffDetector()

    # ── Observation forwarding ────────────────────────────────────────────

    def add_validation(self, validator: str, validated: str, round_id: str) -> None:
        self.collusion.add_validation(validator, validated, round_id)

    def add_rating(self, rater: str, rated: str, rating: float) -> None:
        self.symmetry.add_rating(rater, rated, rating)

    def add_completion(self, agent_id: str, sla_limit: float, actual_time: float) -> None:
        self.sla.add_completion(agent_id, sla_limit, actual_time)

    # ── Scoring ───────────────────────────────────────────────────────────

    def combined_score(self, agent_id: str) -> float:
        """
        Weighted combined suspicion score [0.0–1.0].

        combined = (w_coll * coll_score + w_sym * sym_score + w_sla * sla_score) / 100
        """
        w = self._weights.get_weights()
        s_coll = self.collusion.score(agent_id)
        s_sym = self.symmetry.score(agent_id)
        s_sla = self.sla.score(agent_id)

        combined = (
            w["collusion_graph"] * s_coll
            + w["graph_symmetry"] * s_sym
            + w["sla_cliff"] * s_sla
        ) / 100.0

        logger.debug(
            "AntiGaming: %s scores — collusion=%.3f sym=%.3f sla=%.3f combined=%.3f",
            agent_id, s_coll, s_sym, s_sla, combined,
        )
        return min(1.0, combined)

    def is_suspicious(
        self,
        agent_id: str,
        threshold: float = COMBINED_SUSPICIOUS,
    ) -> bool:
        """Return True if the combined suspicion score exceeds the threshold."""
        return self.combined_score(agent_id) > threshold

    def detector_scores(self, agent_id: str) -> Dict[str, float]:
        """Return individual detector scores for diagnostics."""
        return {
            "collusion_graph": self.collusion.score(agent_id),
            "graph_symmetry":  self.symmetry.score(agent_id),
            "sla_cliff":       self.sla.score(agent_id),
            "combined":        self.combined_score(agent_id),
        }

    def report_threat(self, threat_type: str) -> None:
        """Signal an observed threat to the adaptive weight manager."""
        self._weights.observe_threat(threat_type)
