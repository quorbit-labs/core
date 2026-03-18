"""
QUORBIT Protocol — bus package (AGPL-3.0)

Core open-source protocol layer:
- identity:     Ed25519 keypair management, AgentID, signed messages
- nonce:        Stateless HMAC nonce store, replay protection (Redis DB=1)
- registry:     Authoritative agent registry, CRL, state machine (Redis DB=2)
- heartbeat:    Liveness tracking and signed health signals
- genesis:      Bootstrap validator set, operator signature verification
- key_rotation: Key rotation pipeline, CRL gossip, emergency multi-sig revoke
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
from .key_rotation import KeyRotationError, KeyRotationManager
from .nonce import NonceManager
from .registry import (
    AgentRecord,
    AgentRegistry,
    AgentState,
    InvalidStateTransitionError,
    RegistryIntegrityError,
)

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
    "AgentState",
    "RegistryIntegrityError",
    "InvalidStateTransitionError",
    # heartbeat
    "HeartbeatManager",
    "HeartbeatMessage",
    # genesis
    "GenesisConfig",
    "GenesisError",
    "load_genesis",
    # key_rotation
    "KeyRotationManager",
    "KeyRotationError",
]
