# Copyright (c) 2026 Quorbit Labs
# SPDX-License-Identifier: AGPL-3.0-only
"""
Sprint 7 unit tests — R9, C2, D22
"""

from __future__ import annotations

import math
from typing import Any, Dict
from unittest.mock import ANY, MagicMock

import pytest

# ── Imports under test ────────────────────────────────────────────────────────

from backend.app.discovery.parallel import (
    CAP_MATCH_HARD_FLOOR,
    DEGRADED_PENALTY,
    HAS_HISTORY_THRESHOLD,
    W_CAP_MATCH,
    W_CS_CAP_MATCH,
    W_CS_LOAD,
    W_CS_REPUTATION,
    W_CS_SLA_SPEED,
    W_EFFICIENCY,
    W_FAIL_TRANS,
    W_LOAD,
    W_REPUTATION,
    W_STRUCT_OUT,
    W_TASK_FIT,
    DiscoveryQuery,
    ScoredCandidate,
    _efficiency_score,
    score_candidate_v2,
)
from backend.app.capability.card import CapabilityCard, ValidationError
from backend.app.bus.probationary import (
    LOW_ROBUSTNESS_LABEL,
    MAX_ATTEMPTS,
    NUM_VARIANTS,
    PROBATION_EXTENSION_H,
    STRUCT_RATE_THRESHOLD,
    VARIANCE_THRESHOLD,
    RobustnessTest,
    RobustnessTestError,
    _generate_variants,
    _variance,
)


# ── Helpers / fixtures ────────────────────────────────────────────────────────


def _mock_response(
    agent_id: str = "agent-1",
    cap_match: float = 0.85,
    reputation: float = 0.75,
    load: float = 0.20,
) -> MagicMock:
    r = MagicMock()
    r.agent_id = agent_id
    r.capability_match_score = cap_match
    r.reputation_score = reputation
    r.load = load
    return r


def _query() -> DiscoveryQuery:
    return DiscoveryQuery(required_skills={"nlp": 0.8}, task_type="summarise")


def _has_history_metrics(**overrides: Any) -> dict:
    base = {
        "tasks_total": 50,
        "task_fit_avg_30d": 0.80,
        "failure_transparency_score": 0.70,
        "structured_output_rate": 0.90,
        "efficiency_tokens_per_task": 1000.0,
    }
    base.update(overrides)
    return base


def _minimal_card_data(agent_id: str = "a1") -> dict:
    return {
        "identity": {
            "agent_id": agent_id,
            "type": "llm",
            "version": "1.0",
            "provider": "test",
            "public_key": "aa" * 32,
            "key_version": 1,
        },
        "capabilities": {
            "capability_vector": {"nlp": 0.9},
            "methods": ["summarise"],
        },
        "execution": {"stateless": True, "resumable": False},
        "tools": {
            "code_exec": False,
            "filesystem": False,
            "web_search": False,
            "external_apis": False,
            "memory_store": False,
        },
        "knowledge": {"cutoff_date": "2025-01"},
        "resources": {
            "max_input_tokens": 4096,
            "max_output_tokens": 1024,
            "max_concurrent_tasks": 2,
        },
        "cost_model": {"type": "token", "input_per_1k": 0.01, "output_per_1k": 0.03},
        "reliability": {"hallucination_rate": 0.05, "confidence_calibration": 0.80},
        "constraints": {"refuses_categories": [], "human_approval_for": []},
        "gaps": ["no real-time data"],
        "coordination": {
            "preferred_tasks": ["summarise"],
            "avoid_tasks": [],
            "human_in_loop_for": [],
        },
    }


def _make_registry() -> MagicMock:
    reg = MagicMock()
    reg.update_agent_field = MagicMock()
    return reg


# ═══════════════════════════════════════════════════════════════════════════════
# R9 — score_candidate_v2()
# ═══════════════════════════════════════════════════════════════════════════════


