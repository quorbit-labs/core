"""
QUORBIT Protocol — Validator Election (AGPL-3.0) — D7

VRF-based deterministic validator election.

Eligibility criteria (ALL must hold):
  - agent state == ACTIVE
  - reputation >= 0.70
  - registered age >= 72 h
  - tasks_completed >= 50
  - no quarantine event in the last 30 days

VRF simulation:
  score = HMAC-SHA256(server_secret, f"{last_consensus_hash}:{agent_id}")
  Sort eligible agents by score → take top N (deterministic, unpredictable
  without knowing server_secret).

Pool sizes:
  - Normal:    min 11 validators
  - Emergency: min 7  validators  (flagged when <11 eligible agents exist)

Rotation interval: 3 600 s (1 h).
"""

from __future__ import annotations

import hashlib
import hmac
import math
import time
from dataclasses import dataclass, field
from typing import Optional

from ..bus.identity import AgentID

# ── Constants ─────────────────────────────────────────────────────────────────

NORMAL_POOL_SIZE = 11
EMERGENCY_POOL_SIZE = 7
ROTATION_INTERVAL = 3_600       # seconds — how often a new election is triggered

MIN_REPUTATION = 0.70
MIN_AGE_SECONDS = 72 * 3_600    # 72 h
MIN_TASKS = 50
QUARANTINE_LOOKBACK = 30 * 24 * 3_600  # 30 days


# ── Data structures ───────────────────────────────────────────────────────────


@dataclass
class EligibilityCriteria:
    """All attributes the election engine inspects for a candidate agent."""

    state: str                        # must be "ACTIVE"
    reputation: float                 # must be >= MIN_REPUTATION
    registered_at: float              # Unix timestamp; age must be >= MIN_AGE_SECONDS
    tasks_completed: int              # must be >= MIN_TASKS
    last_quarantine_at: Optional[float] = None  # None or older than QUARANTINE_LOOKBACK


@dataclass
class ValidatorSet:
    """The output of one election round."""

    validators: list[AgentID]
    elected_at: float
    consensus_hash: str   # becomes last_consensus_hash for the NEXT election
    is_emergency: bool = False

    def quorum_threshold(self, non_abstaining: Optional[int] = None) -> int:
        """
        BFT quorum threshold: floor(2 * n / 3) + 1.

        If non_abstaining is provided it is used as n (abstain votes excluded
        from denominator).  Otherwise the full validator count is used.
        """
        n = non_abstaining if non_abstaining is not None else len(self.validators)
        return math.floor(2 * n / 3) + 1

    def __len__(self) -> int:
        return len(self.validators)


# ── Election engine ───────────────────────────────────────────────────────────


class InsufficientValidatorsError(Exception):
    """Raised when not enough eligible agents exist to form a validator set."""


class ValidatorElection:
    """
    Runs validator elections via HMAC-based VRF scoring.

    Parameters
    ----------
    server_secret : bytes
        Server-side secret used as the HMAC key.  Must be kept confidential
        so that agents cannot predict or manipulate their own VRF scores.
    """

    def __init__(self, server_secret: bytes) -> None:
        self._secret = server_secret

    # ── Eligibility ───────────────────────────────────────────────────────

    def is_eligible(
        self,
        criteria: EligibilityCriteria,
        now: Optional[float] = None,
    ) -> bool:
        """Return True if the agent satisfies all eligibility criteria."""
        t = now if now is not None else time.time()

        if criteria.state != "ACTIVE":
            return False
        if criteria.reputation < MIN_REPUTATION:
            return False
        if (t - criteria.registered_at) < MIN_AGE_SECONDS:
            return False
        if criteria.tasks_completed < MIN_TASKS:
            return False
        if criteria.last_quarantine_at is not None:
            if (t - criteria.last_quarantine_at) < QUARANTINE_LOOKBACK:
                return False
        return True

    # ── VRF scoring ───────────────────────────────────────────────────────

    def _vrf_score(self, last_hash: str, agent_id: AgentID) -> str:
        """
        HMAC-SHA256(secret, "{last_hash}:{agent_id}") as hex.

        Deterministic given the inputs, but unpredictable without secret.
        """
        msg = f"{last_hash}:{agent_id}".encode()
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    # ── Election ──────────────────────────────────────────────────────────

    def elect(
        self,
        candidates: list[tuple[AgentID, EligibilityCriteria]],
        last_consensus_hash: str,
        now: Optional[float] = None,
    ) -> ValidatorSet:
        """
        Run an election and return a ValidatorSet.

        Parameters
        ----------
        candidates
            (agent_id, criteria) pairs for all known agents.
        last_consensus_hash
            The consensus_hash from the previous ValidatorSet (or genesis hash
            for the first round).  Used as the VRF nonce.
        now
            Override current time (for testing).

        Raises
        ------
        InsufficientValidatorsError
            If even emergency mode cannot be satisfied.
        """
        t = now if now is not None else time.time()

        eligible: list[AgentID] = [
            agent_id
            for agent_id, criteria in candidates
            if self.is_eligible(criteria, now=t)
        ]

        is_emergency = len(eligible) < NORMAL_POOL_SIZE
        pool_size = EMERGENCY_POOL_SIZE if is_emergency else NORMAL_POOL_SIZE

        if len(eligible) < pool_size:
            raise InsufficientValidatorsError(
                f"Only {len(eligible)} eligible validators, need {pool_size} "
                f"(emergency={is_emergency})"
            )

        # Sort by VRF score — lowest hex string wins (deterministic)
        scored = sorted(
            eligible,
            key=lambda aid: self._vrf_score(last_consensus_hash, aid),
        )
        selected = scored[:pool_size]

        # Derive new consensus_hash from elected set + previous hash
        set_str = ",".join(sorted(selected))
        new_hash = hashlib.sha256(
            f"{last_consensus_hash}:{set_str}".encode()
        ).hexdigest()

        return ValidatorSet(
            validators=selected,
            elected_at=t,
            consensus_hash=new_hash,
            is_emergency=is_emergency,
        )
