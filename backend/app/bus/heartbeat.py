"""
QUORBIT Protocol — Heartbeat Manager (AGPL-3.0)

Agents emit signed heartbeat signals at regular intervals to prove liveness.
The HeartbeatManager validates incoming heartbeats and updates the registry.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .identity import AgentID, AgentIdentity, verify_signature
from .registry import AgentRegistry


@dataclass
class HeartbeatMessage:
    """A signed heartbeat payload sent by an agent."""

    agent_id: AgentID
    public_key_hex: str  # raw Ed25519 public key (hex) — required for verification
    timestamp: float
    signature: bytes     # Ed25519 signature over encode()

    def encode(self) -> bytes:
        """Canonical byte representation that was signed."""
        return f"{self.agent_id}:{self.timestamp}".encode()


class HeartbeatManager:
    """
    Validates and processes heartbeat messages from agents.

    - Verifies the Ed25519 signature against public_key_hex.
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
        - Timestamp is stale (older than max_age seconds)
        - Signature is invalid
        - Agent is not registered
        """
        age = time.time() - msg.timestamp
        if age < 0 or age > self._max_age:
            return False

        if not verify_signature(msg.public_key_hex, msg.encode(), msg.signature):
            return False

        if not self._registry.touch(msg.agent_id):
            return False

        return True

    def build(
        self,
        identity: AgentIdentity,
        timestamp: float | None = None,
    ) -> HeartbeatMessage:
        """Build and sign a HeartbeatMessage from an AgentIdentity."""
        ts = timestamp if timestamp is not None else time.time()
        payload = f"{identity.agent_id}:{ts}".encode()
        return HeartbeatMessage(
            agent_id=identity.agent_id,
            public_key_hex=identity.public_key_hex,
            timestamp=ts,
            signature=identity.sign_raw(payload),
        )
