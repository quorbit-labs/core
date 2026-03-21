# Copyright (c) 2026 Quorbit Labs
# SPDX-License-Identifier: AGPL-3.0-only
"""
Unit tests for the QUORBIT Python SDK (no live server required).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sdk.python.quorbit.client import (
    QuorbitClient,
    QuorbitError,
    QuorbitHTTPError,
    _LightIdentity,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

_AGENT_RECORD = {
    "agent_id":      "aa" * 32,
    "name":          "test-agent",
    "endpoint":      "http://agent.example.com",
    "capabilities":  {"nlp": 0.9},
    "state":         "PROBATIONARY",
    "reputation":    0.75,
    "registered_at": 1_700_000_000.0,
    "last_seen":     1_700_000_001.0,
}


def _mock_resp(payload: Any, status: int = 200) -> MagicMock:
    body = json.dumps(payload).encode()
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _client(base: str = "http://localhost:8000") -> QuorbitClient:
    """Return a client with a fixed deterministic private key."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PrivateFormat, NoEncryption,
    )
    priv = Ed25519PrivateKey.generate()
    raw = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    return QuorbitClient(base, private_key_hex=raw.hex())


# ═══════════════════════════════════════════════════════════════════════════════
# _LightIdentity
# ═══════════════════════════════════════════════════════════════════════════════


class TestLightIdentity:
    def test_generates_keys_without_seed(self):
        ident = _LightIdentity()
        assert len(ident.public_key_hex) == 64
        assert len(ident.private_key_hex) == 64

    def test_agent_id_is_64_hex(self):
        ident = _LightIdentity()
        assert len(ident.agent_id) == 64
        assert all(c in "0123456789abcdef" for c in ident.agent_id)

    def test_two_instances_have_different_keys(self):
        a = _LightIdentity()
        b = _LightIdentity()
        assert a.public_key_hex != b.public_key_hex
        assert a.agent_id != b.agent_id

    def test_round_trip_from_private_key(self):
        a = _LightIdentity()
        b = _LightIdentity(a.private_key_hex)
        assert b.public_key_hex == a.public_key_hex
        assert b.agent_id == a.agent_id

    def test_sign_returns_base64_string(self):
        import base64
        ident = _LightIdentity()
        sig = ident.sign(b"hello world")
        assert isinstance(sig, str)
        # Must be valid base64
        decoded = base64.b64decode(sig)
        assert len(decoded) == 64  # Ed25519 signature is always 64 bytes

    def test_sign_is_deterministic_per_key(self):
        ident = _LightIdentity()
        sig1 = ident.sign(b"same payload")
        sig2 = ident.sign(b"same payload")
        # Ed25519 is deterministic
        assert sig1 == sig2

    def test_different_payloads_produce_different_sigs(self):
        ident = _LightIdentity()
        assert ident.sign(b"payload_a") != ident.sign(b"payload_b")


# ═══════════════════════════════════════════════════════════════════════════════
# QuorbitClient — construction
# ═══════════════════════════════════════════════════════════════════════════════


class TestQuorbitClientInit:
    def test_auto_generates_identity_when_no_key(self):
        c = QuorbitClient("http://localhost:8000")
        assert len(c.agent_id) == 64
        assert len(c.public_key_hex) == 64

    def test_accepts_existing_private_key(self):
        a = _LightIdentity()
        c = QuorbitClient("http://localhost:8000", private_key_hex=a.private_key_hex)
        assert c.agent_id == a.agent_id

    def test_two_clients_without_key_have_different_ids(self):
        c1 = QuorbitClient("http://localhost:8000")
        c2 = QuorbitClient("http://localhost:8000")
        assert c1.agent_id != c2.agent_id

    def test_base_url_trailing_slash_stripped(self):
        c = QuorbitClient("http://localhost:8000/")
        assert not c._base.endswith("/")


# ═══════════════════════════════════════════════════════════════════════════════
# QuorbitClient.register()
# ═══════════════════════════════════════════════════════════════════════════════


