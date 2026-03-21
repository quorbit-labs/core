# Copyright (c) 2026 Quorbit Labs
# SPDX-License-Identifier: AGPL-3.0-only
"""
QUORBIT Protocol — CapabilityCard v2.0 (AGPL-3.0) — C1, C2

Two-section design:
  STATIC  — filled by the agent at registration; immutable between rotations.
  DYNAMIC — written exclusively by the QUORBIT system (registry / reputation
            engine); agents cannot set these fields.

Static sections
───────────────
  identity        agent_id, type, version, provider, public_key, key_version
  capabilities    capability_vector {skill→float}, methods, specializations,
                  input_formats, output_formats
  execution       stateless, session_persistence, resumable,
                  context_overflow, determinism
  tools           code_exec, filesystem, web_search, external_apis,
                  memory_store
  knowledge       cutoff_date, real_time_access
  resources       max_input_tokens, max_output_tokens, max_concurrent_tasks,
                  max_task_duration_s
  cost_model      type, input_per_1k, output_per_1k
  reliability     hallucination_rate, confidence_calibration,
                  self_verification
  constraints     refuses_categories, human_approval_for
  gaps            explicit honest list of limitations
  coordination    preferred_tasks, avoid_tasks, delegation_style,
                  human_in_loop_for

Dynamic section (system-set only) — C2
───────────────────────────────────────
  state                     AgentState string
  current_load              float [0–1]
  queue_depth               int ≥ 0
  reputation_score          float [0–1]
  trust_level               string
  sla_estimates             {speed_score, p50_s, p95_s}
  last_heartbeat_ms         int unix-ms
  tasks_completed_total     int
  tasks_completed_7d        int
  failure_transparency_score float [0–1]
  operational_metrics       {task_fit_avg_30d, structured_output_rate,
                              failure_transparency_score, prompt_robustness_score,
                              efficiency_tokens_per_task, tasks_total,
                              tasks_success_30d, tasks_failed_30d,
                              last_computed_at}
"""

from __future__ import annotations

import copy
import time
from typing import Any, Dict, List, Optional


# ── Exceptions ────────────────────────────────────────────────────────────────


class CapabilityCardError(Exception):
    """Base exception for CapabilityCard errors."""


class ValidationError(CapabilityCardError):
    """Raised when a CapabilityCard fails schema validation."""

    def __init__(self, message: str, path: str = "") -> None:
        self.path = path
        super().__init__(f"{path}: {message}" if path else message)


# ── Schema constants ──────────────────────────────────────────────────────────

CARD_VERSION = "2.0"

# Fields that agents are NOT allowed to set or overwrite
_DYNAMIC_FIELDS = frozenset({
    "state",
    "current_load",
    "queue_depth",
    "reputation_score",
    "trust_level",
    "sla_estimates",
    "last_heartbeat_ms",
    "tasks_completed_total",
    "tasks_completed_7d",
    "failure_transparency_score",
    "operational_metrics",
})

# Default dynamic section baseline
_DYNAMIC_DEFAULTS: Dict[str, Any] = {
    "state": "PROBATIONARY",
    "current_load": 0.0,
    "queue_depth": 0,
    "reputation_score": 0.75,
    "trust_level": "untrusted",
    "sla_estimates": {
        "speed_score": 0.5,
        "p50_s": None,
        "p95_s": None,
    },
    "last_heartbeat_ms": None,
    "tasks_completed_total": 0,
    "tasks_completed_7d": 0,
    "failure_transparency_score": 0.5,
    "operational_metrics": {
        "task_fit_avg_30d": 0.0,
        "structured_output_rate": 0.0,
        "failure_transparency_score": 0.5,
        "prompt_robustness_score": 0.0,
        "efficiency_tokens_per_task": 0.0,
        "tasks_total": 0,
        "tasks_success_30d": 0,
        "tasks_failed_30d": 0,
        "last_computed_at": None,
    },
}


# ── Lightweight schema validator ──────────────────────────────────────────────


def _require(d: dict, key: str, path: str, typ: type) -> Any:
    if key not in d:
        raise ValidationError(f"missing required field '{key}'", path)
    val = d[key]
    if not isinstance(val, typ):
        raise ValidationError(
            f"'{key}' must be {typ.__name__}, got {type(val).__name__}", path
        )
    return val


