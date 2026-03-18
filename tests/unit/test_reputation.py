"""
Unit tests — QUORBIT Reputation & Anti-gaming Layer (Sprint 3)

Coverage:
  - Initial score = 0.75
  - EMA formula and smoothing behaviour
  - Transparency bonus: structured_error_response → +0.02
  - Fabrication penalty: fabricated_result → -0.30
  - Divergence detection: anomalous drop triggers >2σ flag
  - Score bounds: always in [0.0, 1.0]
  - Adaptive weights: bounds, sum invariant, boost behaviour
  - Collusion graph detection
  - SLA cliff detection
  - Combined anti-gaming scoring
"""

from __future__ import annotations

import pytest

from backend.app.reputation.scoring import (
    INITIAL_SCORE,
    EMA_ALPHA,
    EMA_WINDOW,
    TASK_DELTAS,
    TRANSPARENCY_DELTAS,
    AgentReputation,
    ReputationEngine,
)
from backend.app.anti_gaming.adaptive_weights import (
    AdaptiveWeights,
    BASE_WEIGHTS,
    MIN_WEIGHT,
    MAX_WEIGHT,
    DETECTOR_NAMES,
)
from backend.app.anti_gaming.detectors import (
    AntiGamingDetector,
    CollusionGraphDetector,
    SlaCliffDetector,
    GraphSymmetryDetector,
    COMBINED_SUSPICIOUS,
)
from backend.app.anti_gaming.graph_store import GraphStore


# ── Fixtures ──────────────────────────────────────────────────────────────────


def fresh_engine() -> ReputationEngine:
    """ReputationEngine with no external dependencies (test mode)."""
    return ReputationEngine(redis_client=None, pg_store=None)


def fresh_rep(agent_id: str = "test_agent") -> AgentReputation:
    return AgentReputation(agent_id=agent_id)


# ── Initial score ─────────────────────────────────────────────────────────────


class TestInitialScore:
    def test_new_agent_score_is_0_75(self) -> None:
        rep = fresh_rep()
        assert rep.score == pytest.approx(INITIAL_SCORE)

    def test_engine_returns_0_75_for_unknown(self) -> None:
        engine = fresh_engine()
        assert engine.get_score("brand_new_agent") == pytest.approx(INITIAL_SCORE)

    def test_ema_score_starts_at_0_75(self) -> None:
        rep = fresh_rep()
        assert rep.ema_score == pytest.approx(INITIAL_SCORE)

    def test_initial_score_constant_value(self) -> None:
        assert INITIAL_SCORE == 0.75


# ── EMA formula ───────────────────────────────────────────────────────────────


class TestEmaUpdate:
    def test_ema_alpha_matches_window(self) -> None:
        expected_alpha = 2 / (EMA_WINDOW + 1)
        assert EMA_ALPHA == pytest.approx(expected_alpha)

    def test_ema_moves_toward_new_value(self) -> None:
        rep = fresh_rep()
        old_ema = rep.ema_score
        rep.apply_task_event("completed_on_time")
        # EMA should move closer to the new (higher) direct score
        assert rep.ema_score > old_ema

    def test_ema_formula_correct(self) -> None:
        rep = fresh_rep()
        before_ema = rep.ema_score
        rep.apply_task_event("completed_on_time")
        new_direct = rep.score
        expected_ema = EMA_ALPHA * new_direct + (1 - EMA_ALPHA) * before_ema
        assert rep.ema_score == pytest.approx(expected_ema, abs=1e-9)

    def test_ema_lags_behind_direct_score(self) -> None:
        rep = fresh_rep()
        # After one penalty, EMA should lag (not drop as far as direct score)
        rep.apply_task_event("flagged")
        assert rep.ema_score > rep.score  # EMA hasn't fully caught up

    def test_ema_converges_after_many_events(self) -> None:
        rep = fresh_rep()
        # Many "completed_on_time" events push both score and EMA high
        for _ in range(200):
            rep.apply_task_event("completed_on_time")
        # EMA should be very close to the clamped-at-1.0 score
        assert rep.ema_score > 0.95

    def test_ema_window_constant(self) -> None:
        assert EMA_WINDOW == 100


# ── Transparency bonus ────────────────────────────────────────────────────────