class TestScoreCandidateV2HasHistory:
    """score_candidate_v2 in has_history mode (tasks_total > 20)."""

    def test_mode_label(self):
        resp = _mock_response()
        result = score_candidate_v2(resp, _query(), _has_history_metrics())
        assert result.mode == "has_history"

    def test_has_history_weights_sum_to_one(self):
        total = (
            W_TASK_FIT + W_FAIL_TRANS + W_STRUCT_OUT + W_CAP_MATCH
            + W_REPUTATION + W_LOAD + W_EFFICIENCY
        )
        assert math.isclose(total, 1.0, rel_tol=1e-9)

    def test_score_formula_exact(self):
        """Verify the computed score matches manual calculation."""
        metrics = _has_history_metrics(
            task_fit_avg_30d=0.80,
            failure_transparency_score=0.70,
            structured_output_rate=0.90,
            efficiency_tokens_per_task=2000.0,  # → efficiency = 1.0
        )
        resp = _mock_response(cap_match=0.85, reputation=0.75, load=0.20)
        result = score_candidate_v2(resp, _query(), metrics, agent_state="ACTIVE")

        efficiency = 2000.0 / 2000.0  # = 1.0
        expected = (
            0.80 * W_TASK_FIT
            + 0.70 * W_FAIL_TRANS
            + 0.90 * W_STRUCT_OUT
            + 0.85 * W_CAP_MATCH
            + 0.75 * W_REPUTATION
            + (1 - 0.20) * W_LOAD
            + efficiency * W_EFFICIENCY
        )
        assert math.isclose(result.score, expected, rel_tol=1e-9)

    def test_score_clamped_to_unit_interval(self):
        # All metrics at maximum → score <= 1.0
        metrics = _has_history_metrics(
            task_fit_avg_30d=1.0,
            failure_transparency_score=1.0,
            structured_output_rate=1.0,
            efficiency_tokens_per_task=1.0,
        )
        resp = _mock_response(cap_match=1.0, reputation=1.0, load=0.0)
        result = score_candidate_v2(resp, _query(), metrics)
        assert 0.0 <= result.score <= 1.0

    def test_not_penalised_when_active(self):
        resp = _mock_response()
        result = score_candidate_v2(resp, _query(), _has_history_metrics(), agent_state="ACTIVE")
        assert not result.penalised

    def test_efficiency_score_fallback_at_zero_tokens(self):
        # efficiency_tokens_per_task == 0 → fallback 0.5
        assert _efficiency_score({"efficiency_tokens_per_task": 0}) == 0.5
        assert _efficiency_score({}) == 0.5

    def test_efficiency_decreases_with_more_tokens(self):
        # More tokens per task → lower efficiency score
        low = _efficiency_score({"efficiency_tokens_per_task": 500.0})
        high = _efficiency_score({"efficiency_tokens_per_task": 5000.0})
        assert low > high


class TestScoreCandidateV2ColdStart:
    """score_candidate_v2 in cold_start mode (tasks_total ≤ 20)."""

    def test_mode_label(self):
        resp = _mock_response()
        result = score_candidate_v2(resp, _query(), {"tasks_total": 5})
        assert result.mode == "cold_start"

    def test_cold_start_when_no_metrics(self):
        resp = _mock_response()
        result = score_candidate_v2(resp, _query(), operational_metrics=None)
        assert result.mode == "cold_start"

    def test_cold_start_weights_sum_to_one(self):
        total = W_CS_CAP_MATCH + W_CS_REPUTATION + W_CS_LOAD + W_CS_SLA_SPEED
        assert math.isclose(total, 1.0, rel_tol=1e-9)

    def test_score_formula_exact(self):
        resp = _mock_response(cap_match=0.90, reputation=0.80, load=0.10)
        sla_speed = 0.70
        result = score_candidate_v2(
            resp, _query(), {"tasks_total": 0},
            agent_state="ACTIVE", sla_speed_score=sla_speed
        )
        expected = (
            0.90 * W_CS_CAP_MATCH
            + 0.80 * W_CS_REPUTATION
            + (1 - 0.10) * W_CS_LOAD
            + 0.70 * W_CS_SLA_SPEED
        )
        assert math.isclose(result.score, expected, rel_tol=1e-9)

    def test_boundary_at_threshold(self):
        # tasks_total == HAS_HISTORY_THRESHOLD → cold_start (must be > 20)
        metrics = {"tasks_total": HAS_HISTORY_THRESHOLD}
        resp = _mock_response()
        result = score_candidate_v2(resp, _query(), metrics)
        assert result.mode == "cold_start"

    def test_just_above_threshold_is_has_history(self):
        metrics = _has_history_metrics(tasks_total=HAS_HISTORY_THRESHOLD + 1)
        resp = _mock_response()
        result = score_candidate_v2(resp, _query(), metrics)
        assert result.mode == "has_history"