def _float_in_range(val: Any, key: str, path: str, lo: float = 0.0, hi: float = 1.0) -> None:
    if not isinstance(val, (int, float)):
        raise ValidationError(f"'{key}' must be numeric", path)
    if not (lo <= float(val) <= hi):
        raise ValidationError(f"'{key}' must be in [{lo}, {hi}], got {val}", path)


def _validate_static(data: dict) -> None:
    """
    Enforce the static section schema.

    Raises ValidationError on any constraint violation.
    """

    # ── identity ──────────────────────────────────────────────────────────
    identity = _require(data, "identity", "", dict)
    for field in ("agent_id", "type", "version", "provider", "public_key"):
        _require(identity, field, "identity", str)
    _require(identity, "key_version", "identity", int)

    # ── capabilities ──────────────────────────────────────────────────────
    caps = _require(data, "capabilities", "", dict)
    cap_vec = _require(caps, "capability_vector", "capabilities", dict)
    for skill, score in cap_vec.items():
        if not isinstance(skill, str):
            raise ValidationError("capability_vector keys must be strings", "capabilities")
        _float_in_range(score, skill, "capabilities.capability_vector")
    _require(caps, "methods", "capabilities", list)
    # optional: specializations, input_formats, output_formats

    # ── execution ─────────────────────────────────────────────────────────
    exe = _require(data, "execution", "", dict)
    for bool_field in ("stateless", "resumable"):
        if bool_field in exe and not isinstance(exe[bool_field], bool):
            raise ValidationError(f"'{bool_field}' must be bool", "execution")

    # ── tools ─────────────────────────────────────────────────────────────
    tools = _require(data, "tools", "", dict)
    for bool_field in ("code_exec", "filesystem", "web_search", "external_apis", "memory_store"):
        if bool_field in tools and not isinstance(tools[bool_field], bool):
            raise ValidationError(f"'{bool_field}' must be bool", "tools")

    # ── knowledge ─────────────────────────────────────────────────────────
    _require(data, "knowledge", "", dict)

    # ── resources ─────────────────────────────────────────────────────────
    res = _require(data, "resources", "", dict)
    for int_field in ("max_input_tokens", "max_output_tokens", "max_concurrent_tasks"):
        if int_field in res:
            if not isinstance(res[int_field], int) or res[int_field] < 1:
                raise ValidationError(f"'{int_field}' must be positive int", "resources")

    # ── cost_model ────────────────────────────────────────────────────────
    cost = _require(data, "cost_model", "", dict)
    _require(cost, "type", "cost_model", str)
    for num_field in ("input_per_1k", "output_per_1k"):
        if num_field in cost:
            if not isinstance(cost[num_field], (int, float)) or cost[num_field] < 0:
                raise ValidationError(f"'{num_field}' must be non-negative number", "cost_model")

    # ── reliability ───────────────────────────────────────────────────────
    rel = _require(data, "reliability", "", dict)
    for rate_field in ("hallucination_rate", "confidence_calibration"):
        if rate_field in rel:
            _float_in_range(rel[rate_field], rate_field, "reliability")

    # ── constraints ───────────────────────────────────────────────────────
    constr = _require(data, "constraints", "", dict)
    for list_field in ("refuses_categories", "human_approval_for"):
        if list_field in constr and not isinstance(constr[list_field], list):
            raise ValidationError(f"'{list_field}' must be a list", "constraints")

    # ── gaps ──────────────────────────────────────────────────────────────
    gaps = _require(data, "gaps", "", list)
    for item in gaps:
        if not isinstance(item, str):
            raise ValidationError("all gap entries must be strings", "gaps")

    # ── coordination ──────────────────────────────────────────────────────
    coord = _require(data, "coordination", "", dict)
    for list_field in ("preferred_tasks", "avoid_tasks", "human_in_loop_for"):
        if list_field in coord and not isinstance(coord[list_field], list):
            raise ValidationError(f"'{list_field}' must be a list", "coordination")


# ── CapabilityCard ────────────────────────────────────────────────────────────


