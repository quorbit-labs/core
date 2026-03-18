"""
QUORBIT Protocol — Nonce Manager (AGPL-3.0)

Prevents replay attacks by tracking used nonces per agent.
A nonce is a (agent_id, nonce_value) pair that must be unique within a TTL window.
"""

from __future__ import annotations

import os
import time
from collections import defaultdict
from typing import Dict, Set, Tuple


class NonceManager:
    """
    Tracks used nonces to prevent replay attacks.

    Nonces expire after `ttl` seconds, after which the same value may be reused
    (though agents should not do this in practice — use monotonically increasing nonces).

    Thread-safety: Not thread-safe. Wrap in a lock for concurrent use.
    """

    def __init__(self, ttl: float = 300.0) -> None:
        self._ttl = ttl
        # agent_id -> set of (nonce, expiry_time)
        self._store: Dict[str, Set[Tuple[str, float]]] = defaultdict(set)

    # ── Public API ────────────────────────────────────────────────────────

    @staticmethod
    def generate() -> str:
        """Generate a cryptographically random 32-byte nonce (hex string)."""
        return os.urandom(32).hex()

    def consume(self, agent_id: str, nonce: str) -> bool:
        """
        Attempt to consume a nonce for an agent.

        Returns True if the nonce is fresh (first use within TTL window).
        Returns False if the nonce was already used or has an invalid format.
        """
        if not nonce or len(nonce) > 128:
            return False

        self._evict_expired(agent_id)

        existing_nonces = {n for n, _ in self._store[agent_id]}
        if nonce in existing_nonces:
            return False  # Replay detected

        expiry = time.time() + self._ttl
        self._store[agent_id].add((nonce, expiry))
        return True

    def is_used(self, agent_id: str, nonce: str) -> bool:
        """Check if a nonce has already been consumed (without consuming it)."""
        self._evict_expired(agent_id)
        return nonce in {n for n, _ in self._store[agent_id]}

    def purge(self, agent_id: str) -> int:
        """Remove all nonces for an agent. Returns count removed."""
        count = len(self._store.pop(agent_id, set()))
        return count

    def evict_all_expired(self) -> int:
        """Evict expired nonces across all agents. Returns total count removed."""
        total = 0
        now = time.time()
        for agent_id in list(self._store.keys()):
            before = len(self._store[agent_id])
            self._store[agent_id] = {(n, e) for n, e in self._store[agent_id] if e > now}
            total += before - len(self._store[agent_id])
            if not self._store[agent_id]:
                del self._store[agent_id]
        return total

    def stats(self) -> Dict[str, int]:
        """Return per-agent nonce count (after evicting expired entries)."""
        self.evict_all_expired()
        return {aid: len(nonces) for aid, nonces in self._store.items()}

    # ── Internals ─────────────────────────────────────────────────────────

    def _evict_expired(self, agent_id: str) -> None:
        now = time.time()
        self._store[agent_id] = {(n, e) for n, e in self._store[agent_id] if e > now}
