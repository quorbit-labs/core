"""
QUORBIT Protocol — Discovery Package (AGPL-3.0)

Provides:
  parallel    — Parallel 3-layer discovery with Scoring v1
  messages    — Wire message types: TASK_DELEGATION, TASK_STANDBY, TASK_RESUME,
                CapabilityResponse
  relaxation  — Escalation levels (R5/R6) with exclude_list support
"""

from .messages import (
    CapabilityResponse,
    TaskDelegationMessage,
    TaskStandbyMessage,
    TaskResumeMessage,
)
from .relaxation import RelaxationLevel, RelaxationPolicy
from .parallel import DiscoveryQuery, DiscoveryResult, ParallelDiscovery, score_candidate

__all__ = [
    "CapabilityResponse",
    "TaskDelegationMessage",
    "TaskStandbyMessage",
    "TaskResumeMessage",
    "RelaxationLevel",
    "RelaxationPolicy",
    "DiscoveryQuery",
    "DiscoveryResult",
    "ParallelDiscovery",
    "score_candidate",
]
