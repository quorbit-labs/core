"""
Unit tests — QUORBIT Identity Layer (Sprint 1 / D1)

Coverage:
  - Ed25519 keypair generation
  - agent_id = SHA256(public_key).hex()  (deterministic, 64-char hex)
  - sign() returns base64-encoded 64-byte signature
  - verify() / verify_signature() accept valid signatures
  - Wrong key or tampered payload must fail verification
  - SignedMessage envelope structure and integrity
  - Key lifecycle: TTL, rotation warning, rotation request
"""

from __future__ import annotations

import base64
import hashlib
import time

import pytest

from backend.app.bus.identity import (
    KEY_ROTATION_WARNING_SECONDS,
    KEY_TTL_SECONDS,
    AgentIdentity,
    verify_signature,
    verify_signed_message,
)


# ── Keypair generation ────────────────────────────────────────────────────────


class TestKeypairGeneration:
    def test_generate_returns_identity(self) -> None:
        identity = AgentIdentity.generate()
        assert isinstance(identity, AgentIdentity)

    def test_agent_id_is_64_char_hex(self) -> None:
        identity = AgentIdentity.generate()
        agent_id = identity.agent_id
        assert len(agent_id) == 64
        assert all(c in "0123456789abcdef" for c in agent_id)

    def test_agent_id_is_sha256_of_public_key(self) -> None:
        identity = AgentIdentity.generate()
        pubkey_bytes = bytes.fromhex(identity.public_key_hex)
        expected = hashlib.sha256(pubkey_bytes).hexdigest()
        assert identity.agent_id == expected

    def test_agent_id_deterministic_for_same_key(self) -> None:
        identity = AgentIdentity.generate()
        restored = AgentIdentity.from_hex(identity.private_key_hex())
        assert identity.agent_id == restored.agent_id

    def test_public_key_hex_is_32_bytes(self) -> None:
        identity = AgentIdentity.generate()
        raw = bytes.fromhex(identity.public_key_hex)
        assert len(raw) == 32

    def test_two_identities_have_different_agent_ids(self) -> None:
        a = AgentIdentity.generate()
        b = AgentIdentity.generate()
        assert a.agent_id != b.agent_id

    def test_from_hex_round_trip(self) -> None:
        original = AgentIdentity.generate()
        restored = AgentIdentity.from_hex(original.private_key_hex())
        assert original.agent_id == restored.agent_id
        assert original.public_key_hex == restored.public_key_hex


# ── Signing and verification ──────────────────────────────────────────────────


class TestSignAndVerify:
    def test_sign_returns_valid_base64(self) -> None:
        identity = AgentIdentity.generate()
        sig = identity.sign(b"hello quorbit")
        raw = base64.b64decode(sig)
        assert len(raw) == 64  # Ed25519 signature is always 64 bytes

    def test_verify_own_signature_succeeds(self) -> None:
        identity = AgentIdentity.generate()
        payload = b"test payload"
        sig = identity.sign(payload)
        assert identity.verify(payload, sig) is True

    def test_verify_tampered_payload_fails(self) -> None:
        identity = AgentIdentity.generate()
        sig = identity.sign(b"original")
        assert identity.verify(b"tampered", sig) is False

    def test_verify_wrong_key_fails(self) -> None:
        alice = AgentIdentity.generate()
        bob = AgentIdentity.generate()
        payload = b"message from alice"
        sig = alice.sign(payload)
        # Bob's identity must not accept Alice's signature
        assert bob.verify(payload, sig) is False

    def test_verify_signature_standalone_succeeds(self) -> None:
        identity = AgentIdentity.generate()
        payload = b"standalone verification"
        sig = identity.sign(payload)
        assert verify_signature(identity.public_key_hex, payload, sig) is True

    def test_verify_signature_wrong_pubkey_fails(self) -> None:
        alice = AgentIdentity.generate()
        bob = AgentIdentity.generate()
        payload = b"alice's message"
        sig = alice.sign(payload)
        assert verify_signature(bob.public_key_hex, payload, sig) is False

    def test_verify_signature_accepts_raw_bytes(self) -> None:
        identity = AgentIdentity.generate()
        payload = b"raw bytes test"
        sig_raw = identity.sign_raw(payload)
        assert verify_signature(identity.public_key_hex, payload, sig_raw) is True

    def test_verify_signature_invalid_pubkey_returns_false(self) -> None:
        assert verify_signature("deadbeef" * 8, b"msg", b"\x00" * 64) is False


