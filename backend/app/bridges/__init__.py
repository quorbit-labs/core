# Copyright (c) 2026 Quorbit Labs
# SPDX-License-Identifier: AGPL-3.0-only
"""
QUORBIT Protocol — External Agent Bridges

Bridges convert third-party agent manifests into QUORBIT CapabilityCard v2.0
format and register them with the AgentRegistry.

  MCPBridge  — Model Context Protocol (MCP) tool-server manifests
  A2ABridge  — Google Agent-to-Agent (A2A) AgentCard JSON
"""

from .a2a_bridge import A2ABridge
from .mcp_bridge import MCPBridge

__all__ = ["MCPBridge", "A2ABridge"]
