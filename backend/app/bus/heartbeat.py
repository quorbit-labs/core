"""
QUORBIT Protocol — Heartbeat Manager (AGPL-3.0)

Agents emit signed heartbeat signals at regular intervals to prove liveness.
The HeartbeatManager validates incoming heartbeats and updates the registry.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .identity import AgentID, verify_signature
from .registry import AgentRegistry


@dataclass
class HeartbeatMessage:
    """A signed heartbeat payload sent by an agent."""

    agent_id: AgentID
    timestamp: float
    signature: bytes  # Ed25519 signature over f"{agent_id}:{timestamp}"

    def encode(self) -> bytes:
        """Canonical byte representation that was signed."""
        return f"{self.agent_id}:{self.timestamp}".encode()


class HeartbeatManager:
    """
    Validates and processes heartbeat messages from agents.

    - Verifies the Ed25519 signature against the AgentID (public key).
    - Rejects replayed heartbeats (timestamp must be within `max_age` seconds).
    - Updates registry last_seen on success.
    """

    def __init__(
        self,
        registry: AgentRegistry,
        max_age: float = 60.0,
    ) -> None:
        self._registry = registry
        self._max_age = max_age

    def process(self, msg: HeartbeatMessage) -> bool:
        """
        Validate and record a heartbeat. Returns True on success.

        Rejects if:
        - Signature is invalid
        - Timestamp is stale (older than max_age seconds)
        - Agent is not registered
        """
        # 1. Timestamp freshness check
        age = time.time() - msg.timestamp
        if age < 0 or age > self._max_age:
            return False

        # 2. Signature verification
        if not verify_signature(msg.agent_id, msg.encode(), msg.signature):
            return False

        # 3. Registry update
        record = self._registry.get(msg.agent_id)
        if record is None:
            return False

        record.touch()
        return True

    def build(self, identity: object, timestamp: float | None = None) -> HeartbeatMessage:
        """
        Convenience: build and sign a HeartbeatMessage from an AgentIdentity.

        Usage:
            hb = manager.build(my_identity)
        """
        ts = timestamp if timestamp is not None else time.time()
        payload = f"{identity.agent_id}:{ts}".encode()  # type: ignore[union-attr]
        sig = identity.sign(payload)  # type: ignore[union-attr]
        return HeartbeatMessage(
            agent_id=identity.agent_id,  # type: ignore[union-attr]
            timestamp=ts,
            signature=sig,
        )
