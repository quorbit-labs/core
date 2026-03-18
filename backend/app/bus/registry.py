"""
QUORBIT Protocol — Authoritative Registry (AGPL-3.0)

The Registry is the SINGLE SOURCE OF TRUTH for agent state.
All writes must go through the Registry API — no direct Redis mutations.

Redis key schema (DB=2):
  bus:capability:{agent_id}  — Hash,          TTL=300s   (refreshed on heartbeat)
  bus:reputation:{agent_id}  — String (float), no TTL    (persistent score)
  bus:crl:{key_id}           — String (reason), TTL=key_ttl
  bus:pubkey:{agent_id}      — String (hex),   no TTL    (Ed25519 public key)

Reconciliation:
  Any entry absent from the Registry must be rejected and trigger an alert.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import redis

from .identity import AgentID, verify_signature

logger = logging.getLogger(__name__)

REDIS_DB = 2           # isolated DB for registry
CAP_PREFIX = "bus:capability:"
REP_PREFIX = "bus:reputation:"
CRL_PREFIX = "bus:crl:"
PUBKEY_PREFIX = "bus:pubkey:"

CAP_TTL = 300           # seconds — capability hash refreshed on heartbeat
CRL_TTL = 7 * 24 * 3600  # 7 days — mirrors key TTL


@dataclass
class AgentRecord:
    """Authoritative agent record returned by Registry lookups."""

    agent_id: AgentID
    name: str
    endpoint: Optional[str]
    registered_at: float
    last_seen: float
    reputation: float = 1.0

    def touch(self) -> None:
        self.last_seen = time.time()

    def is_alive(self, timeout: float = 90.0) -> bool:
        return (time.time() - self.last_seen) < timeout


class RegistryIntegrityError(Exception):
    """Raised by reconcile() when an agent is absent from the Registry."""


class AgentRegistry:
    """
    Redis-backed authoritative registry of QUORBIT agents.

    Parameters
    ----------
    redis_url : str
        Redis connection string.  DB=2 is selected unconditionally.
    """

    def __init__(self, redis_url: str = "redis://localhost:6379") -> None:
        self._redis = redis.Redis.from_url(
            redis_url, db=REDIS_DB, decode_responses=True
        )

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
        """
        if signed_payload is not None and signature is not None:
            if not verify_signature(public_key_hex, signed_payload, signature):
                raise ValueError(
                    f"Invalid Ed25519 registration signature for agent {agent_id!r}"
                )

        now = time.time()
        cap_key = f"{CAP_PREFIX}{agent_id}"
        cap_data: Dict[str, str] = {
            "name": name,
            "endpoint": endpoint or "",
            "registered_at": str(now),
            "last_seen": str(now),
        }
        if capabilities:
            cap_data.update(capabilities)

        pipe = self._redis.pipeline()
        pipe.set(f"{PUBKEY_PREFIX}{agent_id}", public_key_hex)
        pipe.hset(cap_key, mapping=cap_data)
        pipe.expire(cap_key, CAP_TTL)
        # Initialise reputation only if it does not exist
        pipe.setnx(f"{REP_PREFIX}{agent_id}", "1.0")
        pipe.execute()

        logger.info("Registry: registered agent %s", agent_id)
        return AgentRecord(
            agent_id=agent_id,
            name=name,
            endpoint=endpoint or None,
            registered_at=now,
            last_seen=now,
            reputation=1.0,
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

    def deregister(self, agent_id: AgentID) -> bool:
        """Remove an agent from the Registry. Returns True if it existed."""
        cap_key = f"{CAP_PREFIX}{agent_id}"
        existed = bool(self._redis.exists(cap_key))
        self._redis.delete(
            cap_key,
            f"{PUBKEY_PREFIX}{agent_id}",
        )
        logger.info("Registry: deregistered agent %s (existed=%s)", agent_id, existed)
        return existed

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
        """Add *key_id* to the Certificate Revocation List with an optional reason."""
        self._redis.set(f"{CRL_PREFIX}{key_id}", reason, ex=ttl)
        logger.warning("Registry: key %s added to CRL — %s", key_id, reason)

    def is_revoked(self, key_id: str) -> bool:
        """Return True if *key_id* appears in the CRL."""
        return bool(self._redis.exists(f"{CRL_PREFIX}{key_id}"))

    # ── Reconciliation ────────────────────────────────────────────────────

    def reconcile(self, agent_id: AgentID) -> None:
        """
        Assert that *agent_id* exists in the Registry.

        Raises RegistryIntegrityError (and logs an alert) if not found.
        Callers must reject the triggering request on this exception.
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
        )

    def get_public_key(self, agent_id: AgentID) -> Optional[str]:
        """Return the raw Ed25519 public key (hex) for an agent, or None."""
        return self._redis.get(f"{PUBKEY_PREFIX}{agent_id}")

    def is_registered(self, agent_id: AgentID) -> bool:
        return bool(self._redis.exists(f"{CAP_PREFIX}{agent_id}"))

    def all(self) -> List[AgentRecord]:
        """Return all registered agents (Redis SCAN — avoid in hot paths)."""
        records: List[AgentRecord] = []
        for key in self._redis.scan_iter(f"{CAP_PREFIX}*"):
            agent_id = AgentID(key[len(CAP_PREFIX):])
            record = self.get(agent_id)
            if record:
                records.append(record)
        return records

    def alive(self, timeout: float = 90.0) -> List[AgentRecord]:
        return [r for r in self.all() if r.is_alive(timeout)]

    def count(self) -> int:
        return sum(1 for _ in self._redis.scan_iter(f"{CAP_PREFIX}*"))

    # ── Dunder ────────────────────────────────────────────────────────────

    def __contains__(self, agent_id: AgentID) -> bool:
        return self.is_registered(agent_id)

    def __repr__(self) -> str:
        return f"AgentRegistry(redis_db={REDIS_DB})"
