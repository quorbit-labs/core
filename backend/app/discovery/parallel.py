"""
QUORBIT Protocol — Parallel Discovery (AGPL-3.0) — R1, R2, R3

Three-layer concurrent discovery with merge, dedup, scoring and failover.

Algorithm
─────────
  t=0     Launch three discovery layers concurrently:
            local    — LAN broadcast   (timeout 1.5 s)
            gossip   — gossip network  (TTL=3, timeout 2 s)
            registry — authoritative   (timeout 4 s)

  t=3 s   Merge results + dedup by agent_id (keep highest cap_match).
          Hard filter: cap_match < 0.70 → discard.
          Score survivors with Scoring v1 (has_history or cold_start mode).
          Apply DEGRADED penalty (× 0.70).
          Select tiers: primary, backup1, backup2.

  t=3 s, no candidates
          Escalate RelaxationPolicy (level++ → threshold 0.50).
          Re-query.

  t=8 s, still no candidates
          Emit self_execute(mode=PREPROCESSING) signal.

Scoring v1 (R2)
───────────────
  has_history   (operational_metrics.tasks_total > 20)
    task_fit_avg_30d        × 0.30
    failure_transparency    × 0.15
    structured_output_rate  × 0.15
    capability_vector_match × 0.20
    reputation_score        × 0.10
    (1 − current_load)      × 0.05
    efficiency_score        × 0.05

  cold_start    (tasks_total ≤ 20)
    capability_vector_match × 0.40
    reputation_score        × 0.35
    (1 − current_load)      × 0.15
    sla_estimates.speed_score × 0.10

  DEGRADED state penalty: final_score × 0.70

Failure / failover
──────────────────
  Primary failure → promote backup1, send TASK_RESUME(checkpoint_data).
"""

from __future__ import annotations

import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .messages import CapabilityResponse, TaskDelegationMessage, TaskResumeMessage, TaskStandbyMessage
from .relaxation import RelaxationLevel, RelaxationPolicy

logger = logging.getLogger(__name__)

# ── Timing constants ──────────────────────────────────────────────────────────

TIMEOUT_LOCAL_S: float = 1.5
TIMEOUT_GOSSIP_S: float = 2.0
TIMEOUT_REGISTRY_S: float = 4.0
MERGE_WINDOW_S: float = 3.0        # decision boundary after launch
SELF_EXECUTE_WINDOW_S: float = 8.0 # fallback if still no candidates
CAP_MATCH_HARD_FLOOR: float = 0.70 # hard filter floor (Level 0)

# ── Scoring constants ─────────────────────────────────────────────────────────

HAS_HISTORY_THRESHOLD: int = 20   # tasks_total > this → has_history mode

# has_history weights (must sum to 1.0)
W_TASK_FIT: float = 0.30
W_FAIL_TRANS: float = 0.15
W_STRUCT_OUT: float = 0.15
W_CAP_MATCH: float = 0.20
W_REPUTATION: float = 0.10
W_LOAD: float = 0.05
W_EFFICIENCY: float = 0.05

# cold_start weights (must sum to 1.0)
W_CS_CAP_MATCH: float = 0.40
W_CS_REPUTATION: float = 0.35
W_CS_LOAD: float = 0.15
W_CS_SLA_SPEED: float = 0.10

DEGRADED_PENALTY: float = 0.70     # multiply final score by this for DEGRADED


# ── Discovery query / result types ───────────────────────────────────────────


@dataclass
class DiscoveryQuery:
    """
    Parameters for a single parallel discovery round.

    Attributes
    ----------
    required_skills : Dict[str, float]
        Minimum skill levels required, keyed by skill name.
    task_type : str
        Task type tag for tag-based matching.
    exclude_list : List[str]
        Agent IDs to never select (R6).
    gossip_ttl : int
        Initial gossip TTL (hops).
    """

    required_skills: Dict[str, float] = field(default_factory=dict)
    task_type: str = ""
    exclude_list: List[str] = field(default_factory=list)
    gossip_ttl: int = 3


@dataclass
class ScoredCandidate:
    """A capability response paired with its computed discovery score."""

    response: CapabilityResponse
    score: float
    mode: str       # "has_history" or "cold_start"
    penalised: bool = False   # True if DEGRADED penalty was applied


