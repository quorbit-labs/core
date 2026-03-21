# Copyright (c) 2026 Quorbit Labs
# SPDX-License-Identifier: AGPL-3.0-only
"""
Unit tests for MCP and A2A bridge modules (Sprint 9).
"""

from __future__ import annotations

import json
from io import BytesIO
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from backend.app.bridges.mcp_bridge import MCPBridge, MCPBridgeError
from backend.app.bridges.a2a_bridge import A2ABridge, A2ABridgeError


# ── Fixtures / helpers ────────────────────────────────────────────────────────


def _make_registry(agent_id: str = "dead" * 16) -> MagicMock:
    """Return a mock AgentRegistry that returns a minimal AgentRecord."""
    reg = MagicMock()
    record = MagicMock()
    record.agent_id = agent_id
    record.name = "test-agent"
    record.endpoint = "http://example.com"
    reg.register.return_value = record
    return reg


def _mock_urlopen(payload: dict, status: int = 200):
    """Context manager factory that makes urllib.request.urlopen return *payload*."""
    body = json.dumps(payload).encode("utf-8")
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


MCP_MANIFEST_JSONRPC = {
    "jsonrpc": "2.0",
    "id": 1,
    "result": {
        "tools": [
            {"name": "search_web", "description": "Search the web"},
            {"name": "read_file",  "description": "Read a file"},
            {"name": "run_code",   "description": "Execute code"},
        ]
    },
}

MCP_MANIFEST_FLAT = {
    "tools": [
        {"name": "summarise",  "description": "Summarise text"},
        {"name": "translate",  "description": "Translate text"},
    ]
}

A2A_AGENT_CARD_FULL = {
    "name": "ResearchAgent",
    "version": "1.2",
    "url": "https://research.example.com",
    "description": "An A2A research agent",
    "skills": [
        {"name": "web_research", "description": "Research topics online"},
        {"name": "summarise",    "description": "Summarise documents", "score": 0.9},
    ],
    "capabilities": {
        "web_search": True,
        "external_apis": True,
        "code_execution": False,
    },
}

A2A_AGENT_CARD_MINIMAL = {
    "name": "MinimalAgent",
    "url": "https://minimal.example.com",
}


# ═══════════════════════════════════════════════════════════════════════════════
# MCPBridge tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestMCPBridgeCapabilityVector:
    def test_tools_list_maps_to_capability_vector(self):
        bridge = MCPBridge(_make_registry())
        tools = [{"name": "search"}, {"name": "embed"}, {"name": "classify"}]
        vector = bridge._build_capability_vector(tools, extra=None)
        assert set(vector.keys()) == {"search", "embed", "classify"}

    def test_tool_score_is_1_0(self):
        bridge = MCPBridge(_make_registry())
        tools = [{"name": "t1"}]
        assert bridge._build_capability_vector(tools, None)["t1"] == 1.0

    def test_extra_capabilities_merged(self):
        bridge = MCPBridge(_make_registry())
        tools = [{"name": "search"}]
        vector = bridge._build_capability_vector(tools, extra={"nlp": 0.8})
        assert "nlp" in vector
        assert vector["nlp"] == pytest.approx(0.8)

    def test_scores_clamped_to_unit_interval(self):
        bridge = MCPBridge(_make_registry())
        vector = bridge._build_capability_vector([], extra={"a": 1.5, "b": -0.1})
        assert vector["a"] == 1.0
        assert vector["b"] == 0.0

    def test_tools_missing_name_skipped(self):
        bridge = MCPBridge(_make_registry())
        tools = [{"description": "no name here"}, {"name": "valid"}]
        vector = bridge._build_capability_vector(tools, None)
        assert "valid" in vector
        assert len(vector) == 1


class TestMCPBridgeExtractTools:
    def test_jsonrpc_envelope(self):
        tools = MCPBridge._extract_tools(MCP_MANIFEST_JSONRPC)
        assert len(tools) == 3
        assert tools[0]["name"] == "search_web"

    def test_flat_format(self):
        tools = MCPBridge._extract_tools(MCP_MANIFEST_FLAT)
        assert len(tools) == 2
        assert tools[0]["name"] == "summarise"

    def test_empty_tools(self):
        assert MCPBridge._extract_tools({}) == []
        assert MCPBridge._extract_tools({"result": {}}) == []

    def test_invalid_tools_type_raises(self):
        with pytest.raises(MCPBridgeError):
            MCPBridge._extract_tools({"tools": "not-a-list"})


