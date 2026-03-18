"""
QUORBIT Protocol — Nonce Store (AGPL-3.0)

Stateless HMAC-based nonce generation with Redis-backed replay protection.
Redis DB=1 (isolated, dedicated to nonce tracking).

Nonce token format (colon-separated):
  {agent_id}:{bucket}:{counter}:{hmac}

  bucket  = floor(timestamp_ms / 30_000)   — 30-second slot
  counter = random 8-byte hex              — per-nonce uniqueness within a slot
  hmac    = HMAC-SHA256(server_secret, f"{agent_id}:{bucket}:{counter}")

Verification window: bucket must be within ±1 of the current bucket (±30 s).
Rate limit:          max 10 successful verifications per second per agent_id.
Replay protection:   used nonces are recorded in Redis; explicit DEL on revocation.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import Optional

import redis

logger = logging.getLogger(__name__)

REDIS_DB = 1                 # isolated DB for nonce tracking
NONCE_PREFIX = "bus:nonce:"
RATE_PREFIX = "bus:rate:"

BUCKET_WINDOW_MS = 30_000   # 30 seconds per bucket (milliseconds)
RATE_LIMIT = 10              # max verifications per second per agent_id
NONCE_TTL = 90               # Redis TTL for used-nonce records (3 buckets)


def _bucket(ts_ms: Optional[int] = None) -> int:
    """Map a millisecond timestamp to a 30-second bucket index."""
    ms = ts_ms if ts_ms is not None else int(time.time() * 1000)
    return ms // BUCKET_WINDOW_MS


def _compute_hmac(secret: bytes, agent_id: str, bucket: int, counter: str) -> str:
    """Compute HMAC-SHA256(secret, '{agent_id}:{bucket}:{counter}')."""
    msg = f"{agent_id}:{bucket}:{counter}".encode()
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


class NonceError(Exception):
    """Raised when nonce verification fails for a specific reason."""


class NonceManager:
    """
    Stateless HMAC nonce manager with Redis-backed replay protection.

    Parameters
    ----------
    server_secret : bytes | None
        HMAC secret key (min 32 bytes recommended).
        Falls back to the NONCE_SECRET environment variable, then a random key.
    redis_url : str
        Redis connection string.  DB=1 is selected unconditionally.
    """

    def __init__(
        self,
        server_secret: bytes | None = None,
        redis_url: str = "redis://localhost:6379",
    ) -> None:
        env_secret = os.environ.get("NONCE_SECRET", "").encode()
        self._secret: bytes = server_secret or env_secret or os.urandom(32)
        self._redis = redis.Redis.from_url(
            redis_url, db=REDIS_DB, decode_responses=True
        )

    # ── Public API ────────────────────────────────────────────────────────

    def generate(self, agent_id: str, ts_ms: Optional[int] = None) -> str:
        """
        Generate a stateless HMAC nonce token for the given agent.

        The nonce is self-authenticating: its HMAC binds it to the agent_id
        and time bucket, so no server-side state is needed for issuance.
        """
        ms = ts_ms if ts_ms is not None else int(time.time() * 1000)
        bkt = _bucket(ms)
        counter = os.urandom(8).hex()
        h = _compute_hmac(self._secret, agent_id, bkt, counter)
        return f"{agent_id}:{bkt}:{counter}:{h}"

    def verify(self, nonce: str, agent_id: str) -> bool:
        """
        Verify a nonce token.

        Checks (in order):
          1. Parse and structural validity
          2. agent_id match
          3. Time window (current bucket within ±1 of nonce bucket)
          4. HMAC integrity (constant-time comparison)
          5. Rate limit (max 10 req/s per agent_id)
          6. Replay detection (not already used)
          7. Record as used in Redis

        Returns True on success.  Logs a warning and returns False on failure.
        """
        parts = nonce.split(":")
        if len(parts) != 4:
            logger.warning("Nonce: malformed token for agent %s", agent_id)
            return False

        nonce_agent_id, bucket_str, counter, received_hmac = parts

        if nonce_agent_id != agent_id:
            logger.warning("Nonce: agent_id mismatch (%s vs %s)", nonce_agent_id, agent_id)
            return False

        try:
            bkt = int(bucket_str)
        except ValueError:
            return False

        current_bkt = _bucket()
        if abs(current_bkt - bkt) > 1:
            logger.warning(
                "Nonce: expired or future bucket for agent %s (delta=%d)",
                agent_id,
                current_bkt - bkt,
            )
            return False

        expected = _compute_hmac(self._secret, agent_id, bkt, counter)
        if not hmac.compare_digest(expected, received_hmac):
            logger.warning("Nonce: HMAC verification failed for agent %s", agent_id)
            return False

        if not self._check_rate_limit(agent_id):
            logger.warning("Nonce: rate limit exceeded for agent %s", agent_id)
            return False

        nonce_key = f"{NONCE_PREFIX}{agent_id}:{bkt}:{counter}"
        if self._redis.exists(nonce_key):
            logger.warning("Nonce: replay detected for agent %s", agent_id)
            return False

        # Mark as used.  TTL covers 3 bucket windows; explicit DEL via revoke().
        self._redis.set(nonce_key, "1", ex=NONCE_TTL)
        return True

    def revoke(self, nonce: str) -> bool:
        """
        Explicitly DELETE a nonce from Redis before its TTL expires.

        Use this for early revocation (e.g. session logout, key rotation).
        Returns True if the key existed and was deleted.
        """
        parts = nonce.split(":")
        if len(parts) != 4:
            return False
        agent_id, bkt_str, counter, _ = parts
        nonce_key = f"{NONCE_PREFIX}{agent_id}:{bkt_str}:{counter}"
        deleted = self._redis.delete(nonce_key)
        return bool(deleted)

    # ── Internals ─────────────────────────────────────────────────────────

    def _check_rate_limit(self, agent_id: str) -> bool:
        """
        Sliding per-second rate limiter using Redis INCR.

        Key: bus:rate:{agent_id}:{unix_second}   TTL: 2s (covers clock skew)
        Returns True if the agent is under the rate limit.
        """
        key = f"{RATE_PREFIX}{agent_id}:{int(time.time())}"
        pipe = self._redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, 2)
        results = pipe.execute()
        count: int = results[0]
        return count <= RATE_LIMIT
