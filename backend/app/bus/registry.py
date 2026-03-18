"""
QUORBIT Protocol — Agent Registry (AGPL-3.0)

Maintains a local view of known agents: their AgentID, public metadata,
and registration timestamp. Supports discovery and lookup.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .identity import AgentID


@dataclass
class AgentRecord:
    """A registered agent entry in the registry."""

    agent_id: AgentID
    name: str
    endpoint: Optional[str]
    registered_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    reputation: float = 1.0  # Initial reputation score (range: 0.0 – 1.0)

    def touch(self) -> None:
        """Update last_seen to now (called on heartbeat receipt)."""
        self.last_seen = time.time()

    def is_alive(self, timeout: float = 90.0) -> bool:
        """Return True if the agent sent a heartbeat within the timeout window."""
        return (time.time() - self.last_seen) < timeout


class AgentRegistry:
    """
    In-memory registry of known QUORBIT agents.

    Phase 0 stub — persistence and P2P sync are planned for Sprint 1.
    """

    def __init__(self) -> None:
        self._agents: Dict[AgentID, AgentRecord] = {}

    # ── Write ─────────────────────────────────────────────────────────────

    def register(
        self,
        agent_id: AgentID,
        name: str = "",
        endpoint: Optional[str] = None,
    ) -> AgentRecord:
        """Register a new agent or refresh an existing one."""
        if agent_id in self._agents:
            record = self._agents[agent_id]
            record.touch()
            return record

        record = AgentRecord(agent_id=agent_id, name=name, endpoint=endpoint)
        self._agents[agent_id] = record
        return record

    def deregister(self, agent_id: AgentID) -> bool:
        """Remove an agent from the registry. Returns True if it existed."""
        return self._agents.pop(agent_id, None) is not None

    def update_reputation(self, agent_id: AgentID, score: float) -> None:
        """Overwrite the reputation score for a registered agent."""
        if agent_id not in self._agents:
            raise KeyError(f"Agent {agent_id!r} not found in registry.")
        self._agents[agent_id].reputation = max(0.0, min(1.0, score))

    # ── Read ──────────────────────────────────────────────────────────────

    def get(self, agent_id: AgentID) -> Optional[AgentRecord]:
        return self._agents.get(agent_id)

    def all(self) -> List[AgentRecord]:
        return list(self._agents.values())

    def alive(self, timeout: float = 90.0) -> List[AgentRecord]:
        return [r for r in self._agents.values() if r.is_alive(timeout)]

    def count(self) -> int:
        return len(self._agents)

    def __contains__(self, agent_id: AgentID) -> bool:
        return agent_id in self._agents

    def __repr__(self) -> str:
        return f"AgentRegistry(agents={self.count()})"
