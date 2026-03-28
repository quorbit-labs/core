# Copyright (c) 2026 Quorbit Labs
# SPDX-License-Identifier: AGPL-3.0-only
"""
Sprint 9.5 — BusAI cooldown and oscillation detection.

PATCH TARGET: busai/backend/app/busai/engine.py
Wrap the existing adjust() function with cooldown logic.
"""

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ParamHistory:
    """Tracks adjustment history for a single parameter."""
    last_adjusted_at: float = 0.0
    last_direction: int = 0  # +1 or -1
    direction_changes_1h: list = field(default_factory=list)
    frozen_until: float = 0.0


COOLDOWN_SECONDS = 900  # 15 minutes per parameter
OSCILLATION_THRESHOLD = 3  # direction changes in 1h → freeze
FREEZE_DURATION = 3600  # 1 hour freeze on oscillation


class CooldownGuard:
    """
    Wraps BusAI adjust() to prevent oscillation.

    Rules:
    1. Each parameter: max 1 adjustment per 15 minutes
    2. If direction changed 3× in 1h → freeze for 1h + alert
    3. All skips and freezes are logged for Merkle audit

    Usage:
        guard = CooldownGuard()

        # In the policy engine loop:
        if guard.can_adjust("collusion_graph_weight", direction=+1):
            adjust("collusion_graph_weight", +5, max=70)
            guard.record_adjustment("collusion_graph_weight", direction=+1)
        else:
            reason = guard.skip_reason("collusion_graph_weight")
            audit_log({"event": "adjust_skipped", "param": "collusion_graph_weight",
                       "reason": reason})
    """

    def __init__(self):
        self._params: dict[str, ParamHistory] = {}

    def _get(self, param: str) -> ParamHistory:
        if param not in self._params:
            self._params[param] = ParamHistory()
        return self._params[param]

    def can_adjust(self, param: str, direction: int) -> bool:
        """Check if adjustment is allowed right now."""
        now = time.time()
        h = self._get(param)

        # Check freeze (oscillation detected previously)
        if now < h.frozen_until:
            return False

        # Check cooldown
        if (now - h.last_adjusted_at) < COOLDOWN_SECONDS:
            return False

        return True

    def record_adjustment(self, param: str, direction: int) -> Optional[str]:
        """
        Record that an adjustment was made.
        Returns alert string if oscillation detected, None otherwise.
        """
        now = time.time()
        h = self._get(param)

        # Detect direction change
        if h.last_direction != 0 and direction != h.last_direction:
            h.direction_changes_1h.append(now)

        # Prune old direction changes (keep only last 1h)
        cutoff = now - 3600
        h.direction_changes_1h = [t for t in h.direction_changes_1h if t > cutoff]

        # Check oscillation
        alert = None
        if len(h.direction_changes_1h) >= OSCILLATION_THRESHOLD:
            h.frozen_until = now + FREEZE_DURATION
            h.direction_changes_1h.clear()
            alert = (
                f"OSCILLATION_DETECTED: {param} changed direction "
                f"{OSCILLATION_THRESHOLD}× in 1h. "
                f"Frozen until {time.strftime('%H:%M:%S', time.localtime(h.frozen_until))}. "
                f"Operator review recommended."
            )

        h.last_adjusted_at = now
        h.last_direction = direction

        return alert

    def skip_reason(self, param: str) -> str:
        """Human-readable reason why adjustment was skipped."""
        now = time.time()
        h = self._get(param)

        if now < h.frozen_until:
            remaining = int(h.frozen_until - now)
            return f"oscillation_freeze ({remaining}s remaining)"

        remaining = int(COOLDOWN_SECONDS - (now - h.last_adjusted_at))
        return f"cooldown_active ({remaining}s remaining)"

    def status(self) -> dict:
        """Current status of all tracked parameters, for debugging."""
        now = time.time()
        result = {}
        for param, h in self._params.items():
            result[param] = {
                "last_adjusted_ago_s": round(now - h.last_adjusted_at, 1) if h.last_adjusted_at else None,
                "last_direction": h.last_direction,
                "direction_changes_1h": len(h.direction_changes_1h),
                "frozen": now < h.frozen_until,
                "frozen_remaining_s": max(0, int(h.frozen_until - now)) if now < h.frozen_until else 0,
            }
        return result
