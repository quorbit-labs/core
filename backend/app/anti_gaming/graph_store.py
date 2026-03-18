"""
QUORBIT Protocol — Graph Store (AGPL-3.0) — D12

In-memory directed weighted graph for tracking agent interactions.
Interface designed for future Neo4j backend migration.

Ring detection uses spectral analysis of the graph Laplacian (via numpy).
Clustering coefficient provides a local suspicion metric per node.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


class GraphStore:
    """
    In-memory directed weighted graph.

    Nodes are agent_ids (strings).
    Edge weight represents interaction strength (e.g. number of mutual validations).

    Methods
    -------
    add_edge(a, b, weight)   — add/increment a directed edge a → b
    get_neighbors(a)         — return outgoing neighbours with weights
    clustering_coefficient(a)— local clustering coefficient for undirected view
    detect_rings(min_size)   — list of suspected ring groups (spectral approach)
    suspicion_score(a)       — combined suspicion metric [0.0, 1.0]
    """

    def __init__(self) -> None:
        # adj[source][target] = weight
        self._adj: Dict[str, Dict[str, float]] = defaultdict(dict)

    # ── Mutation ──────────────────────────────────────────────────────────

    def add_edge(
        self,
        agent_a: str,
        agent_b: str,
        weight: float = 1.0,
    ) -> None:
        """Add or increment edge a → b.  Ensures both nodes exist."""
        self._adj[agent_a][agent_b] = self._adj[agent_a].get(agent_b, 0.0) + weight
        # Ensure agent_b exists as a node even if it has no outgoing edges yet
        if agent_b not in self._adj:
            self._adj[agent_b] = {}

    def remove_edge(self, agent_a: str, agent_b: str) -> bool:
        """Remove edge a → b. Returns True if the edge existed."""
        if agent_a in self._adj and agent_b in self._adj[agent_a]:
            del self._adj[agent_a][agent_b]
            return True
        return False

    # ── Query ─────────────────────────────────────────────────────────────

    def get_neighbors(self, agent_id: str) -> Dict[str, float]:
        """Return outgoing neighbours of agent_id with edge weights."""
        return dict(self._adj.get(agent_id, {}))

    def get_in_neighbors(self, agent_id: str) -> Dict[str, float]:
        """Return incoming neighbours of agent_id with edge weights."""
        return {
            src: edges[agent_id]
            for src, edges in self._adj.items()
            if agent_id in edges
        }

    def nodes(self) -> List[str]:
        return list(self._adj.keys())

    def edge_weight(self, agent_a: str, agent_b: str) -> float:
        return self._adj.get(agent_a, {}).get(agent_b, 0.0)

    # ── Clustering coefficient ────────────────────────────────────────────

    def clustering_coefficient(self, agent_id: str) -> float:
        """
        Local clustering coefficient for the undirected view of the graph.

        CC = 2 * triangles / (degree * (degree - 1))

        Returns 0.0 if degree < 2.
        """
        # Undirected neighbours = union of out- and in-neighbours
        out_nb = set(self._adj.get(agent_id, {}).keys())
        in_nb = {src for src, edges in self._adj.items() if agent_id in edges}
        nb: Set[str] = (out_nb | in_nb) - {agent_id}

        degree = len(nb)
        if degree < 2:
            return 0.0

        triangles = 0
        nb_list = list(nb)
        for i, u in enumerate(nb_list):
            for v in nb_list[i + 1:]:
                # Edge u-v exists in either direction?
                if v in self._adj.get(u, {}) or u in self._adj.get(v, {}):
                    triangles += 1

        return 2.0 * triangles / (degree * (degree - 1))

    # ── Spectral ring detection ───────────────────────────────────────────

    def detect_rings(self, min_size: int = 3) -> List[List[str]]:
        """
        Detect suspected collusion rings using spectral graph clustering.

        Algorithm:
          1. Build symmetric adjacency matrix for the undirected graph view.
          2. Compute normalised Laplacian eigenvalues.
          3. Identify clusters (approximate connected components in spectral space).
          4. Filter clusters with high internal clustering coefficient (> 0.5).

        Returns a list of groups.  Each group is a list of agent_ids
        suspected of forming a ring.

        Falls back to connected-components-based detection if numpy is unavailable.
        """
        nodes = self.nodes()
        if len(nodes) < min_size:
            return []

        try:
            return self._spectral_rings(nodes, min_size)
        except Exception as exc:
            logger.warning("GraphStore: spectral ring detection failed (%s), using fallback", exc)
            return self._connected_component_rings(nodes, min_size)

    def _spectral_rings(self, nodes: List[str], min_size: int) -> List[List[str]]:
        """Spectral clustering on the graph Laplacian."""
        import numpy as np

        n = len(nodes)
        idx = {nid: i for i, nid in enumerate(nodes)}

        # Build symmetric weighted adjacency matrix
        A = np.zeros((n, n), dtype=np.float64)
        for src, edges in self._adj.items():
            if src not in idx:
                continue
            i = idx[src]
            for dst, w in edges.items():
                if dst not in idx:
                    continue
                j = idx[dst]
                A[i, j] += w
                A[j, i] += w   # symmetrise

        # Degree matrix and normalised Laplacian: L = I - D^{-1/2} A D^{-1/2}
        degree = A.sum(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            d_inv_sqrt = np.where(degree > 0, 1.0 / np.sqrt(degree), 0.0)
        D_inv_sqrt = np.diag(d_inv_sqrt)
        L = np.eye(n) - D_inv_sqrt @ A @ D_inv_sqrt

        # Eigenvalues of normalised Laplacian (0 = connected component)
        eigenvalues = np.linalg.eigvalsh(L)
        # Count near-zero eigenvalues → number of connected components
        n_components = int(np.sum(eigenvalues < 0.1))
        if n_components < 1:
            n_components = 1

        # Eigenvectors for the smallest n_components eigenvalues → cluster assignment
        _, eigenvectors = np.linalg.eigh(L)
        features = eigenvectors[:, :n_components]  # (n_nodes, n_components)

        # Simple k-means on spectral features to assign clusters
        from collections import Counter

        # Assign each node to the cluster of its closest centroid (naive)
        # For small graphs, use rounded spectral features as cluster labels
        labels = np.argmax(np.abs(features), axis=1)

        rings: List[List[str]] = []
        for cluster_id in range(features.shape[1]):
            members = [nodes[i] for i in range(n) if labels[i] == cluster_id]
            if len(members) >= min_size:
                # Only flag as ring if internal clustering is high
                avg_cc = (
                    sum(self.clustering_coefficient(m) for m in members) / len(members)
                )
                if avg_cc > 0.4:
                    rings.append(members)
        return rings

    def _connected_component_rings(
        self, nodes: List[str], min_size: int
    ) -> List[List[str]]:
        """Fallback: BFS-based connected components with CC filter."""
        visited: Set[str] = set()
        rings: List[List[str]] = []

        for start in nodes:
            if start in visited:
                continue
            # BFS on undirected graph
            component: List[str] = []
            queue = [start]
            while queue:
                node = queue.pop(0)
                if node in visited:
                    continue
                visited.add(node)
                component.append(node)
                out_nb = set(self._adj.get(node, {}).keys())
                in_nb = {s for s, e in self._adj.items() if node in e}
                for nb in (out_nb | in_nb) - visited:
                    queue.append(nb)

            if len(component) >= min_size:
                avg_cc = (
                    sum(self.clustering_coefficient(m) for m in component) / len(component)
                )
                if avg_cc > 0.4:
                    rings.append(component)
        return rings

    # ── Suspicion score ───────────────────────────────────────────────────

    def suspicion_score(self, agent_id: str) -> float:
        """
        Combined suspicion score [0.0, 1.0] based on:
          - Clustering coefficient (high CC in a dense group → suspicious)
          - Mutual edge ratio (fraction of out-edges that have a reciprocal)
        """
        cc = self.clustering_coefficient(agent_id)

        out_nb = set(self._adj.get(agent_id, {}).keys())
        if not out_nb:
            return cc * 0.5

        mutual = sum(1 for nb in out_nb if agent_id in self._adj.get(nb, {}))
        mutual_ratio = mutual / len(out_nb)

        # Weighted combination: 60% CC + 40% mutual ratio
        return min(1.0, cc * 0.6 + mutual_ratio * 0.4)

    def __repr__(self) -> str:
        return f"GraphStore(nodes={len(self.nodes())}, edges={sum(len(e) for e in self._adj.values())})"