class TestMCPBridgeRegister:
    def test_register_calls_registry(self):
        reg = _make_registry()
        bridge = MCPBridge(reg)

        with patch("backend.app.bridges.mcp_bridge.urllib_request.urlopen") as mock_open:
            mock_open.return_value = _mock_urlopen(MCP_MANIFEST_JSONRPC)
            bridge.register_mcp_server("http://mcp.example.com", "MyMCP")

        reg.register.assert_called_once()
        call_kwargs = reg.register.call_args
        assert call_kwargs.kwargs["name"] == "MyMCP"
        assert call_kwargs.kwargs["endpoint"] == "http://mcp.example.com"

    def test_register_generates_unique_agent_ids(self):
        reg = _make_registry()
        bridge = MCPBridge(reg)
        agent_ids = set()

        with patch("backend.app.bridges.mcp_bridge.urllib_request.urlopen") as mock_open:
            mock_open.return_value = _mock_urlopen(MCP_MANIFEST_FLAT)
            for _ in range(5):
                # Reset return value each iteration
                mock_open.return_value = _mock_urlopen(MCP_MANIFEST_FLAT)
                bridge.register_mcp_server("http://mcp.example.com", "MyMCP")
                agent_ids.add(reg.register.call_args.kwargs["agent_id"])

        # Each call must produce a unique agent_id (distinct Ed25519 keypair)
        assert len(agent_ids) == 5

    def test_register_returns_agent_record(self):
        reg = _make_registry()
        bridge = MCPBridge(reg)

        with patch("backend.app.bridges.mcp_bridge.urllib_request.urlopen") as mock_open:
            mock_open.return_value = _mock_urlopen(MCP_MANIFEST_FLAT)
            record = bridge.register_mcp_server("http://x.com", "X")

        assert record is reg.register.return_value

    def test_fetch_failure_raises_bridge_error(self):
        reg = _make_registry()
        bridge = MCPBridge(reg)

        with patch("backend.app.bridges.mcp_bridge.urllib_request.urlopen") as mock_open:
            mock_open.side_effect = OSError("connection refused")
            with pytest.raises(MCPBridgeError, match="Cannot fetch"):
                bridge.register_mcp_server("http://down.example.com", "Down")

    def test_public_key_hex_passed_to_registry(self):
        reg = _make_registry()
        bridge = MCPBridge(reg)

        with patch("backend.app.bridges.mcp_bridge.urllib_request.urlopen") as mock_open:
            mock_open.return_value = _mock_urlopen(MCP_MANIFEST_FLAT)
            bridge.register_mcp_server("http://x.com", "X")

        pubkey = reg.register.call_args.kwargs["public_key_hex"]
        # Ed25519 public key = 32 bytes = 64 hex chars
        assert isinstance(pubkey, str)
        assert len(pubkey) == 64


class TestMCPBridgeCardStatic:
    def test_card_static_identity_type_is_mcp(self):
        from backend.app.bus.identity import AgentIdentity
        identity = AgentIdentity.generate()
        card = MCPBridge._build_card_static(identity, "test", {"a": 1.0}, ["a"])
        assert card["identity"]["type"] == "mcp"

    def test_card_static_capability_vector_present(self):
        from backend.app.bus.identity import AgentIdentity
        identity = AgentIdentity.generate()
        vec = {"search": 1.0, "read": 1.0}
        card = MCPBridge._build_card_static(identity, "t", vec, ["search", "read"])
        assert card["capabilities"]["capability_vector"] == vec

    def test_card_static_preferred_tasks(self):
        from backend.app.bus.identity import AgentIdentity
        identity = AgentIdentity.generate()
        card = MCPBridge._build_card_static(identity, "t", {}, ["task_a", "task_b"])
        assert "task_a" in card["coordination"]["preferred_tasks"]


