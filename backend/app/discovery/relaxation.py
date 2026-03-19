"""
QUORBIT Protocol — Discovery Relaxation / Escalation Levels (AGPL-3.0) — R5, R6

When the primary discovery round returns no viable candidates the query
is re-issued with progressively relaxed constraints.

Levels
──────
  Level 0 (default)
    cap_match threshold : 0.70
    TTL multiplier      : 1.0×  (normal gossip TTL)
    tag matching        : exact skill tags only
    exclude_list        : respected

  Level 1 (first relaxation — triggered at t=3 s with no results)
    cap_match threshold : 0.50
    TTL multiplier      : 1.5×  (longer gossip spread)
    tag matching        : exact + related tags
    exclude_list        : respected

  Level 2 (second relaxation — triggered at t=6 s with no results)
    cap_match threshold : 0.00  (accept any available agent)
    TTL multiplier      : 2.0×
    tag matching        : any available agent
    exclude_list        : respected (R6 — already-tried agents excluded)

  After Level 2 with no results → caller falls back to self_execute.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Set

logger = logging.getLogger(__name__)


class RelaxationLevel(enum.IntEnum):
    STRICT = 0       # cap_match ≥ 0.70
    RELAXED = 1      # cap_match ≥ 0.50
    BROAD = 2        # any available agent


# Per-level parameters
_LEVEL_PARAMS = {
    RelaxationLevel.STRICT:  {"threshold": 0.70, "ttl_mult": 1.0, "tags": "exact"},
    RelaxationLevel.RELAXED: {"threshold": 0.50, "ttl_mult": 1.5, "tags": "related"},
    RelaxationLevel.BROAD:   {"threshold": 0.00, "ttl_mult": 2.0, "tags": "any"},
}


@dataclass
class RelaxationPolicy:
    """
    Tracks the current relaxation state for a single discovery round.

    Parameters
    ----------
    exclude_list : List[str]
        Agent IDs that must never be selected (R6 — already tried and
        failed or deliberately excluded by the caller).
    initial_level : RelaxationLevel
        Starting level (usually STRICT).
    """

    exclude_list: List[str] = field(default_factory=list)
    _level: RelaxationLevel = field(default=RelaxationLevel.STRICT, init=False)

    def __post_init__(self) -> None:
        self._exclude_set: Set[str] = set(self.exclude_list)

    # ── Level management ──────────────────────────────────────────────────

    @property
    def level(self) -> RelaxationLevel:
        return self._level

    def escalate(self) -> bool:
        """
        Advance to the next relaxation level.

        Returns True if escalation succeeded, False if already at maximum.
        """
        if self._level < RelaxationLevel.BROAD:
            self._level = RelaxationLevel(self._level + 1)
            logger.info(
                "RelaxationPolicy: escalated to level %d (%s) — threshold=%.2f",
                self._level,
                self._level.name,
                self.threshold,
            )
            return True
        return False

    @property
    def is_at_maximum(self) -> bool:
        """True if we have already reached the broadest level."""
        return self._level == RelaxationLevel.BROAD

    # ── Threshold & TTL ───────────────────────────────────────────────────

    @property
    def threshold(self) -> float:
        """Minimum cap_match score required at the current level."""
        return _LEVEL_PARAMS[self._level]["threshold"]

    @property
    def ttl_multiplier(self) -> float:
        """Gossip TTL multiplier at the current level."""
        return _LEVEL_PARAMS[self._level]["ttl_mult"]

    @property
    def tag_mode(self) -> str:
        """Tag matching strategy at the current level."""
        return _LEVEL_PARAMS[self._level]["tags"]

    # ── Exclude list (R6) ─────────────────────────────────────────────────

    def add_excluded(self, agent_id: str) -> None:
        """Add an agent to the exclude list (e.g. after a failed attempt)."""
        self._exclude_set.add(agent_id)

    def is_excluded(self, agent_id: str) -> bool:
        """Return True if agent_id is in the exclude list."""
        return agent_id in self._exclude_set

    def filter_candidates(self, candidates: list) -> list:
        """
        Remove excluded agents and those below the current threshold.

        Expects each candidate to have an attribute or key
        ``capability_match_score`` and ``agent_id``.
        """
        result = []
        for c in candidates:
            # Support both dataclass and dict candidates
            aid = c.agent_id if hasattr(c, "agent_id") else c["agent_id"]
            score = (
                c.capability_match_score
                if hasattr(c, "capability_match_score")
                else c["capability_match_score"]
            )
            if self.is_excluded(aid):
                logger.debug("RelaxationPolicy: excluded agent %s", aid)
                continue
            if float(score) < self.threshold:
                logger.debug(
                    "RelaxationPolicy: agent %s below threshold (%.3f < %.2f)",
                    aid, score, self.threshold,
                )
                continue
            result.append(c)
        return result

    def __repr__(self) -> str:
        return (
            f"RelaxationPolicy("
            f"level={self._level.name}, "
            f"threshold={self.threshold}, "
            f"excluded={len(self._exclude_set)})"
        )
