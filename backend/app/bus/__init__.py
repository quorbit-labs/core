"""
QUORBIT Protocol — bus package (AGPL-3.0)

Core open-source protocol layer:
- identity:  Ed25519 keypair management, AgentID, signed messages
- nonce:     Stateless HMAC nonce store, replay protection (Redis DB=1)
- registry:  Authoritative agent registry, CRL, reconciliation (Redis DB=2)
- heartbeat: Liveness tracking and signed health signals
- genesis:   Bootstrap validator set, operator signature verification
"""

from .genesis import GenesisConfig, GenesisError, load_genesis
from .heartbeat import HeartbeatManager, HeartbeatMessage
from .identity import (
    AgentID,
    AgentIdentity,
    SignedMessage,
    verify_signature,
    verify_signed_message,
)
from .nonce import NonceManager
from .registry import AgentRecord, AgentRegistry, RegistryIntegrityError

__all__ = [
    # identity
    "AgentIdentity",
    "AgentID",
    "SignedMessage",
    "verify_signature",
    "verify_signed_message",
    # nonce
    "NonceManager",
    # registry
    "AgentRegistry",
    "AgentRecord",
    "RegistryIntegrityError",
    # heartbeat
    "HeartbeatManager",
    "HeartbeatMessage",
    # genesis
    "GenesisConfig",
    "GenesisError",
    "load_genesis",
]