class TestRegister:
    def test_posts_to_correct_endpoint(self):
        c = _client()
        with patch("sdk.python.quorbit.client.urllib_request.urlopen") as mock_open:
            mock_open.return_value = _mock_resp(_AGENT_RECORD, status=201)
            c.register("test-agent", {"nlp": 0.9})

        req = mock_open.call_args[0][0]
        assert req.full_url == "http://localhost:8000/api/v1/agents"
        assert req.method == "POST"

    def test_includes_name_and_capabilities_in_body(self):
        c = _client()
        with patch("sdk.python.quorbit.client.urllib_request.urlopen") as mock_open:
            mock_open.return_value = _mock_resp(_AGENT_RECORD, status=201)
            c.register("my-agent", {"nlp": 0.8, "code": 0.6}, endpoint="http://me.com")

        body = json.loads(mock_open.call_args[0][0].data.decode())
        assert body["name"] == "my-agent"
        assert body["capabilities"] == {"nlp": 0.8, "code": 0.6}
        assert body["endpoint"] == "http://me.com"

    def test_includes_public_key_hex(self):
        c = _client()
        with patch("sdk.python.quorbit.client.urllib_request.urlopen") as mock_open:
            mock_open.return_value = _mock_resp(_AGENT_RECORD, status=201)
            c.register("a", {})

        body = json.loads(mock_open.call_args[0][0].data.decode())
        assert body["public_key_hex"] == c.public_key_hex

    def test_includes_signature(self):
        c = _client()
        with patch("sdk.python.quorbit.client.urllib_request.urlopen") as mock_open:
            mock_open.return_value = _mock_resp(_AGENT_RECORD, status=201)
            c.register("a", {})

        body = json.loads(mock_open.call_args[0][0].data.decode())
        assert "signature" in body
        assert isinstance(body["signature"], str)
        assert len(body["signature"]) > 0

    def test_returns_agent_record(self):
        c = _client()
        with patch("sdk.python.quorbit.client.urllib_request.urlopen") as mock_open:
            mock_open.return_value = _mock_resp(_AGENT_RECORD, status=201)
            result = c.register("a", {})

        assert result["agent_id"] == _AGENT_RECORD["agent_id"]
        assert result["name"] == "test-agent"

    def test_raises_on_http_error(self):
        from urllib.error import HTTPError
        c = _client()
        with patch("sdk.python.quorbit.client.urllib_request.urlopen") as mock_open:
            mock_open.side_effect = HTTPError(
                "http://localhost:8000/api/v1/agents", 409,
                "Conflict", {}, None
            )
            with pytest.raises(QuorbitHTTPError) as exc_info:
                c.register("dup", {})
        assert exc_info.value.status == 409


# ═══════════════════════════════════════════════════════════════════════════════
# QuorbitClient.heartbeat()
# ═══════════════════════════════════════════════════════════════════════════════


class TestHeartbeat:
    def test_posts_to_correct_endpoint(self):
        c = _client()
        with patch("sdk.python.quorbit.client.urllib_request.urlopen") as mock_open:
            mock_open.return_value = _mock_resp({"ok": True, "agent_id": c.agent_id, "timestamp_ms": 1})
            c.heartbeat()

        req = mock_open.call_args[0][0]
        assert f"/api/v1/agents/{c.agent_id}/heartbeat" in req.full_url

    def test_returns_true_on_ok(self):
        c = _client()
        with patch("sdk.python.quorbit.client.urllib_request.urlopen") as mock_open:
            mock_open.return_value = _mock_resp({"ok": True, "agent_id": c.agent_id, "timestamp_ms": 1})
            result = c.heartbeat()
        assert result is True

    def test_returns_false_when_ok_missing(self):
        c = _client()
        with patch("sdk.python.quorbit.client.urllib_request.urlopen") as mock_open:
            mock_open.return_value = _mock_resp({"agent_id": c.agent_id})
            result = c.heartbeat()
        assert result is False


