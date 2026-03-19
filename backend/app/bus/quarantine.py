"""
QUORBIT Protocol — Quarantine & Blocklist (AGPL-3.0) — D9

Blocklist propagation spans all three layers:
  Registry  — agent state set to QUARANTINED
  Local     — in-memory set for O(1) hot-path checks
  Bazaar    — Redis blocklist key for cross-node propagation

Trigger conditions (either):
  • reputation score < 0.20
  • policy breach count ≥ 3

Actions on quarantine:
  1. Forced key revoke (added to Registry CRL)
  2. State → QUARANTINED in Registry
  3. Redis key: bus:blocklist:{agent_id}  (permanent, no TTL)
  4. Local in-memory set updated atomically

Rehabilitation path:
  • minimum 30-day cooling period (checked via quarantine_log timestamp)
  • operator explicit approval required

Redis key schema (DB=2):
  bus:blocklist:{agent_id}  — String (reason), permanent
  bus:breach:{agent_id}     — String (int count), no TTL
"""

from __future__ import annotations

import logging
import time
from typing import Optional, Set

import redis

from .identity import AgentID
from .registry import AgentRegistry, AgentState

logger = logging.getLogger(__name__)

BLOCKLIST_PREFIX = "bus:blocklist:"
BREACH_PREFIX = "bus:breach:"

SCORE_THRESHOLD = 0.20          # below this → auto-quarantine
BREACH_THRESHOLD = 3            # 3 or more breaches → auto-quarantine
REHABILITATION_DAYS = 30        # minimum days before re-admission
REHABILITATION_SECONDS = REHABILITATION_DAYS * 24 * 3600


class QuarantineError(Exception):
    """Raised when a quarantine operation fails."""


class RehabilitationError(Exception):
    """Raised when rehabilitation conditions are not yet met."""


