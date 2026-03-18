"""
QUORBIT Protocol — Phi Accrual Failure Detector (AGPL-3.0) — D10

Replaces fixed-threshold liveness checks with a probabilistic suspicion level (φ).

Heartbeat schedule:
  - Active ping every 5 s ± 500 ms jitter
  - Redis TTL per agent: 15 s  (≈ 3 missed pings → DEGRADED)

φ calculation (exponential inter-arrival model, as used in Cassandra/Akka):
  φ(Δt) = Δt / (μ · ln 10)
  where Δt = time since last heartbeat, μ = mean inter-arrival interval

State thresholds:
  φ < 4   → ACTIVE
  φ < 8   → DEGRADED
  φ >= 8  → ISOLATED        ← is_available() returns False at threshold=8.0

Circuit breaker:
  If > 40 % of tracked agents become ISOLATED within a 60 s window → SYSTEM FREEZE.
  Freeze is stored in Redis under bus:system:freeze (TTL = 300 s).
  All agents are considered unavailable while the system is FROZEN.
"""

from __future__ import annotations

import enum
import logging
import math
import time
from collections import deque
from typing import Deque, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

PING_INTERVAL = 5.0        # seconds between active pings
PING_JITTER = 0.5          # ±jitter seconds
REDIS_TTL = 15             # Redis heartbeat key TTL (3 missed pings)
WINDOW_SIZE = 100          # max inter-arrival samples kept per agent

PHI_ACTIVE = 4.0
PHI_DEGRADED = 8.0         # is_available() threshold

CIRCUIT_BREAKER_RATIO = 0.40    # fraction of ISOLATED agents to trigger freeze
CIRCUIT_BREAKER_WINDOW = 60.0   # seconds to look back for isolation events
FREEZE_DURATION = 300.0         # seconds freeze remains active


# ── State enum ────────────────────────────────────────────────────────────────


class AgentLivenessState(str, enum.Enum):
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    ISOLATED = "ISOLATED"
    FROZEN = "FROZEN"     # system-level — all agents unavailable


# ── Per-agent sample window ───────────────────────────────────────────────────


class _AgentSamples:
    """Ring buffer of inter-arrival intervals for one agent."""

    __slots__ = ("_intervals", "_last_ts", "_isolation_events")

    def __init__(self) -> None:
        self._intervals: Deque[float] = deque(maxlen=WINDOW_SIZE)
        self._last_ts: Optional[float] = None
        self._isolation_events: Deque[float] = deque()  # timestamps of ISOLATED transitions

    def record(self, ts: float) -> None:
        if self._last_ts is not None:
            interval = ts - self._last_ts
            if interval > 0:
                self._intervals.append(interval)
        self._last_ts = ts

    @property
    def last_ts(self) -> Optional[float]:
        return self._last_ts

    def mean(self) -> float:
        if not self._intervals:
            return PING_INTERVAL  # default assumption
        return sum(self._intervals) / len(self._intervals)

    def add_isolation_event(self, ts: float) -> None:
        self._isolation_events.append(ts)

    def isolation_events_since(self, since: float) -> int:
        return sum(1 for t in self._isolation_events if t >= since)

    def prune_isolation_events(self, cutoff: float) -> None:
        while self._isolation_events and self._isolation_events[0] < cutoff:
            self._isolation_events.popleft()


# ── Phi Accrual Detector ──────────────────────────────────────────────────────