@dataclass
class DiscoveryResult:
    """
    Output of a completed parallel discovery round.

    Attributes
    ----------
    primary : ScoredCandidate | None
        Highest-scoring eligible agent.
    backups : List[ScoredCandidate]
        Next-best agents (up to 2).
    relaxation_level : RelaxationLevel
        Level at which candidates were found.
    self_execute : bool
        True if no candidates were found within SELF_EXECUTE_WINDOW_S.
    all_candidates : List[ScoredCandidate]
        All surviving candidates after filtering and scoring.
    """

    primary: Optional[ScoredCandidate]
    backups: List[ScoredCandidate]
    relaxation_level: RelaxationLevel
    self_execute: bool = False
    all_candidates: List[ScoredCandidate] = field(default_factory=list)


# ── Capability vector match ───────────────────────────────────────────────────


def _capability_vector_match(
    required: Dict[str, float],
    agent_vector: Dict[str, float],
) -> float:
    """
    Compute the capability match score between a query and an agent.

    Score = mean of per-skill satisfaction, where each skill is:
      min(agent_level / required_level, 1.0)  if required_level > 0
      1.0  if required_level == 0 (no minimum)

    Returns 0.0 if *required* is empty.
    """
    if not required:
        return 0.0
    total = 0.0
    for skill, req_level in required.items():
        agent_level = agent_vector.get(skill, 0.0)
        if req_level <= 0:
            total += 1.0
        else:
            total += min(agent_level / req_level, 1.0)
    return total / len(required)


# ── Efficiency score helper ───────────────────────────────────────────────────


def _efficiency_score(metrics: dict) -> float:
    """
    Derive a normalised efficiency score from operational_metrics.

    Uses tokens_per_task inversely: fewer tokens per task → higher score.
    Falls back to 0.5 if metric is unavailable or zero.
    """
    tpt = float(metrics.get("efficiency_tokens_per_task", 0))
    if tpt <= 0:
        return 0.5
    # Reference: 2000 tokens/task = score 0.5
    # Sigmoid-like normalisation capped at [0, 1]
    return max(0.0, min(1.0, 2000.0 / tpt))


# ── Scoring v1 ────────────────────────────────────────────────────────────────


def score_candidate(
    response: CapabilityResponse,
    query: DiscoveryQuery,
    operational_metrics: Optional[dict] = None,
    agent_state: str = "ACTIVE",
    sla_speed_score: float = 0.5,
) -> ScoredCandidate:
    """
    Compute a Scoring v1 discovery score for a single candidate.

    Parameters
    ----------
    response : CapabilityResponse
        The candidate's capability response (contains load, reputation, etc.).
    query : DiscoveryQuery
        The originating query (required_skills used for cap_match).
    operational_metrics : dict | None
        The candidate's ``operational_metrics`` from its CapabilityCard
        (if available).  When None, cold_start mode is used.
    agent_state : str
        The candidate's registry state (e.g. "ACTIVE", "DEGRADED").
    sla_speed_score : float
        The candidate's SLA speed score from its CapabilityCard (used in
        cold_start mode).

    Returns
    -------
    ScoredCandidate
        The scored candidate with mode and penalty flag set.
    """
    metrics = operational_metrics or {}
    tasks_total = int(metrics.get("tasks_total", 0))
    has_history = tasks_total > HAS_HISTORY_THRESHOLD

    cap_match = float(response.capability_match_score)
    reputation = float(response.reputation_score)
    load = max(0.0, min(1.0, float(response.load)))

    if has_history:
        task_fit = float(metrics.get("task_fit_avg_30d", 0.0))
        fail_trans = float(metrics.get("failure_transparency_score", 0.5))
        struct_out = float(metrics.get("structured_output_rate", 0.0))
        efficiency = _efficiency_score(metrics)

        raw_score = (
            task_fit        * W_TASK_FIT
            + fail_trans    * W_FAIL_TRANS
            + struct_out    * W_STRUCT_OUT
            + cap_match     * W_CAP_MATCH
            + reputation    * W_REPUTATION
            + (1 - load)    * W_LOAD
            + efficiency    * W_EFFICIENCY
        )
        mode = "has_history"
    else:
        raw_score = (
            cap_match           * W_CS_CAP_MATCH
            + reputation        * W_CS_REPUTATION
            + (1 - load)        * W_CS_LOAD
            + sla_speed_score   * W_CS_SLA_SPEED
        )
        mode = "cold_start"

    raw_score = max(0.0, min(1.0, raw_score))

    # DEGRADED penalty
    penalised = agent_state == "DEGRADED"
    if penalised:
        raw_score *= DEGRADED_PENALTY

    return ScoredCandidate(
        response=response,
        score=raw_score,
        mode=mode,
        penalised=penalised,
    )


# ── Layer type aliases ────────────────────────────────────────────────────────

# A layer function accepts a DiscoveryQuery and returns a list of CapabilityResponse
LayerFn = Callable[[DiscoveryQuery], List[CapabilityResponse]]