# ═══════════════════════════════════════════════════════════════════════════════
# A2ABridge tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestA2ABridgeCardUrlResolution:
    def test_appends_well_known_path(self):
        url = A2ABridge._resolve_card_url("https://agent.example.com")
        assert url == "https://agent.example.com/.well-known/agent.json"

    def test_does_not_double_append(self):
        url = A2ABridge._resolve_card_url(
            "https://agent.example.com/.well-known/agent.json"
        )
        assert url == "https://agent.example.com/.well-known/agent.json"

    def test_strips_trailing_slash_before_appending(self):
        url = A2ABridge._resolve_card_url("https://agent.example.com/")
        assert url == "https://agent.example.com/.well-known/agent.json"

    def test_json_url_not_modified(self):
        raw = "https://agent.example.com/card.json"
        assert A2ABridge._resolve_card_url(raw) == raw


class TestA2ABridgeExtractSkills:
    def test_extracts_top_level_skills(self):
        skills = A2ABridge._extract_skills(A2A_AGENT_CARD_FULL)
        assert len(skills) == 2
        assert skills[0]["name"] == "web_research"

    def test_extracts_capabilities_skills(self):
        card = {
            "name": "X",
            "capabilities": {
                "skills": [{"name": "analyse"}, {"name": "report"}]
            }
        }
        skills = A2ABridge._extract_skills(card)
        assert len(skills) == 2

    def test_empty_when_no_skills(self):
        assert A2ABridge._extract_skills({"name": "X"}) == []

    def test_non_list_skills_returns_empty(self):
        assert A2ABridge._extract_skills({"name": "X", "skills": "bad"}) == []


class TestA2ABridgeCapabilityVector:
    def test_default_score_applied(self):
        skills = [{"name": "search"}]
        vector = A2ABridge._build_capability_vector(skills, extra=None)
        assert vector["search"] == pytest.approx(0.80)

    def test_explicit_score_used(self):
        skills = [{"name": "summarise", "score": 0.9}]
        vector = A2ABridge._build_capability_vector(skills, extra=None)
        assert vector["summarise"] == pytest.approx(0.9)

    def test_scores_clamped(self):
        skills = [{"name": "x", "score": 2.0}, {"name": "y", "score": -1.0}]
        vector = A2ABridge._build_capability_vector(skills, extra=None)
        assert vector["x"] == 1.0
        assert vector["y"] == 0.0

    def test_extra_merged(self):
        vector = A2ABridge._build_capability_vector([], extra={"nlp": 0.75})
        assert vector["nlp"] == pytest.approx(0.75)

    def test_skill_without_name_and_without_id_skipped(self):
        # Neither name nor id — nothing to key on, should be skipped
        skills = [{"description": "no name, no id"}]
        vector = A2ABridge._build_capability_vector(skills, extra=None)
        assert len(vector) == 0

    def test_skill_with_id_used_when_no_name(self):
        # Some A2A impls use "id" only
        # Our implementation checks "name" first, then "id"
        skills = [{"id": "fallback_id"}]
        vector = A2ABridge._build_capability_vector(skills, extra=None)
        # id-only fallback gives "fallback_id" key
        assert "fallback_id" in vector


class TestA2ABridgeToolsFlags:
    def test_web_search_mapped(self):
        flags = A2ABridge._extract_tools_flags(A2A_AGENT_CARD_FULL)
        assert flags["web_search"] is True

    def test_code_exec_mapped(self):
        flags = A2ABridge._extract_tools_flags(A2A_AGENT_CARD_FULL)
        assert flags["code_exec"] is False

    def test_defaults_when_no_capabilities(self):
        flags = A2ABridge._extract_tools_flags({"name": "X"})
        assert flags["code_exec"] is False
        assert flags["external_apis"] is True   # default True


class TestA2ABridgeValidation:
    def test_missing_name_raises(self):
        with pytest.raises(A2ABridgeError, match="missing required field"):
            A2ABridge._validate_card({"url": "http://x.com"})

    def test_non_dict_raises(self):
        with pytest.raises(A2ABridgeError):
            A2ABridge._validate_card(["not", "a", "dict"])

    def test_valid_minimal_card_passes(self):
        A2ABridge._validate_card(A2A_AGENT_CARD_MINIMAL)  # must not raise


