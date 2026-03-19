"""
QUORBIT Protocol — Authoritative Registry (AGPL-3.0)

The Registry is the SINGLE SOURCE OF TRUTH for agent state.
All writes must go through the Registry API — no direct Redis mutations.

Redis key schema (DB=2):
  bus:capability:{agent_id}  — Hash,          TTL=300s   (refreshed on heartbeat)
  bus:reputation:{agent_id}  — String (float), no TTL    (persistent score)
  bus:crl:{key_id}           — String (reason), TTL=key_ttl
  bus:pubkey:{agent_id}      — String (hex),   no TTL    (Ed25519 public key)
  bus:state:{agent_id}       — String (AgentState), no TTL
  bus:quarantine_log:{aid}   — String (float timestamp), no TTL
  bus:shard:{shard_id}       — Set of agent_ids, no TTL

Reconciliation:
  Any entry absent from the Registry must be rejected and trigger an alert.

Sharding:
  shard_id = HMAC-SHA256(agent_id, server_salt) % num_shards
  Max 200 agents per shard.
"""

from __future__ import annotations

import enum
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import redis

from .identity import AgentID, verify_signature

logger = logging.getLogger(__name__)

REDIS_DB = 2
CAP_PREFIX = "bus:capability:"
REP_PREFIX = "bus:reputation:"
CRL_PREFIX = "bus:crl:"
PUBKEY_PREFIX = "bus:pubkey:"
STATE_PREFIX = "bus:state:"
QUARANTINE_LOG_PREFIX = "bus:quarantine_log:"
SHARD_PREFIX = "bus:shard:"

CAP_TTL = 300
CRL_TTL = 7 * 24 * 3600

MAX_AGENTS_PER_SHARD = 200
DEFAULT_NUM_SHARDS = 10


# ── Agent state machine ────────────────────────────────────────────────────────


class AgentState(str, enum.Enum):
    """
    Ordered agent lifecycle states.

    Transitions:
      PROBATIONARY  → ACTIVE            (eligibility met)
      ACTIVE        → DEGRADED          (missed heartbeats)
      ACTIVE        → SOFT_QUARANTINED  (policy violation)
      DEGRADED      → ACTIVE            (recovered)
      DEGRADED      → ISOLATED          (continued degradation)
      ISOLATED      → ACTIVE            (recovered)
      ISOLATED      → QUARANTINED       (forced by operator/detector)
      SOFT_QUARANTINED → ACTIVE         (cleared)
      SOFT_QUARANTINED → QUARANTINED    (escalated)
      QUARANTINED   → PROBATIONARY      (after key rotation + review)
    """

    PROBATIONARY = "PROBATIONARY"
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    ISOLATED = "ISOLATED"
    SOFT_QUARANTINED = "SOFT_QUARANTINED"
    QUARANTINED = "QUARANTINED"


# Valid state transitions
_VALID_TRANSITIONS: Dict[AgentState, set[AgentState]] = {
    AgentState.PROBATIONARY:     {AgentState.ACTIVE},
    AgentState.ACTIVE:           {AgentState.DEGRADED, AgentState.SOFT_QUARANTINED},
    AgentState.DEGRADED:         {AgentState.ACTIVE, AgentState.ISOLATED},
    AgentState.ISOLATED:         {AgentState.ACTIVE, AgentState.QUARANTINED},
    AgentState.SOFT_QUARANTINED: {AgentState.ACTIVE, AgentState.QUARANTINED},
    AgentState.QUARANTINED:      {AgentState.PROBATIONARY},
}


# ── Data structures ───────────────────────────────────────────────────────────


@dataclass
class AgentRecord:
    """Authoritative agent record returned by Registry lookups."""

    agent_id: AgentID
    name: str
    endpoint: Optional[str]
    registered_at: float
    last_seen: float
    reputation: float = 1.0
    state: AgentState = AgentState.PROBATIONARY

    def touch(self) -> None:
        self.last_seen = time.time()

    def is_alive(self, timeout: float = 90.0) -> bool:
        return (time.time() - self.last_seen) < timeout


class RegistryIntegrityError(Exception):
    """Raised by reconcile() when an agent is absent from the Registry."""


