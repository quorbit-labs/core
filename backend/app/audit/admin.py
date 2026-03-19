"""
QUORBIT Protocol — Admin Access Model (AGPL-3.0) — D6

Principle of least privilege via Redis ACL per-role.
Critical operations require ≥2 operator signatures.
Every admin action is recorded in the Merkle log and the session log.

Roles and privileges:
  heartbeat_writer  — write heartbeat data only
  reputation_reader — read reputation scores only
  admin             — full access (requires multi-sig for critical ops)

Critical operations (require ≥2 operators):
  • change_genesis_validators
  • force_quarantine
  • change_consensus_parameters

Redis key schema (DB=2):
  admin:session:{session_id} — Hash {operator_id, started_at, actions_count}
  admin:actions              — List of JSON action records (session recording)
  admin:operators            — Set of registered operator IDs
"""

from __future__ import annotations

import enum
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

import redis

from .merkle_log import MerkleLog

logger = logging.getLogger(__name__)

# Redis keys
SESSION_PREFIX = "admin:session:"
ACTIONS_KEY = "admin:actions"
OPERATORS_KEY = "admin:operators"

# Operations that require ≥2 operator signatures
CRITICAL_OPERATIONS: Set[str] = {
    "change_genesis_validators",
    "force_quarantine",
    "change_consensus_parameters",
}

# Minimum operators required for critical operations
MIN_CRITICAL_OPERATORS = 2


class AdminRole(str, enum.Enum):
    HEARTBEAT_WRITER = "heartbeat_writer"
    REPUTATION_READER = "reputation_reader"
    ADMIN = "admin"


# Redis ACL rules per role (descriptive — actual ACL commands
# are applied at Redis startup via redis.conf or ACL SETUSER)
ROLE_ACL: Dict[AdminRole, str] = {
    AdminRole.HEARTBEAT_WRITER: (
        "ACL SETUSER heartbeat_writer on ~bus:capability:* ~bus:state:* "
        "+hset +expire -@all"
    ),
    AdminRole.REPUTATION_READER: (
        "ACL SETUSER reputation_reader on ~bus:reputation:* "
        "+get -@all"
    ),
    AdminRole.ADMIN: (
        "ACL SETUSER admin on ~* +@all"
    ),
}


class InsufficientOperatorsError(Exception):
    """Raised when a critical operation is attempted without enough operators."""


class UnauthorizedOperatorError(Exception):
    """Raised when an unregistered operator attempts an admin action."""


@dataclass
class AdminSession:
    """Tracks a single operator's admin session."""

    session_id: str
    operator_id: str
    started_at: float
    actions: List[str] = field(default_factory=list)

    @property
    def actions_count(self) -> int:
        return len(self.actions)


