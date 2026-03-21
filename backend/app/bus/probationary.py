# Copyright (c) 2026 Quorbit Labs
# SPDX-License-Identifier: AGPL-3.0-only
"""
QUORBIT Protocol — Probationary Robustness Testing (AGPL-3.0) — D22

RobustnessTest evaluates a probationary agent's output stability under semantic
variation of its seed task.

Algorithm
─────────
1. Generate 5 semantic variants of the seed task (paraphrase / reorder constraints).
2. Run each variant through the agent's output function; collect output_quality scores
   in [0, 1] and track structured_output_rate (fraction of outputs that are
   well-structured).
3. Pass criteria:
     variance(output_quality) < 0.25   AND   structured_output_rate >= 0.80
4. On fail:
     - Extend probation by 24 hours (stored in the registry as an extended_probation_until
       field via update_dynamic or equivalent).
     - Increment attempt counter.
5. After 3 consecutive failures:
     - Mark agent_type = "low_robustness" in the registry.
     - No further retries are scheduled.
"""

from __future__ import annotations

import copy
import logging
import statistics
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

NUM_VARIANTS: int = 5
VARIANCE_THRESHOLD: float = 0.25       # variance must be strictly below this
STRUCT_RATE_THRESHOLD: float = 0.80    # structured_output_rate must be >= this
PROBATION_EXTENSION_H: int = 24        # hours to extend probation on failure
MAX_ATTEMPTS: int = 3                  # maximum consecutive failures before labelling
LOW_ROBUSTNESS_LABEL: str = "low_robustness"

# Semantic variation templates applied to seed task fields
_VARIANT_TRANSFORMS: List[str] = [
    "original",              # 0 — unchanged
    "paraphrase_objective",  # 1 — rephrase the goal statement
    "reorder_constraints",   # 2 — shuffle constraint ordering
    "add_noise_context",     # 3 — prepend irrelevant context sentence
    "simplify_prompt",       # 4 — strip optional elaboration
]


# ── Helper: generate semantic variants ───────────────────────────────────────


