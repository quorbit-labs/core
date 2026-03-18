"""
QUORBIT Protocol — Adaptive Detector Weights (AGPL-3.0) — D17

Observes active threat patterns and shifts detector weights accordingly.

Invariants:
  - Each weight ∈ [MIN_WEIGHT=10, MAX_WEIGHT=70]
  - sum(weights) == 100 at all times

Threat → weight adjustment:
  "collusion"  → boost collusion_graph, reduce others proportionally
  "symmetry"   → boost graph_symmetry, reduce others proportionally
  "sla"        → boost sla_cliff,       reduce others proportionally
"""

from __future__ import annotations

import logging
from typing import Dict

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

DETECTOR_NAMES = ("collusion_graph", "graph_symmetry", "sla_cliff")

BASE_WEIGHTS: Dict[str, int] = {
    "collusion_graph": 50,
    "graph_symmetry":  30,
    "sla_cliff":       20,
}

MIN_WEIGHT = 10
MAX_WEIGHT = 70
WEIGHT_STEP = 5      # points shifted per observed threat event
DECAY_STEP = 1       # points shifted back toward baseline per decay tick


# ── Adaptive Weights ───────────────────────────────────────────────────────────


class AdaptiveWeights:
    """
    Maintains detector weights and adjusts them based on threat observations.

    Parameters
    ----------
    initial : dict | None
        Override initial weights.  Must satisfy invariants; defaults to BASE_WEIGHTS.
    """

    def __init__(self, initial: Dict[str, int] | None = None) -> None:
        self._weights: Dict[str, int] = dict(initial or BASE_WEIGHTS)
        self._threat_counts: Dict[str, int] = {k: 0 for k in DETECTOR_NAMES}
        self._validate()

    # ── Observation ───────────────────────────────────────────────────────

    def observe_threat(self, threat_type: str) -> None:
        """
        Record one threat observation and rebalance weights.

        threat_type must be one of: "collusion", "symmetry", "sla".
        Maps to detector names: collusion_graph, graph_symmetry, sla_cliff.
        """
        mapping = {
            "collusion": "collusion_graph",
            "symmetry":  "graph_symmetry",
            "sla":       "sla_cliff",
        }
        detector = mapping.get(threat_type)
        if detector is None:
            logger.warning("AdaptiveWeights: unknown threat type %r", threat_type)
            return

        self._threat_counts[detector] += 1
        self._boost(detector, WEIGHT_STEP)
        logger.info(
            "AdaptiveWeights: observed '%s' → boosted %s → weights=%s",
            threat_type,
            detector,
            self._weights,
        )

    # ── Decay ─────────────────────────────────────────────────────────────

    def decay(self) -> None:
        """
        Gradually return weights toward the baseline.
        Call periodically (e.g. every 10 minutes) when no active threat is seen.
        """
        changed = False
        for detector in DETECTOR_NAMES:
            base = BASE_WEIGHTS[detector]
            current = self._weights[detector]
            if current > base:
                self._weights[detector] = max(base, current - DECAY_STEP)
                changed = True
            elif current < base:
                self._weights[detector] = min(base, current + DECAY_STEP)
                changed = True
        if changed:
            self._rebalance()

    # ── Query ─────────────────────────────────────────────────────────────

    def get_weights(self) -> Dict[str, int]:
        """Return a copy of current weights."""
        return dict(self._weights)

    def weight(self, detector_name: str) -> int:
        """Return current weight for a specific detector."""
        return self._weights.get(detector_name, 0)

    def threat_counts(self) -> Dict[str, int]:
        """Return cumulative threat observation counts per detector."""
        return dict(self._threat_counts)

    # ── Internals ─────────────────────────────────────────────────────────

    def _boost(self, target: str, step: int) -> None:
        """Increase target by step, reduce others proportionally, then clamp."""
        others = [d for d in DETECTOR_NAMES if d != target]
        gain = min(step, MAX_WEIGHT - self._weights[target])
        if gain <= 0:
            return

        # Reduce others proportionally (round-robin if ties)
        total_others = sum(self._weights[d] for d in others)
        remaining = gain
        for d in sorted(others, key=lambda d: -self._weights[d]):
            if remaining <= 0:
                break
            reducible = max(0, self._weights[d] - MIN_WEIGHT)
            cut = min(reducible, remaining)
            self._weights[d] -= cut
            remaining -= cut

        self._weights[target] += gain - remaining
        self._rebalance()

    def _rebalance(self) -> None:
        """
        Enforce bounds and ensure sum == 100.

        Algorithm:
          1. Clamp each weight to [MIN_WEIGHT, MAX_WEIGHT].
          2. Scale to restore sum == 100.
        """
        # Step 1: clamp
        for d in DETECTOR_NAMES:
            self._weights[d] = max(MIN_WEIGHT, min(MAX_WEIGHT, self._weights[d]))

        # Step 2: adjust to sum == 100
        total = sum(self._weights[d] for d in DETECTOR_NAMES)
        diff = 100 - total
        if diff == 0:
            return

        # Distribute diff to the largest (or smallest) detector
        if diff > 0:
            # Need more weight: add to the detector farthest below MAX
            target = max(DETECTOR_NAMES, key=lambda d: MAX_WEIGHT - self._weights[d])
        else:
            # Need less weight: remove from the detector farthest above MIN
            target = max(DETECTOR_NAMES, key=lambda d: self._weights[d] - MIN_WEIGHT)

        self._weights[target] = max(
            MIN_WEIGHT, min(MAX_WEIGHT, self._weights[target] + diff)
        )

    def _validate(self) -> None:
        for d in DETECTOR_NAMES:
            if d not in self._weights:
                self._weights[d] = BASE_WEIGHTS[d]
            self._weights[d] = max(MIN_WEIGHT, min(MAX_WEIGHT, self._weights[d]))
        total = sum(self._weights[d] for d in DETECTOR_NAMES)
        if total != 100:
            # Normalize proportionally
            for d in DETECTOR_NAMES:
                self._weights[d] = round(self._weights[d] * 100 / total)
            self._rebalance()

    def __repr__(self) -> str:
        return f"AdaptiveWeights({self._weights})"
