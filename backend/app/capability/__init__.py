"""
QUORBIT Protocol — Capability Package (AGPL-3.0)

Provides:
  card   — CapabilityCard v2.0 with static/dynamic sections and JSON schema validation
"""

from .card import CapabilityCard, CapabilityCardError, ValidationError

__all__ = ["CapabilityCard", "CapabilityCardError", "ValidationError"]
