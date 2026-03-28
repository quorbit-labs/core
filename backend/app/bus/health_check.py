# Copyright (c) 2026 Quorbit Labs
# SPDX-License-Identifier: AGPL-3.0-only
"""
Sprint 9.5 — Extended health check with pgvector and Redis verification.

PATCH TARGET: backend/app/main.py
Replace the existing /health endpoint with this implementation.
"""

import time
import asyncio
from typing import Any

import redis.asyncio as aioredis

# NOTE: adjust imports to match your actual project structure
# from backend.app.bus.registry import get_redis_pool
# from backend.app.reputation.pgvector_store import get_pg_pool


async def check_redis(redis_pool: aioredis.Redis, timeout: float = 2.0) -> dict:
    """Verify Redis DB=0 (bus) is responsive."""
    try:
        start = time.monotonic()
        pong = await asyncio.wait_for(redis_pool.ping(), timeout=timeout)
        latency_ms = round((time.monotonic() - start) * 1000, 1)
        return {
            "status": "ok",
            "latency_ms": latency_ms,
            "connected": bool(pong),
        }
    except (asyncio.TimeoutError, Exception) as e:
        return {"status": "degraded", "error": str(e)}


async def check_redis_nonce(nonce_pool: aioredis.Redis, timeout: float = 2.0) -> dict:
    """Verify Redis DB=1 (nonce store) is responsive."""
    try:
        start = time.monotonic()
        pong = await asyncio.wait_for(nonce_pool.ping(), timeout=timeout)
        latency_ms = round((time.monotonic() - start) * 1000, 1)
        return {
            "status": "ok",
            "latency_ms": latency_ms,
            "connected": bool(pong),
        }
    except (asyncio.TimeoutError, Exception) as e:
        return {"status": "degraded", "error": str(e)}


async def check_pgvector(pg_pool: Any, timeout: float = 3.0) -> dict:
    """
    Verify PostgreSQL + pgvector extension is responsive.

    Checks:
    1. Connection alive (SELECT 1)
    2. pgvector extension loaded
    3. task_history table accessible
    """
    try:
        start = time.monotonic()
        async with pg_pool.acquire() as conn:
            # Basic connectivity
            await asyncio.wait_for(conn.fetchval("SELECT 1"), timeout=timeout)

            # pgvector extension check
            ext = await conn.fetchval(
                "SELECT installed_version FROM pg_available_extensions "
                "WHERE name = 'vector'"
            )

            # task_history table exists
            table_exists = await conn.fetchval(
                "SELECT EXISTS ("
                "  SELECT FROM information_schema.tables "
                "  WHERE table_name = 'task_history'"
                ")"
            )

            latency_ms = round((time.monotonic() - start) * 1000, 1)

            if not ext:
                return {
                    "status": "degraded",
                    "error": "pgvector extension not installed",
                    "latency_ms": latency_ms,
                    "impact": "scoring_v2_unavailable — falling back to cold_start scoring",
                }

            if not table_exists:
                return {
                    "status": "degraded",
                    "error": "task_history table missing",
                    "latency_ms": latency_ms,
                    "pgvector_version": ext,
                    "impact": "task_fit_score unavailable — falling back to cold_start scoring",
                }

            return {
                "status": "ok",
                "latency_ms": latency_ms,
                "pgvector_version": ext,
                "task_history_table": True,
            }
    except asyncio.TimeoutError:
        return {
            "status": "degraded",
            "error": "pgvector timeout",
            "impact": "scoring_v2_unavailable — falling back to cold_start scoring",
        }
    except Exception as e:
        return {
            "status": "degraded",
            "error": str(e),
            "impact": "scoring_v2_unavailable — falling back to cold_start scoring",
        }


async def health_endpoint(
    redis_pool: aioredis.Redis,
    nonce_pool: aioredis.Redis,
    pg_pool: Any,
) -> dict:
    """
    Extended /health endpoint.

    Returns:
        200 if all components OK
        200 with degraded flags if non-critical components down
              (pgvector down = scoring degrades to cold_start, system still works)
        503 if Redis bus is down (system cannot function)

    Usage in main.py:

        @app.get("/health")
        async def health():
            result = await health_endpoint(redis_pool, nonce_pool, pg_pool)
            status_code = 503 if result["status"] == "critical" else 200
            return JSONResponse(content=result, status_code=status_code)
    """
    redis_check, nonce_check, pg_check = await asyncio.gather(
        check_redis(redis_pool),
        check_redis_nonce(nonce_pool),
        check_pgvector(pg_pool),
    )

    # Determine overall status
    # Redis bus down = critical (system cannot function)
    # Nonce store down = critical (cannot authenticate)
    # pgvector down = degraded (scoring falls back to cold_start)
    if redis_check["status"] != "ok":
        overall = "critical"
    elif nonce_check["status"] != "ok":
        overall = "critical"
    elif pg_check["status"] != "ok":
        overall = "degraded"
    else:
        overall = "ok"

    return {
        "status": overall,
        "version": "0.1.1",
        "components": {
            "redis_bus": redis_check,
            "redis_nonce": nonce_check,
            "pgvector": pg_check,
        },
    }
