# Copyright (c) 2026 Quorbit Labs
# SPDX-License-Identifier: AGPL-3.0-only
"""
QUORBIT Protocol — FastAPI entry-point.

Routes
------
GET  /health                              — liveness probe
POST /api/v1/agents                       — register agent (CapabilityCard v2.0)
POST /api/v1/agents/{agent_id}/heartbeat  — signed heartbeat
GET  /api/v1/agents/{agent_id}            — fetch agent record
POST /api/v1/discover                     — discover agents for a task

Sprint 10: Updated to accept CapabilityCard v2.0 registration format
           and structured heartbeat/discover payloads.
           Legacy flat format (name + public_key_hex) still supported.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Path, Request, status

logger = logging.getLogger(__name__)
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

app = FastAPI(
    title="QUORBIT Protocol API",
    version="0.1.1",
    description="Trust layer for AI agents — identity, reputation, consensus.",
)

# ── In-memory agent store (stub; replace with AgentRegistry + Redis) ──────────

_agents: Dict[str, Dict[str, Any]] = {}


# ── Routes ────────────────────────────────────────────────────────────────────


@app.get("/health", tags=["ops"])
def health() -> dict:
    """Liveness probe — returns 200 when the API is up."""
    return {
        "status": "ok",
        "version": "0.1.1",
        "timestamp_ms": int(time.time() * 1000),
    }


@app.post("/api/v1/agents", status_code=status.HTTP_201_CREATED, tags=["agents"])
async def register_agent(request: Request) -> dict:
    """Register an agent — accepts CapabilityCard v2.0 or legacy flat format.

    CapabilityCard v2.0 (nested):
        { identity: { public_key, agent_type, ... },
          capabilities: { capability_vector: { skill: score } }, ... }

    Legacy flat (SDK):
        { name: "...", public_key_hex: "...", capabilities: { skill: score } }
    """
    body = await request.json()

    # Detect format: if "identity" key present → v2.0, else legacy
    if "identity" in body:
        return _register_v2(body)
    elif "public_key_hex" in body:
        return _register_legacy(body)
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Unrecognized registration format. "
                   "Provide either 'identity' (CapabilityCard v2.0) "
                   "or 'public_key_hex' (legacy).",
        )


def _register_v2(body: dict) -> dict:
    """Handle CapabilityCard v2.0 registration."""
    identity = body.get("identity", {})
    capabilities = body.get("capabilities", {})
    resource_limits = body.get("resource_limits", {})
    coordination = body.get("coordination", {})

    public_key_hex = identity.get("public_key", "")
    if not public_key_hex:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="identity.public_key is required",
        )

    try:
        pub_bytes = bytes.fromhex(public_key_hex)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="identity.public_key must be valid hex",
        )

    agent_id = hashlib.sha256(pub_bytes).hexdigest()
    cap_vector = capabilities.get("capability_vector", {})

    record = {
        "agent_id": agent_id,
        "name": identity.get("provider", "unknown"),
        "capabilities": cap_vector,
        "state": "PROBATIONARY",
        "reputation": 0.75,
        "registered_at": time.time(),
        "last_seen": time.time(),
        "_card": {
            "schema_version": body.get("schema_version", "2.0"),
            "identity": identity,
            "capabilities": capabilities,
            "resource_limits": resource_limits,
            "coordination": coordination,
            "gaps": body.get("gaps", []),
        },
        "_dynamic": {
            "state": "PROBATIONARY",
            "reputation_score": 0.75,
            "heartbeat_count": 0,
        },
    }
    _agents[agent_id] = record
    logger.info("Registered agent %s (v2.0, provider=%s)", agent_id[:12], identity.get("provider"))
    return record


def _register_legacy(body: dict) -> dict:
    """Handle legacy flat-format registration."""
    public_key_hex = body["public_key_hex"]
    try:
        pub_bytes = bytes.fromhex(public_key_hex)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="public_key_hex must be valid hex",
        )

    agent_id = hashlib.sha256(pub_bytes).hexdigest()

    record = {
        "agent_id": agent_id,
        "name": body.get("name", "unknown"),
        "endpoint": body.get("endpoint"),
        "capabilities": body.get("capabilities", {}),
        "state": "PROBATIONARY",
        "reputation": 0.75,
        "registered_at": time.time(),
        "last_seen": time.time(),
    }
    _agents[agent_id] = record
    logger.info("Registered agent %s (legacy, name=%s)", agent_id[:12], body.get("name"))
    return record


@app.post("/api/v1/agents/{agent_id}/heartbeat", tags=["agents"])
async def heartbeat(request: Request, agent_id: str = Path(...)) -> dict:
    """Update last_seen timestamp for an agent.

    Accepts optional signed payload:
        { payload: { agent_id, timestamp_ms },
          signature: "hex",
          key_version: 1 }

    Also works with empty body (legacy).
    """
    if agent_id not in _agents:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")

    # Parse optional body (may be empty for legacy clients)
    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass

    agent = _agents[agent_id]
    agent["last_seen"] = time.time()

    # Track heartbeat count in _dynamic if present
    if "_dynamic" in agent:
        agent["_dynamic"]["heartbeat_count"] = agent["_dynamic"].get("heartbeat_count", 0) + 1

    response: Dict[str, Any] = {
        "ok": True,
        "agent_id": agent_id,
        "timestamp_ms": int(time.time() * 1000),
    }

    # If signed payload was provided, acknowledge it
    if body.get("signature"):
        response["signature_received"] = True
        response["key_version"] = body.get("key_version")

    return response


@app.get("/api/v1/agents/{agent_id}", tags=["agents"])
def get_agent(agent_id: str = Path(...)) -> dict:
    """Fetch the record for a single agent."""
    if agent_id not in _agents:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")
    return _agents[agent_id]


@app.post("/api/v1/discover", tags=["discovery"])
async def discover(request: Request) -> dict:
    """Return agents matching the discovery query.

    Accepts both structured and legacy format.
    Always returns { candidates: [...] } wrapper.

    Structured (e2e_demo.py):
        { task: { intent, ... }, required_capabilities: {...},
          min_capability_match: 0.50 }

    Legacy (SDK):
        { task_description: "...", min_score: 0.70,
          required_capabilities: {...} }
    """
    body = await request.json()

    # Extract required_capabilities from either format
    required = body.get("required_capabilities", {})

    # Extract minimum score threshold
    min_score = body.get("min_capability_match", body.get("min_score", 0.50))

    results = []
    for record in _agents.values():
        cap_match = _cap_match(record.get("capabilities", {}), required)
        if cap_match >= min_score:
            results.append({
                "agent_id": record["agent_id"],
                "name": record.get("name", "unknown"),
                "state": record.get("state", "unknown"),
                "score": round(cap_match, 4),
                "capability_match_score": round(cap_match, 4),
                "reputation": record.get("reputation", 0.0),
            })

    results.sort(key=lambda r: r["score"], reverse=True)
    return {"candidates": results}


def _cap_match(agent_caps: dict, required: dict) -> float:
    """Calculate capability match score between agent and requirements."""
    if not required:
        return 1.0
    total = sum(
        min(agent_caps.get(k, 0.0) / v, 1.0) if v > 0 else 1.0
        for k, v in required.items()
    )
    return total / len(required)