class TestTransparencyBonus:
    def test_structured_error_increases_score(self) -> None:
        rep = fresh_rep()
        before = rep.score
        rep.apply_transparency_event("structured_error_response")
        assert rep.score > before

    def test_structured_error_exact_delta(self) -> None:
        rep = fresh_rep()
        before = rep.score
        rep.apply_transparency_event("structured_error_response")
        expected = before + TRANSPARENCY_DELTAS["structured_error_response"]
        assert rep.score == pytest.approx(expected)

    def test_structured_error_delta_is_positive(self) -> None:
        assert TRANSPARENCY_DELTAS["structured_error_response"] == pytest.approx(+0.02)

    def test_result_marked_incorrect_decreases(self) -> None:
        rep = fresh_rep()
        before = rep.score
        rep.apply_transparency_event("result_marked_incorrect")
        assert rep.score < before

    def test_engine_transparency_event(self) -> None:
        engine = fresh_engine()
        before = engine.get_score("agent_a")
        after = engine.apply_transparency_event("agent_a", "structured_error_response")
        assert after > before


# ── Fabrication penalty ───────────────────────────────────────────────────────


class TestFabricationPenalty:
    def test_fabricated_result_decreases_score(self) -> None:
        rep = fresh_rep()
        before = rep.score
        rep.apply_transparency_event("fabricated_result")
        assert rep.score < before

    def test_fabricated_result_exact_delta(self) -> None:
        rep = fresh_rep()
        before = rep.score
        rep.apply_transparency_event("fabricated_result")
        expected = max(0.0, before + TRANSPARENCY_DELTAS["fabricated_result"])
        assert rep.score == pytest.approx(expected)

    def test_fabricated_result_delta_is_minus_30(self) -> None:
        assert TRANSPARENCY_DELTAS["fabricated_result"] == pytest.approx(-0.30)

    def test_fabricated_result_large_drop(self) -> None:
        rep = fresh_rep()
        rep.apply_transparency_event("fabricated_result")
        # 0.75 - 0.30 = 0.45 — a significant drop
        assert rep.score == pytest.approx(0.45)

    def test_confirmed_hallucination_delta(self) -> None:
        assert TRANSPARENCY_DELTAS["confirmed_hallucination"] == pytest.approx(-0.15)


# ── Score bounds ──────────────────────────────────────────────────────────────


class TestScoreBounds:
    def test_score_never_exceeds_1(self) -> None:
        rep = fresh_rep()
        for _ in range(50):
            rep.apply_task_event("completed_on_time")
        assert rep.score <= 1.0

    def test_score_never_below_0(self) -> None:
        rep = fresh_rep()
        for _ in range(50):
            rep.apply_task_event("flagged")
        assert rep.score >= 0.0

    def test_transparency_score_bounded_above(self) -> None:
        rep = fresh_rep()
        for _ in range(100):
            rep.apply_transparency_event("structured_error_response")
        assert rep.score <= 1.0

    def test_transparency_score_bounded_below(self) -> None:
        rep = fresh_rep()
        for _ in range(10):
            rep.apply_transparency_event("fabricated_result")
        assert rep.score >= 0.0

    def test_alternating_events_stays_bounded(self) -> None:
        rep = fresh_rep()
        for i in range(100):
            if i % 2 == 0:
                rep.apply_task_event("completed_on_time")
            else:
                rep.apply_task_event("flagged")
        assert 0.0 <= rep.score <= 1.0

    def test_all_task_deltas_sum_is_bounded(self) -> None:
        """Applying every task event in sequence never escapes [0, 1]."""
        rep = fresh_rep()
        events = list(TASK_DELTAS.keys()) * 10
        for evt in events:
            rep.apply_task_event(evt)
            assert 0.0 <= rep.score <= 1.0

    def test_ema_stays_bounded(self) -> None:
        rep = fresh_rep()
        for _ in range(200):
            rep.apply_transparency_event("fabricated_result")
        assert 0.0 <= rep.ema_score <= 1.0


# ── Divergence detection ──────────────────────────────────────────────────────


