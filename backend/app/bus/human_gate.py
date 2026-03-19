"""
QUORBIT Protocol — HumanGate (AGPL-3.0) — D13

Human-in-the-loop gate for ambiguous or high-risk agent decisions.

Design rules:
  • Rate-limited submission queue: humangate:pending  (Redis List)
  • Threshold is NEVER revealed to agents (opaque scoring)
  • Ambiguous cases bypass consensus and enter the pending queue directly
  • ≥1 operator required for approve/reject decisions
  • BusAI controls the rate-limit parameter at runtime
  • Every decision (submit / approve / reject) is logged

Redis key schema (DB=2):
  humangate:pending          — List of agent_ids awaiting review (FIFO)
  humangate:meta:{agent_id} — Hash  {reason, submitted_at, status}
  humangate:rate:{agent_id} — String (int submission count), TTL=rate_window_seconds
  humangate:log              — List of JSON decision records (append-only)
"""

from __future__ import annotations

import json
import logging
import time
from typing import Dict, List, Optional

import redis

from .identity import AgentID

logger = logging.getLogger(__name__)

# Redis keys
PENDING_KEY = "humangate:pending"
META_PREFIX = "humangate:meta:"
RATE_PREFIX = "humangate:rate:"
LOG_KEY = "humangate:log"

# Defaults (can be overridden by BusAI at runtime)
DEFAULT_RATE_LIMIT = 5          # max submissions per window per agent
DEFAULT_RATE_WINDOW = 3600      # window = 1 hour (seconds)

# Status values
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"


class HumanGateError(Exception):
    """Raised when a HumanGate operation fails."""


class RateLimitExceededError(HumanGateError):
    """Raised when submission rate limit is exceeded."""