# ── ParallelDiscovery ─────────────────────────────────────────────────────────


class ParallelDiscovery:
    """
    Orchestrates the parallel three-layer discovery process.

    Parameters
    ----------
    local_layer : LayerFn
        LAN broadcast query function (timeout enforced externally to 1.5 s).
    gossip_layer : LayerFn
        Gossip network query function (timeout 2 s).
    registry_layer : LayerFn
        Registry query function (timeout 4 s).
    metrics_provider : Callable[[str], dict] | None
        Optional callable that returns operational_metrics for an agent_id.
    state_provider : Callable[[str], str] | None
        Optional callable that returns the state string for an agent_id.
    sla_provider : Callable[[str], dict] | None
        Optional callable that returns sla_estimates for an agent_id.
    """

    def __init__(
        self,
        local_layer: LayerFn,
        gossip_layer: LayerFn,
        registry_layer: LayerFn,
        metrics_provider: Optional[Callable[[str], dict]] = None,
        state_provider: Optional[Callable[[str], str]] = None,
        sla_provider: Optional[Callable[[str], dict]] = None,
    ) -> None:
        self._local = local_layer
        self._gossip = gossip_layer
        self._registry = registry_layer
        self._metrics = metrics_provider or (lambda _: {})
        self._state = state_provider or (lambda _: "ACTIVE")
        self._sla = sla_provider or (lambda _: {"speed_score": 0.5})

    # ── Layer queries ─────────────────────────────────────────────────────

    def _query_layers(
        self,
        query: DiscoveryQuery,
    ) -> List[CapabilityResponse]:
        """
        Fire all three layers concurrently and collect responses.

        Each layer has its own timeout.  Results are labelled with
        source_layer for diagnostics.
        """
        layer_configs: List[Tuple[str, LayerFn, float]] = [
            ("local",    self._local,    TIMEOUT_LOCAL_S),
            ("gossip",   self._gossip,   TIMEOUT_GOSSIP_S),
            ("registry", self._registry, TIMEOUT_REGISTRY_S),
        ]

        collected: List[CapabilityResponse] = []

        with ThreadPoolExecutor(max_workers=3) as pool:
            future_to_layer = {
                pool.submit(fn, query): (name, timeout)
                for name, fn, timeout in layer_configs
            }
            for future, (name, timeout) in future_to_layer.items():
                try:
                    responses = future.result(timeout=timeout)
                    for r in responses:
                        r.source_layer = name
                        collected.append(r)
                    logger.debug("Discovery layer %s: %d responses", name, len(responses))
                except FutureTimeout:
                    logger.warning("Discovery layer %s timed out", name)
                except Exception as exc:
                    logger.error("Discovery layer %s error: %s", name, exc)

        return collected

    # ── Dedup ─────────────────────────────────────────────────────────────

    @staticmethod
    def _dedup(
        responses: List[CapabilityResponse],
    ) -> List[CapabilityResponse]:
        """
        Deduplicate by agent_id, keeping the response with the highest
        capability_match_score when the same agent appears in multiple layers.
        """
        best: Dict[str, CapabilityResponse] = {}
        for r in responses:
            existing = best.get(r.agent_id)
            if existing is None or r.capability_match_score > existing.capability_match_score:
                best[r.agent_id] = r
        return list(best.values())

    # ── Hard filter ───────────────────────────────────────────────────────

    @staticmethod
    def _hard_filter(
        responses: List[CapabilityResponse],
        threshold: float,
    ) -> List[CapabilityResponse]:
        """Discard candidates below the cap_match threshold."""
        return [r for r in responses if r.capability_match_score >= threshold]

    # ── Score and rank ────────────────────────────────────────────────────

    def _score_and_rank(
        self,
        responses: List[CapabilityResponse],
        query: DiscoveryQuery,
    ) -> List[ScoredCandidate]:
        scored: List[ScoredCandidate] = []
        for r in responses:
            metrics = self._metrics(r.agent_id)
            state = self._state(r.agent_id)
            sla = self._sla(r.agent_id)
            sla_speed = float(sla.get("speed_score", 0.5))

            candidate = score_candidate(
                response=r,
                query=query,
                operational_metrics=metrics if metrics else None,
                agent_state=state,
                sla_speed_score=sla_speed,
            )
            scored.append(candidate)

        scored.sort(key=lambda c: c.score, reverse=True)
        return scored

    # ── Main discover() ───────────────────────────────────────────────────

    def discover(self, query: DiscoveryQuery) -> DiscoveryResult:
        """
        Execute the full parallel discovery algorithm.

        Returns a DiscoveryResult with primary, backups, and flags.
        """
        policy = RelaxationPolicy(exclude_list=list(query.exclude_list))
        start = time.monotonic()

        while True:
            # Apply gossip TTL multiplier to a query copy
            effective_query = DiscoveryQuery(
                required_skills=query.required_skills,
                task_type=query.task_type,
                exclude_list=list(query.exclude_list),
                gossip_ttl=max(1, round(query.gossip_ttl * policy.ttl_multiplier)),
            )

            responses = self._query_layers(effective_query)
            deduped = self._dedup(responses)
            filtered = self._hard_filter(deduped, threshold=policy.threshold)
            # Apply exclude list
            filtered = policy.filter_candidates(filtered)

            if filtered:
                # Compute capability_match_score if not already set by layer
                # (some layers may return 0.0; recompute from required_skills)
                for r in filtered:
                    if r.capability_match_score == 0.0 and query.required_skills:
                        # We don't have the agent's capability vector here;
                        # rely on what the layer returned.
                        pass

                scored = self._score_and_rank(filtered, query)
                primary = scored[0] if scored else None
                backups = scored[1:3]

                elapsed = time.monotonic() - start
                logger.info(
                    "Discovery: found %d candidates in %.2f s (level=%s)",
                    len(scored), elapsed, policy.level.name,
                )
                return DiscoveryResult(
                    primary=primary,
                    backups=backups,
                    relaxation_level=policy.level,
                    all_candidates=scored,
                )

            elapsed = time.monotonic() - start

            if elapsed >= SELF_EXECUTE_WINDOW_S:
                logger.warning(
                    "Discovery: no candidates after %.1f s — self_execute", elapsed
                )
                return DiscoveryResult(
                    primary=None,
                    backups=[],
                    relaxation_level=policy.level,
                    self_execute=True,
                )

            if not policy.escalate():
                # Already at BROAD; wait for self_execute window
                time.sleep(max(0.0, SELF_EXECUTE_WINDOW_S - elapsed))
                return DiscoveryResult(
                    primary=None,
                    backups=[],
                    relaxation_level=policy.level,
                    self_execute=True,
                )

            logger.info(
                "Discovery: no results at level %d — escalating to %s",
                policy.level - 1,
                policy.level.name,
            )

    # ── Failover ──────────────────────────────────────────────────────────

    def handle_primary_failure(
        self,
        result: DiscoveryResult,
        task_id: str,
        sender_id: str,
        checkpoint_data: Dict[str, Any],
        failed_agent_id: str,
        failure_reason: str = "",
    ) -> Optional[TaskResumeMessage]:
        """
        Promote backup1 to primary and emit a TASK_RESUME message.

        Returns None if no backup is available.
        """
        if not result.backups:
            logger.error(
                "Discovery failover: no backup for task %s — cannot resume",
                task_id,
            )
            return None

        backup1 = result.backups[0]
        msg = TaskResumeMessage(
            task_id=task_id,
            sender_id=sender_id,
            resumed_agent_id=backup1.response.agent_id,
            failed_agent_id=failed_agent_id,
            checkpoint_data=checkpoint_data,
            failure_reason=failure_reason,
        )
        logger.warning(
            "Discovery failover: task %s resumed by %s (was %s)",
            task_id,
            backup1.response.agent_id,
            failed_agent_id,
        )
        return msg

    # ── Message factories ─────────────────────────────────────────────────

    def build_delegation_messages(
        self,
        result: DiscoveryResult,
        task_id: str,
        sender_id: str,
        task_type: str,
        payload: Dict[str, Any],
        sla_deadline_ms: int,
    ) -> Tuple[Optional[TaskDelegationMessage], List[TaskStandbyMessage]]:
        """
        Build the TASK_DELEGATION message for the primary and
        TASK_STANDBY messages for each backup.

        Returns (delegation_msg, standby_msgs).
        """
        if result.primary is None:
            return None, []

        backup_ids = [b.response.agent_id for b in result.backups]

        delegation = TaskDelegationMessage(
            task_id=task_id,
            sender_id=sender_id,
            primary_agent_id=result.primary.response.agent_id,
            task_type=task_type,
            payload=payload,
            sla_deadline_ms=sla_deadline_ms,
            backup_agent_ids=backup_ids,
        )

        standbys = []
        for rank, backup in enumerate(result.backups, start=1):
            standbys.append(TaskStandbyMessage(
                task_id=task_id,
                sender_id=sender_id,
                backup_agent_id=backup.response.agent_id,
                task_type=task_type,
                task_summary=f"standby:{task_type}",
                sla_deadline_ms=sla_deadline_ms,
                standby_rank=rank,
            ))

        return delegation, standbys
