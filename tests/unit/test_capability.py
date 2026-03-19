"""
Unit tests — QUORBIT CapabilityCard v2.0 (Sprint 5) — C1, C2

Coverage:
  - Valid card passes schema validation
  - Dynamic fields cannot be set by agent (stripped on from_dict)
  - capability_vector skill scores are in [0.0, 1.0]
  - Invalid card raises ValidationError
  - update_dynamic() correctly sets system fields
  - Dynamic-field injection attempt is silently stripped
  - Schema errors are precise (path-qualified)
"""

from __future__ import annotations

import copy

import pytest

from backend.app.capability.card import (
    CapabilityCard,
    ValidationError,
    _DYNAMIC_FIELDS,
    _DYNAMIC_DEFAULTS,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _valid_static() -> dict:
    """Minimal valid static payload for a CapabilityCard."""
    return {
        "identity": {
            "agent_id": "deadbeef" * 8,
            "type": "llm",
            "version": "1.0",
            "provider": "anthropic",
            "public_key": "abcd1234" * 8,
            "key_version": 1,
        },
        "capabilities": {
            "capability_vector": {
                "coding": 0.9,
                "reasoning": 0.85,
                "summarisation": 0.7,
            },
            "methods": ["text_completion", "structured_output"],
            "specializations": ["python", "security"],
            "input_formats": ["text", "json"],
            "output_formats": ["text", "json", "markdown"],
        },
        "execution": {
            "stateless": True,
            "session_persistence": False,
            "resumable": True,
            "context_overflow": "truncate",
            "determinism": "non_deterministic",
        },
        "tools": {
            "code_exec": False,
            "filesystem": False,
            "web_search": False,
            "external_apis": False,
            "memory_store": True,
        },
        "knowledge": {
            "cutoff_date": "2025-08",
            "real_time_access": False,
        },
        "resources": {
            "max_input_tokens": 200_000,
            "max_output_tokens": 8_192,
            "max_concurrent_tasks": 4,
            "max_task_duration_s": 300,
        },
        "cost_model": {
            "type": "per_token",
            "input_per_1k": 0.003,
            "output_per_1k": 0.015,
        },
        "reliability": {
            "hallucination_rate": 0.05,
            "confidence_calibration": 0.85,
            "self_verification": True,
        },
        "constraints": {
            "refuses_categories": ["violence", "illegal"],
            "human_approval_for": ["financial_transactions"],
        },
        "gaps": [
            "cannot access real-time data",
            "no persistent memory across sessions",
        ],
        "coordination": {
            "preferred_tasks": ["analysis", "summarisation"],
            "avoid_tasks": ["low_latency_streaming"],
            "delegation_style": "cooperative",
            "human_in_loop_for": ["irreversible_actions"],
        },
    }


# ── test_card_validation ──────────────────────────────────────────────────────


class TestCardValidation:
    def test_valid_card_does_not_raise(self):
        card = CapabilityCard.from_dict(_valid_static())
        card.validate()   # must not raise

    def test_from_dict_returns_capability_card(self):
        card = CapabilityCard.from_dict(_valid_static())
        assert isinstance(card, CapabilityCard)

    def test_agent_id_accessible(self):
        card = CapabilityCard.from_dict(_valid_static())
        assert card.agent_id == "deadbeef" * 8

    def test_capability_vector_accessible(self):
        card = CapabilityCard.from_dict(_valid_static())
        assert card.capability_vector["coding"] == pytest.approx(0.9)

    def test_state_defaults_to_probationary(self):
        card = CapabilityCard.from_dict(_valid_static())
        assert card.state == "PROBATIONARY"

    def test_reputation_defaults_to_0_75(self):
        card = CapabilityCard.from_dict(_valid_static())
        assert card.reputation_score == pytest.approx(0.75)

    def test_to_dict_contains_dynamic_section(self):
        card = CapabilityCard.from_dict(_valid_static())
        d = card.to_dict()
        assert "_dynamic" in d

    def test_to_dict_static_fields_preserved(self):
        card = CapabilityCard.from_dict(_valid_static())
        d = card.to_dict()
        assert d["identity"]["type"] == "llm"
        assert d["capabilities"]["capability_vector"]["coding"] == pytest.approx(0.9)

    def test_round_trip_static_fields(self):
        static = _valid_static()
        card = CapabilityCard.from_dict(static)
        d = card.to_dict()
        # Static identity must be fully preserved
        assert d["identity"] == static["identity"]


# ── test_dynamic_not_agent_reported ──────────────────────────────────────────


class TestDynamicNotAgentReported:
    def test_dynamic_fields_stripped_from_agent_input(self):
        """from_dict() must silently strip all dynamic fields."""
        data = _valid_static()
        data["state"] = "ACTIVE"                   # dynamic — must be stripped
        data["reputation_score"] = 0.99            # dynamic — must be stripped
        data["current_load"] = 0.95                # dynamic — must be stripped
        card = CapabilityCard.from_dict(data)
        # Dynamic section should still have defaults, not agent-injected values
        assert card.state == "PROBATIONARY"
        assert card.reputation_score == pytest.approx(0.75)
        assert card.current_load == pytest.approx(0.0)

    def test_all_dynamic_fields_stripped(self):
        data = _valid_static()
        for field in _DYNAMIC_FIELDS:
            data[field] = "injected"
        card = CapabilityCard.from_dict(data)
        d = card.to_dict()
        # The dynamic section must only contain system defaults
        assert d["_dynamic"]["state"] == "PROBATIONARY"

    def test_update_dynamic_sets_state(self):
        card = CapabilityCard.from_dict(_valid_static())
        card.update_dynamic({"state": "ACTIVE"})
        assert card.state == "ACTIVE"

    def test_update_dynamic_sets_load(self):
        card = CapabilityCard.from_dict(_valid_static())
        card.update_dynamic({"current_load": 0.6})
        assert card.current_load == pytest.approx(0.6)

    def test_update_dynamic_sets_reputation(self):
        card = CapabilityCard.from_dict(_valid_static())
        card.update_dynamic({"reputation_score": 0.88})
        assert card.reputation_score == pytest.approx(0.88)

    def test_update_dynamic_merges_operational_metrics(self):
        card = CapabilityCard.from_dict(_valid_static())
        card.update_dynamic({
            "operational_metrics": {
                "tasks_total": 42,
                "task_fit_avg_30d": 0.85,
            }
        })
        m = card.operational_metrics
        assert m["tasks_total"] == 42
        assert m["task_fit_avg_30d"] == pytest.approx(0.85)
        # Other metrics should still have defaults
        assert "structured_output_rate" in m

    def test_update_dynamic_unknown_key_raises(self):
        card = CapabilityCard.from_dict(_valid_static())
        with pytest.raises(ValidationError, match="Unknown dynamic fields"):
            card.update_dynamic({"hacked_field": "bad"})

    def test_update_dynamic_invalid_load_raises(self):
        card = CapabilityCard.from_dict(_valid_static())
        with pytest.raises(ValidationError):
            card.update_dynamic({"current_load": 1.5})   # > 1.0

    def test_update_dynamic_does_not_affect_static(self):
        card = CapabilityCard.from_dict(_valid_static())
        card.update_dynamic({"state": "DEGRADED"})
        d = card.to_dict()
        assert d["identity"]["type"] == "llm"   # static unchanged


# ── test_capability_vector_bounds ─────────────────────────────────────────────


class TestCapabilityVectorBounds:
    def test_skills_at_zero_accepted(self):
        data = _valid_static()
        data["capabilities"]["capability_vector"]["new_skill"] = 0.0
        card = CapabilityCard.from_dict(data)
        assert card.capability_vector["new_skill"] == pytest.approx(0.0)

    def test_skills_at_one_accepted(self):
        data = _valid_static()
        data["capabilities"]["capability_vector"]["new_skill"] = 1.0
        card = CapabilityCard.from_dict(data)
        assert card.capability_vector["new_skill"] == pytest.approx(1.0)

    def test_skill_above_one_raises(self):
        data = _valid_static()
        data["capabilities"]["capability_vector"]["too_high"] = 1.01
        with pytest.raises(ValidationError):
            CapabilityCard.from_dict(data)

    def test_skill_below_zero_raises(self):
        data = _valid_static()
        data["capabilities"]["capability_vector"]["negative"] = -0.01
        with pytest.raises(ValidationError):
            CapabilityCard.from_dict(data)

    def test_empty_capability_vector_accepted(self):
        data = _valid_static()
        data["capabilities"]["capability_vector"] = {}
        card = CapabilityCard.from_dict(data)
        assert card.capability_vector == {}

    def test_multiple_skills_all_in_bounds(self):
        data = _valid_static()
        vec = {f"skill_{i}": i / 10.0 for i in range(11)}   # 0.0 .. 1.0
        data["capabilities"]["capability_vector"] = vec
        card = CapabilityCard.from_dict(data)
        for k, v in card.capability_vector.items():
            assert 0.0 <= v <= 1.0


# ── test_schema_enforcement ───────────────────────────────────────────────────


class TestSchemaEnforcement:
    def test_missing_identity_raises(self):
        data = _valid_static()
        del data["identity"]
        with pytest.raises(ValidationError, match="identity"):
            CapabilityCard.from_dict(data)

    def test_missing_identity_agent_id_raises(self):
        data = _valid_static()
        del data["identity"]["agent_id"]
        with pytest.raises(ValidationError, match="agent_id"):
            CapabilityCard.from_dict(data)

    def test_missing_capabilities_raises(self):
        data = _valid_static()
        del data["capabilities"]
        with pytest.raises(ValidationError, match="capabilities"):
            CapabilityCard.from_dict(data)

    def test_non_dict_capability_vector_raises(self):
        data = _valid_static()
        data["capabilities"]["capability_vector"] = "not_a_dict"
        with pytest.raises(ValidationError):
            CapabilityCard.from_dict(data)

    def test_missing_methods_raises(self):
        data = _valid_static()
        del data["capabilities"]["methods"]
        with pytest.raises(ValidationError, match="methods"):
            CapabilityCard.from_dict(data)

    def test_methods_not_list_raises(self):
        data = _valid_static()
        data["capabilities"]["methods"] = "text_completion"
        with pytest.raises(ValidationError):
            CapabilityCard.from_dict(data)

    def test_negative_max_input_tokens_raises(self):
        data = _valid_static()
        data["resources"]["max_input_tokens"] = 0
        with pytest.raises(ValidationError):
            CapabilityCard.from_dict(data)

    def test_negative_cost_raises(self):
        data = _valid_static()
        data["cost_model"]["input_per_1k"] = -0.01
        with pytest.raises(ValidationError):
            CapabilityCard.from_dict(data)

    def test_hallucination_rate_above_one_raises(self):
        data = _valid_static()
        data["reliability"]["hallucination_rate"] = 1.5
        with pytest.raises(ValidationError):
            CapabilityCard.from_dict(data)

    def test_gaps_not_list_raises(self):
        data = _valid_static()
        data["gaps"] = "not_a_list"
        with pytest.raises(ValidationError, match="gaps"):
            CapabilityCard.from_dict(data)

    def test_gap_entry_not_string_raises(self):
        data = _valid_static()
        data["gaps"] = [123, "valid"]
        with pytest.raises(ValidationError):
            CapabilityCard.from_dict(data)

    def test_missing_cost_model_raises(self):
        data = _valid_static()
        del data["cost_model"]
        with pytest.raises(ValidationError, match="cost_model"):
            CapabilityCard.from_dict(data)

    def test_key_version_must_be_int(self):
        data = _valid_static()
        data["identity"]["key_version"] = "1"   # string, not int
        with pytest.raises(ValidationError):
            CapabilityCard.from_dict(data)

    def test_refuses_categories_not_list_raises(self):
        data = _valid_static()
        data["constraints"]["refuses_categories"] = "violence"
        with pytest.raises(ValidationError):
            CapabilityCard.from_dict(data)