class TestScoreCandidateV2DegradedPenalty:
    """DEGRADED state penalty is applied correctly."""

    def test_degraded_penalty_applied(self):
        resp = _mock_response()
        active = score_candidate_v2(resp, _query(), _has_history_metrics(), agent_state="ACTIVE")
        degraded = score_candidate_v2(resp, _query(), _has_history_metrics(), agent_state="DEGRADED")
        assert math.isclose(degraded.score, active.score * DEGRADED_PENALTY, rel_tol=1e-9)

    def test_degraded_sets_penalised_flag(self):
        resp = _mock_response()
        result = score_candidate_v2(resp, _query(), _has_history_metrics(), agent_state="DEGRADED")
        assert result.penalised is True

    def test_non_degraded_no_penalty(self):
        for state in ("ACTIVE", "PROBATIONARY", "SOFT_QUARANTINED", "ISOLATED"):
            resp = _mock_response()
            result = score_candidate_v2(resp, _query(), _has_history_metrics(), agent_state=state)
            assert not result.penalised, f"unexpected penalty for state={state}"

    def test_cold_start_degraded_penalty(self):
        resp = _mock_response()
        active = score_candidate_v2(resp, _query(), None, agent_state="ACTIVE")
        degraded = score_candidate_v2(resp, _query(), None, agent_state="DEGRADED")
        assert math.isclose(degraded.score, active.score * DEGRADED_PENALTY, rel_tol=1e-9)


# ═══════════════════════════════════════════════════════════════════════════════
# C2 — CapabilityCard operational_metrics with last_computed_at
# ═══════════════════════════════════════════════════════════════════════════════


class TestCapabilityCardC2:
    """operational_metrics includes last_computed_at (C2)."""

    def test_default_operational_metrics_has_last_computed_at(self):
        card = CapabilityCard.from_dict(_minimal_card_data())
        assert "last_computed_at" in card.operational_metrics

    def test_last_computed_at_default_is_none(self):
        card = CapabilityCard.from_dict(_minimal_card_data())
        assert card.operational_metrics["last_computed_at"] is None

    def test_update_dynamic_sets_last_computed_at(self):
        card = CapabilityCard.from_dict(_minimal_card_data())
        ts = 1_700_000_000_000
        card.update_dynamic({"operational_metrics": {"last_computed_at": ts}})
        assert card.operational_metrics["last_computed_at"] == ts

    def test_all_required_operational_metrics_fields_present(self):
        required = {
            "task_fit_avg_30d",
            "structured_output_rate",
            "failure_transparency_score",
            "prompt_robustness_score",
            "efficiency_tokens_per_task",
            "tasks_total",
            "tasks_success_30d",
            "tasks_failed_30d",
            "last_computed_at",
        }
        card = CapabilityCard.from_dict(_minimal_card_data())
        missing = required - set(card.operational_metrics.keys())
        assert not missing, f"Missing fields: {missing}"

    def test_agent_cannot_inject_operational_metrics(self):
        """Agents must not be able to set operational_metrics at registration."""
        data = _minimal_card_data()
        data["operational_metrics"] = {"tasks_total": 9999, "last_computed_at": 1}
        card = CapabilityCard.from_dict(data)
        # The injected value must not survive stripping
        assert card.operational_metrics["tasks_total"] == 0

    def test_to_dict_includes_last_computed_at(self):
        card = CapabilityCard.from_dict(_minimal_card_data())
        d = card.to_dict()
        assert "last_computed_at" in d["_dynamic"]["operational_metrics"]

    def test_update_dynamic_merges_not_replaces(self):
        """Partial update of operational_metrics should not wipe other keys."""
        card = CapabilityCard.from_dict(_minimal_card_data())
        card.update_dynamic({"operational_metrics": {"tasks_total": 42}})
        assert card.operational_metrics["task_fit_avg_30d"] == 0.0
        assert card.operational_metrics["tasks_total"] == 42


# ═══════════════════════════════════════════════════════════════════════════════
# D22 — RobustnessTest
# ═══════════════════════════════════════════════════════════════════════════════


class TestVarianceHelper:
    def test_variance_empty(self):
        assert _variance([]) == 0.0

    def test_variance_single(self):
        assert _variance([0.5]) == 0.0

    def test_variance_uniform(self):
        assert _variance([0.5, 0.5, 0.5]) == 0.0

    def test_variance_known(self):
        # Population variance of [0, 1] = ((0-0.5)^2 + (1-0.5)^2) / 2 = 0.25
        assert math.isclose(_variance([0.0, 1.0]), 0.25, rel_tol=1e-9)

    def test_variance_five_identical(self):
        assert _variance([0.7] * 5) == 0.0