# ═══════════════════════════════════════════════════════════════════════════════
# QuorbitClient.get_agent()
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetAgent:
    def test_gets_correct_endpoint(self):
        c = _client()
        target_id = "bb" * 32
        with patch("sdk.python.quorbit.client.urllib_request.urlopen") as mock_open:
            mock_open.return_value = _mock_resp(_AGENT_RECORD)
            c.get_agent(target_id)

        req = mock_open.call_args[0][0]
        assert req.full_url == f"http://localhost:8000/api/v1/agents/{target_id}"
        assert req.method == "GET"

    def test_returns_record_dict(self):
        c = _client()
        with patch("sdk.python.quorbit.client.urllib_request.urlopen") as mock_open:
            mock_open.return_value = _mock_resp(_AGENT_RECORD)
            result = c.get_agent("aa" * 32)
        assert result["name"] == "test-agent"

    def test_raises_on_404(self):
        from urllib.error import HTTPError
        c = _client()
        with patch("sdk.python.quorbit.client.urllib_request.urlopen") as mock_open:
            mock_open.side_effect = HTTPError(
                "http://localhost:8000/api/v1/agents/notfound", 404,
                "Not Found", {}, None
            )
            with pytest.raises(QuorbitHTTPError) as exc_info:
                c.get_agent("notfound")
        assert exc_info.value.status == 404


# ═══════════════════════════════════════════════════════════════════════════════
# QuorbitClient.discover()
# ═══════════════════════════════════════════════════════════════════════════════


class TestDiscover:
    _CANDIDATES = [_AGENT_RECORD, {**_AGENT_RECORD, "name": "agent-2", "discovery_score": 0.72}]

    def test_posts_to_correct_endpoint(self):
        c = _client()
        with patch("sdk.python.quorbit.client.urllib_request.urlopen") as mock_open:
            mock_open.return_value = _mock_resp(self._CANDIDATES)
            c.discover("summarise document")

        req = mock_open.call_args[0][0]
        assert req.full_url == "http://localhost:8000/api/v1/discover"
        assert req.method == "POST"

    def test_includes_task_description_and_min_score(self):
        c = _client()
        with patch("sdk.python.quorbit.client.urllib_request.urlopen") as mock_open:
            mock_open.return_value = _mock_resp(self._CANDIDATES)
            c.discover("translate text", min_score=0.80)

        body = json.loads(mock_open.call_args[0][0].data.decode())
        assert body["task_description"] == "translate text"
        assert body["min_score"] == 0.80

    def test_includes_required_capabilities(self):
        c = _client()
        with patch("sdk.python.quorbit.client.urllib_request.urlopen") as mock_open:
            mock_open.return_value = _mock_resp(self._CANDIDATES)
            c.discover("nlp task", required_capabilities={"nlp": 0.8})

        body = json.loads(mock_open.call_args[0][0].data.decode())
        assert body["required_capabilities"] == {"nlp": 0.8}

    def test_empty_required_capabilities_when_omitted(self):
        c = _client()
        with patch("sdk.python.quorbit.client.urllib_request.urlopen") as mock_open:
            mock_open.return_value = _mock_resp([])
            c.discover("any task")

        body = json.loads(mock_open.call_args[0][0].data.decode())
        assert body["required_capabilities"] == {}

    def test_returns_list(self):
        c = _client()
        with patch("sdk.python.quorbit.client.urllib_request.urlopen") as mock_open:
            mock_open.return_value = _mock_resp(self._CANDIDATES)
            result = c.discover("task")
        assert isinstance(result, list)
        assert len(result) == 2

    def test_default_min_score_is_0_70(self):
        c = _client()
        with patch("sdk.python.quorbit.client.urllib_request.urlopen") as mock_open:
            mock_open.return_value = _mock_resp([])
            c.discover("task")

        body = json.loads(mock_open.call_args[0][0].data.decode())
        assert body["min_score"] == 0.70


# ═══════════════════════════════════════════════════════════════════════════════
# Error handling
# ═══════════════════════════════════════════════════════════════════════════════


class TestErrorHandling:
    def test_http_error_exposes_status_and_body(self):
        from urllib.error import HTTPError
        c = _client()
        with patch("sdk.python.quorbit.client.urllib_request.urlopen") as mock_open:
            mock_open.side_effect = HTTPError(
                "http://localhost:8000/api/v1/agents", 503,
                "Service Unavailable", {}, None
            )
            with pytest.raises(QuorbitHTTPError) as exc_info:
                c.register("a", {})

        err = exc_info.value
        assert err.status == 503
        assert isinstance(err, QuorbitError)

    def test_connection_error_propagates(self):
        c = _client()
        with patch("sdk.python.quorbit.client.urllib_request.urlopen") as mock_open:
            mock_open.side_effect = OSError("connection refused")
            with pytest.raises(OSError):
                c.heartbeat()
