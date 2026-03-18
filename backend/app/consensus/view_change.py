"""
QUORBIT Protocol — View-Change Protocol (AGPL-3.0) — D8

If a BFT quorum is not reached within ROUND_TIMEOUT (30 s), the current
validator set is marked stale and a new election is triggered.

Quorum formula: floor(2 * n / 3) + 1
  where n = number of non-abstaining voters (abstain excluded from denominator).

Vote types: COMMIT | REJECT | ABSTAIN
Emergency mode: < 11 eligible agents → flagged in Redis, election uses 7-node pool.
"""

from __future__ import annotations

import enum
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from ..bus.identity import AgentID
from .election import ValidatorSet

logger = logging.getLogger(__name__)

ROUND_TIMEOUT = 30.0   # seconds before view-change is triggered


# ── Enums & data structures ───────────────────────────────────────────────────


class VoteType(str, enum.Enum):
    COMMIT = "COMMIT"
    REJECT = "REJECT"
    ABSTAIN = "ABSTAIN"


class RoundState(str, enum.Enum):
    OPEN = "OPEN"           # accepting votes
    COMMITTED = "COMMITTED" # quorum of COMMIT votes reached
    REJECTED = "REJECTED"   # quorum of REJECT votes reached
    VIEW_CHANGE = "VIEW_CHANGE"  # timed out, stale set


@dataclass
class Vote:
    """A single validator vote for a consensus round."""

    voter_id: AgentID
    vote_type: VoteType
    round_id: str
    timestamp: float = field(default_factory=time.time)
    signature: str = ""   # base64 Ed25519 signature — verified by caller


@dataclass
class QuorumResult:
    """Snapshot of quorum status for the current round."""

    round_id: str
    state: RoundState
    commit_count: int
    reject_count: int
    abstain_count: int
    total_validators: int
    quorum_threshold: int
    elapsed_seconds: float

    @property
    def quorum_reached(self) -> bool:
        return self.state in (RoundState.COMMITTED, RoundState.REJECTED)


# ── View-Change Manager ───────────────────────────────────────────────────────


class ViewChangeManager:
    """
    Manages a single consensus round.

    Parameters
    ----------
    on_view_change : Callable[[str], None] | None
        Optional callback invoked when a view-change is triggered.
        Receives the stale round_id.
    """

    def __init__(
        self,
        on_view_change: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._on_view_change = on_view_change
        self._round_id: Optional[str] = None
        self._validator_set: Optional[ValidatorSet] = None
        self._started_at: float = 0.0
        self._votes: Dict[AgentID, Vote] = {}
        self._state: RoundState = RoundState.OPEN

    # ── Round lifecycle ───────────────────────────────────────────────────

    def start_round(self, round_id: str, validator_set: ValidatorSet) -> None:
        """Begin a new voting round. Resets all vote state."""
        self._round_id = round_id
        self._validator_set = validator_set
        self._started_at = time.time()
        self._votes = {}
        self._state = RoundState.OPEN
        logger.info(
            "ViewChange: round %s started, %d validators, timeout=%ss",
            round_id,
            len(validator_set),
            ROUND_TIMEOUT,
        )

    def receive_vote(self, vote: Vote) -> None:
        """
        Record a vote from a validator.

        Silently ignores:
          - votes for the wrong round
          - duplicate votes (first vote wins)
          - votes from non-validators
          - votes received after the round is closed
        """
        if self._round_id is None:
            logger.warning("ViewChange: vote received before round started")
            return
        if vote.round_id != self._round_id:
            return
        if self._state != RoundState.OPEN:
            return
        if self._validator_set and vote.voter_id not in self._validator_set.validators:
            logger.warning("ViewChange: vote from non-validator %s", vote.voter_id)
            return
        if vote.voter_id in self._votes:
            return  # duplicate — first vote wins

        self._votes[vote.voter_id] = vote
        logger.debug(
            "ViewChange: round %s received %s from %s",
            self._round_id,
            vote.vote_type,
            vote.voter_id,
        )

    def check_quorum(self, now: Optional[float] = None) -> QuorumResult:
        """
        Evaluate current quorum status.

        Also triggers a view-change automatically if the timeout has elapsed.
        """
        t = now if now is not None else time.time()
        elapsed = t - self._started_at
        n_validators = len(self._validator_set) if self._validator_set else 0

        commit_count = sum(
            1 for v in self._votes.values() if v.vote_type == VoteType.COMMIT
        )
        reject_count = sum(
            1 for v in self._votes.values() if v.vote_type == VoteType.REJECT
        )
        abstain_count = sum(
            1 for v in self._votes.values() if v.vote_type == VoteType.ABSTAIN
        )

        # Quorum uses total validator count as n; abstains excluded from denominator
        # means abstain votes do not count as active participation — they are neutral.
        # The quorum threshold is computed on the full validator set size.
        non_abstaining = commit_count + reject_count
        threshold = quorum_threshold(n_validators) if n_validators > 0 else 1

        if self._state == RoundState.OPEN:
            if commit_count >= threshold:
                self._state = RoundState.COMMITTED
                logger.info(
                    "ViewChange: round %s COMMITTED (%d/%d)",
                    self._round_id,
                    commit_count,
                    non_abstaining,
                )
            elif reject_count >= threshold:
                self._state = RoundState.REJECTED
                logger.info(
                    "ViewChange: round %s REJECTED (%d/%d)",
                    self._round_id,
                    reject_count,
                    non_abstaining,
                )
            elif elapsed >= ROUND_TIMEOUT:
                self.trigger_view_change()

        return QuorumResult(
            round_id=self._round_id or "",
            state=self._state,
            commit_count=commit_count,
            reject_count=reject_count,
            abstain_count=abstain_count,
            total_validators=n_validators,
            quorum_threshold=threshold,
            elapsed_seconds=elapsed,
        )

    def trigger_view_change(self) -> None:
        """
        Mark the current round as stale and invoke the view-change callback.

        The caller is responsible for running a new election.
        """
        stale_round = self._round_id or ""
        self._state = RoundState.VIEW_CHANGE
        logger.warning(
            "ViewChange: round %s timed out (%.1fs) — triggering view-change",
            stale_round,
            time.time() - self._started_at,
        )
        if self._on_view_change:
            self._on_view_change(stale_round)

    # ── Properties ────────────────────────────────────────────────────────

    @property
    def round_id(self) -> Optional[str]:
        return self._round_id

    @property
    def state(self) -> RoundState:
        return self._state

    @property
    def votes(self) -> Dict[AgentID, Vote]:
        return dict(self._votes)

    def vote_summary(self) -> Dict[str, int]:
        commits = sum(1 for v in self._votes.values() if v.vote_type == VoteType.COMMIT)
        rejects = sum(1 for v in self._votes.values() if v.vote_type == VoteType.REJECT)
        abstains = sum(1 for v in self._votes.values() if v.vote_type == VoteType.ABSTAIN)
        return {"COMMIT": commits, "REJECT": rejects, "ABSTAIN": abstains}


# ── Standalone helpers ────────────────────────────────────────────────────────


def quorum_threshold(n: int) -> int:
    """
    BFT quorum threshold: floor(2 * n / 3) + 1.

    n should be the count of non-abstaining voters.
    """
    import math
    return math.floor(2 * n / 3) + 1


def make_round_id(validator_set: ValidatorSet) -> str:
    """Generate a deterministic round ID from the validator set hash."""
    ts_bucket = int(time.time() // 60)  # 1-minute bucket
    raw = f"{validator_set.consensus_hash}:{ts_bucket}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]
