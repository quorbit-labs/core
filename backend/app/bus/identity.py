"""
QUORBIT Protocol — Identity Module (AGPL-3.0)

Each agent holds an Ed25519 keypair. The public key is the agent's globally
unique identity (AgentID). All messages are signed for non-repudiation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import NewType

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

# AgentID is the hex-encoded Ed25519 public key (64 hex chars = 32 bytes)
AgentID = NewType("AgentID", str)


@dataclass
class AgentIdentity:
    """Holds an agent's Ed25519 keypair and provides sign/verify helpers."""

    _private_key: Ed25519PrivateKey = field(repr=False)

    # ── Construction ──────────────────────────────────────────────────────

    @classmethod
    def generate(cls) -> "AgentIdentity":
        """Generate a new random Ed25519 keypair."""
        return cls(_private_key=Ed25519PrivateKey.generate())

    @classmethod
    def from_hex(cls, private_key_hex: str) -> "AgentIdentity":
        """Restore identity from a hex-encoded 32-byte private key seed."""
        raw = bytes.fromhex(private_key_hex)
        if len(raw) != 32:
            raise ValueError("Private key seed must be exactly 32 bytes.")
        key = Ed25519PrivateKey.from_private_bytes(raw)
        return cls(_private_key=key)

    # ── Public API ────────────────────────────────────────────────────────

    @property
    def agent_id(self) -> AgentID:
        """Returns the hex-encoded public key as the canonical AgentID."""
        raw = self._public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
        return AgentID(raw.hex())

    def sign(self, message: bytes) -> bytes:
        """Sign a message with the agent's private key. Returns 64-byte signature."""
        return self._private_key.sign(message)

    def verify(self, message: bytes, signature: bytes) -> bool:
        """Verify a signature against this agent's public key."""
        try:
            self._public_key.verify(signature, message)
            return True
        except Exception:
            return False

    def private_key_hex(self) -> str:
        """Export the private key seed as hex (handle with care)."""
        raw = self._private_key.private_bytes(
            Encoding.Raw, PrivateFormat.Raw, NoEncryption()
        )
        return raw.hex()

    # ── Internals ─────────────────────────────────────────────────────────

    @property
    def _public_key(self) -> Ed25519PublicKey:
        return self._private_key.public_key()

    def __repr__(self) -> str:
        return f"AgentIdentity(agent_id={self.agent_id!r})"


def verify_signature(agent_id: AgentID, message: bytes, signature: bytes) -> bool:
    """Verify a message signature given only the AgentID (public key hex)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    raw_pub = bytes.fromhex(agent_id)
    pub_key = Ed25519PublicKey.from_public_bytes(raw_pub)
    try:
        pub_key.verify(signature, message)
        return True
    except Exception:
        return False
