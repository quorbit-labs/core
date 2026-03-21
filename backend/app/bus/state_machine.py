"""
QUORBIT Protocol — Atomic State Transitions with Redlock (AGPL-3.0) — D18

Provides Compare-And-Set (CAS) semantics for agent state transitions using
a single-node Redlock-style distributed lock.

Lock protocol
─────────────
  1. SET bus:lock:state:{agent_id} <token> NX PX 5000
     (atomic acquire; NX = only if not exists; PX = TTL in ms)
  2. Read current state — verify it matches from_state.
  3. Write new state.
  4. Lua compare-and-delete: DEL the lock only if our token still matches.

If the current state does not match from_state the transition is a no-op
(returns False).  The lock is always released in the finally clause.

Merkle logging
──────────────
  Every committed transition appends to the MerkleLog (if provided).
  Operation: "state_transition"
  Data:      JSON {"agent_id", "from_state", "to_state"}
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

import redis

from .registry import AgentState, InvalidStateTransitionError, _VALID_TRANSITIONS

logger = logging.getLogger(__name__)

STATE_PREFIX = "bus:state:"
LOCK_PREFIX = "bus:lock:state:"
LOCK_TTL_MS = 5_000  # 5 seconds


# Lua script for atomic compare-and-delete (lock release)
_RELEASE_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


class LockAcquisitionError(Exception):
    """Raised when the distributed state-transition lock cannot be acquired."""


class StateMachine:
    """
    Distributed atomic state transition manager.

    Parameters
    ----------
    redis_client : redis.Redis
        Redis client connected to DB=2.
    merkle_log : MerkleLog | None
        If provided, every committed transition is appended to the log.
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        merkle_log=None,
    ) -> None:
        self._redis = redis_client
        self._merkle_log = merkle_log

    # ── Lock helpers ──────────────────────────────────────────────────────

    def _acquire_lock(self, agent_id: str) -> Optional[str]:
        """
        Attempt to acquire a distributed lock for agent_id.

        Returns the lock token (str) on success, None if already locked.
        """
        lock_key = f"{LOCK_PREFIX}{agent_id}"
        token = str(uuid.uuid4())
        acquired = self._redis.set(lock_key, token, nx=True, px=LOCK_TTL_MS)
        return token if acquired else None

    def _release_lock(self, agent_id: str, token: str) -> bool:
        """
        Release the lock for agent_id only if our token still matches.

        Uses a Lua script for atomic compare-and-delete.
        Returns True if the lock was released.
        """
        lock_key = f"{LOCK_PREFIX}{agent_id}"
        result = self._redis.eval(_RELEASE_LUA, 1, lock_key, token)
        return bool(result)

    # ── Public API ────────────────────────────────────────────────────────

    def atomic_transition(
        self,
        agent_id: str,
        from_state: AgentState,
        to_state: AgentState,
        force: bool = False,
    ) -> bool:
        """
        Atomically transition agent_id from from_state to to_state.

        Parameters
        ----------
        agent_id : str
            The agent to transition.
        from_state : AgentState
            Expected current state (CAS check).
        to_state : AgentState
            Target state.
        force : bool
            Skip _VALID_TRANSITIONS check (operator override).

        Returns
        -------
        bool
            True  — transition committed.
            False — current state did not match from_state (CAS failure).

        Raises
        ------
        InvalidStateTransitionError
            If the transition is not in _VALID_TRANSITIONS and force=False.
        LockAcquisitionError
            If the distributed lock cannot be acquired.
        """
        if not force and to_state not in _VALID_TRANSITIONS.get(from_state, set()):
            raise InvalidStateTransitionError(
                f"Transition {from_state.value} → {to_state.value} is not allowed."
            )

        token = self._acquire_lock(agent_id)
        if token is None:
            raise LockAcquisitionError(
                f"Could not acquire state lock for agent {agent_id!r} — try again."
            )

        try:
            state_key = f"{STATE_PREFIX}{agent_id}"
            current_val = self._redis.get(state_key)

            # Normalise: absent key == PROBATIONARY
            if current_val and current_val in AgentState._value2member_map_:
                current = AgentState(current_val)
            else:
                current = AgentState.PROBATIONARY

            if current != from_state:
                logger.warning(
                    "StateMachine: CAS failure for %s — expected %s, got %s",
                    agent_id, from_state.value, current.value,
                )
                return False

            self._redis.set(state_key, to_state.value)

            if self._merkle_log is not None:
                data = json.dumps({
                    "agent_id": agent_id,
                    "from_state": from_state.value,
                    "to_state": to_state.value,
                })
                self._merkle_log.append("state_transition", data)

            logger.info(
                "StateMachine: %s transitioned %s → %s (atomic)",
                agent_id, from_state.value, to_state.value,
            )
            return True

        finally:
            self._release_lock(agent_id, token)

    def __repr__(self) -> str:
        return (
            f"StateMachine("
            f"merkle_log={'attached' if self._merkle_log else 'none'})"
        )