# ── SignedMessage envelope ────────────────────────────────────────────────────


class TestSignedMessage:
    def test_build_signed_message_has_all_fields(self) -> None:
        identity = AgentIdentity.generate()
        msg = identity.build_signed_message(b"quorbit")
        for field in ("payload", "sender_id", "public_key", "signature",
                      "key_version", "timestamp_ms"):
            assert field in msg, f"Missing field: {field}"

    def test_sender_id_matches_agent_id(self) -> None:
        identity = AgentIdentity.generate()
        msg = identity.build_signed_message(b"test")
        assert msg["sender_id"] == identity.agent_id

    def test_verify_signed_message_passes(self) -> None:
        identity = AgentIdentity.generate()
        msg = identity.build_signed_message(b"envelope integrity")
        assert verify_signed_message(msg) is True

    def test_tampered_payload_fails_verify(self) -> None:
        identity = AgentIdentity.generate()
        msg = identity.build_signed_message(b"original")
        msg_tampered = dict(msg)
        msg_tampered["payload"] = base64.b64encode(b"tampered").decode()
        assert verify_signed_message(msg_tampered) is False  # type: ignore[arg-type]

    def test_timestamp_ms_is_recent(self) -> None:
        identity = AgentIdentity.generate()
        before = int(time.time() * 1000)
        msg = identity.build_signed_message(b"ts test")
        after = int(time.time() * 1000)
        assert before <= msg["timestamp_ms"] <= after


# ── Key lifecycle ─────────────────────────────────────────────────────────────


class TestKeyLifecycle:
    def test_key_version_default_zero(self) -> None:
        identity = AgentIdentity.generate()
        assert identity.key_version == 0

    def test_new_key_is_not_expired(self) -> None:
        identity = AgentIdentity.generate()
        assert identity.is_expired() is False

    def test_old_key_is_expired(self) -> None:
        identity = AgentIdentity.generate()
        identity._created_at = time.time() - (KEY_TTL_SECONDS + 1)
        assert identity.is_expired() is True

    def test_new_key_does_not_need_rotation(self) -> None:
        identity = AgentIdentity.generate()
        assert identity.should_rotate() is False

    def test_key_near_expiry_should_rotate(self) -> None:
        identity = AgentIdentity.generate()
        # Simulate key that is 6 days 23 hours old (within 24h warning)
        identity._created_at = time.time() - (
            KEY_TTL_SECONDS - KEY_ROTATION_WARNING_SECONDS + 3600
        )
        assert identity.should_rotate() is True

    def test_seconds_until_expiry_positive_for_new_key(self) -> None:
        identity = AgentIdentity.generate()
        remaining = identity.seconds_until_expiry()
        assert remaining > 0
        assert remaining <= KEY_TTL_SECONDS

    def test_seconds_until_expiry_negative_for_expired_key(self) -> None:
        identity = AgentIdentity.generate()
        identity._created_at = time.time() - (KEY_TTL_SECONDS + 60)
        assert identity.seconds_until_expiry() < 0

    def test_key_rotation_request_structure(self) -> None:
        old_id = AgentIdentity.generate()
        new_id = AgentIdentity.generate()
        new_id._key_version = 1

        rotation = old_id.request_key_rotation(new_id)

        assert rotation["old_agent_id"] == old_id.agent_id
        assert rotation["new_agent_id"] == new_id.agent_id
        assert "signature" in rotation
        assert rotation["key_version"] == 0

    def test_key_rotation_signature_verifies_with_old_key(self) -> None:
        old_id = AgentIdentity.generate()
        new_id = AgentIdentity.generate()
        new_id._key_version = 1

        rotation = old_id.request_key_rotation(new_id)
        payload_bytes = rotation["payload"].encode()

        # Signature must verify with the OLD key's public key
        assert verify_signature(
            old_id.public_key_hex, payload_bytes, rotation["signature"]
        ) is True

    def test_key_rotation_signature_does_not_verify_with_new_key(self) -> None:
        old_id = AgentIdentity.generate()
        new_id = AgentIdentity.generate()
        new_id._key_version = 1

        rotation = old_id.request_key_rotation(new_id)
        payload_bytes = rotation["payload"].encode()

        # Must NOT verify with the new key
        assert verify_signature(
            new_id.public_key_hex, payload_bytes, rotation["signature"]
        ) is False
