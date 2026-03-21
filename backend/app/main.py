# Copyright (c) 2026 Quorbit Labs
# SPDX-License-Identifier: AGPL-3.0-only
"""
QUORBIT Protocol — FastAPI entry-point.

Routes
------
GET  /health                              — liveness probe
POST /api/v1/agents                       — register agent
POST /api/v1/agents/{agent_id}/heartbeat  — heartbeat
GET  /api/v1/agents/{agent_id}            — fetch agent record
POST /api/v1/discover                     — discover agents for a task
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Path, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

app = FastAPI(
    title="QUORBIT Protocol API",
    version="0.1.0",
    description="Trust layer for AI agents — identity, reputation, consensus.",
)

# ── In-memory agent store (stub; replace with AgentRegistry + Redis) ──────────

_agents: Dict[str, Dict[str, Any]] = {}


# ── Request / response models ─────────────────────────────────────────────────


class RegisterRequest(BaseModel):
    name: str
    public_key_hex: str
    capabilities: Dict[str, float] = {}
    endpoint: Optional[str] = None
    signature: Optional[str] = None


class DiscoverRequest(BaseModel):
    task_description: str
    min_score: float = 0.70
    required_capabilities: Dict[str, float] = {}


# ── Routes ────────────────────────────────────────────────────────────────────


@app.get("/health", tags=["ops"])
def health() -> dict:
    """Liveness probe — returns 200 when the API is up."""
    return {
        "status": "ok",
        "version": "0.1.0",
        "timestamp_ms": int(time.time() * 1000),
    }


@app.post("/api/v1/agents", status_code=status.HTTP_201_CREATED, tags=["agents"])
def register_agent(req: RegisterRequest) -> dict:
    """Register a new agent (or re-register an existing one)."""
    import hashlib
    agent_id = hashlib.sha256(bytes.fromhex(req.public_key_hex)).hexdigest()

    record = {
        "agent_id":      agent_id,
        "name":          req.name,
        "endpoint":      req.endpoint,
        "capabilities":  req.capabilities,
        "state":         "PROBATIONARY",
        "reputation":    0.75,
        "registered_at": time.time(),
        "last_seen":     time.time(),
    }
    _agents[agent_id] = record
    logger.info("Registered agent %s (%s)", agent_id[:12], req.name)
    return record


@app.post("/api/v1/agents/{agent_id}/heartbeat", tags=["agents"])
def heartbeat(agent_id: str = Path(...)) -> dict:
    """Update last_seen timestamp for an agent."""
    if agent_id not in _agents:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")
    _agents[agent_id]["last_seen"] = time.time()
    return {"ok": True, "agent_id": agent_id, "timestamp_ms": int(time.time() * 1000)}


@app.get("/api/v1/agents/{agent_id}", tags=["agents"])
def get_agent(agent_id: str = Path(...)) -> dict:
    """Fetch the record for a single agent."""
    if agent_id not in _agents:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id!r} not found")
    return _agents[agent_id]


@app.post("/api/v1/discover", tags=["discovery"])
def discover(req: DiscoverRequest) -> List[dict]:
    """
    Return agents whose reputation and capability scores meet the request.

    This is a stub implementation that filters the in-memory store.
    The production path routes through ParallelDiscovery (Sprint 5–7).
    """
    results = []
    for record in _agents.values():
        if record.get("reputation", 0.0) >= req.min_score:
            cap_match = _cap_match(record.get("capabilities", {}), req.required_capabilities)
            results.append({**record, "discovery_score": cap_match})
    results.sort(key=lambda r: r["discovery_score"], reverse=True)
    return results


def _cap_match(agent_caps: dict, required: dict) -> float:
    if not required:
        return 1.0
    total = sum(
        min(agent_caps.get(k, 0.0) / v, 1.0) if v > 0 else 1.0
        for k, v in required.items()
    )
    return total / len(required)