class TestA2ABridgeRegister:
    def test_register_calls_registry(self):
        reg = _make_registry()
        bridge = A2ABridge(reg)

        with patch("backend.app.bridges.a2a_bridge.urllib_request.urlopen") as mock_open:
            mock_open.return_value = _mock_urlopen(A2A_AGENT_CARD_FULL)
            bridge.register_a2a_agent("https://research.example.com")

        reg.register.assert_called_once()
        assert reg.register.call_args.kwargs["name"] == "ResearchAgent"

    def test_register_uses_service_url_as_endpoint(self):
        reg = _make_registry()
        bridge = A2ABridge(reg)

        with patch("backend.app.bridges.a2a_bridge.urllib_request.urlopen") as mock_open:
            mock_open.return_value = _mock_urlopen(A2A_AGENT_CARD_FULL)
            bridge.register_a2a_agent("https://research.example.com")

        endpoint = reg.register.call_args.kwargs["endpoint"]
        assert "research.example.com" in endpoint

    def test_register_generates_unique_agent_ids(self):
        reg = _make_registry()
        bridge = A2ABridge(reg)
        agent_ids = set()

        for _ in range(5):
            with patch("backend.app.bridges.a2a_bridge.urllib_request.urlopen") as mock_open:
                mock_open.return_value = _mock_urlopen(A2A_AGENT_CARD_MINIMAL)
                bridge.register_a2a_agent("https://minimal.example.com")
                agent_ids.add(reg.register.call_args.kwargs["agent_id"])

        assert len(agent_ids) == 5

    def test_fetch_failure_raises_bridge_error(self):
        reg = _make_registry()
        bridge = A2ABridge(reg)

        with patch("backend.app.bridges.a2a_bridge.urllib_request.urlopen") as mock_open:
            mock_open.side_effect = OSError("timeout")
            with pytest.raises(A2ABridgeError, match="Cannot fetch"):
                bridge.register_a2a_agent("https://down.example.com")

    def test_register_returns_agent_record(self):
        reg = _make_registry()
        bridge = A2ABridge(reg)

        with patch("backend.app.bridges.a2a_bridge.urllib_request.urlopen") as mock_open:
            mock_open.return_value = _mock_urlopen(A2A_AGENT_CARD_FULL)
            record = bridge.register_a2a_agent("https://research.example.com")

        assert record is reg.register.return_value

    def test_public_key_hex_is_64_chars(self):
        reg = _make_registry()
        bridge = A2ABridge(reg)

        with patch("backend.app.bridges.a2a_bridge.urllib_request.urlopen") as mock_open:
            mock_open.return_value = _mock_urlopen(A2A_AGENT_CARD_FULL)
            bridge.register_a2a_agent("https://research.example.com")

        pubkey = reg.register.call_args.kwargs["public_key_hex"]
        assert len(pubkey) == 64

    def test_minimal_card_registered_without_error(self):
        reg = _make_registry()
        bridge = A2ABridge(reg)

        with patch("backend.app.bridges.a2a_bridge.urllib_request.urlopen") as mock_open:
            mock_open.return_value = _mock_urlopen(A2A_AGENT_CARD_MINIMAL)
            record = bridge.register_a2a_agent("https://minimal.example.com")

        assert record is not None


# ═══════════════════════════════════════════════════════════════════════════════
# Cross-bridge: both produce distinct QUORBIT agent IDs for the same external URL
# ═══════════════════════════════════════════════════════════════════════════════


class TestBridgeIsolation:
    def test_mcp_and_a2a_produce_different_agent_ids(self):
        """Different bridge types for the same URL must not clash."""
        reg = _make_registry()
        mcp_ids: set = set()
        a2a_ids: set = set()

        mcp_bridge = MCPBridge(reg)
        a2a_bridge = A2ABridge(reg)

        for _ in range(3):
            with patch("backend.app.bridges.mcp_bridge.urllib_request.urlopen") as m:
                m.return_value = _mock_urlopen(MCP_MANIFEST_FLAT)
                mcp_bridge.register_mcp_server("http://x.com", "X")
                mcp_ids.add(reg.register.call_args.kwargs["agent_id"])

            with patch("backend.app.bridges.a2a_bridge.urllib_request.urlopen") as m:
                m.return_value = _mock_urlopen(A2A_AGENT_CARD_MINIMAL)
                a2a_bridge.register_a2a_agent("http://x.com")
                a2a_ids.add(reg.register.call_args.kwargs["agent_id"])

        # No ID should appear in both sets
        assert mcp_ids.isdisjoint(a2a_ids)
