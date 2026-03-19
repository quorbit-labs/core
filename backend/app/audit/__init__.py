"""
QUORBIT Protocol — Audit Package (AGPL-3.0)

Provides:
  merkle_log   — Append-only signed operation log with chain verification
  admin        — Multi-operator admin access model with session recording
"""

from .merkle_log import MerkleLog, MerkleEntry
from .admin import AdminManager, AdminRole, InsufficientOperatorsError

__all__ = [
    "MerkleLog",
    "MerkleEntry",
    "AdminManager",
    "AdminRole",
    "InsufficientOperatorsError",
]