class AdminManager:
    """
    Enforces the admin access model with multi-sig for critical operations.

    Parameters
    ----------
    redis_client : redis.Redis
        Redis client connected to DB=2.
    merkle_log : MerkleLog
        Merkle log for immutable audit trail.
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        merkle_log: MerkleLog,
    ) -> None:
        self._redis = redis_client
        self._merkle = merkle_log

    # ── Operator registry ─────────────────────────────────────────────────

    def register_operator(self, operator_id: str) -> None:
        """Add an operator to the registered set."""
        self._redis.sadd(OPERATORS_KEY, operator_id)
        logger.info("AdminManager: operator registered: %s", operator_id)

    def deregister_operator(self, operator_id: str) -> None:
        """Remove an operator from the registered set."""
        self._redis.srem(OPERATORS_KEY, operator_id)
        logger.info("AdminManager: operator deregistered: %s", operator_id)

    def is_registered_operator(self, operator_id: str) -> bool:
        """Return True if operator_id is in the registered operator set."""
        return bool(self._redis.sismember(OPERATORS_KEY, operator_id))

    def registered_operators(self) -> List[str]:
        """Return the list of all registered operator IDs."""
        return list(self._redis.smembers(OPERATORS_KEY))

    # ── Multi-sig enforcement ─────────────────────────────────────────────

    def require_multi_sig(
        self,
        operation: str,
        operators: List[str],
    ) -> None:
        """
        Enforce multi-operator approval for critical operations.

        For operations in CRITICAL_OPERATIONS, exactly MIN_CRITICAL_OPERATORS
        distinct, registered operator IDs must be provided.

        For non-critical operations, at least 1 registered operator is required.

        Raises InsufficientOperatorsError or UnauthorizedOperatorError.

        Parameters
        ----------
        operation : str
            The operation being requested.
        operators : List[str]
            Operator IDs co-signing this action.
        """
        # Deduplicate
        unique_ops = list(dict.fromkeys(operators))

        # Verify all operators are registered
        for op in unique_ops:
            if not self.is_registered_operator(op):
                raise UnauthorizedOperatorError(
                    f"Operator {op!r} is not a registered operator."
                )

        required = MIN_CRITICAL_OPERATORS if operation in CRITICAL_OPERATIONS else 1

        if len(unique_ops) < required:
            raise InsufficientOperatorsError(
                f"Operation {operation!r} requires ≥{required} distinct operators; "
                f"got {len(unique_ops)}: {unique_ops}"
            )

        logger.info(
            "AdminManager: multi-sig OK for %r (%d/%d operators: %s)",
            operation, len(unique_ops), required, unique_ops,
        )

    # ── Session management ────────────────────────────────────────────────

    def start_session(self, operator_id: str) -> str:
        """
        Begin a tracked admin session for an operator.

        Returns the session_id (UUID).
        """
        session_id = str(uuid.uuid4())
        started_at = time.time()

        self._redis.hset(
            f"{SESSION_PREFIX}{session_id}",
            mapping={
                "operator_id": operator_id,
                "started_at": str(started_at),
                "actions_count": "0",
            },
        )
        logger.info(
            "AdminManager: session %s started for operator %s",
            session_id, operator_id,
        )
        return session_id

    def end_session(self, session_id: str) -> None:
        """Close and record the end of an admin session."""
        self._redis.hset(f"{SESSION_PREFIX}{session_id}", "ended_at", str(time.time()))
        logger.info("AdminManager: session %s ended", session_id)

    # ── Action logging ────────────────────────────────────────────────────

    def log_admin_action(
        self,
        operator_id: str,
        operation: str,
        details: Dict,
        session_id: Optional[str] = None,
    ) -> None:
        """
        Record an admin action in both the session log and the Merkle log.

        Parameters
        ----------
        operator_id : str
            The operator performing the action.
        operation : str
            The operation name.
        details : Dict
            Additional context (sanitised — no secrets).
        session_id : str | None
            Active session ID (if any).
        """
        timestamp_ms = int(time.time() * 1000)
        record = {
            "operator_id": operator_id,
            "operation": operation,
            "details": details,
            "session_id": session_id,
            "timestamp_ms": timestamp_ms,
        }

        # Session recording: append to admin:actions list
        self._redis.rpush(ACTIONS_KEY, json.dumps(record))

        # Increment session action count
        if session_id:
            self._redis.hincrby(f"{SESSION_PREFIX}{session_id}", "actions_count", 1)

        # Append to Merkle log (immutable audit trail)
        try:
            self._merkle.append(
                operation=f"admin:{operation}",
                data=json.dumps({
                    "operator_id": operator_id,
                    "details": details,
                    "session_id": session_id,
                }),
            )
        except Exception as exc:
            logger.error(
                "AdminManager: failed to append to Merkle log: %s", exc
            )

        logger.info(
            "AdminManager: action logged — operator=%s operation=%s",
            operator_id, operation,
        )

    # ── Session inspection ────────────────────────────────────────────────

    def get_session(self, session_id: str) -> Optional[Dict]:
        """Return session metadata, or None if not found."""
        data = self._redis.hgetall(f"{SESSION_PREFIX}{session_id}")
        return data if data else None

    def get_action_log(self, limit: int = 100) -> List[Dict]:
        """Return the last *limit* admin action records."""
        raw = self._redis.lrange(ACTIONS_KEY, -limit, -1)
        return [json.loads(r) for r in raw]

    # ── ACL helpers ───────────────────────────────────────────────────────

    @staticmethod
    def get_acl_command(role: AdminRole) -> str:
        """Return the Redis ACL SETUSER command string for a given role."""
        return ROLE_ACL[role]

    @staticmethod
    def is_critical_operation(operation: str) -> bool:
        """Return True if this operation requires multi-sig approval."""
        return operation in CRITICAL_OPERATIONS

    def __repr__(self) -> str:
        n_ops = len(self.registered_operators())
        return f"AdminManager(registered_operators={n_ops})"