class HumanGateManager:
    """
    Manages the human-in-the-loop review queue.

    Parameters
    ----------
    redis_client : redis.Redis
        Redis client connected to DB=2.
    rate_limit : int
        Max submissions per agent per rate_window_seconds.
    rate_window_seconds : int
        Rolling window for rate limiting.
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        rate_limit: int = DEFAULT_RATE_LIMIT,
        rate_window_seconds: int = DEFAULT_RATE_WINDOW,
    ) -> None:
        self._redis = redis_client
        self._rate_limit = rate_limit
        self._rate_window = rate_window_seconds

    # ── BusAI runtime configuration ──────────────────────────────────────

    def set_rate_limit(self, rate_limit: int, window_seconds: int) -> None:
        """
        Update the rate-limit parameters at runtime (called by BusAI).

        Parameters
        ----------
        rate_limit : int
            Maximum number of submissions per agent per window.
        window_seconds : int
            Rolling window duration in seconds.
        """
        if rate_limit < 1:
            raise ValueError("rate_limit must be ≥ 1")
        if window_seconds < 1:
            raise ValueError("window_seconds must be ≥ 1")
        self._rate_limit = rate_limit
        self._rate_window = window_seconds
        logger.info(
            "HumanGate: rate-limit updated to %d per %ds", rate_limit, window_seconds
        )

    def get_rate_limit(self) -> Dict[str, int]:
        """Return the current rate-limit configuration."""
        return {
            "rate_limit": self._rate_limit,
            "rate_window_seconds": self._rate_window,
        }

    # ── Rate limiting ─────────────────────────────────────────────────────

    def _check_rate_limit(self, agent_id: AgentID) -> None:
        """
        Enforce the per-agent submission rate limit.

        Uses a Redis counter with TTL equal to the rate window.
        Raises RateLimitExceededError if the limit is exceeded.

        NOTE: The actual threshold value is NOT returned to the caller —
        the error message only indicates that the limit was exceeded,
        not what the limit is (opaque by design).
        """
        rate_key = f"{RATE_PREFIX}{agent_id}"
        count = self._redis.incr(rate_key)
        if count == 1:
            # First submission in this window — set TTL
            self._redis.expire(rate_key, self._rate_window)
        if int(count) > self._rate_limit:
            logger.warning(
                "HumanGate: rate limit exceeded for agent %s (count=%d)", agent_id, count
            )
            raise RateLimitExceededError(
                f"Submission rate limit exceeded for agent {agent_id!r}. "
                "Please wait before resubmitting."
            )

    # ── Queue operations ──────────────────────────────────────────────────

    def submit(self, agent_id: AgentID, reason: str) -> None:
        """
        Submit an agent for human review.

        • Enforces the per-agent rate limit (threshold opaque to callers).
        • Adds agent_id to humangate:pending (FIFO list).
        • Records submission metadata.
        • Logs the decision event.

        Parameters
        ----------
        agent_id : AgentID
            The agent being submitted for review.
        reason : str
            Human-readable reason for submission (e.g. "ambiguous_output").
        """
        self._check_rate_limit(agent_id)

        submitted_at = time.time()
        meta_key = f"{META_PREFIX}{agent_id}"

        # Idempotent: skip if already pending
        existing_status = self._redis.hget(meta_key, "status")
        if existing_status == STATUS_PENDING:
            logger.info(
                "HumanGate: agent %s already in pending queue — skipping duplicate",
                agent_id,
            )
            return

        pipe = self._redis.pipeline()
        pipe.rpush(PENDING_KEY, agent_id)
        pipe.hset(
            meta_key,
            mapping={
                "reason": reason,
                "submitted_at": str(submitted_at),
                "status": STATUS_PENDING,
            },
        )
        pipe.execute()

        self._log_decision(
            event="submit",
            agent_id=agent_id,
            operator_id=None,
            reason=reason,
            timestamp=submitted_at,
        )
        logger.info(
            "HumanGate: agent %s submitted for review — reason: %s",
            agent_id,
            reason,
        )

    def approve(self, agent_id: AgentID, operator_id: str) -> None:
        """
        Approve an agent from the review queue.

        Requires at least one operator (operator_id must be non-empty).
        Removes the agent from the pending queue and updates metadata.

        Parameters
        ----------
        agent_id : AgentID
            The agent being approved.
        operator_id : str
            Identity of the approving operator (must be non-empty).
        """
        if not operator_id or not operator_id.strip():
            raise HumanGateError("Operator ID is required to approve a HumanGate case.")

        self._require_pending(agent_id)

        now = time.time()
        meta_key = f"{META_PREFIX}{agent_id}"

        pipe = self._redis.pipeline()
        pipe.lrem(PENDING_KEY, 0, agent_id)
        pipe.hset(
            meta_key,
            mapping={
                "status": STATUS_APPROVED,
                "decided_at": str(now),
                "decided_by": operator_id,
            },
        )
        pipe.execute()

        self._log_decision(
            event="approve",
            agent_id=agent_id,
            operator_id=operator_id,
            reason=None,
            timestamp=now,
        )
        logger.info(
            "HumanGate: agent %s APPROVED by operator %s", agent_id, operator_id
        )

    def reject(self, agent_id: AgentID, operator_id: str) -> None:
        """
        Reject an agent from the review queue.

        Requires at least one operator (operator_id must be non-empty).
        Removes the agent from the pending queue and updates metadata.

        Parameters
        ----------
        agent_id : AgentID
            The agent being rejected.
        operator_id : str
            Identity of the rejecting operator (must be non-empty).
        """
        if not operator_id or not operator_id.strip():
            raise HumanGateError("Operator ID is required to reject a HumanGate case.")

        self._require_pending(agent_id)

        now = time.time()
        meta_key = f"{META_PREFIX}{agent_id}"

        pipe = self._redis.pipeline()
        pipe.lrem(PENDING_KEY, 0, agent_id)
        pipe.hset(
            meta_key,
            mapping={
                "status": STATUS_REJECTED,
                "decided_at": str(now),
                "decided_by": operator_id,
            },
        )
        pipe.execute()

        self._log_decision(
            event="reject",
            agent_id=agent_id,
            operator_id=operator_id,
            reason=None,
            timestamp=now,
        )
        logger.info(
            "HumanGate: agent %s REJECTED by operator %s", agent_id, operator_id
        )

    # ── Queue inspection ──────────────────────────────────────────────────

    def pending_queue(self) -> List[AgentID]:
        """Return the current ordered list of agents awaiting review."""
        return [AgentID(a) for a in self._redis.lrange(PENDING_KEY, 0, -1)]

    def queue_length(self) -> int:
        """Return the number of agents currently in the pending queue."""
        return self._redis.llen(PENDING_KEY)

    def get_status(self, agent_id: AgentID) -> Optional[str]:
        """Return the current review status for an agent, or None if unknown."""
        return self._redis.hget(f"{META_PREFIX}{agent_id}", "status")

    def get_meta(self, agent_id: AgentID) -> Optional[Dict[str, str]]:
        """Return full metadata for an agent's review entry, or None."""
        meta = self._redis.hgetall(f"{META_PREFIX}{agent_id}")
        return meta if meta else None

    # ── Decision log ──────────────────────────────────────────────────────

    def _log_decision(
        self,
        event: str,
        agent_id: AgentID,
        operator_id: Optional[str],
        reason: Optional[str],
        timestamp: float,
    ) -> None:
        """Append a decision record to humangate:log (append-only)."""
        record = {
            "event": event,
            "agent_id": agent_id,
            "operator_id": operator_id,
            "reason": reason,
            "timestamp_ms": int(timestamp * 1000),
        }
        self._redis.rpush(LOG_KEY, json.dumps(record))

    def get_log(self, limit: int = 100) -> List[Dict]:
        """Return the last *limit* decision log entries (newest last)."""
        raw = self._redis.lrange(LOG_KEY, -limit, -1)
        return [json.loads(r) for r in raw]

    # ── Internal helpers ──────────────────────────────────────────────────

    def _require_pending(self, agent_id: AgentID) -> None:
        """Assert that agent_id is currently in the pending queue."""
        status = self.get_status(agent_id)
        if status != STATUS_PENDING:
            raise HumanGateError(
                f"Agent {agent_id!r} is not in the pending queue "
                f"(current status: {status!r})."
            )

    def __repr__(self) -> str:
        return (
            f"HumanGateManager("
            f"queue_length={self.queue_length()}, "
            f"rate_limit={self._rate_limit}/{self._rate_window}s)"
        )