class PhiAccrualDetector:
    """
    In-memory Phi Accrual Failure Detector.

    Optionally integrates with Redis for system-level freeze state.
    When redis_client is None, circuit-breaker state is stored in memory only.

    Parameters
    ----------
    redis_client : optional
        A redis.Redis instance.  If provided, system freeze is persisted to
        bus:system:freeze so all nodes see the FROZEN state.
    """

    def __init__(self, redis_client: Optional[object] = None) -> None:
        self._samples: Dict[str, _AgentSamples] = {}
        self._redis = redis_client
        self._system_frozen_until: float = 0.0   # in-memory fallback

    # ── Heartbeat recording ───────────────────────────────────────────────

    def record_heartbeat(self, agent_id: str, ts: Optional[float] = None) -> None:
        """Record a heartbeat arrival for agent_id."""
        t = ts if ts is not None else time.time()
        if agent_id not in self._samples:
            self._samples[agent_id] = _AgentSamples()
        self._samples[agent_id].record(t)

    # ── Phi calculation ───────────────────────────────────────────────────

    def phi(self, agent_id: str, now: Optional[float] = None) -> float:
        """
        Compute the suspicion level φ for agent_id.

        Uses the exponential inter-arrival model:
          φ = Δt / (μ · ln 10)

        Returns 0.0 if no heartbeats have been recorded yet.
        Returns a very large value (999.0) if the agent was never seen.
        """
        t = now if now is not None else time.time()
        samples = self._samples.get(agent_id)
        if samples is None or samples.last_ts is None:
            return 0.0

        delta_t = t - samples.last_ts
        if delta_t <= 0:
            return 0.0

        mu = samples.mean()
        if mu <= 0:
            mu = PING_INTERVAL

        return delta_t / (mu * math.log(10))

    # ── State ─────────────────────────────────────────────────────────────

    def get_state(
        self,
        agent_id: str,
        now: Optional[float] = None,
        threshold: float = PHI_DEGRADED,
    ) -> AgentLivenessState:
        """Classify agent_id into a liveness state based on current φ."""
        if self.is_system_frozen(now=now):
            return AgentLivenessState.FROZEN

        phi_val = self.phi(agent_id, now=now)
        if phi_val < PHI_ACTIVE:
            return AgentLivenessState.ACTIVE
        if phi_val < PHI_DEGRADED:
            return AgentLivenessState.DEGRADED
        return AgentLivenessState.ISOLATED

    def is_available(
        self,
        agent_id: str,
        threshold: float = PHI_DEGRADED,
        now: Optional[float] = None,
    ) -> bool:
        """
        Return False if φ >= threshold OR the system is FROZEN.

        Default threshold is 8.0 (ISOLATED boundary).
        """
        if self.is_system_frozen(now=now):
            return False
        return self.phi(agent_id, now=now) < threshold

    # ── Circuit breaker ───────────────────────────────────────────────────

    def check_circuit_breaker(self, now: Optional[float] = None) -> bool:
        """
        Evaluate the circuit breaker condition.

        If more than 40 % of tracked agents are currently ISOLATED,
        trigger a system FREEZE.  Returns True if a freeze was triggered.
        """
        t = now if now is not None else time.time()
        if not self._samples:
            return False

        isolated = [
            aid for aid in self._samples
            if self.phi(aid, now=t) >= PHI_DEGRADED
        ]
        ratio = len(isolated) / len(self._samples)

        if ratio > CIRCUIT_BREAKER_RATIO:
            self._trigger_freeze(t)
            logger.critical(
                "PhiAccrual: CIRCUIT BREAKER TRIGGERED — %.0f%% agents ISOLATED "
                "(%d/%d) — SYSTEM FREEZE for %ds",
                ratio * 100,
                len(isolated),
                len(self._samples),
                FREEZE_DURATION,
            )
            return True
        return False

    def _trigger_freeze(self, now: float) -> None:
        self._system_frozen_until = now + FREEZE_DURATION
        if self._redis is not None:
            try:
                self._redis.set(  # type: ignore[union-attr]
                    "bus:system:freeze",
                    str(now),
                    ex=int(FREEZE_DURATION),
                )
            except Exception:
                pass  # Redis unavailable — in-memory freeze still active

    def is_system_frozen(self, now: Optional[float] = None) -> bool:
        """Return True if a system-wide freeze is in effect."""
        t = now if now is not None else time.time()
        if t < self._system_frozen_until:
            return True
        if self._redis is not None:
            try:
                return bool(self._redis.exists("bus:system:freeze"))  # type: ignore[union-attr]
            except Exception:
                pass
        return False

    def thaw(self) -> None:
        """Manually lift the system freeze (operator action)."""
        self._system_frozen_until = 0.0
        if self._redis is not None:
            try:
                self._redis.delete("bus:system:freeze")  # type: ignore[union-attr]
            except Exception:
                pass

    # ── Bulk stats ────────────────────────────────────────────────────────

    def all_states(self, now: Optional[float] = None) -> Dict[str, AgentLivenessState]:
        """Return current liveness state for all tracked agents."""
        t = now if now is not None else time.time()
        return {aid: self.get_state(aid, now=t) for aid in self._samples}

    def state_counts(self, now: Optional[float] = None) -> Dict[str, int]:
        """Return count of agents per state."""
        counts: Dict[str, int] = {s.value: 0 for s in AgentLivenessState}
        for state in self.all_states(now=now).values():
            counts[state.value] += 1
        return counts

    def tracked_agents(self) -> List[str]:
        return list(self._samples.keys())