def _generate_variants(seed_task: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Produce NUM_VARIANTS semantic variants of *seed_task*.

    Each variant is a shallow-cloned dict with a ``_variant`` tag and a
    lightweight transformation applied to the ``prompt`` field (if present).
    Callers that supply richer task dicts may override this via subclassing.

    Returns
    -------
    List[Dict[str, Any]]
        Exactly NUM_VARIANTS task dicts; the first is the seed unchanged.
    """
    variants: List[Dict[str, Any]] = []
    prompt: str = str(seed_task.get("prompt", ""))

    transforms: List[Tuple[str, str]] = [
        ("original",             prompt),
        ("paraphrase_objective", f"Please accomplish the following: {prompt}"),
        ("reorder_constraints",  _reorder_sentences(prompt)),
        ("add_noise_context",    f"Note: context may vary. {prompt}"),
        ("simplify_prompt",      _first_sentence(prompt) or prompt),
    ]

    for label, transformed_prompt in transforms[:NUM_VARIANTS]:
        variant = copy.deepcopy(seed_task)
        variant["prompt"] = transformed_prompt
        variant["_variant"] = label
        variants.append(variant)

    return variants


def _reorder_sentences(text: str) -> str:
    """Reverse sentence order as a lightweight constraint reordering."""
    sentences = [s.strip() for s in text.split(".") if s.strip()]
    return ". ".join(reversed(sentences)) + ("." if sentences else "")


def _first_sentence(text: str) -> str:
    """Extract the first sentence for prompt simplification."""
    parts = text.split(".")
    return parts[0].strip() + "." if parts and parts[0].strip() else ""


# ── RobustnessTest ────────────────────────────────────────────────────────────


class RobustnessTestError(Exception):
    """Raised for invalid RobustnessTest usage."""


class RobustnessTest:
    """
    Evaluates output stability of a probationary agent across semantic variants.

    Parameters
    ----------
    agent_id : str
        ID of the agent under test.
    seed_task : Dict[str, Any]
        The canonical task dict used to generate variants.
        Must contain at least a ``"prompt"`` key.
    registry : Any
        Registry object; must expose ``update_agent_field(agent_id, field, value)``
        (or equivalent) so the tester can persist probation extensions and labels.
        A minimal duck-type: object with ``update_agent_field(str, str, Any) -> None``.

    Usage
    -----
    ::

        test = RobustnessTest(agent_id="abc", seed_task=task, registry=reg)
        passed = test.run(output_fn=my_agent_fn)
    """

    def __init__(
        self,
        agent_id: str,
        seed_task: Dict[str, Any],
        registry: Any,
    ) -> None:
        if not agent_id:
            raise RobustnessTestError("agent_id must not be empty")
        if not isinstance(seed_task, dict):
            raise RobustnessTestError("seed_task must be a dict")

        self.agent_id = agent_id
        self.seed_task = seed_task
        self.registry = registry

        # Persistent counters (survive across run() calls on the same instance)
        self._attempts: int = 0
        self._labelled: bool = False

    # ── Public API ────────────────────────────────────────────────────────

    @property
    def attempts(self) -> int:
        """Number of consecutive failures recorded so far."""
        return self._attempts

    @property
    def labelled(self) -> bool:
        """True if the agent has been marked low_robustness."""
        return self._labelled

    def run(
        self,
        output_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
    ) -> bool:
        """
        Execute the robustness test.

        Parameters
        ----------
        output_fn : Callable[[Dict[str, Any]], Dict[str, Any]]
            Callable that accepts a task dict and returns a result dict with:
              - ``output_quality`` : float in [0, 1]
              - ``structured``     : bool  (True if output is well-structured)

        Returns
        -------
        bool
            True if the agent passes the robustness criteria; False otherwise.

        Side-effects
        ------------
        On failure:
          * Extends probation by 24 h via ``registry.update_agent_field``.
          * If this is the 3rd failure, marks the agent as low_robustness.
        """
        if self._labelled:
            logger.warning(
                "RobustnessTest: agent %s is already labelled %s — skipping",
                self.agent_id,
                LOW_ROBUSTNESS_LABEL,
            )
            return False

        variants = _generate_variants(self.seed_task)
        quality_scores: List[float] = []
        structured_count: int = 0

        for variant in variants:
            try:
                result = output_fn(variant)
            except Exception as exc:
                logger.error(
                    "RobustnessTest: output_fn raised for agent %s variant %r: %s",
                    self.agent_id,
                    variant.get("_variant"),
                    exc,
                )
                # Treat exception as lowest quality, unstructured
                result = {"output_quality": 0.0, "structured": False}

            quality = max(0.0, min(1.0, float(result.get("output_quality", 0.0))))
            is_structured = bool(result.get("structured", False))
            quality_scores.append(quality)
            if is_structured:
                structured_count += 1

        variance = _variance(quality_scores)
        struct_rate = structured_count / NUM_VARIANTS

        passed = variance < VARIANCE_THRESHOLD and struct_rate >= STRUCT_RATE_THRESHOLD

        logger.info(
            "RobustnessTest agent=%s variance=%.4f struct_rate=%.2f passed=%s",
            self.agent_id,
            variance,
            struct_rate,
            passed,
        )

        if not passed:
            self._handle_failure(variance=variance, struct_rate=struct_rate)

        return passed

    # ── Internal failure handling ──────────────────────────────────────────

    def _handle_failure(self, variance: float, struct_rate: float) -> None:
        self._attempts += 1

        extended_until_ms = int((time.time() + PROBATION_EXTENSION_H * 3600) * 1000)
        self._registry_set("extended_probation_until_ms", extended_until_ms)

        logger.warning(
            "RobustnessTest: agent %s failed (attempt %d/%d) "
            "variance=%.4f struct_rate=%.2f — probation extended %dh",
            self.agent_id,
            self._attempts,
            MAX_ATTEMPTS,
            variance,
            struct_rate,
            PROBATION_EXTENSION_H,
        )

        if self._attempts >= MAX_ATTEMPTS:
            self._mark_low_robustness()

    def _mark_low_robustness(self) -> None:
        self._labelled = True
        self._registry_set("agent_type", LOW_ROBUSTNESS_LABEL)
        logger.error(
            "RobustnessTest: agent %s marked %s after %d failures",
            self.agent_id,
            LOW_ROBUSTNESS_LABEL,
            self._attempts,
        )

    def _registry_set(self, field: str, value: Any) -> None:
        """Write *field* = *value* to the registry; silently logs on error."""
        try:
            self.registry.update_agent_field(self.agent_id, field, value)
        except Exception as exc:
            logger.error(
                "RobustnessTest: registry update failed for agent %s field %r: %s",
                self.agent_id,
                field,
                exc,
            )


# ── Variance helper ───────────────────────────────────────────────────────────


def _variance(scores: List[float]) -> float:
    """
    Return population variance of *scores*.

    Returns 0.0 for fewer than 2 samples (no spread).
    """
    if len(scores) < 2:
        return 0.0
    mean = sum(scores) / len(scores)
    return sum((x - mean) ** 2 for x in scores) / len(scores)