class CapabilityCard:
    """
    Versioned capability advertisement for a QUORBIT agent.

    Construction
    ------------
    Use :meth:`from_dict` to build from a raw agent-submitted dict.
    The ``_dynamic`` section is always initialised to system defaults —
    agents cannot inject dynamic data through ``from_dict``.

    Mutating dynamic fields
    -----------------------
    Only the QUORBIT system should call :meth:`update_dynamic`.
    Attempting to set a dynamic field via ``from_dict`` or by directly
    passing it in the static payload raises ``ValidationError``.
    """

    def __init__(self, static: dict, dynamic: Optional[dict] = None) -> None:
        self._static: dict = copy.deepcopy(static)
        self._dynamic: dict = copy.deepcopy(_DYNAMIC_DEFAULTS)
        if dynamic:
            self._dynamic.update(copy.deepcopy(dynamic))

    # ── Construction ──────────────────────────────────────────────────────

    @classmethod
    def from_dict(cls, data: dict) -> "CapabilityCard":
        """
        Build a CapabilityCard from a raw agent-submitted dict.

        Strips any dynamic fields the agent may have tried to inject.
        Validates the static section.  Raises ValidationError on failure.
        """
        # Strip dynamic fields — agents MUST NOT set these
        static_data = {k: v for k, v in data.items() if k not in _DYNAMIC_FIELDS}

        card = cls(static=static_data)
        card.validate()
        return card

    # ── Validation ────────────────────────────────────────────────────────

    def validate(self) -> None:
        """
        Validate the static section against the CapabilityCard schema.

        Raises ValidationError on the first constraint violation.
        """
        _validate_static(self._static)

    # ── Dynamic update (system-only) ──────────────────────────────────────

    def update_dynamic(self, updates: dict) -> None:
        """
        Apply system-computed dynamic updates.

        Only keys present in _DYNAMIC_FIELDS are accepted.
        Unknown keys raise ValidationError (not silently ignored).
        """
        unknown = set(updates) - _DYNAMIC_FIELDS
        if unknown:
            raise ValidationError(
                f"Unknown dynamic fields: {sorted(unknown)}. "
                "Dynamic updates may only contain system-defined keys.",
                "_dynamic",
            )

        for key, value in updates.items():
            if key == "current_load":
                _float_in_range(value, "current_load", "_dynamic")
            elif key == "reputation_score":
                _float_in_range(value, "reputation_score", "_dynamic")
            elif key == "failure_transparency_score":
                _float_in_range(value, "failure_transparency_score", "_dynamic")
            elif key == "operational_metrics" and isinstance(value, dict):
                # Merge into existing operational_metrics
                self._dynamic["operational_metrics"].update(value)
                continue
            elif key == "sla_estimates" and isinstance(value, dict):
                self._dynamic["sla_estimates"].update(value)
                continue
            self._dynamic[key] = value

    # ── Serialisation ─────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Return the full card as a plain dict (static + _dynamic)."""
        result = copy.deepcopy(self._static)
        result["_dynamic"] = copy.deepcopy(self._dynamic)
        return result

    # ── Convenience accessors ─────────────────────────────────────────────

    @property
    def agent_id(self) -> str:
        return self._static["identity"]["agent_id"]

    @property
    def capability_vector(self) -> Dict[str, float]:
        return dict(self._static["capabilities"]["capability_vector"])

    @property
    def state(self) -> str:
        return self._dynamic["state"]

    @property
    def current_load(self) -> float:
        return float(self._dynamic["current_load"])

    @property
    def reputation_score(self) -> float:
        return float(self._dynamic["reputation_score"])

    @property
    def operational_metrics(self) -> dict:
        return dict(self._dynamic["operational_metrics"])

    @property
    def sla_estimates(self) -> dict:
        return dict(self._dynamic["sla_estimates"])

    @property
    def tasks_total(self) -> int:
        return int(self._dynamic["operational_metrics"]["tasks_total"])

    def __repr__(self) -> str:
        return (
            f"CapabilityCard(agent_id={self.agent_id!r}, "
            f"state={self.state!r}, "
            f"reputation={self.reputation_score:.3f})"
        )