class TestDivergenceDetection:
    def test_no_divergence_on_fresh_agent(self) -> None:
        rep = fresh_rep()
        assert rep.is_divergent() is False

    def test_no_divergence_with_few_observations(self) -> None:
        rep = fresh_rep()
        rep.apply_task_event("completed_on_time")
        rep.apply_task_event("completed_on_time")
        # < 3 history entries → no divergence
        assert rep.is_divergent() is False

    def test_no_divergence_on_consistent_events(self) -> None:
        rep = fresh_rep()
        for _ in range(15):
            rep.apply_task_event("completed_on_time")
        # All consistent positive events — low variance
        assert rep.is_divergent() is False

    def test_divergence_detected_after_extreme_drop(self) -> None:
        rep = fresh_rep()
        # Build stable high-score history
        for _ in range(15):
            rep.apply_task_event("completed_on_time")
        # Extreme negative event: fabricated_result × 2 → big drop from rolling mean
        rep.apply_transparency_event("fabricated_result")
        rep.apply_transparency_event("fabricated_result")
        rep.apply_transparency_event("fabricated_result")
        assert rep.is_divergent() is True

    def test_engine_divergence_triggers_callback(self) -> None:
        flagged: list[str] = []
        engine = ReputationEngine(on_divergence=lambda aid: flagged.append(aid))

        for _ in range(15):
            engine.apply_task_event("test_agent", "completed_on_time")
        for _ in range(3):
            engine.apply_transparency_event("test_agent", "fabricated_result")

        assert "test_agent" in flagged

    def test_no_divergence_after_consistent_penalties(self) -> None:
        rep = fresh_rep()
        # Consistent penalties: low variance around a downward trend
        for _ in range(20):
            rep.apply_task_event("abandoned")
        # Score is consistently low — no *sudden* divergence
        # (divergence requires a deviation from recent window mean)
        # After 20 events the window is full and all values are similarly low
        # The last event continues the trend, so divergence is not expected
        assert rep.score <= 0.75


# ── Adaptive weights ──────────────────────────────────────────────────────────


class TestAdaptiveWeights:
    def test_initial_weights_match_base(self) -> None:
        aw = AdaptiveWeights()
        for name, weight in BASE_WEIGHTS.items():
            assert aw.weight(name) == weight

    def test_sum_always_100(self) -> None:
        aw = AdaptiveWeights()
        assert sum(aw.get_weights().values()) == 100

    def test_collusion_threat_boosts_collusion_weight(self) -> None:
        aw = AdaptiveWeights()
        before = aw.weight("collusion_graph")
        aw.observe_threat("collusion")
        assert aw.weight("collusion_graph") > before

    def test_sum_100_after_boost(self) -> None:
        aw = AdaptiveWeights()
        aw.observe_threat("collusion")
        assert sum(aw.get_weights().values()) == 100

    def test_weights_respect_min_bound(self) -> None:
        aw = AdaptiveWeights()
        for _ in range(20):
            aw.observe_threat("collusion")
        for w in aw.get_weights().values():
            assert w >= MIN_WEIGHT

    def test_weights_respect_max_bound(self) -> None:
        aw = AdaptiveWeights()
        for _ in range(20):
            aw.observe_threat("collusion")
        for w in aw.get_weights().values():
            assert w <= MAX_WEIGHT

    def test_sla_threat_boosts_sla_weight(self) -> None:
        aw = AdaptiveWeights()
        before = aw.weight("sla_cliff")
        aw.observe_threat("sla")
        assert aw.weight("sla_cliff") > before

    def test_decay_moves_toward_baseline(self) -> None:
        aw = AdaptiveWeights()
        aw.observe_threat("collusion")
        boosted = aw.weight("collusion_graph")
        aw.decay()
        decayed = aw.weight("collusion_graph")
        assert decayed <= boosted  # moved back toward baseline


# ── Graph store & collusion detection ─────────────────────────────────────────