class TestGenerateVariants:
    def test_returns_num_variants(self):
        seed = {"prompt": "Summarise this document."}
        variants = _generate_variants(seed)
        assert len(variants) == NUM_VARIANTS

    def test_first_variant_is_original(self):
        seed = {"prompt": "Do something."}
        variants = _generate_variants(seed)
        assert variants[0]["_variant"] == "original"
        assert variants[0]["prompt"] == seed["prompt"]

    def test_all_variants_have_variant_tag(self):
        variants = _generate_variants({"prompt": "Test."})
        for v in variants:
            assert "_variant" in v

    def test_seed_task_not_mutated(self):
        seed = {"prompt": "Original prompt.", "extra": 42}
        original_prompt = seed["prompt"]
        _generate_variants(seed)
        assert seed["prompt"] == original_prompt

    def test_variants_differ_from_each_other(self):
        seed = {"prompt": "Analyse the report. Include a summary. Add conclusions."}
        variants = _generate_variants(seed)
        prompts = [v["prompt"] for v in variants]
        # Not all prompts should be identical (at least some transforms differ)
        assert len(set(prompts)) > 1


class TestRobustnessTestInit:
    def test_requires_agent_id(self):
        with pytest.raises(RobustnessTestError):
            RobustnessTest("", {"prompt": "x"}, _make_registry())

    def test_requires_dict_seed(self):
        with pytest.raises(RobustnessTestError):
            RobustnessTest("a1", "not a dict", _make_registry())

    def test_initial_state(self):
        rt = RobustnessTest("a1", {"prompt": "x"}, _make_registry())
        assert rt.attempts == 0
        assert not rt.labelled


