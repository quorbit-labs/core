"""
QUORBIT Protocol — anti_gaming package (AGPL-3.0)

Anti-gaming and collusion detection:
- detectors:        Collusion, symmetry, and SLA-cliff detectors + combined scorer
- graph_store:      In-memory directed graph with spectral ring detection
- adaptive_weights: Threat-aware weight rebalancing (sum=100, each ∈ [10,70])
"""

from .adaptive_weights import AdaptiveWeights, BASE_WEIGHTS, MIN_WEIGHT, MAX_WEIGHT
from .graph_store import GraphStore
from .detectors import (
    AntiGamingDetector,
    CollusionGraphDetector,
    GraphSymmetryDetector,
    SlaCliffDetector,
    COMBINED_SUSPICIOUS,
    COLLUSION_THRESHOLD,
    SYMMETRY_THRESHOLD,
    SLA_CLIFF_THRESHOLD,
)

__all__ = [
    # weights
    "AdaptiveWeights",
    "BASE_WEIGHTS",
    "MIN_WEIGHT",
    "MAX_WEIGHT",
    # graph
    "GraphStore",
    # detectors
    "AntiGamingDetector",
    "CollusionGraphDetector",
    "GraphSymmetryDetector",
    "SlaCliffDetector",
    "COMBINED_SUSPICIOUS",
    "COLLUSION_THRESHOLD",
    "SYMMETRY_THRESHOLD",
    "SLA_CLIFF_THRESHOLD",
]
