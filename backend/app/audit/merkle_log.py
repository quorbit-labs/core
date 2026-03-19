"""
QUORBIT Protocol — Merkle Append-Only Log (AGPL-3.0) — D16

An append-only, cryptographically chained operation log.

Entry structure:
  {
    "operation":    str,
    "data":         str,
    "timestamp_ms": int,
    "prev_hash":    str,   # SHA256 hex of the previous entry's hash
    "hash":         str,   # SHA256(prev_hash + operation + data + timestamp_ms)
  }

Properties:
  • Append-only: no entry can be modified once written.
  • Tamper-evident: any modification breaks the chain (verify_chain detects it).
  • Gossipable: get_checkpoint() returns the latest hash for peer comparison.
  • Verifiable by any agent: verify_chain() replays the entire log.

Checkpoints are gossiped every 5 minutes (caller is responsible for scheduling).

Redis key schema (DB=2):
  audit:merkle:entries     — List of JSON entry strings (append-only, RPUSH)
  audit:merkle:checkpoint  — String (latest hash + timestamp), updated on append
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import redis

logger = logging.getLogger(__name__)

# Redis keys
ENTRIES_KEY = "audit:merkle:entries"
CHECKPOINT_KEY = "audit:merkle:checkpoint"

# Sentinel value for the genesis (first) entry
GENESIS_HASH = "0" * 64   # 64 zero hex chars


@dataclass(frozen=True)
class MerkleEntry:
    """
    A single immutable record in the Merkle log.

    Attributes
    ----------
    operation : str
        The operation name (e.g. "quarantine", "admin_action").
    data : str
        JSON-serialisable payload string.
    timestamp_ms : int
        Unix timestamp in milliseconds at insertion time.
    prev_hash : str
        SHA256 hex digest of the previous entry (GENESIS_HASH for the first entry).
    hash : str
        SHA256(prev_hash + operation + data + timestamp_ms) as a hex string.
    """

    operation: str
    data: str
    timestamp_ms: int
    prev_hash: str
    hash: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "operation": self.operation,
            "data": self.data,
            "timestamp_ms": self.timestamp_ms,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "MerkleEntry":
        return cls(
            operation=d["operation"],
            data=d["data"],
            timestamp_ms=int(d["timestamp_ms"]),
            prev_hash=d["prev_hash"],
            hash=d["hash"],
        )


def _compute_hash(
    prev_hash: str,
    operation: str,
    data: str,
    timestamp_ms: int,
) -> str:
    """
    Compute SHA256(prev_hash + operation + data + timestamp_ms).

    All components are concatenated as UTF-8 strings before hashing.
    """
    raw = f"{prev_hash}{operation}{data}{timestamp_ms}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class MerkleLog:
    """
    Redis-backed append-only Merkle log.

    Parameters
    ----------
    redis_client : redis.Redis
        Redis client connected to DB=2.

    Thread-safety note:
        append() is not transactionally atomic across multiple processes.
        For multi-process deployments use a distributed lock before calling
        append(). Verification (verify_chain) is always safe to call concurrently.
    """

    def __init__(self, redis_client: redis.Redis) -> None:
        self._redis = redis_client

    # ── Internal helpers ──────────────────────────────────────────────────

    def _get_prev_hash(self) -> str:
        """Return the hash of the most recent entry, or GENESIS_HASH if empty."""
        last_raw = self._redis.lindex(ENTRIES_KEY, -1)
        if last_raw is None:
            return GENESIS_HASH
        try:
            last_entry = json.loads(last_raw)
            return last_entry["hash"]
        except (json.JSONDecodeError, KeyError):
            return GENESIS_HASH

    def _update_checkpoint(self, entry_hash: str) -> None:
        """Write the current checkpoint (latest hash + timestamp) to Redis."""
        checkpoint = json.dumps({
            "hash": entry_hash,
            "timestamp_ms": int(time.time() * 1000),
        })
        self._redis.set(CHECKPOINT_KEY, checkpoint)

    # ── Public API ────────────────────────────────────────────────────────

    def append(self, operation: str, data: str) -> MerkleEntry:
        """
        Append a new entry to the log.

        Parameters
        ----------
        operation : str
            The operation being recorded (e.g. "quarantine", "admin_key_revoke").
        data : str
            Serialised payload (typically JSON string).

        Returns
        -------
        MerkleEntry
            The newly created and appended entry.
        """
        timestamp_ms = int(time.time() * 1000)
        prev_hash = self._get_prev_hash()
        entry_hash = _compute_hash(prev_hash, operation, data, timestamp_ms)

        entry = MerkleEntry(
            operation=operation,
            data=data,
            timestamp_ms=timestamp_ms,
            prev_hash=prev_hash,
            hash=entry_hash,
        )

        pipe = self._redis.pipeline()
        pipe.rpush(ENTRIES_KEY, json.dumps(entry.to_dict()))
        pipe.execute()

        self._update_checkpoint(entry_hash)

        logger.debug(
            "MerkleLog: appended %s (hash=%s…)", operation, entry_hash[:12]
        )
        return entry

    def verify_chain(self) -> bool:
        """
        Replay the entire log and verify every entry's hash.

        Returns True iff the chain is intact (no tampering detected).
        An empty log is considered valid.
        """
        raw_entries = self._redis.lrange(ENTRIES_KEY, 0, -1)
        if not raw_entries:
            return True

        expected_prev = GENESIS_HASH

        for i, raw in enumerate(raw_entries):
            try:
                d = json.loads(raw)
                entry = MerkleEntry.from_dict(d)
            except (json.JSONDecodeError, KeyError) as exc:
                logger.error(
                    "MerkleLog: parse error at index %d: %s", i, exc
                )
                return False

            # Verify prev_hash linkage
            if entry.prev_hash != expected_prev:
                logger.error(
                    "MerkleLog: chain break at index %d — "
                    "expected prev=%s…, got %s…",
                    i, expected_prev[:12], entry.prev_hash[:12],
                )
                return False

            # Verify the entry's own hash
            recomputed = _compute_hash(
                entry.prev_hash,
                entry.operation,
                entry.data,
                entry.timestamp_ms,
            )
            if recomputed != entry.hash:
                logger.error(
                    "MerkleLog: hash mismatch at index %d — "
                    "stored=%s…, computed=%s…",
                    i, entry.hash[:12], recomputed[:12],
                )
                return False

            expected_prev = entry.hash

        return True

    def get_checkpoint(self) -> Optional[Dict]:
        """
        Return the latest checkpoint for gossip.

        The checkpoint contains the most recent hash and a timestamp.
        Any agent can compare this against its own chain tip.
        Returns None if the log is empty.
        """
        raw = self._redis.get(CHECKPOINT_KEY)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def length(self) -> int:
        """Return the total number of entries in the log."""
        return self._redis.llen(ENTRIES_KEY)

    def get_entries(self, start: int = 0, end: int = -1) -> List[MerkleEntry]:
        """
        Return a slice of the log as MerkleEntry objects.

        Parameters
        ----------
        start : int
            Start index (inclusive, 0-based).
        end : int
            End index (inclusive, -1 = last entry).
        """
        raw_entries = self._redis.lrange(ENTRIES_KEY, start, end)
        entries = []
        for raw in raw_entries:
            try:
                entries.append(MerkleEntry.from_dict(json.loads(raw)))
            except (json.JSONDecodeError, KeyError):
                pass
        return entries

    def __repr__(self) -> str:
        checkpoint = self.get_checkpoint()
        tip = checkpoint["hash"][:12] + "…" if checkpoint else "empty"
        return f"MerkleLog(length={self.length()}, tip={tip})"
