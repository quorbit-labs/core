"""
QUORBIT Protocol — Key Rotation Pipeline (AGPL-3.0) — D5

Key lifecycle:
  - TTL: 7 days
  - Warning issued 24 h before expiry
  - Rotation request is signed by the OLD key (continuity proof)
  - Registry atomically replaces the public key on approval

CRL gossip:
  - Every 60 s, revoked key IDs are broadcast to peers (caller-driven)

Revocation types:
  - Normal:    request_rotation() + approve_rotation()
  - Forced:    force_revoke()  — triggered on QUARANTINE state
  - Emergency: emergency_revoke() — requires multi-sig (>= 2 operator signatures)

Redis key schema (DB=2, shared with registry):
  bus:rotation:{request_id}         — Hash, TTL=3600s
  bus:rotation_approval:{req_id}    — String (approver_id), TTL=3600s
  bus:emergency_revoke:{key_id}     — Hash of {operator_id -> signature}
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from typing import Dict, List, Optional

import redis

from .identity import AgentID, AgentIdentity, verify_signature

logger = logging.getLogger(__name__)

REDIS_DB = 2                  # shared with registry
REQUEST_TTL = 3_600           # rotation request valid for 1 h
CRL_GOSSIP_INTERVAL = 60      # seconds between CRL gossip broadcasts
EMERGENCY_MIN_SIGS = 2        # minimum operator signatures for emergency revoke

ROT_PREFIX = "bus:rotation:"
EMERG_PREFIX = "bus:emergency_revoke:"


class KeyRotationError(Exception):
    """Raised on invalid rotation request or insufficient authorisation."""


class KeyRotationManager:
    """
    Manages the key rotation pipeline.

    Parameters
    ----------
    registry_redis : redis.Redis
        The registry Redis client (DB=2).  Key rotation and CRL share this DB.
    """

    def __init__(self, redis_url: str = "redis://localhost:6379") -> None:
        self._redis = redis.Redis.from_url(
            redis_url, db=REDIS_DB, decode_responses=True
        )

    # ── Normal rotation ───────────────────────────────────────────────────

    def request_rotation(
        self,
        old_identity: AgentIdentity,
        new_identity: AgentIdentity,
    ) -> str:
        """
        Create and store a key rotation request signed by the old key.

        Returns a request_id that must be passed to approve_rotation().
        The request is valid for REQUEST_TTL seconds.
        """
        rotation_data = old_identity.request_key_rotation(new_identity)
        request_id = str(uuid.uuid4())

        record: Dict[str, str] = {
            "request_id": request_id,
            "old_agent_id": rotation_data["old_agent_id"],
            "new_agent_id": rotation_data["new_agent_id"],
            "payload": rotation_data["payload"],
            "signature": rotation_data["signature"],
            "key_version": str(rotation_data["key_version"]),
            "new_public_key": new_identity.public_key_hex,
            "created_at": str(time.time()),
            "status": "PENDING",
        }
        self._redis.hset(f"{ROT_PREFIX}{request_id}", mapping=record)
        self._redis.expire(f"{ROT_PREFIX}{request_id}", REQUEST_TTL)

        logger.info(
            "KeyRotation: request %s created for agent %s",
            request_id,
            rotation_data["old_agent_id"],
        )
        return request_id

    def approve_rotation(
        self,
        request_id: str,
        old_public_key_hex: str,
    ) -> bool:
        """
        Validate and approve a pending rotation request.

        Verifies the Ed25519 signature from the old key before swapping.
        Returns True if the rotation was applied; False on any failure.
        The caller (Registry) must atomically update bus:pubkey:{agent_id}.
        """
        record = self._redis.hgetall(f"{ROT_PREFIX}{request_id}")
        if not record:
            logger.warning("KeyRotation: request %s not found", request_id)
            return False
        if record.get("status") != "PENDING":
            logger.warning("KeyRotation: request %s already processed", request_id)
            return False

        payload_bytes = record["payload"].encode()
        signature = record["signature"]

        if not verify_signature(old_public_key_hex, payload_bytes, signature):
            logger.error(
                "KeyRotation: signature verification failed for request %s", request_id
            )
            self._redis.hset(f"{ROT_PREFIX}{request_id}", "status", "REJECTED")
            return False

        self._redis.hset(f"{ROT_PREFIX}{request_id}", "status", "APPROVED")
        logger.info(
            "KeyRotation: request %s approved — agent %s rotated to %s",
            request_id,
            record["old_agent_id"],
            record["new_agent_id"],
        )
        return True

    def get_request(self, request_id: str) -> Optional[Dict[str, str]]:
        """Return raw rotation request record or None."""
        data = self._redis.hgetall(f"{ROT_PREFIX}{request_id}")
        return data if data else None

    # ── Forced revocation (QUARANTINE) ────────────────────────────────────

    def force_revoke(
        self,
        key_id: str,
        agent_id: AgentID,
        reason: str = "QUARANTINE",
    ) -> None:
        """
        Immediately add key_id to the CRL.

        Must be called when an agent transitions to QUARANTINED state.
        Adds to bus:crl:{key_id} (Registry handles the actual Redis write).
        """
        # Store revocation record in the rotation namespace for auditing
        record: Dict[str, str] = {
            "key_id": key_id,
            "agent_id": agent_id,
            "reason": reason,
            "revoked_at": str(time.time()),
            "type": "FORCED",
        }
        rev_key = f"{ROT_PREFIX}revoked:{key_id}"
        self._redis.hset(rev_key, mapping=record)
        self._redis.expire(rev_key, 7 * 24 * 3600)

        logger.warning(
            "KeyRotation: FORCED revoke key %s for agent %s — %s",
            key_id,
            agent_id,
            reason,
        )

    # ── Emergency revocation (multi-sig) ─────────────────────────────────

    def submit_emergency_revoke_sig(
        self,
        key_id: str,
        operator_id: str,
        signature: str,
        payload: bytes,
        operator_public_key_hex: str,
    ) -> int:
        """
        Submit one operator signature for an emergency revoke of key_id.

        Returns the current number of valid signatures collected.
        Signatures are verified before being stored.
        """
        canonical = json.dumps(
            {"action": "emergency_revoke", "key_id": key_id},
            sort_keys=True,
        ).encode()
        if payload != canonical:
            raise KeyRotationError("Emergency revoke payload mismatch.")

        if not verify_signature(operator_public_key_hex, canonical, signature):
            raise KeyRotationError(
                f"Invalid operator signature from {operator_id!r}."
            )

        sig_key = f"{EMERG_PREFIX}{key_id}"
        self._redis.hset(sig_key, operator_id, signature)
        self._redis.expire(sig_key, REQUEST_TTL)

        count = self._redis.hlen(sig_key)
        logger.warning(
            "KeyRotation: emergency revoke sig %d/%d for key %s from operator %s",
            count,
            EMERGENCY_MIN_SIGS,
            key_id,
            operator_id,
        )
        return count

    def emergency_revoke(self, key_id: str) -> bool:
        """
        Execute emergency revocation if >= EMERGENCY_MIN_SIGS signatures exist.

        Returns True if the revoke was executed, False if insufficient sigs.
        The caller (Registry) must call revoke_key() on success.
        """
        sig_key = f"{EMERG_PREFIX}{key_id}"
        count = self._redis.hlen(sig_key)

        if count < EMERGENCY_MIN_SIGS:
            logger.warning(
                "KeyRotation: emergency revoke for key %s denied — "
                "only %d/%d signatures",
                key_id,
                count,
                EMERGENCY_MIN_SIGS,
            )
            return False

        self._redis.delete(sig_key)
        logger.critical(
            "KeyRotation: EMERGENCY REVOKE executed for key %s (%d sigs)",
            key_id,
            count,
        )
        return True

    # ── CRL gossip ────────────────────────────────────────────────────────

    def collect_crl_for_gossip(self, registry_redis: object) -> List[str]:
        """
        Collect all currently revoked key IDs from the Registry's CRL namespace.

        Call this every CRL_GOSSIP_INTERVAL seconds and broadcast the result
        to peers so they can update their local CRL views.

        Returns a list of revoked key IDs.
        """
        revoked: List[str] = []
        try:
            for key in registry_redis.scan_iter("bus:crl:*"):  # type: ignore[union-attr]
                key_id = key[len("bus:crl:"):]
                revoked.append(key_id)
        except Exception as exc:
            logger.error("KeyRotation: CRL gossip scan failed — %s", exc)
        logger.debug("KeyRotation: CRL gossip collected %d entries", len(revoked))
        return revoked
