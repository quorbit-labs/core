# Copyright (c) 2026 Quorbit Labs
# SPDX-License-Identifier: AGPL-3.0-only
"""
QUORBIT Protocol — MCP Bridge (Sprint 9)

Converts a Model Context Protocol (MCP) tool-server manifest into a QUORBIT
CapabilityCard v2.0 and registers the external server as a QUORBIT agent.

MCP → QUORBIT field mapping
────────────────────────────
  MCP server name          → identity.agent_id  (derived from auto-generated Ed25519 pubkey)
  MCP server name          → identity.type = "mcp", identity.provider = name
  MCP tools[].name         → capabilities.capability_vector  {tool_name: 1.0}
  MCP tools[].description  → coordination.preferred_tasks
  url                      → registry endpoint

External agents receive an auto-generated Ed25519 keypair — they do not hold
the private key; QUORBIT acts as the identity anchor on their behalf.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, List, Optional
from urllib import request as urllib_request
from urllib.error import URLError

from ..bus.identity import AgentIdentity
from ..bus.registry import AgentRecord, AgentRegistry

logger = logging.getLogger(__name__)

# Capability score assigned to every declared MCP tool (present = fully capable)
_TOOL_SCORE: float = 1.0

# Timeout for HTTP manifest fetch
_FETCH_TIMEOUT_S: int = 10


class MCPBridgeError(Exception):
    """Raised when an MCP bridge operation fails."""


class MCPBridge:
    """
    Converts MCP tool-server manifests into QUORBIT CapabilityCard v2.0 format
    and registers the server with the AgentRegistry.

    Parameters
    ----------
    registry : AgentRegistry
        The authoritative QUORBIT agent registry.
    """

    def __init__(self, registry: AgentRegistry) -> None:
        self._registry = registry

    # ── Public API ────────────────────────────────────────────────────────

    def register_mcp_server(
        self,
        url: str,
        name: str,
        extra_capabilities: Optional[Dict[str, float]] = None,
    ) -> AgentRecord:
        """
        Fetch an MCP tool manifest and register the server as a QUORBIT agent.

        Parameters
        ----------
        url : str
            Base URL of the MCP server.  The bridge will try to fetch the
            manifest from ``{url}/tools/list`` (JSON-RPC style) or fall back
            to ``{url}/.well-known/mcp.json``.
        name : str
            Human-readable name for this MCP server.
        extra_capabilities : dict | None
            Additional capability scores to merge in beyond the tool list.

        Returns
        -------
        AgentRecord
            The newly registered agent record.

        Raises
        ------
        MCPBridgeError
            If the manifest cannot be fetched or parsed.
        """
        manifest = self._fetch_manifest(url)
        tools = self._extract_tools(manifest)
        identity = AgentIdentity.generate()
        cap_vector = self._build_capability_vector(tools, extra_capabilities)
        preferred_tasks = [t["name"] for t in tools]
        card_static = self._build_card_static(
            identity=identity,
            name=name,
            cap_vector=cap_vector,
            preferred_tasks=preferred_tasks,
        )
        record = self._registry.register(
            agent_id=identity.agent_id,
            public_key_hex=identity.public_key_hex,
            name=name,
            endpoint=url,
            capabilities={"mcp_tools": str(len(tools))},
        )
        logger.info(
            "MCPBridge: registered MCP server %r as agent %s (%d tools)",
            name, identity.agent_id, len(tools),
        )
        return record

    # ── Manifest fetching ─────────────────────────────────────────────────

    def _fetch_manifest(self, url: str) -> Dict[str, Any]:
        """
        Attempt to retrieve the MCP tool manifest from the server.

        Tries ``POST {url}/tools/list`` (JSON-RPC 2.0) first, then falls
        back to ``GET {url}/.well-known/mcp.json``.
        """
        candidates = [
            (url.rstrip("/") + "/tools/list",       "POST", b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'),
            (url.rstrip("/") + "/.well-known/mcp.json", "GET", None),
        ]
        last_err: Optional[Exception] = None
        for endpoint, method, body in candidates:
            try:
                return self._http_json(endpoint, method=method, body=body)
            except Exception as exc:
                last_err = exc
                logger.debug("MCPBridge: %s %s failed: %s", method, endpoint, exc)

        raise MCPBridgeError(
            f"Cannot fetch MCP manifest from {url!r}: {last_err}"
        )

    @staticmethod
    def _http_json(
        url: str,
        method: str = "GET",
        body: Optional[bytes] = None,
    ) -> Dict[str, Any]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        req = urllib_request.Request(url, data=body, headers=headers, method=method)
        with urllib_request.urlopen(req, timeout=_FETCH_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # ── Tool extraction ───────────────────────────────────────────────────

    @staticmethod
    def _extract_tools(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Extract the tools list from a raw MCP manifest.

        Handles both the JSON-RPC envelope (``result.tools``) and the
        well-known flat format (``tools``).
        """
        # JSON-RPC response: {"result": {"tools": [...]}}
        if "result" in manifest and isinstance(manifest["result"], dict):
            tools = manifest["result"].get("tools", [])
        else:
            tools = manifest.get("tools", [])

        if not isinstance(tools, list):
            raise MCPBridgeError(f"Unexpected tools format: {type(tools)}")
        return tools

    # ── Capability vector ─────────────────────────────────────────────────

    @staticmethod
    def _build_capability_vector(
        tools: List[Dict[str, Any]],
        extra: Optional[Dict[str, float]],
    ) -> Dict[str, float]:
        """Map each MCP tool name to score 1.0; merge extras."""
        vector: Dict[str, float] = {
            t["name"]: _TOOL_SCORE
            for t in tools
            if isinstance(t.get("name"), str)
        }
        if extra:
            vector.update(extra)
        # Ensure all values are in [0, 1]
        return {k: max(0.0, min(1.0, v)) for k, v in vector.items()}

    # ── Card static builder ───────────────────────────────────────────────

    @staticmethod
    def _build_card_static(
        identity: AgentIdentity,
        name: str,
        cap_vector: Dict[str, float],
        preferred_tasks: List[str],
    ) -> Dict[str, Any]:
        """Construct the static section of a CapabilityCard v2.0 dict."""
        return {
            "identity": {
                "agent_id": identity.agent_id,
                "type": "mcp",
                "version": "1.0",
                "provider": name,
                "public_key": identity.public_key_hex,
                "key_version": 1,
            },
            "capabilities": {
                "capability_vector": cap_vector,
                "methods": list(cap_vector.keys()),
            },
            "execution": {"stateless": True, "resumable": False},
            "tools": {
                "code_exec": False,
                "filesystem": False,
                "web_search": False,
                "external_apis": True,
                "memory_store": False,
            },
            "knowledge": {},
            "resources": {
                "max_input_tokens": 4096,
                "max_output_tokens": 4096,
                "max_concurrent_tasks": 10,
            },
            "cost_model": {"type": "external"},
            "reliability": {},
            "constraints": {"refuses_categories": [], "human_approval_for": []},
            "gaps": ["external MCP server — reliability unknown"],
            "coordination": {
                "preferred_tasks": preferred_tasks,
                "avoid_tasks": [],
                "human_in_loop_for": [],
            },
        }