class InvalidStateTransitionError(Exception):
    """Raised when a requested state transition is not allowed."""


# ── Registry ──────────────────────────────────────────────────────────────────


class AgentRegistry:
    """
    Redis-backed authoritative registry of QUORBIT agents.

    Parameters
    ----------
    redis_url : str
        Redis connection string.  DB=2 is selected unconditionally.
    server_salt : bytes | None
        Salt used for shard key computation (HMAC-SHA256).
        If None, sharding uses a zero-salt (not recommended for production).
    num_shards : int
        Number of logical shards.  Default 10 → max 2 000 agents.
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        server_salt: Optional[bytes] = None,
        num_shards: int = DEFAULT_NUM_SHARDS,
    ) -> None:
        self._redis = redis.Redis.from_url(
            redis_url, db=REDIS_DB, decode_responses=True
        )
        self._salt = server_salt or b"\x00" * 32
        self._num_shards = num_shards

    # ── Sharding ──────────────────────────────────────────────────────────

    def _shard_id(self, agent_id: AgentID) -> int:
        """Deterministic shard assignment: HMAC-SHA256(agent_id, salt) % num_shards."""
        h = hmac.new(self._salt, agent_id.encode(), hashlib.sha256).digest()
        return int.from_bytes(h[:4], "big") % self._num_shards

    def _shard_key(self, agent_id: AgentID) -> str:
        return f"{SHARD_PREFIX}{self._shard_id(agent_id)}"

    def shard_count(self, shard_id: int) -> int:
        """Return the number of agents assigned to a shard."""
        return self._redis.scard(f"{SHARD_PREFIX}{shard_id}")

    def is_shard_full(self, agent_id: AgentID) -> bool:
        """True if the target shard already holds MAX_AGENTS_PER_SHARD agents."""
        return self.shard_count(self._shard_id(agent_id)) >= MAX_AGENTS_PER_SHARD

    # ── Registration ──────────────────────────────────────────────────────

    def register(
        self,
        agent_id: AgentID,
        public_key_hex: str,
        name: str = "",
        endpoint: Optional[str] = None,
        capabilities: Optional[Dict[str, str]] = None,
        signed_payload: Optional[bytes] = None,
        signature: Optional[str] = None,
    ) -> AgentRecord:
        """
        Register or re-register an agent.

        If *signed_payload* and *signature* are provided, the Ed25519 signature
        is verified against *public_key_hex* before any state is written.
        Raises ValueError on invalid signature.
        Raises RegistryIntegrityError if the target shard is full.
        """
        if signed_payload is not None and signature is not None:
            if not verify_signature(public_key_hex, signed_payload, signature):
                raise ValueError(
                    f"Invalid Ed25519 registration signature for agent {agent_id!r}"
                )

        if not self.is_registered(agent_id) and self.is_shard_full(agent_id):
            raise RegistryIntegrityError(
                f"Shard {self._shard_id(agent_id)} is full "
                f"(max {MAX_AGENTS_PER_SHARD} agents)."
            )

        now = time.time()
        cap_key = f"{CAP_PREFIX}{agent_id}"
        cap_data: Dict[str, str] = {
            "name": name,
            "endpoint": endpoint or "",
            "registered_at": str(now),
            "last_seen": str(now),
            "tasks_completed": "0",
        }
        if capabilities:
            cap_data.update(capabilities)

        pipe = self._redis.pipeline()
        pipe.set(f"{PUBKEY_PREFIX}{agent_id}", public_key_hex)
        pipe.hset(cap_key, mapping=cap_data)
        pipe.expire(cap_key, CAP_TTL)
        pipe.setnx(f"{REP_PREFIX}{agent_id}", "1.0")
        pipe.setnx(f"{STATE_PREFIX}{agent_id}", AgentState.PROBATIONARY.value)
        pipe.sadd(self._shard_key(agent_id), agent_id)
        pipe.execute()

        logger.info("Registry: registered agent %s (shard=%d)", agent_id, self._shard_id(agent_id))
        return AgentRecord(
            agent_id=agent_id,
            name=name,
            endpoint=endpoint or None,
            registered_at=now,
            last_seen=now,
            reputation=1.0,
            state=AgentState.PROBATIONARY,
        )

    def touch(self, agent_id: AgentID) -> bool:
        """Refresh capability TTL and last_seen timestamp (call on heartbeat receipt)."""
        cap_key = f"{CAP_PREFIX}{agent_id}"
        if not self._redis.exists(cap_key):
            return False
        now = time.time()
        pipe = self._redis.pipeline()
        pipe.hset(cap_key, "last_seen", str(now))
        pipe.expire(cap_key, CAP_TTL)
        pipe.execute()
        return True

    def increment_tasks(self, agent_id: AgentID, count: int = 1) -> int:
        """Increment the completed task counter for an agent. Returns new total."""
        cap_key = f"{CAP_PREFIX}{agent_id}"
        return int(self._redis.hincrby(cap_key, "tasks_completed", count))

    def deregister(self, agent_id: AgentID) -> bool:
        """Remove an agent from the Registry. Returns True if it existed."""
        cap_key = f"{CAP_PREFIX}{agent_id}"
        existed = bool(self._redis.exists(cap_key))
        pipe = self._redis.pipeline()
        pipe.delete(cap_key, f"{PUBKEY_PREFIX}{agent_id}", f"{STATE_PREFIX}{agent_id}")
        pipe.srem(self._shard_key(agent_id), agent_id)
        pipe.execute()
        logger.info("Registry: deregistered agent %s (existed=%s)", agent_id, existed)
        return existed

    # ── Agent state machine ───────────────────────────────────────────────

    def get_state(self, agent_id: AgentID) -> AgentState:
        """Return the current state of an agent. Defaults to PROBATIONARY."""
        val = self._redis.get(f"{STATE_PREFIX}{agent_id}")
        if val is None:
            return AgentState.PROBATIONARY
        try:
            return AgentState(val)
        except ValueError:
            return AgentState.PROBATIONARY

    def set_state(
        self,
        agent_id: AgentID,
        new_state: AgentState,
        force: bool = False,
    ) -> None:
        """
        Transition agent_id to new_state.

        Raises InvalidStateTransitionError if the transition is not allowed,
        unless force=True (operator override).

        QUARANTINE transitions also record a timestamp in quarantine_log.
        """
        current = self.get_state(agent_id)
        if not force and new_state not in _VALID_TRANSITIONS.get(current, set()):
            raise InvalidStateTransitionError(
                f"Cannot transition {agent_id!r}: {current} → {new_state}"
            )

        self._redis.set(f"{STATE_PREFIX}{agent_id}", new_state.value)

        if new_state == AgentState.QUARANTINED:
            self._redis.set(f"{QUARANTINE_LOG_PREFIX}{agent_id}", str(time.time()))
            logger.warning("Registry: agent %s → QUARANTINED", agent_id)
        else:
            logger.info("Registry: agent %s → %s", agent_id, new_state.value)

    def last_quarantine_at(self, agent_id: AgentID) -> Optional[float]:
        """Return the Unix timestamp of the agent's most recent quarantine, or None."""
        val = self._redis.get(f"{QUARANTINE_LOG_PREFIX}{agent_id}")
        return float(val) if val is not None else None

    # ── Reputation ────────────────────────────────────────────────────────

    def update_reputation(self, agent_id: AgentID, score: float) -> None:
        """Overwrite reputation score. Persistent — no TTL. Clamped to [0.0, 1.0]."""
        if not self.is_registered(agent_id):
            raise KeyError(f"Agent {agent_id!r} not found in Registry.")
        clamped = max(0.0, min(1.0, score))
        self._redis.set(f"{REP_PREFIX}{agent_id}", str(clamped))

    def get_reputation(self, agent_id: AgentID) -> float:
        val = self._redis.get(f"{REP_PREFIX}{agent_id}")
        return float(val) if val is not None else 0.0

    # ── CRL ───────────────────────────────────────────────────────────────

    def revoke_key(
        self,
        key_id: str,
        reason: str = "revoked",
        ttl: int = CRL_TTL,
    ) -> None:
        """Add *key_id* to the Certificate Revocation List."""
        self._redis.set(f"{CRL_PREFIX}{key_id}", reason, ex=ttl)
        logger.warning("Registry: key %s added to CRL — %s", key_id, reason)

    def is_revoked(self, key_id: str) -> bool:
        return bool(self._redis.exists(f"{CRL_PREFIX}{key_id}"))

    # ── Reconciliation ────────────────────────────────────────────────────

    def reconcile(self, agent_id: AgentID) -> None:
        """
        Assert that *agent_id* exists in the Registry.

        Raises RegistryIntegrityError (and logs an alert) if not found.
        """
        if not self.is_registered(agent_id):
            msg = (
                f"RECONCILIATION FAILURE: agent {agent_id!r} not in Registry — "
                "request rejected"
            )
            logger.error("ALERT: %s", msg)
            raise RegistryIntegrityError(msg)

    # ── Lookup ────────────────────────────────────────────────────────────

    def get(self, agent_id: AgentID) -> Optional[AgentRecord]:
        """Fetch a full AgentRecord from Redis, or None if not registered."""
        data = self._redis.hgetall(f"{CAP_PREFIX}{agent_id}")
        if not data:
            return None
        return AgentRecord(
            agent_id=agent_id,
            name=data.get("name", ""),
            endpoint=data.get("endpoint") or None,
            registered_at=float(data.get("registered_at", 0)),
            last_seen=float(data.get("last_seen", 0)),
            reputation=self.get_reputation(agent_id),
            state=self.get_state(agent_id),
        )

    def get_public_key(self, agent_id: AgentID) -> Optional[str]:
        return self._redis.get(f"{PUBKEY_PREFIX}{agent_id}")

    def is_registered(self, agent_id: AgentID) -> bool:
        return bool(self._redis.exists(f"{CAP_PREFIX}{agent_id}"))

    def all(self) -> List[AgentRecord]:
        records: List[AgentRecord] = []
        for key in self._redis.scan_iter(f"{CAP_PREFIX}*"):
            agent_id = AgentID(key[len(CAP_PREFIX):])
            record = self.get(agent_id)
            if record:
                records.append(record)
        return records

    def alive(self, timeout: float = 90.0) -> List[AgentRecord]:
        return [r for r in self.all() if r.is_alive(timeout)]

    def by_state(self, state: AgentState) -> List[AgentRecord]:
        """Return all agents currently in the given state."""
        return [r for r in self.all() if r.state == state]

    def count(self) -> int:
        return sum(1 for _ in self._redis.scan_iter(f"{CAP_PREFIX}*"))

    # ── PROBATIONARY restrictions (D14) ───────────────────────────────────

    def can_receive_tasks(self, agent_id: AgentID) -> bool:
        """
        Return True iff this agent is eligible to receive task assignments.

        PROBATIONARY agents are restricted to heartbeat-only mode and must
        NOT be assigned tasks.  Any other non-QUARANTINED active state is
        permitted.
        """
        state = self.get_state(agent_id)
        if state == AgentState.PROBATIONARY:
            logger.info(
                "Registry: task assignment rejected for PROBATIONARY agent %s",
                agent_id,
            )
            return False
        if state == AgentState.QUARANTINED:
            logger.warning(
                "Registry: task assignment rejected for QUARANTINED agent %s",
                agent_id,
            )
            return False
        return True

    def assign_task(self, agent_id: AgentID) -> None:
        """
        Attempt to mark a task assignment for an agent.

        Raises ValueError if the agent is PROBATIONARY or QUARANTINED,
        logging the rejection in both cases.
        """
        if not self.can_receive_tasks(agent_id):
            state = self.get_state(agent_id)
            msg = (
                f"Task assignment rejected: agent {agent_id!r} "
                f"is in {state.value} state and cannot receive tasks."
            )
            logger.warning("Registry: %s", msg)
            raise ValueError(msg)

    # ── Dunder ────────────────────────────────────────────────────────────

    def __contains__(self, agent_id: AgentID) -> bool:
        return self.is_registered(agent_id)

    def __repr__(self) -> str:
        return f"AgentRegistry(redis_db={REDIS_DB}, num_shards={self._num_shards})"
