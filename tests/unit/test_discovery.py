"""
Unit tests — QUORBIT Parallel Discovery & Scoring v1 (Sprint 5) — R1, R2, R3, R5, R6

Coverage:
  - Scoring has_history formula (weights correct)
  - Scoring cold_start formula (weights correct)
  - DEGRADED penalty (score × 0.70)
  - Relaxation levels (threshold drops 0.70 → 0.50 → 0.00)
  - Dedup by agent_id (best cap_match_score kept)
  - Exclude list (R6)
  - Parallel discovery merges results from multiple layers
  - Failover: backup1 promoted on primary failure
  - Message types: TASK_DELEGATION, TASK_STANDBY, TASK_RESUME
"""

from __future__ import annotations

import pytest

from backend.app.discovery.parallel import (
    DiscoveryQuery,
    DiscoveryResult,
    ParallelDiscovery,
    ScoredCandidate,
    _capability_vector_match,
    _efficiency_score,
    score_candidate,
    DEGRADED_PENALTY,
    HAS_HISTORY_THRESHOLD,
    W_TASK_FIT, W_FAIL_TRANS, W_STRUCT_OUT, W_CAP_MATCH, W_REPUTATION, W_LOAD, W_EFFICIENCY,
    W_CS_CAP_MATCH, W_CS_REPUTATION, W_CS_LOAD, W_CS_SLA_SPEED,
)
from backend.app.discovery.relaxation import (
    RelaxationLevel,
    RelaxationPolicy,
)
from backend.app.discovery.messages import (
    CapabilityResponse,
    TaskDelegationMessage,
    TaskStandbyMessage,
    TaskResumeMessage,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _resp(
    agent_id: str = "agent_x",
    cap_match: float = 0.85,
    load: float = 0.2,
    reputation: float = 0.80,
    queue: int = 0,
) -> CapabilityResponse:
    return CapabilityResponse(
        agent_id=agent_id,
        endpoint=f"http://{agent_id}:8080",
        capability_match_score=cap_match,
        load=load,
        reputation_score=reputation,
        queue_depth=queue,
    )


def _metrics(
    tasks_total: int = 50,
    task_fit: float = 0.80,
    fail_trans: float = 0.75,
    struct_out: float = 0.90,
    tokens_per_task: float = 1500.0,
) -> dict:
    return {
        "tasks_total": tasks_total,
        "task_fit_avg_30d": task_fit,
        "failure_transparency_score": fail_trans,
        "structured_output_rate": struct_out,
        "efficiency_tokens_per_task": tokens_per_task,
        "tasks_success_30d": max(0, tasks_total - 5),
        "tasks_failed_30d": 5,
    }


# ── test_scoring_has_history ──────────────────────────────────────────────────


class TestScoringHasHistory:
    def test_mode_is_has_history_above_threshold(self):
        resp = _resp(cap_match=0.80)
        m = _metrics(tasks_total=HAS_HISTORY_THRESHOLD + 1)
        result = score_candidate(resp, DiscoveryQuery(), operational_metrics=m)
        assert result.mode == "has_history"

    def test_mode_is_cold_start_at_threshold(self):
        resp = _resp(cap_match=0.80)
        m = _metrics(tasks_total=HAS_HISTORY_THRESHOLD)
        result = score_candidate(resp, DiscoveryQuery(), operational_metrics=m)
        assert result.mode == "cold_start"

    def test_has_history_weights_sum_to_one(self):
        total = W_TASK_FIT + W_FAIL_TRANS + W_STRUCT_OUT + W_CAP_MATCH + W_REPUTATION + W_LOAD + W_EFFICIENCY
        assert total == pytest.approx(1.0)

    def test_has_history_formula_exact(self):
        cap_match = 0.80
        reputation = 0.75
        load = 0.20
        task_fit = 0.80
        fail_trans = 0.75
        struct_out = 0.90
        tokens_per_task = 2000.0   # efficiency_score = 1.0 at 2000 reference

        resp = _resp(cap_match=cap_match, load=load, reputation=reputation)
        m = _metrics(
            tasks_total=50,
            task_fit=task_fit,
            fail_trans=fail_trans,
            struct_out=struct_out,
            tokens_per_task=tokens_per_task,
        )
        result = score_candidate(resp, DiscoveryQuery(), operational_metrics=m)

        efficiency = _efficiency_score(m)
        expected = (
            task_fit    * W_TASK_FIT
            + fail_trans * W_FAIL_TRANS
            + struct_out * W_STRUCT_OUT
            + cap_match  * W_CAP_MATCH
            + reputation * W_REPUTATION
            + (1 - load) * W_LOAD
            + efficiency * W_EFFICIENCY
        )
        assert result.score == pytest.approx(expected, abs=1e-6)

    def test_has_history_score_bounded_0_1(self):
        resp = _resp(cap_match=1.0, load=0.0, reputation=1.0)
        m = _metrics(tasks_total=100, task_fit=1.0, fail_trans=1.0, struct_out=1.0)
        result = score_candidate(resp, DiscoveryQuery(), operational_metrics=m)
        assert 0.0 <= result.score <= 1.0

    def test_zero_metrics_gives_lower_score(self):
        resp = _resp(cap_match=0.80)
        m = _metrics(tasks_total=50, task_fit=0.0, fail_trans=0.0, struct_out=0.0)
        result = score_candidate(resp, DiscoveryQuery(), operational_metrics=m)
        # With zero task_fit/fail_trans/struct_out the score should be low
        assert result.score < 0.5


# ── test_scoring_cold_start ───────────────────────────────────────────────────


class TestScoringColdStart:
    def test_mode_is_cold_start_no_metrics(self):
        resp = _resp(cap_match=0.80)
        result = score_candidate(resp, DiscoveryQuery(), operational_metrics=None)
        assert result.mode == "cold_start"

    def test_mode_is_cold_start_low_task_count(self):
        resp = _resp(cap_match=0.80)
        m = _metrics(tasks_total=5)
        result = score_candidate(resp, DiscoveryQuery(), operational_metrics=m)
        assert result.mode == "cold_start"

    def test_cold_start_weights_sum_to_one(self):
        total = W_CS_CAP_MATCH + W_CS_REPUTATION + W_CS_LOAD + W_CS_SLA_SPEED
        assert total == pytest.approx(1.0)

    def test_cold_start_formula_exact(self):
        cap_match = 0.75
        reputation = 0.80
        load = 0.30
        sla_speed = 0.65

        resp = _resp(cap_match=cap_match, load=load, reputation=reputation)
        result = score_candidate(
            resp,
            DiscoveryQuery(),
            operational_metrics=None,
            sla_speed_score=sla_speed,
        )
        expected = (
            cap_match   * W_CS_CAP_MATCH
            + reputation * W_CS_REPUTATION
            + (1 - load) * W_CS_LOAD
            + sla_speed  * W_CS_SLA_SPEED
        )
        assert result.score == pytest.approx(expected, abs=1e-6)

    def test_cold_start_higher_cap_match_increases_score(self):
        base = score_candidate(
            _resp(cap_match=0.60),
            DiscoveryQuery(),
            operational_metrics=None,
        )
        better = score_candidate(
            _resp(cap_match=0.90),
            DiscoveryQuery(),
            operational_metrics=None,
        )
        assert better.score > base.score

    def test_cold_start_higher_load_decreases_score(self):
        low_load = score_candidate(
            _resp(load=0.1),
            DiscoveryQuery(),
            operational_metrics=None,
        )
        high_load = score_candidate(
            _resp(load=0.9),
            DiscoveryQuery(),
            operational_metrics=None,
        )
        assert low_load.score > high_load.score


# ── test_degraded_penalty ─────────────────────────────────────────────────────


class TestDegradedPenalty:
    def test_degraded_reduces_score_by_factor(self):
        resp = _resp(cap_match=0.80, reputation=0.80)
        active = score_candidate(
            resp,
            DiscoveryQuery(),
            operational_metrics=None,
            agent_state="ACTIVE",
        )
        degraded = score_candidate(
            resp,
            DiscoveryQuery(),
            operational_metrics=None,
            agent_state="DEGRADED",
        )
        assert degraded.score == pytest.approx(active.score * DEGRADED_PENALTY, abs=1e-6)

    def test_degraded_flag_set(self):
        resp = _resp()
        result = score_candidate(resp, DiscoveryQuery(), agent_state="DEGRADED")
        assert result.penalised is True

    def test_active_flag_not_set(self):
        resp = _resp()
        result = score_candidate(resp, DiscoveryQuery(), agent_state="ACTIVE")
        assert result.penalised is False

    def test_degraded_penalty_constant(self):
        assert DEGRADED_PENALTY == pytest.approx(0.70)

    def test_degraded_score_bounded_at_zero(self):
        resp = _resp(cap_match=0.0, reputation=0.0, load=1.0)
        result = score_candidate(resp, DiscoveryQuery(), agent_state="DEGRADED")
        assert result.score >= 0.0


# ── test_relaxation_levels ────────────────────────────────────────────────────


class TestRelaxationLevels:
    def test_initial_threshold_is_0_70(self):
        policy = RelaxationPolicy()
        assert policy.threshold == pytest.approx(0.70)

    def test_level_0_is_strict(self):
        policy = RelaxationPolicy()
        assert policy.level == RelaxationLevel.STRICT

    def test_escalate_once_gives_0_50(self):
        policy = RelaxationPolicy()
        policy.escalate()
        assert policy.threshold == pytest.approx(0.50)

    def test_escalate_twice_gives_0_00(self):
        policy = RelaxationPolicy()
        policy.escalate()
        policy.escalate()
        assert policy.threshold == pytest.approx(0.00)

    def test_escalate_at_max_returns_false(self):
        policy = RelaxationPolicy()
        policy.escalate()
        policy.escalate()
        result = policy.escalate()
        assert result is False
        assert policy.is_at_maximum is True

    def test_escalate_level_sequence(self):
        policy = RelaxationPolicy()
        assert policy.level == RelaxationLevel.STRICT
        policy.escalate()
        assert policy.level == RelaxationLevel.RELAXED
        policy.escalate()
        assert policy.level == RelaxationLevel.BROAD

    def test_ttl_multiplier_increases_with_level(self):
        policy = RelaxationPolicy()
        m0 = policy.ttl_multiplier
        policy.escalate()
        m1 = policy.ttl_multiplier
        policy.escalate()
        m2 = policy.ttl_multiplier
        assert m1 > m0
        assert m2 > m1

    def test_filter_candidates_below_threshold_removed(self):
        policy = RelaxationPolicy()
        candidates = [
            _resp("a1", cap_match=0.80),
            _resp("a2", cap_match=0.65),   # below 0.70
            _resp("a3", cap_match=0.71),
        ]
        filtered = policy.filter_candidates(candidates)
        ids = [c.agent_id for c in filtered]
        assert "a2" not in ids
        assert "a1" in ids
        assert "a3" in ids

    def test_after_relaxation_lower_threshold_accepted(self):
        policy = RelaxationPolicy()
        policy.escalate()   # threshold → 0.50
        candidates = [_resp("low", cap_match=0.55)]
        filtered = policy.filter_candidates(candidates)
        assert len(filtered) == 1


# ── test_dedup_by_agent_id ────────────────────────────────────────────────────


class TestDedupByAgentId:
    def test_dedup_removes_duplicate_ids(self):
        responses = [
            _resp("agent_a", cap_match=0.70, load=0.2),
            _resp("agent_a", cap_match=0.90, load=0.1),  # same id, higher score
            _resp("agent_b", cap_match=0.80),
        ]
        deduped = ParallelDiscovery._dedup(responses)
        ids = [r.agent_id for r in deduped]
        assert ids.count("agent_a") == 1
        assert len(deduped) == 2

    def test_dedup_keeps_highest_cap_match(self):
        responses = [
            _resp("agent_a", cap_match=0.60),
            _resp("agent_a", cap_match=0.85),
        ]
        deduped = ParallelDiscovery._dedup(responses)
        assert deduped[0].capability_match_score == pytest.approx(0.85)

    def test_dedup_empty_returns_empty(self):
        assert ParallelDiscovery._dedup([]) == []

    def test_dedup_no_duplicates_unchanged(self):
        responses = [_resp("a1"), _resp("a2"), _resp("a3")]
        deduped = ParallelDiscovery._dedup(responses)
        assert len(deduped) == 3

    def test_dedup_from_multiple_layers(self):
        # Simulate same agent appearing in local + registry layers
        local_r = _resp("shared", cap_match=0.75)
        local_r.source_layer = "local"
        registry_r = _resp("shared", cap_match=0.82)
        registry_r.source_layer = "registry"
        other = _resp("unique", cap_match=0.90)
        other.source_layer = "gossip"

        deduped = ParallelDiscovery._dedup([local_r, registry_r, other])
        assert len(deduped) == 2
        shared = next(r for r in deduped if r.agent_id == "shared")
        assert shared.capability_match_score == pytest.approx(0.82)


# ── test_exclude_list ─────────────────────────────────────────────────────────


class TestExcludeList:
    def test_excluded_agent_not_selected(self):
        policy = RelaxationPolicy(exclude_list=["bad_agent"])
        candidates = [_resp("bad_agent", cap_match=0.90), _resp("good_agent", cap_match=0.75)]
        filtered = policy.filter_candidates(candidates)
        ids = [c.agent_id for c in filtered]
        assert "bad_agent" not in ids
        assert "good_agent" in ids

    def test_add_excluded_after_init(self):
        policy = RelaxationPolicy()
        policy.add_excluded("late_exclusion")
        assert policy.is_excluded("late_exclusion")

    def test_exclude_persists_across_escalation(self):
        policy = RelaxationPolicy(exclude_list=["excluded"])
        policy.escalate()
        assert policy.is_excluded("excluded")

    def test_empty_exclude_list_passes_all(self):
        policy = RelaxationPolicy()
        candidates = [_resp("a1"), _resp("a2")]
        filtered = policy.filter_candidates(candidates)
        assert len(filtered) == 2


# ── test_parallel_discovery ───────────────────────────────────────────────────


class TestParallelDiscovery:
    def _make_discovery(self, responses_by_layer: dict) -> ParallelDiscovery:
        """Build a ParallelDiscovery with stub layers returning fixed responses."""
        def make_layer(layer_name):
            def layer_fn(query):
                return list(responses_by_layer.get(layer_name, []))
            return layer_fn

        return ParallelDiscovery(
            local_layer=make_layer("local"),
            gossip_layer=make_layer("gossip"),
            registry_layer=make_layer("registry"),
            metrics_provider=lambda aid: {},
            state_provider=lambda aid: "ACTIVE",
            sla_provider=lambda aid: {"speed_score": 0.5},
        )

    def test_returns_primary_from_single_layer(self):
        disc = self._make_discovery({
            "registry": [_resp("agent_best", cap_match=0.90)],
        })
        result = disc.discover(DiscoveryQuery())
        assert result.primary is not None
        assert result.primary.response.agent_id == "agent_best"

    def test_primary_is_highest_scored(self):
        disc = self._make_discovery({
            "local":    [_resp("a1", cap_match=0.75, reputation=0.70)],
            "registry": [_resp("a2", cap_match=0.95, reputation=0.90)],
        })
        result = disc.discover(DiscoveryQuery())
        assert result.primary.response.agent_id == "a2"

    def test_backups_are_next_best(self):
        disc = self._make_discovery({
            "registry": [
                _resp("best",   cap_match=0.95),
                _resp("second", cap_match=0.85),
                _resp("third",  cap_match=0.75),
            ],
        })
        result = disc.discover(DiscoveryQuery())
        backup_ids = [b.response.agent_id for b in result.backups]
        assert "second" in backup_ids or "third" in backup_ids

    def test_below_threshold_agent_excluded(self):
        disc = self._make_discovery({
            "local": [_resp("low", cap_match=0.60)],  # < 0.70
        })
        result = disc.discover(DiscoveryQuery())
        # No valid candidates → escalation → eventually self_execute or relaxed
        # At minimum, "low" should not be primary if threshold is still 0.70
        # The discovery will escalate; after level 2 it may accept low
        # Just verify no crash and primary.cap_match reflects what was found
        if result.primary is not None:
            # If returned, it was after relaxation
            assert result.relaxation_level > RelaxationLevel.STRICT

    def test_self_execute_when_no_candidates(self):
        disc = self._make_discovery({})   # all layers return nothing
        result = disc.discover(DiscoveryQuery())
        assert result.self_execute is True
        assert result.primary is None

    def test_dedup_across_layers(self):
        """Same agent appearing in multiple layers is deduped."""
        disc = self._make_discovery({
            "local":    [_resp("shared", cap_match=0.75)],
            "gossip":   [_resp("shared", cap_match=0.80)],
            "registry": [_resp("shared", cap_match=0.85)],
        })
        result = disc.discover(DiscoveryQuery())
        # There should be only one candidate for "shared"
        shared_count = sum(
            1 for c in result.all_candidates if c.response.agent_id == "shared"
        )
        assert shared_count == 1
        assert result.primary.response.capability_match_score == pytest.approx(0.85)

    def test_relaxation_level_in_result(self):
        disc = self._make_discovery({
            "registry": [_resp("ok", cap_match=0.90)],
        })
        result = disc.discover(DiscoveryQuery())
        assert isinstance(result.relaxation_level, RelaxationLevel)


# ── test_failover ─────────────────────────────────────────────────────────────


class TestFailover:
    def _make_result(self) -> DiscoveryResult:
        primary = ScoredCandidate(
            response=_resp("primary_agent", cap_match=0.90),
            score=0.88,
            mode="cold_start",
        )
        backup1 = ScoredCandidate(
            response=_resp("backup_1", cap_match=0.80),
            score=0.75,
            mode="cold_start",
        )
        backup2 = ScoredCandidate(
            response=_resp("backup_2", cap_match=0.75),
            score=0.70,
            mode="cold_start",
        )
        return DiscoveryResult(
            primary=primary,
            backups=[backup1, backup2],
            relaxation_level=RelaxationLevel.STRICT,
            all_candidates=[primary, backup1, backup2],
        )

    def _make_discovery(self):
        return ParallelDiscovery(
            local_layer=lambda q: [],
            gossip_layer=lambda q: [],
            registry_layer=lambda q: [],
        )

    def test_failover_promotes_backup1(self):
        disc = self._make_discovery()
        result = self._make_result()
        resume = disc.handle_primary_failure(
            result,
            task_id="t1",
            sender_id="orchestrator",
            checkpoint_data={"step": 3},
            failed_agent_id="primary_agent",
            failure_reason="timeout",
        )
        assert resume is not None
        assert resume.resumed_agent_id == "backup_1"

    def test_failover_sets_failed_agent_id(self):
        disc = self._make_discovery()
        result = self._make_result()
        resume = disc.handle_primary_failure(
            result,
            task_id="t1",
            sender_id="orch",
            checkpoint_data={},
            failed_agent_id="primary_agent",
        )
        assert resume.failed_agent_id == "primary_agent"

    def test_failover_carries_checkpoint_data(self):
        disc = self._make_discovery()
        result = self._make_result()
        checkpoint = {"progress": 0.5, "partial_output": "abc"}
        resume = disc.handle_primary_failure(
            result,
            task_id="t1",
            sender_id="orch",
            checkpoint_data=checkpoint,
            failed_agent_id="primary_agent",
        )
        assert resume.checkpoint_data == checkpoint

    def test_failover_no_backup_returns_none(self):
        disc = self._make_discovery()
        result = DiscoveryResult(
            primary=ScoredCandidate(_resp("p"), 0.9, "cold_start"),
            backups=[],
            relaxation_level=RelaxationLevel.STRICT,
        )
        resume = disc.handle_primary_failure(
            result, "t1", "orch", {}, "p"
        )
        assert resume is None

    def test_failover_message_type(self):
        disc = self._make_discovery()
        result = self._make_result()
        resume = disc.handle_primary_failure(result, "t1", "orch", {}, "primary_agent")
        assert resume.message_type == "TASK_RESUME"


# ── test_messages ─────────────────────────────────────────────────────────────


class TestMessageTypes:
    def test_delegation_message_type(self):
        msg = TaskDelegationMessage(
            task_id="t1",
            sender_id="s1",
            primary_agent_id="p1",
            task_type="analysis",
            payload={"input": "data"},
            sla_deadline_ms=9999999,
        )
        assert msg.message_type == "TASK_DELEGATION"

    def test_standby_message_type(self):
        msg = TaskStandbyMessage(
            task_id="t1",
            sender_id="s1",
            backup_agent_id="b1",
            task_type="analysis",
            task_summary="standby for analysis",
            sla_deadline_ms=9999999,
        )
        assert msg.message_type == "TASK_STANDBY"

    def test_resume_message_type(self):
        msg = TaskResumeMessage(
            task_id="t1",
            sender_id="s1",
            resumed_agent_id="b1",
            failed_agent_id="p1",
            checkpoint_data={"step": 2},
        )
        assert msg.message_type == "TASK_RESUME"

    def test_delegation_to_dict_contains_payload(self):
        msg = TaskDelegationMessage(
            task_id="t1",
            sender_id="s1",
            primary_agent_id="p1",
            task_type="coding",
            payload={"code": "print('hi')"},
            sla_deadline_ms=9999999,
        )
        d = msg.to_dict()
        assert d["payload"] == {"code": "print('hi')"}
        assert d["message_type"] == "TASK_DELEGATION"

    def test_standby_has_no_payload(self):
        msg = TaskStandbyMessage(
            task_id="t1",
            sender_id="s1",
            backup_agent_id="b1",
            task_type="coding",
            task_summary="pre-warm for coding",
            sla_deadline_ms=9999999,
            standby_rank=2,
        )
        d = msg.to_dict()
        assert "payload" not in d
        assert d["standby_rank"] == 2

    def test_build_delegation_messages(self):
        """ParallelDiscovery.build_delegation_messages produces correct output."""
        disc = ParallelDiscovery(
            local_layer=lambda q: [],
            gossip_layer=lambda q: [],
            registry_layer=lambda q: [],
        )
        primary = ScoredCandidate(_resp("primary"), 0.9, "cold_start")
        backup1 = ScoredCandidate(_resp("backup1"), 0.8, "cold_start")
        result = DiscoveryResult(
            primary=primary,
            backups=[backup1],
            relaxation_level=RelaxationLevel.STRICT,
        )
        deleg, standbys = disc.build_delegation_messages(
            result=result,
            task_id="task_42",
            sender_id="orch",
            task_type="analysis",
            payload={"q": "hello"},
            sla_deadline_ms=9999999,
        )
        assert deleg is not None
        assert deleg.primary_agent_id == "primary"
        assert len(standbys) == 1
        assert standbys[0].backup_agent_id == "backup1"
        assert standbys[0].standby_rank == 1


# ── test_capability_vector_match ──────────────────────────────────────────────


class TestCapabilityVectorMatch:
    def test_perfect_match_returns_one(self):
        required = {"coding": 0.8, "reasoning": 0.7}
        agent = {"coding": 0.9, "reasoning": 0.9}
        score = _capability_vector_match(required, agent)
        assert score == pytest.approx(1.0)

    def test_missing_skill_reduces_score(self):
        required = {"coding": 0.8, "missing_skill": 0.5}
        agent = {"coding": 0.9}
        score = _capability_vector_match(required, agent)
        assert score < 1.0

    def test_empty_required_returns_zero(self):
        agent = {"coding": 0.9}
        assert _capability_vector_match({}, agent) == pytest.approx(0.0)

    def test_partial_skill_partial_score(self):
        required = {"coding": 1.0}
        agent = {"coding": 0.5}
        score = _capability_vector_match(required, agent)
        assert score == pytest.approx(0.5)

    def test_score_capped_at_one_for_oversupplied_skill(self):
        required = {"coding": 0.5}
        agent = {"coding": 1.0}   # exceeds requirement
        score = _capability_vector_match(required, agent)
        assert score == pytest.approx(1.0)
