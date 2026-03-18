"""
QUORBIT Protocol — Identity Module (AGPL-3.0)

Ed25519 keypair management. agent_id = SHA256(public_key).
All messages are signed for non-repudiation and wrapped in a SignedMessage envelope.

Key lifecycle:
  - TTL: 7 days
  - Rotation warning: 24h before expiry
  - Rotation request is signed by the OLD key
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, TypedDict

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

# AgentID = SHA256(public_key_bytes).hex()  — 64 hex chars
AgentID = str

KEY_TTL_SECONDS = 7 * 24 * 3600           # 7 days
KEY_ROTATION_WARNING_SECONDS = 24 * 3600  # warn 24h before expiry


class SignedMessage(TypedDict):
    """Signed message envelope — the canonical wire format for all bus messages."""

    payload: str      # base64-encoded raw payload bytes
    sender_id: str    # AgentID (SHA256 of public key, hex)
    public_key: str   # hex-encoded raw Ed25519 public key (for offline verification)
    signature: str    # base64-encoded Ed25519 signature over the raw payload bytes
    key_version: int  # monotonically increasing; incremented on each key rotation
    timestamp_ms: int # Unix timestamp in milliseconds at signing time


@dataclass
class AgentIdentity:
    """Holds an agent's Ed25519 keypair and manages its lifecycle."""

    _private_key: Ed25519PrivateKey = field(repr=False)
    _created_at: float = field(default_factory=time.time)
    _key_version: int = 0

    # ── Construction ──────────────────────────────────────────────────────

    @classmethod
    def generate(cls) -> "AgentIdentity":
        """Generate a new random Ed25519 keypair."""
        return cls(_private_key=Ed25519PrivateKey.generate())

    @classmethod
    def from_hex(
        cls,
        private_key_hex: str,
        created_at: float | None = None,
        key_version: int = 0,
    ) -> "AgentIdentity":
        """Restore identity from a hex-encoded 32-byte private key seed."""
        raw = bytes.fromhex(private_key_hex)
        if len(raw) != 32:
            raise ValueError("Private key seed must be exactly 32 bytes.")
        key = Ed25519PrivateKey.from_private_bytes(raw)
        obj = cls(_private_key=key, _key_version=key_version)
        if created_at is not None:
            obj._created_at = created_at
        return obj

    # ── Identity ──────────────────────────────────────────────────────────

    @property
    def agent_id(self) -> AgentID:
        """SHA256 of the raw public key bytes, hex-encoded (64 chars)."""
        raw = self._public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
        return hashlib.sha256(raw).hexdigest()

    @property
    def public_key_hex(self) -> str:
        """Raw 32-byte Ed25519 public key as hex (required for signature verification)."""
        return self._public_key.public_bytes(Encoding.Raw, PublicFormat.Raw).hex()

    @property
    def key_version(self) -> int:
        return self._key_version

    @property
    def created_at(self) -> float:
        return self._created_at

    # ── Signing ───────────────────────────────────────────────────────────

    def sign(self, payload: bytes) -> str:
        """Sign payload bytes. Returns base64-encoded signature string."""
        return base64.b64encode(self._private_key.sign(payload)).decode()

    def sign_raw(self, payload: bytes) -> bytes:
        """Sign payload bytes. Returns raw 64-byte Ed25519 signature."""
        return self._private_key.sign(payload)

    def verify(self, payload: bytes, signature_b64: str) -> bool:
        """Verify a base64-encoded signature against this identity's own public key."""
        try:
            sig = base64.b64decode(signature_b64)
            self._public_key.verify(sig, payload)
            return True
        except Exception:
            return False

    def build_signed_message(self, payload: bytes) -> SignedMessage:
        """Wrap raw payload bytes in a signed envelope."""
        ts_ms = int(time.time() * 1000)
        return SignedMessage(
            payload=base64.b64encode(payload).decode(),
            sender_id=self.agent_id,
            public_key=self.public_key_hex,
            signature=self.sign(payload),
            key_version=self._key_version,
            timestamp_ms=ts_ms,
        )

    # ── Key lifecycle ─────────────────────────────────────────────────────

    def is_expired(self, now: float | None = None) -> bool:
        """True if the key has exceeded KEY_TTL_SECONDS."""
        t = now if now is not None else time.time()
        return (t - self._created_at) >= KEY_TTL_SECONDS

    def should_rotate(self, now: float | None = None) -> bool:
        """True if within the 24h rotation warning window (or already expired)."""
        t = now if now is not None else time.time()
        age = t - self._created_at
        return age >= (KEY_TTL_SECONDS - KEY_ROTATION_WARNING_SECONDS)

    def seconds_until_expiry(self, now: float | None = None) -> float:
        """Remaining seconds before this key expires (negative if already expired)."""
        t = now if now is not None else time.time()
        return (self._created_at + KEY_TTL_SECONDS) - t

    def request_key_rotation(self, new_identity: "AgentIdentity") -> dict[str, Any]:
        """
        Build a key rotation request signed by the CURRENT (old) key.

        The returned dict contains a JSON payload plus the old key's signature,
        so the Registry can verify continuity of identity.
        """
        payload_dict: dict[str, Any] = {
            "action": "key_rotation",
            "old_agent_id": self.agent_id,
            "old_key_version": self._key_version,
            "new_agent_id": new_identity.agent_id,
            "new_public_key": new_identity.public_key_hex,
            "new_key_version": new_identity.key_version,
            "timestamp_ms": int(time.time() * 1000),
        }
        payload_bytes = json.dumps(payload_dict, sort_keys=True).encode()
        return {
            "payload": payload_bytes.decode(),
            "old_agent_id": self.agent_id,
            "new_agent_id": new_identity.agent_id,
            "signature": self.sign(payload_bytes),  # signed by OLD key
            "key_version": self._key_version,
        }

    # ── Export ────────────────────────────────────────────────────────────

    def private_key_hex(self) -> str:
        """Export the private key seed as hex. Handle with care — never log."""
        raw = self._private_key.private_bytes(
            Encoding.Raw, PrivateFormat.Raw, NoEncryption()
        )
        return raw.hex()

    # ── Internals ─────────────────────────────────────────────────────────

    @property
    def _public_key(self) -> Ed25519PublicKey:
        return self._private_key.public_key()

    def __repr__(self) -> str:
        return (
            f"AgentIdentity(agent_id={self.agent_id!r}, "
            f"key_version={self._key_version})"
        )


# ── Standalone verification helpers ───────────────────────────────────────────


def verify_signature(
    public_key_hex: str,
    message: bytes,
    signature: bytes | str,
) -> bool:
    """
    Verify an Ed25519 signature given the raw public key (hex).

    signature may be raw 64-byte bytes or a base64-encoded string.
    """
    try:
        raw_pub = bytes.fromhex(public_key_hex)
        pub_key = Ed25519PublicKey.from_public_bytes(raw_pub)
        if isinstance(signature, str):
            signature = base64.b64decode(signature)
        pub_key.verify(signature, message)
        return True
    except Exception:
        return False


def verify_signed_message(msg: SignedMessage) -> bool:
    """Verify the cryptographic integrity of a SignedMessage envelope."""
    try:
        payload = base64.b64decode(msg["payload"])
        return verify_signature(msg["public_key"], payload, msg["signature"])
    except Exception:
        return False