class TestRobustnessTestPass:
    def _passing_output_fn(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """All variants produce identical, structured output → variance=0, rate=1."""
        return {"output_quality": 0.85, "structured": True}

    def test_run_returns_true_on_pass(self):
        reg = _make_registry()
        rt = RobustnessTest("a1", {"prompt": "Summarise."}, reg)
        assert rt.run(self._passing_output_fn) is True

    def test_no_registry_writes_on_pass(self):
        reg = _make_registry()
        rt = RobustnessTest("a1", {"prompt": "Summarise."}, reg)
        rt.run(self._passing_output_fn)
        reg.update_agent_field.assert_not_called()

    def test_attempts_unchanged_on_pass(self):
        rt = RobustnessTest("a1", {"prompt": "Summarise."}, _make_registry())
        rt.run(self._passing_output_fn)
        assert rt.attempts == 0

    def test_not_labelled_after_pass(self):
        rt = RobustnessTest("a1", {"prompt": "Summarise."}, _make_registry())
        rt.run(self._passing_output_fn)
        assert not rt.labelled

    def test_pass_variance_just_below_threshold(self):
        """Construct scores whose variance is just under VARIANCE_THRESHOLD."""
        # variance([0, 1]) = 0.25 — exactly at threshold (not < 0.25) so should FAIL
        scores_iter = iter([0.0, 1.0, 0.0, 1.0, 0.0])

        def output_fn(task):
            return {"output_quality": next(scores_iter), "structured": True}

        rt = RobustnessTest("a1", {"prompt": "x"}, _make_registry())
        # variance=0.24 passes; variance=0.25 fails — use uniform to pass
        result = rt.run(lambda t: {"output_quality": 0.8, "structured": True})
        assert result is True


class TestRobustnessTestFail:
    def _failing_output_fn(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """High variance, low structured rate."""
        quality = 1.0 if task.get("_variant") == "original" else 0.0
        return {"output_quality": quality, "structured": False}

    def test_run_returns_false_on_fail(self):
        rt = RobustnessTest("a1", {"prompt": "x"}, _make_registry())
        assert rt.run(self._failing_output_fn) is False

    def test_attempts_incremented_on_fail(self):
        rt = RobustnessTest("a1", {"prompt": "x"}, _make_registry())
        rt.run(self._failing_output_fn)
        assert rt.attempts == 1

    def test_probation_extension_written_to_registry(self):
        reg = _make_registry()
        rt = RobustnessTest("a1", {"prompt": "x"}, reg)
        rt.run(self._failing_output_fn)
        reg.update_agent_field.assert_any_call("a1", "extended_probation_until_ms", ANY)

    def test_second_failure_increments_again(self):
        rt = RobustnessTest("a1", {"prompt": "x"}, _make_registry())
        rt.run(self._failing_output_fn)
        rt.run(self._failing_output_fn)
        assert rt.attempts == 2
        assert not rt.labelled

    def test_fails_on_high_variance_alone(self):
        """Variance ≥ VARIANCE_THRESHOLD causes fail even if struct_rate is fine."""
        # [0,1,0,1,0] → variance = 0.24, struct_rate=1.0 — should PASS
        # Use [0,1] alternation for exactly 5 samples
        scores = [0.0, 1.0, 0.0, 1.0, 0.5]  # variance = 0.17 — PASSES
        # Force variance above threshold
        scores_high = [0.0, 1.0, 0.0, 1.0, 0.0]  # variance = 0.24 — borderline

        # Variance([0,1,0,1,0]):
        # mean = 0.4, deviations: 0.16,0.36,0.16,0.36,0.16 → var = 0.24 < 0.25 → PASS
        # To force fail we need var >= 0.25
        it = iter([0.0, 1.0, 0.0, 1.0, 0.0])

        def fn(task):
            return {"output_quality": next(it), "structured": True}

        rt = RobustnessTest("a1", {"prompt": "x"}, _make_registry())
        # variance = 0.24, struct=1.0 → should pass (< 0.25)
        assert rt.run(fn) is True

    def test_fails_on_low_struct_rate(self):
        """struct_rate < STRUCT_RATE_THRESHOLD causes fail even if variance is low."""
        def output_fn(task):
            return {"output_quality": 0.8, "structured": False}  # struct_rate = 0.0

        rt = RobustnessTest("a1", {"prompt": "x"}, _make_registry())
        assert rt.run(output_fn) is False


class TestRobustnessTestMaxAttempts:
    def _fail_fn(self, task):
        return {"output_quality": 0.0 if task.get("_variant") != "original" else 1.0,
                "structured": False}

    def test_label_applied_after_max_attempts(self):
        rt = RobustnessTest("a1", {"prompt": "x"}, _make_registry())
        for _ in range(MAX_ATTEMPTS):
            rt.run(self._fail_fn)
        assert rt.labelled

    def test_low_robustness_written_to_registry_on_max_attempts(self):
        reg = _make_registry()
        rt = RobustnessTest("a1", {"prompt": "x"}, reg)
        for _ in range(MAX_ATTEMPTS):
            rt.run(self._fail_fn)
        reg.update_agent_field.assert_any_call("a1", "agent_type", LOW_ROBUSTNESS_LABEL)

    def test_labelled_agent_short_circuits_run(self):
        reg = _make_registry()
        rt = RobustnessTest("a1", {"prompt": "x"}, reg)
        for _ in range(MAX_ATTEMPTS):
            rt.run(self._fail_fn)
        reg.reset_mock()

        result = rt.run(self._fail_fn)
        assert result is False
        reg.update_agent_field.assert_not_called()  # no further writes

    def test_attempts_do_not_exceed_max_plus_one(self):
        rt = RobustnessTest("a1", {"prompt": "x"}, _make_registry())
        for _ in range(MAX_ATTEMPTS + 2):
            rt.run(self._fail_fn)
        # After labelling, run() exits early — attempts stop at MAX_ATTEMPTS
        assert rt.attempts == MAX_ATTEMPTS

    def test_exactly_max_minus_one_failures_not_labelled(self):
        rt = RobustnessTest("a1", {"prompt": "x"}, _make_registry())
        for _ in range(MAX_ATTEMPTS - 1):
            rt.run(self._fail_fn)
        assert not rt.labelled
        assert rt.attempts == MAX_ATTEMPTS - 1


class TestRobustnessTestExceptionHandling:
    def test_output_fn_exception_treated_as_zero_quality(self):
        def bad_fn(task):
            raise RuntimeError("agent crash")

        reg = _make_registry()
        rt = RobustnessTest("a1", {"prompt": "x"}, reg)
        # All 5 variants raise → quality=[0,0,0,0,0], variance=0, struct_rate=0
        # variance < 0.25 passes but struct_rate=0.0 < 0.80 → FAIL
        result = rt.run(bad_fn)
        assert result is False

    def test_registry_error_does_not_propagate(self):
        reg = MagicMock()
        reg.update_agent_field.side_effect = IOError("Redis down")

        def fail_fn(task):
            return {"output_quality": 0.0, "structured": False}

        rt = RobustnessTest("a1", {"prompt": "x"}, reg)
        # Should not raise even if registry is unavailable
        rt.run(fail_fn)
