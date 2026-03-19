"""
QUORBIT Protocol — Discovery Message Types (AGPL-3.0) — R3

Wire messages used during parallel discovery and task delegation.

Message types
─────────────
  TASK_DELEGATION   →  primary agent    (full task payload)
  TASK_STANDBY      →  backup1/2        (metadata only, pre-warm)
  TASK_RESUME       →  on failure       (checkpoint_data to promoted backup)

CapabilityResponse  — sent back by agents in reply to a discovery query
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ── CapabilityResponse ────────────────────────────────────────────────────────


@dataclass
class CapabilityResponse:
    """
    Response sent by an agent in answer to a capability query.

    All performance fields are agent-self-reported EXCEPT reputation_score,
    which is injected by the registry during response processing.

    Fields
    ------
    agent_id                  Unique agent identifier.
    endpoint                  Contact address (e.g. "http://host:port").
    capability_match_score    Pre-computed by the querier; float [0–1].
    estimated_start_delay_sec Seconds until the agent can begin the task.
    estimated_completion_sec  Estimated total wall-clock seconds.
    queue_depth               Current number of queued tasks.
    load                      Current CPU/resource load [0–1].
    reputation_score          Registry-injected; NOT self-reported.
    cost_estimate             Estimated cost in protocol units.
    signature                 Ed25519 signature over canonical fields.
    """

    agent_id: str
    endpoint: str
    capability_match_score: float
    estimated_start_delay_sec: float = 0.0
    estimated_completion_sec: float = 0.0
    queue_depth: int = 0
    load: float = 0.0
    reputation_score: float = 0.75      # injected by registry
    cost_estimate: float = 0.0
    signature: Optional[str] = None
    # Discovery layer that sourced this response
    source_layer: str = "unknown"       # "local" | "gossip" | "registry"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "endpoint": self.endpoint,
            "capability_match_score": self.capability_match_score,
            "estimated_start_delay_sec": self.estimated_start_delay_sec,
            "estimated_completion_sec": self.estimated_completion_sec,
            "queue_depth": self.queue_depth,
            "load": self.load,
            "reputation_score": self.reputation_score,
            "cost_estimate": self.cost_estimate,
            "signature": self.signature,
            "source_layer": self.source_layer,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CapabilityResponse":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ── Task messages ─────────────────────────────────────────────────────────────


@dataclass
class TaskDelegationMessage:
    """
    TASK_DELEGATION — full task payload sent to the primary agent.

    Carries the complete task specification.  Only the primary candidate
    receives this; backups receive TASK_STANDBY.
    """

    task_id: str
    sender_id: str
    primary_agent_id: str
    task_type: str
    payload: Dict[str, Any]
    sla_deadline_ms: int                # absolute unix-ms deadline
    backup_agent_ids: List[str] = field(default_factory=list)
    timestamp_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    signature: Optional[str] = None

    @property
    def message_type(self) -> str:
        return "TASK_DELEGATION"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message_type": self.message_type,
            "task_id": self.task_id,
            "sender_id": self.sender_id,
            "primary_agent_id": self.primary_agent_id,
            "task_type": self.task_type,
            "payload": self.payload,
            "sla_deadline_ms": self.sla_deadline_ms,
            "backup_agent_ids": self.backup_agent_ids,
            "timestamp_ms": self.timestamp_ms,
            "signature": self.signature,
        }


@dataclass
class TaskStandbyMessage:
    """
    TASK_STANDBY — metadata-only pre-warm sent to backup agents.

    Backup agents receive only enough information to prepare (e.g. load
    relevant context) without receiving the full payload.  This reduces
    failover latency.
    """

    task_id: str
    sender_id: str
    backup_agent_id: str
    task_type: str
    task_summary: str                   # abbreviated, NOT full payload
    sla_deadline_ms: int
    standby_rank: int = 1               # 1 = backup1, 2 = backup2
    timestamp_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    signature: Optional[str] = None

    @property
    def message_type(self) -> str:
        return "TASK_STANDBY"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message_type": self.message_type,
            "task_id": self.task_id,
            "sender_id": self.sender_id,
            "backup_agent_id": self.backup_agent_id,
            "task_type": self.task_type,
            "task_summary": self.task_summary,
            "sla_deadline_ms": self.sla_deadline_ms,
            "standby_rank": self.standby_rank,
            "timestamp_ms": self.timestamp_ms,
            "signature": self.signature,
        }


@dataclass
class TaskResumeMessage:
    """
    TASK_RESUME — sent to a promoted backup agent on primary failure.

    Carries the checkpoint data captured before the failure so the backup
    can continue from a known-good state rather than restarting from scratch.
    """

    task_id: str
    sender_id: str
    resumed_agent_id: str               # promoted backup
    failed_agent_id: str                # original primary that failed
    checkpoint_data: Dict[str, Any]     # serialised intermediate state
    failure_reason: str = ""
    timestamp_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    signature: Optional[str] = None

    @property
    def message_type(self) -> str:
        return "TASK_RESUME"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message_type": self.message_type,
            "task_id": self.task_id,
            "sender_id": self.sender_id,
            "resumed_agent_id": self.resumed_agent_id,
            "failed_agent_id": self.failed_agent_id,
            "checkpoint_data": self.checkpoint_data,
            "failure_reason": self.failure_reason,
            "timestamp_ms": self.timestamp_ms,
            "signature": self.signature,
        }
