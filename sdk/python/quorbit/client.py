# Copyright (c) 2026 Quorbit Labs
# SPDX-License-Identifier: AGPL-3.0-only
"""
QUORBIT Protocol — Python SDK client.

QuorbitClient provides typed access to the QUORBIT API:
  register()   — onboard an agent with a signed capability declaration
  heartbeat()  — signal liveness
  get_agent()  — fetch an agent record by ID
  discover()   — find best agents for a task description
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional
from urllib import request as urllib_request
from urllib.error import HTTPError

logger = logging.getLogger(__name__)

# ── Late import so the SDK works without the full backend installed ────────────
# If backend is available (monorepo), use AgentIdentity; otherwise fall back to
# a lightweight built-in keygen.

try:
    from backend.app.bus.identity import AgentIdentity as _AgentIdentity
    _HAS_BACKEND = True
except ImportError:
    _HAS_BACKEND = False


# ── Lightweight fallback identity (no backend dependency) ─────────────────────

class _LightIdentity:
    """Minimal Ed25519 identity used when the QUORBIT backend is not installed."""

    def __init__(self, private_key_hex: Optional[str] = None) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding, PublicFormat, PrivateFormat, NoEncryption,
        )
        import hashlib, base64

        if private_key_hex:
            raw = bytes.fromhex(private_key_hex)
            self._priv = Ed25519PrivateKey.from_private_bytes(raw)
        else:
            self._priv = Ed25519PrivateKey.generate()

        pub_raw = self._priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        priv_raw = self._priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())

        self.public_key_hex: str = pub_raw.hex()
        self.private_key_hex: str = priv_raw.hex()
        self.agent_id: str = hashlib.sha256(pub_raw).hexdigest()
        self._b64 = base64

    def sign(self, payload: bytes) -> str:
        import base64
        sig = self._priv.sign(payload)
        return base64.b64encode(sig).decode()


def _make_identity(private_key_hex: Optional[str]) -> Any:
    if _HAS_BACKEND:
        if private_key_hex:
            return _AgentIdentity.from_hex(private_key_hex)
        return _AgentIdentity.generate()
    return _LightIdentity(private_key_hex)


# ── Exceptions ────────────────────────────────────────────────────────────────


class QuorbitError(Exception):
    """Base exception for SDK errors."""


class QuorbitHTTPError(QuorbitError):
    """Raised when the API returns a non-2xx response."""

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body}")


# ── Client ────────────────────────────────────────────────────────────────────


class QuorbitClient:
    """
    Python client for the QUORBIT Protocol API.

    Parameters
    ----------
    base_url : str
        Base URL of the QUORBIT API (e.g. ``"http://localhost:8000"``).
    private_key_hex : str | None
        Hex-encoded Ed25519 private key.  If omitted, a new keypair is
        generated automatically (useful for first-time registration).
    timeout : int
        HTTP request timeout in seconds (default: 15).

    Examples
    --------
    ::

        client = QuorbitClient("http://localhost:8000")
        record = client.register("my-agent", {"nlp": 0.9, "code": 0.7})
        client.heartbeat()
        agents = client.discover("summarise a document")
    """

    def __init__(
        self,
        base_url: str,
        private_key_hex: Optional[str] = None,
        timeout: int = 15,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._identity = _make_identity(private_key_hex)

    # ── Identity helpers ──────────────────────────────────────────────────

    @property
    def agent_id(self) -> str:
        """The SHA256-derived agent ID for this client instance."""
        return self._identity.agent_id

    @property
    def public_key_hex(self) -> str:
        """Hex-encoded Ed25519 public key."""
        return self._identity.public_key_hex

    # ── API methods ───────────────────────────────────────────────────────

    def register(
        self,
        name: str,
        capabilities: Dict[str, float],
        endpoint: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Register this agent with the QUORBIT network.

        Signs the registration payload with the agent's Ed25519 private key.

        Parameters
        ----------
        name : str
            Human-readable agent name.
        capabilities : Dict[str, float]
            Capability vector — ``{skill_name: score}`` where score ∈ [0, 1].
        endpoint : str | None
            Public URL at which this agent can receive delegated tasks.

        Returns
        -------
        dict
            The ``AgentRecord`` returned by the API.
        """
        payload_bytes = json.dumps({
            "name": name,
            "capabilities": capabilities,
            "endpoint": endpoint,
        }, sort_keys=True).encode()

        body = {
            "name": name,
            "public_key_hex": self._identity.public_key_hex,
            "capabilities": capabilities,
            "endpoint": endpoint,
            "signature": self._identity.sign(payload_bytes),
        }
        return self._post("/api/v1/agents", body, expected_status=201)

    def heartbeat(self) -> bool:
        """
        Signal liveness to the QUORBIT network.

        Returns
        -------
        bool
            True if the heartbeat was acknowledged.
        """
        result = self._post(f"/api/v1/agents/{self.agent_id}/heartbeat", {})
        return bool(result.get("ok", False))

    def get_agent(self, agent_id: str) -> Dict[str, Any]:
        """
        Fetch the record for any registered agent.

        Parameters
        ----------
        agent_id : str
            SHA256-derived agent ID (64 hex chars).

        Returns
        -------
        dict
            The agent record.

        Raises
        ------
        QuorbitHTTPError
            If the agent is not found (HTTP 404) or another error occurs.
        """
        return self._get(f"/api/v1/agents/{agent_id}")

    def discover(
        self,
        task_description: str,
        min_score: float = 0.70,
        required_capabilities: Optional[Dict[str, float]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Discover agents suitable for a given task.

        Parameters
        ----------
        task_description : str
            Natural-language description of the task to delegate.
        min_score : float
            Minimum reputation score for candidates (default: 0.70).
        required_capabilities : dict | None
            Minimum capability levels, e.g. ``{"nlp": 0.8}``.

        Returns
        -------
        list[dict]
            Candidates ranked by discovery score (highest first).
        """
        body = {
            "task_description": task_description,
            "min_score": min_score,
            "required_capabilities": required_capabilities or {},
        }
        return self._post("/api/v1/discover", body)

    # ── HTTP helpers ──────────────────────────────────────────────────────

    def _post(
        self,
        path: str,
        body: dict,
        expected_status: int = 200,
    ) -> Any:
        url = self._base + path
        data = json.dumps(body).encode()
        req = urllib_request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        return self._exec(req, expected_status)

    def _get(self, path: str) -> Any:
        url = self._base + path
        req = urllib_request.Request(
            url,
            headers={"Accept": "application/json"},
            method="GET",
        )
        return self._exec(req, 200)

    def _exec(self, req: urllib_request.Request, expected_status: int) -> Any:
        try:
            with urllib_request.urlopen(req, timeout=self._timeout) as resp:
                if resp.status != expected_status:
                    raise QuorbitHTTPError(resp.status, resp.read().decode())
                return json.loads(resp.read().decode())
        except HTTPError as exc:
            raise QuorbitHTTPError(exc.code, exc.read().decode()) from exc