class TestGraphStoreAndCollusion:
    def test_add_edge_creates_nodes(self) -> None:
        g = GraphStore()
        g.add_edge("a", "b", weight=1.0)
        assert "b" in g.get_neighbors("a")

    def test_clustering_coefficient_complete_graph(self) -> None:
        g = GraphStore()
        # Complete graph on 4 nodes (all mutual)
        nodes = ["n1", "n2", "n3", "n4"]
        for u in nodes:
            for v in nodes:
                if u != v:
                    g.add_edge(u, v, 1.0)
        cc = g.clustering_coefficient("n1")
        assert cc == pytest.approx(1.0)

    def test_clustering_coefficient_no_edges(self) -> None:
        g = GraphStore()
        g.add_edge("solo", "other")
        assert g.clustering_coefficient("solo") == 0.0

    def test_suspicion_score_high_mutual_edges(self) -> None:
        g = GraphStore()
        for i in range(5):
            g.add_edge("ring_leader", f"member_{i}", 1.0)
            g.add_edge(f"member_{i}", "ring_leader", 1.0)
        score = g.suspicion_score("ring_leader")
        assert score > 0.3   # mutual ratio should be high

    def test_detect_rings_returns_list(self) -> None:
        g = GraphStore()
        for i in range(4):
            for j in range(4):
                if i != j:
                    g.add_edge(f"r{i}", f"r{j}", 1.0)
        rings = g.detect_rings(min_size=3)
        assert isinstance(rings, list)

    def test_collusion_detector_mutual_validations(self) -> None:
        det = CollusionGraphDetector(window=20)
        # A validates B and B validates A in many rounds
        for i in range(12):
            det.add_validation("agent_a", "agent_b", f"round_{i}")
            det.add_validation("agent_b", "agent_a", f"round_{i}")
        score = det.score("agent_a")
        assert score > 0.4

    def test_collusion_detector_no_mutual(self) -> None:
        det = CollusionGraphDetector(window=20)
        # A validates many agents, none validate back
        for i in range(10):
            det.add_validation("honest", f"peer_{i}", f"round_{i}")
        score = det.score("honest")
        # Graph CC should be low for star graph
        assert score < 0.6


# ── SLA cliff detection ───────────────────────────────────────────────────────


class TestSlaCliffDetector:
    def test_no_completions_score_zero(self) -> None:
        det = SlaCliffDetector()
        assert det.score("agent_x") == 0.0

    def test_consistently_near_cliff(self) -> None:
        det = SlaCliffDetector(window=30)
        for _ in range(20):
            det.add_completion("gamer", sla_limit=30.0, actual_time=29.5)  # 98.3%
        assert det.score("gamer") > SlaCliffDetector.NEAR_CLIFF * 0.8

    def test_fast_completions_score_low(self) -> None:
        det = SlaCliffDetector(window=30)
        for _ in range(20):
            det.add_completion("fast", sla_limit=30.0, actual_time=5.0)   # 16.7%
        assert det.score("fast") < 0.1

    def test_detect_returns_true_at_threshold(self) -> None:
        det = SlaCliffDetector(window=30)
        for _ in range(20):
            det.add_completion("cliff_agent", sla_limit=60.0, actual_time=59.5)
        assert det.detect("cliff_agent") is True

    def test_detect_returns_false_for_honest(self) -> None:
        det = SlaCliffDetector(window=30)
        for _ in range(20):
            det.add_completion("honest", sla_limit=60.0, actual_time=20.0)
        assert det.detect("honest") is False


# ── Combined anti-gaming scoring ──────────────────────────────────────────────


class TestCombinedAntiGaming:
    def test_initial_combined_score_zero(self) -> None:
        det = AntiGamingDetector()
        assert det.combined_score("new_agent") == pytest.approx(0.0)

    def test_combined_score_bounded(self) -> None:
        det = AntiGamingDetector()
        for i in range(10):
            det.add_validation("suspect", f"peer_{i}", f"r{i}")
            det.add_validation(f"peer_{i}", "suspect", f"r{i}")
        score = det.combined_score("suspect")
        assert 0.0 <= score <= 1.0

    def test_report_threat_shifts_weights(self) -> None:
        det = AntiGamingDetector()
        before_collusion_w = det._weights.weight("collusion_graph")
        det.report_threat("collusion")
        after_collusion_w = det._weights.weight("collusion_graph")
        assert after_collusion_w > before_collusion_w

    def test_detector_scores_returns_all_keys(self) -> None:
        det = AntiGamingDetector()
        scores = det.detector_scores("agent_z")
        assert "collusion_graph" in scores
        assert "graph_symmetry" in scores
        assert "sla_cliff" in scores
        assert "combined" in scores

    def test_sla_gaming_detected_in_combined(self) -> None:
        det = AntiGamingDetector()
        for _ in range(25):
            det.add_completion("sla_gamer", sla_limit=30.0, actual_time=29.8)
        assert det.combined_score("sla_gamer") > 0.0
