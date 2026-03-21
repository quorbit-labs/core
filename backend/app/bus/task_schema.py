"""
QUORBIT Protocol — Extended Task Schema (AGPL-3.0) — L2

TaskSchema is the validated, serialisable task specification used throughout
the delegation pipeline (discovery, assignment, checkpoint/resume).

Fields
──────
  task_id              UUID4 string — globally unique task identifier
  type                 Task type string (e.g. "analysis", "summarisation")
  priority             Integer 1–10 (1 = lowest urgency, 10 = highest)
  ttl_seconds          Maximum seconds the task may live unassigned
  payload              Arbitrary task data (must be a dict)
  required_capabilities  {skill: float} — capability requirements [0.0, 1.0]
  min_reputation       Minimum agent reputation score required [0.0, 1.0]
  checkpoint_data      Optional resumption state (for TASK_RESUME)
  timeout_policy       {soft_timeout_s, hard_timeout_s}
  retry_policy         {max_retries, backoff_seconds}

Construction
────────────
  Use from_dict() to build from raw wire data — it calls validate() automatically.
  Direct instantiation is allowed but validate() must be called explicitly.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


class ValidationError(Exception):
    """Raised when a TaskSchema fails validation."""


@dataclass
class TimeoutPolicy:
    """
    Dual-timeout policy for task execution.

    soft_timeout_s : grace-period warning threshold (must be > 0).
    hard_timeout_s : absolute kill threshold (must be >= soft_timeout_s).
    """

    soft_timeout_s: float
    hard_timeout_s: float


@dataclass
class RetryPolicy:
    """
    Retry policy for failed tasks.

    max_retries    : maximum number of retries (>= 0).
    backoff_seconds: seconds to wait between retries (>= 0).
    """

    max_retries: int
    backoff_seconds: float


@dataclass
class TaskSchema:
    """
    Fully-validated task specification for the QUORBIT delegation pipeline.
    """

    task_id: str
    type: str
    priority: int                              # 1–10
    ttl_seconds: int
    payload: Dict[str, Any]
    required_capabilities: Dict[str, float]
    min_reputation: float = 0.0
    checkpoint_data: Optional[Dict[str, Any]] = None
    timeout_policy: Optional[TimeoutPolicy] = None
    retry_policy: Optional[RetryPolicy] = None

    # ── Validation ────────────────────────────────────────────────────────

    def validate(self) -> None:
        """
        Validate all fields.

        Raises ValidationError on the first violation found.
        """
        # task_id — must be a valid UUID4
        try:
            uuid.UUID(str(self.task_id))
        except (ValueError, AttributeError):
            raise ValidationError(
                f"task_id must be a valid UUID4, got {self.task_id!r}"
            )

        # type
        if not isinstance(self.type, str) or not self.type.strip():
            raise ValidationError("type must be a non-empty string")

        # priority
        if not isinstance(self.priority, int) or not (1 <= self.priority <= 10):
            raise ValidationError(
                f"priority must be an integer in [1, 10], got {self.priority!r}"
            )

        # ttl_seconds
        if not isinstance(self.ttl_seconds, int) or self.ttl_seconds <= 0:
            raise ValidationError(
                f"ttl_seconds must be a positive integer, got {self.ttl_seconds!r}"
            )

        # payload
        if not isinstance(self.payload, dict):
            raise ValidationError(
                f"payload must be a dict, got {type(self.payload).__name__}"
            )

        # required_capabilities
        if not isinstance(self.required_capabilities, dict):
            raise ValidationError("required_capabilities must be a dict")
        for skill, score in self.required_capabilities.items():
            try:
                s = float(score)
            except (TypeError, ValueError):
                raise ValidationError(
                    f"required_capabilities[{skill!r}] must be numeric, got {score!r}"
                )
            if not (0.0 <= s <= 1.0):
                raise ValidationError(
                    f"required_capabilities[{skill!r}] must be in [0.0, 1.0], got {s}"
                )

        # min_reputation
        try:
            mr = float(self.min_reputation)
        except (TypeError, ValueError):
            raise ValidationError("min_reputation must be numeric")
        if not (0.0 <= mr <= 1.0):
            raise ValidationError(
                f"min_reputation must be in [0.0, 1.0], got {mr}"
            )

        # checkpoint_data — optional, but must be dict if provided
        if self.checkpoint_data is not None and not isinstance(self.checkpoint_data, dict):
            raise ValidationError("checkpoint_data must be a dict or None")

        # timeout_policy
        if self.timeout_policy is not None:
            tp = self.timeout_policy
            if not isinstance(tp.soft_timeout_s, (int, float)) or tp.soft_timeout_s <= 0:
                raise ValidationError("timeout_policy.soft_timeout_s must be positive")
            if not isinstance(tp.hard_timeout_s, (int, float)) or tp.hard_timeout_s <= 0:
                raise ValidationError("timeout_policy.hard_timeout_s must be positive")
            if tp.hard_timeout_s < tp.soft_timeout_s:
                raise ValidationError(
                    "timeout_policy.hard_timeout_s must be >= soft_timeout_s"
                )

        # retry_policy
        if self.retry_policy is not None:
            rp = self.retry_policy
            if not isinstance(rp.max_retries, int) or rp.max_retries < 0:
                raise ValidationError(
                    "retry_policy.max_retries must be a non-negative integer"
                )
            if not isinstance(rp.backoff_seconds, (int, float)) or rp.backoff_seconds < 0:
                raise ValidationError(
                    "retry_policy.backoff_seconds must be non-negative"
                )

    # ── Serialisation ─────────────────────────────────────────────────────

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TaskSchema":
        """
        Build a TaskSchema from a raw dict and validate it.

        Raises ValidationError on invalid input.
        """
        tp_data = d.get("timeout_policy")
        rp_data = d.get("retry_policy")

        task = cls(
            task_id=d.get("task_id", ""),
            type=d.get("type", ""),
            priority=d.get("priority", 0),
            ttl_seconds=d.get("ttl_seconds", 0),
            payload=d.get("payload", {}),
            required_capabilities=d.get("required_capabilities", {}),
            min_reputation=float(d.get("min_reputation", 0.0)),
            checkpoint_data=d.get("checkpoint_data"),
            timeout_policy=(
                TimeoutPolicy(
                    soft_timeout_s=float(tp_data["soft_timeout_s"]),
                    hard_timeout_s=float(tp_data["hard_timeout_s"]),
                )
                if tp_data is not None
                else None
            ),
            retry_policy=(
                RetryPolicy(
                    max_retries=int(rp_data["max_retries"]),
                    backoff_seconds=float(rp_data["backoff_seconds"]),
                )
                if rp_data is not None
                else None
            ),
        )
        task.validate()
        return task

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a plain dict (JSON-safe)."""
        d: Dict[str, Any] = {
            "task_id": self.task_id,
            "type": self.type,
            "priority": self.priority,
            "ttl_seconds": self.ttl_seconds,
            "payload": self.payload,
            "required_capabilities": self.required_capabilities,
            "min_reputation": self.min_reputation,
            "checkpoint_data": self.checkpoint_data,
        }
        if self.timeout_policy is not None:
            d["timeout_policy"] = {
                "soft_timeout_s": self.timeout_policy.soft_timeout_s,
                "hard_timeout_s": self.timeout_policy.hard_timeout_s,
            }
        if self.retry_policy is not None:
            d["retry_policy"] = {
                "max_retries": self.retry_policy.max_retries,
                "backoff_seconds": self.retry_policy.backoff_seconds,
            }
        return d

    def __repr__(self) -> str:
        return (
            f"TaskSchema(task_id={self.task_id!r}, type={self.type!r}, "
            f"priority={self.priority})"
        )
