# Copyright (c) 2026 Quorbit Labs
# SPDX-License-Identifier: AGPL-3.0-only
"""
Sprint 10 — API integration tests.

Validates that main.py correctly handles the payloads sent by e2e_demo.py:
  - CapabilityCard v2.0 registration (nested identity/capabilities)
  - Signed heartbeat with payload body
  - Structured discover request with {candidates} wrapper
  - Legacy flat-format backward compatibility
  - Error handling for invalid payloads

15 tests total.
"""

from __future__ import annotations

import hashlib
import json
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient

from backend.app.main import app, _agents


@pytest.fixture(autouse=True)
def clear_agents():
    """Reset in-memory store before each test."""
    _agents.clear()
    yield
    _agents.clear()


@pytest.fixture
def client():
    return TestClient(app)


def _make_identity(name: str = "test-agent"):
    """Generate Ed25519 identity matching e2e_demo.py format."""
    pk = Ed25519PrivateKey.generate()
    pub = pk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    agent_id = hashlib.sha256(pub).hexdigest()
    return {
        "private_key": pk,
        "public_key_hex": pub.hex(),
        "agent_id": agent_id,
        "name": name,
    }


def _v2_card(identity: dict, capabilities: dict | None = None) -> dict:
    """Build a CapabilityCard v2.0 payload."""
    caps = capabilities or {"data_analysis": 0.85, "reasoning": 0.90}
    return {
        "schema_version": "2.0",
        "identity": {
            "agent_id": identity["agent_id"],
            "agent_type": "llm",
            "version": "1.0.0",
            "provider": identity["name"],
            "public_key": identity["public_key_hex"],
            "key_version": 1,
        },
        "capabilities": {
            "capability_vector": caps,
            "input_formats": ["text", "json"],
            "output_formats": ["json", "markdown"],
        },
        "resource_limits": {
            "max_input_tokens": 128000,
            "max_output_tokens": 4096,
            "max_concurrent_tasks": 1,
            "max_task_duration_s": 120,
        },
        "coordination": {
            "preferred_task_types": ["reasoning", "analysis"],
            "delegation_style": "autonomous",
        },
        "gaps": ["no_persistent_memory"],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Health
# ═══════════════════════════════════════════════════════════════════════════════


class TestHealth:
    def test_health_returns_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["version"] == "0.1.1"
        assert "timestamp_ms" in body


# ═══════════════════════════════════════════════════════════════════════════════
# Registration — CapabilityCard v2.0
# ═══════════════════════════════════════════════════════════════════════════════


class TestRegisterV2:
    def test_register_v2_success(self, client):
        ident = _make_identity("alpha")
        card = _v2_card(ident)
        r = client.post("/api/v1/agents", json=card)
        assert r.status_code == 201
        body = r.json()
        assert body["agent_id"] == ident["agent_id"]
        assert body["state"] == "PROBATIONARY"
        assert body["capabilities"]["data_analysis"] == 0.85

    def test_register_v2_stores_card(self, client):
        ident = _make_identity("beta")
        card = _v2_card(ident)
        r = client.post("/api/v1/agents", json=card)
        assert r.status_code == 201
        body = r.json()
        assert "_card" in body
        assert body["_card"]["schema_version"] == "2.0"
        assert body["_card"]["identity"]["public_key"] == ident["public_key_hex"]

    def test_register_v2_has_dynamic(self, client):
        ident = _make_identity()
        r = client.post("/api/v1/agents", json=_v2_card(ident))
        body = r.json()
        assert "_dynamic" in body
        assert body["_dynamic"]["state"] == "PROBATIONARY"
        assert body["_dynamic"]["heartbeat_count"] == 0

    def test_register_v2_missing_public_key(self, client):
        card = {
            "identity": {"agent_id": "x", "agent_type": "llm"},
            "capabilities": {},
        }
        r = client.post("/api/v1/agents", json=card)
        assert r.status_code == 422

    def test_register_v2_invalid_hex(self, client):
        card = {
            "identity": {"public_key": "not-valid-hex"},
            "capabilities": {},
        }
        r = client.post("/api/v1/agents", json=card)
        assert r.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════════
# Registration — Legacy format
# ═══════════════════════════════════════════════════════════════════════════════


class TestRegisterLegacy:
    def test_legacy_registration(self, client):
        ident = _make_identity("legacy-agent")
        r = client.post("/api/v1/agents", json={
            "name": "legacy-agent",
            "public_key_hex": ident["public_key_hex"],
            "capabilities": {"coding": 0.9},
        })
        assert r.status_code == 201
        body = r.json()
        assert body["agent_id"] == ident["agent_id"]
        assert body["name"] == "legacy-agent"
        assert "_card" not in body  # legacy doesn't store card

    def test_unrecognized_format_rejected(self, client):
        r = client.post("/api/v1/agents", json={"foo": "bar"})
        assert r.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════════
# Heartbeat
# ═══════════════════════════════════════════════════════════════════════════════


class TestHeartbeat:
    def test_heartbeat_basic(self, client):
        ident = _make_identity()
        client.post("/api/v1/agents", json=_v2_card(ident))
        r = client.post(f"/api/v1/agents/{ident['agent_id']}/heartbeat", json={})
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_heartbeat_signed_payload(self, client):
        ident = _make_identity()
        client.post("/api/v1/agents", json=_v2_card(ident))

        payload = json.dumps({
            "agent_id": ident["agent_id"],
            "timestamp_ms": int(time.time() * 1000),
        }).encode()
        signature = ident["private_key"].sign(payload).hex()

        r = client.post(
            f"/api/v1/agents/{ident['agent_id']}/heartbeat",
            json={
                "payload": json.loads(payload),
                "signature": signature,
                "key_version": 1,
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["signature_received"] is True
        assert body["key_version"] == 1

    def test_heartbeat_increments_count(self, client):
        ident = _make_identity()
        client.post("/api/v1/agents", json=_v2_card(ident))

        for i in range(3):
            client.post(f"/api/v1/agents/{ident['agent_id']}/heartbeat", json={})

        r = client.get(f"/api/v1/agents/{ident['agent_id']}")
        assert r.json()["_dynamic"]["heartbeat_count"] == 3

    def test_heartbeat_unknown_agent(self, client):
        r = client.post("/api/v1/agents/nonexistent/heartbeat", json={})
        assert r.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════════
# Discovery
# ═══════════════════════════════════════════════════════════════════════════════


class TestDiscover:
    def test_discover_structured_format(self, client):
        """e2e_demo.py sends this format."""
        ident = _make_identity()
        client.post("/api/v1/agents", json=_v2_card(ident, {"data_analysis": 0.85}))

        r = client.post("/api/v1/discover", json={
            "task": {
                "intent": "Analyze CSV anomalies",
                "constraints": [],
                "success_criteria": "valid JSON",
            },
            "required_capabilities": {"data_analysis": 0.50},
            "min_capability_match": 0.50,
        })
        assert r.status_code == 200
        body = r.json()
        assert "candidates" in body
        assert len(body["candidates"]) >= 1
        assert body["candidates"][0]["score"] > 0

    def test_discover_legacy_format(self, client):
        ident = _make_identity()
        client.post("/api/v1/agents", json={
            "name": "legacy",
            "public_key_hex": ident["public_key_hex"],
            "capabilities": {"nlp": 0.9},
        })

        r = client.post("/api/v1/discover", json={
            "task_description": "Summarize text",
            "min_score": 0.50,
            "required_capabilities": {"nlp": 0.7},
        })
        assert r.status_code == 200
        body = r.json()
        assert "candidates" in body
        assert len(body["candidates"]) >= 1

    def test_discover_empty_returns_candidates_key(self, client):
        r = client.post("/api/v1/discover", json={
            "required_capabilities": {},
        })
        assert r.status_code == 200
        assert "candidates" in r.json()