class QuarantineManager:
    """
    Atomic quarantine & blocklist manager.

    Parameters
    ----------
    registry : AgentRegistry
        Authoritative registry (must share the same Redis instance).
    redis_client : redis.Redis
        Redis client connected to DB=2.
    """

    def __init__(
        self,
        registry: AgentRegistry,
        redis_client: redis.Redis,
    ) -> None:
        self._registry = registry
        self._redis = redis_client
        # Local in-memory fast-path blocklist
        self._local_blocklist: Set[AgentID] = set()
        # Sync local cache from Redis on init
        self._sync_local_cache()

    # ── Cache sync ────────────────────────────────────────────────────────

    def _sync_local_cache(self) -> None:
        """Rebuild the in-memory blocklist from Redis (called on init / reconnect)."""
        self._local_blocklist.clear()
        for key in self._redis.scan_iter(f"{BLOCKLIST_PREFIX}*"):
            agent_id = key[len(BLOCKLIST_PREFIX):]
            self._local_blocklist.add(AgentID(agent_id))

    # ── Core operations ───────────────────────────────────────────────────

    def propagate_quarantine(
        self,
        agent_id: AgentID,
        reason: str = "policy_violation",
    ) -> None:
        """
        Atomically quarantine an agent across all layers.

        1. Revoke the agent's key in the Registry CRL.
        2. Force-transition the Registry state to QUARANTINED.
        3. Write bus:blocklist:{agent_id} in Redis (permanent).
        4. Add agent_id to the local in-memory blocklist.

        This method is idempotent — calling it on an already-quarantined
        agent is safe and re-enforces the blocklist entry.
        """
        now = time.time()

        # Revoke the agent's active key (keyed by agent_id as key_id)
        try:
            self._registry.revoke_key(
                key_id=agent_id,
                reason=f"quarantine:{reason}",
                ttl=365 * 24 * 3600,  # 1 year in CRL
            )
        except Exception as exc:
            logger.error("QuarantineManager: CRL revoke failed for %s: %s", agent_id, exc)
            raise QuarantineError(f"Key revoke failed: {exc}") from exc

        # Force state transition (bypasses normal state-machine rules)
        try:
            self._registry.set_state(agent_id, AgentState.QUARANTINED, force=True)
        except Exception as exc:
            logger.error("QuarantineManager: state transition failed for %s: %s", agent_id, exc)
            raise QuarantineError(f"State transition failed: {exc}") from exc

        # Write to Redis blocklist (permanent — no TTL)
        pipe = self._redis.pipeline()
        pipe.set(f"{BLOCKLIST_PREFIX}{agent_id}", f"{reason}:{now}")
        pipe.execute()

        # Update local fast-path cache
        self._local_blocklist.add(agent_id)

        logger.warning(
            "QuarantineManager: agent %s QUARANTINED (%s) — blocklist propagated",
            agent_id,
            reason,
        )

    def is_blocked(self, agent_id: AgentID) -> bool:
        """
        Check whether an agent is blocklisted across all layers.

        Checks in order:
          1. Local in-memory set (O(1), fastest).
          2. Redis bus:blocklist:{agent_id} (cross-node).
          3. Registry state == QUARANTINED (authoritative fallback).

        Returns True if the agent is blocked in *any* layer and updates
        the local cache if a remote blocklist entry is discovered.
        """
        # Layer 1: local in-memory
        if agent_id in self._local_blocklist:
            return True

        # Layer 2: Redis blocklist
        if self._redis.exists(f"{BLOCKLIST_PREFIX}{agent_id}"):
            # Sync local cache
            self._local_blocklist.add(agent_id)
            return True

        # Layer 3: Registry state fallback
        try:
            state = self._registry.get_state(agent_id)
            if state == AgentState.QUARANTINED:
                # Back-fill Redis blocklist for consistency
                self._redis.set(
                    f"{BLOCKLIST_PREFIX}{agent_id}",
                    f"state_sync:{time.time()}",
                )
                self._local_blocklist.add(agent_id)
                return True
        except Exception:
            pass

        return False

    # ── Auto-quarantine triggers ──────────────────────────────────────────

    def check_and_quarantine(
        self,
        agent_id: AgentID,
        score: float,
    ) -> bool:
        """
        Evaluate quarantine conditions and enforce if triggered.

        Triggers if:
          • score < SCORE_THRESHOLD (0.20), or
          • breach count ≥ BREACH_THRESHOLD (3)

        Returns True if the agent was quarantined.
        """
        breach_count = self.get_breach_count(agent_id)

        if score < SCORE_THRESHOLD:
            reason = f"score_below_threshold:{score:.4f}"
            logger.warning(
                "QuarantineManager: auto-quarantine %s — score %.4f < %.2f",
                agent_id, score, SCORE_THRESHOLD,
            )
            self.propagate_quarantine(agent_id, reason)
            return True

        if breach_count >= BREACH_THRESHOLD:
            reason = f"breach_count:{breach_count}"
            logger.warning(
                "QuarantineManager: auto-quarantine %s — %d breaches ≥ %d",
                agent_id, breach_count, BREACH_THRESHOLD,
            )
            self.propagate_quarantine(agent_id, reason)
            return True

        return False

    # ── Breach tracking ───────────────────────────────────────────────────

    def record_breach(self, agent_id: AgentID) -> int:
        """Increment the breach counter for an agent. Returns new count."""
        count = self._redis.incr(f"{BREACH_PREFIX}{agent_id}")
        logger.info("QuarantineManager: breach recorded for %s (total=%d)", agent_id, count)
        return int(count)

    def get_breach_count(self, agent_id: AgentID) -> int:
        """Return the current breach count for an agent (0 if none)."""
        val = self._redis.get(f"{BREACH_PREFIX}{agent_id}")
        return int(val) if val is not None else 0

    # ── Rehabilitation ────────────────────────────────────────────────────

    def is_eligible_for_rehabilitation(self, agent_id: AgentID) -> bool:
        """
        Return True iff the agent has served the minimum 30-day quarantine period.

        Checks bus:quarantine_log:{agent_id} in the Registry.
        """
        quarantined_at = self._registry.last_quarantine_at(agent_id)
        if quarantined_at is None:
            return False
        elapsed = time.time() - quarantined_at
        return elapsed >= REHABILITATION_SECONDS

    def rehabilitate(
        self,
        agent_id: AgentID,
        operator_id: str,
    ) -> None:
        """
        Lift quarantine and restore agent to PROBATIONARY state.

        Requires:
          • At least 30 days since quarantine (checked here).
          • Caller must pass operator_id (identity not verified here —
            AdminManager.require_multi_sig must be called by the caller).

        Raises RehabilitationError if conditions are not met.
        """
        if not self.is_eligible_for_rehabilitation(agent_id):
            quarantined_at = self._registry.last_quarantine_at(agent_id)
            if quarantined_at:
                days_elapsed = (time.time() - quarantined_at) / 86400
                raise RehabilitationError(
                    f"Agent {agent_id!r} has only served {days_elapsed:.1f} days "
                    f"of required {REHABILITATION_DAYS}-day quarantine."
                )
            raise RehabilitationError(
                f"Agent {agent_id!r} has no quarantine record."
            )

        # Remove from Redis blocklist
        self._redis.delete(f"{BLOCKLIST_PREFIX}{agent_id}")
        # Remove from local cache
        self._local_blocklist.discard(agent_id)
        # Reset breach counter
        self._redis.delete(f"{BREACH_PREFIX}{agent_id}")
        # Transition back to PROBATIONARY (operator override)
        self._registry.set_state(agent_id, AgentState.PROBATIONARY, force=True)

        logger.warning(
            "QuarantineManager: agent %s rehabilitated by operator %s → PROBATIONARY",
            agent_id,
            operator_id,
        )

    # ── Diagnostics ───────────────────────────────────────────────────────

    def blocklist_size(self) -> int:
        """Return the number of agents currently blocklisted in Redis."""
        return sum(1 for _ in self._redis.scan_iter(f"{BLOCKLIST_PREFIX}*"))

    def get_blocklist_reason(self, agent_id: AgentID) -> Optional[str]:
        """Return the blocklist reason string, or None if not blocked."""
        val = self._redis.get(f"{BLOCKLIST_PREFIX}{agent_id}")
        return val if val is not None else None

    def __repr__(self) -> str:
        return (
            f"QuarantineManager("
            f"local_blocked={len(self._local_blocklist)}, "
            f"thresholds=score<{SCORE_THRESHOLD}/breach≥{BREACH_THRESHOLD})"
        )
