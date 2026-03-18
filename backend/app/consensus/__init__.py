"""
QUORBIT Protocol — consensus package (AGPL-3.0)

Consensus and liveness layer:
- election:    VRF-based validator election, eligibility criteria
- view_change: BFT view-change protocol, quorum tracking
- phi_accrual: Phi Accrual failure detector, circuit breaker
"""

from .election import (
    EligibilityCriteria,
    InsufficientValidatorsError,
    ValidatorElection,
    ValidatorSet,
    NORMAL_POOL_SIZE,
    EMERGENCY_POOL_SIZE,
    ROTATION_INTERVAL,
    MIN_REPUTATION,
    MIN_AGE_SECONDS,
    MIN_TASKS,
)
from .phi_accrual import (
    AgentLivenessState,
    PhiAccrualDetector,
    PHI_ACTIVE,
    PHI_DEGRADED,
    CIRCUIT_BREAKER_RATIO,
    FREEZE_DURATION,
)
from .view_change import (
    Vote,
    VoteType,
    ViewChangeManager,
    QuorumResult,
    RoundState,
    quorum_threshold,
    make_round_id,
    ROUND_TIMEOUT,
)

__all__ = [
    # election
    "EligibilityCriteria",
    "InsufficientValidatorsError",
    "ValidatorElection",
    "ValidatorSet",
    "NORMAL_POOL_SIZE",
    "EMERGENCY_POOL_SIZE",
    "ROTATION_INTERVAL",
    "MIN_REPUTATION",
    "MIN_AGE_SECONDS",
    "MIN_TASKS",
    # phi_accrual
    "AgentLivenessState",
    "PhiAccrualDetector",
    "PHI_ACTIVE",
    "PHI_DEGRADED",
    "CIRCUIT_BREAKER_RATIO",
    "FREEZE_DURATION",
    # view_change
    "Vote",
    "VoteType",
    "ViewChangeManager",
    "QuorumResult",
    "RoundState",
    "quorum_threshold",
    "make_round_id",
    "ROUND_TIMEOUT",
]
