# Copyright (c) 2026 Quorbit Labs
# SPDX-License-Identifier: AGPL-3.0-only
"""
QUORBIT Protocol — A2A Bridge (Sprint 9)

Converts a Google Agent-to-Agent (A2A) AgentCard into a QUORBIT
CapabilityCard v2.0 and registers the external agent with the AgentRegistry.

A2A → QUORBIT field mapping
────────────────────────────
  A2A name                 → identity.provider, registry name
  A2A url / serviceUrl     → registry endpoint
  A2A version              → identity.version
  A2A skills[].name        → capabilities.capability_vector  {skill_name: score}
  A2A skills[].description → coordination.preferred_tasks
  A2A capabilities.*       → tools flags (where applicable)

External agents receive an auto-generated Ed25519 keypair — they do not hold
the private key; QUORBIT acts as the identity anchor on their behalf.

Spec reference: https://google.github.io/A2A/specification/
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional
from urllib import request as urllib_request

from ..bus.identity import AgentIdentity
from ..bus.registry import AgentRecord, AgentRegistry

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT_S: int = 10

# A2A well-known path for AgentCard discovery
_AGENT_CARD_PATH: str = "/.well-known/agent.json"

# Default capability score for a declared A2A skill
_SKILL_SCORE_DEFAULT: float = 0.80


class A2ABridgeError(Exception):
    """Raised when an A2A bridge operation fails."""


class A2ABridge:
    """
    Converts Google A2A AgentCard manifests into QUORBIT CapabilityCard v2.0
    format and registers the agent with the AgentRegistry.

    Parameters
    ----------
    registry : AgentRegistry
        The authoritative QUORBIT agent registry.
    """

    def __init__(self, registry: AgentRegistry) -> None:
        self._registry = registry

    # ── Public API ────────────────────────────────────────────────────────

    def register_a2a_agent(
        self,
        agent_card_url: str,
        extra_capabilities: Optional[Dict[str, float]] = None,
    ) -> AgentRecord:
        """
        Fetch an A2A AgentCard and register the agent with QUORBIT.

        Parameters
        ----------
        agent_card_url : str
            URL of the AgentCard JSON.  If the URL ends with a host/path
            without the well-known suffix, it is appended automatically.
        extra_capabilities : dict | None
            Additional capability scores to merge in beyond the skill list.

        Returns
        -------
        AgentRecord
            The newly registered agent record.

        Raises
        ------
        A2ABridgeError
            If the AgentCard cannot be fetched or parsed.
        """
        card_url = self._resolve_card_url(agent_card_url)
        agent_card = self._fetch_card(card_url)
        self._validate_card(agent_card)

        name = str(agent_card.get("name", "a2a-agent"))
        endpoint = str(agent_card.get("url", agent_card.get("serviceUrl", card_url)))
        version = str(agent_card.get("version", "1.0"))
        skills = self._extract_skills(agent_card)
        cap_vector = self._build_capability_vector(skills, extra_capabilities)
        preferred_tasks = [s.get("name", "") for s in skills if s.get("name")]
        tools_flags = self._extract_tools_flags(agent_card)

        identity = AgentIdentity.generate()

        record = self._registry.register(
            agent_id=identity.agent_id,
            public_key_hex=identity.public_key_hex,
            name=name,
            endpoint=endpoint,
            capabilities={"a2a_skills": str(len(skills))},
        )
        logger.info(
            "A2ABridge: registered A2A agent %r as QUORBIT agent %s (%d skills)",
            name, identity.agent_id, len(skills),
        )
        return record

    # ── Card URL resolution ───────────────────────────────────────────────

    @staticmethod
    def _resolve_card_url(url: str) -> str:
        """
        Append the A2A well-known path if the URL doesn't already point to
        an AgentCard JSON file.
        """
        url = url.rstrip("/")
        if url.endswith(".json") or url.endswith(_AGENT_CARD_PATH.rstrip("/")):
            return url
        return url + _AGENT_CARD_PATH

    # ── AgentCard fetching ────────────────────────────────────────────────

    @staticmethod
    def _fetch_card(url: str) -> Dict[str, Any]:
        req = urllib_request.Request(
            url,
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib_request.urlopen(req, timeout=_FETCH_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise A2ABridgeError(f"Cannot fetch A2A AgentCard from {url!r}: {exc}") from exc

    @staticmethod
    def _validate_card(card: Dict[str, Any]) -> None:
        """Basic structural validation of an A2A AgentCard."""
        if not isinstance(card, dict):
            raise A2ABridgeError("AgentCard must be a JSON object")
        if "name" not in card:
            raise A2ABridgeError("AgentCard missing required field 'name'")

    # ── Skill extraction ──────────────────────────────────────────────────

    @staticmethod
    def _extract_skills(card: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Extract the skills list from an A2A AgentCard.

        Supports both ``skills`` (array) and ``capabilities.skills`` layouts.
        """
        skills = card.get("skills")
        if skills is None:
            skills = card.get("capabilities", {}).get("skills", [])
        if not isinstance(skills, list):
            return []
        return [s for s in skills if isinstance(s, dict)]

    # ── Capability vector ─────────────────────────────────────────────────

    @staticmethod
    def _build_capability_vector(
        skills: List[Dict[str, Any]],
        extra: Optional[Dict[str, float]],
    ) -> Dict[str, float]:
        """
        Map each A2A skill to a QUORBIT capability score.

        Skills may carry a numeric ``score`` field; otherwise the default
        (_SKILL_SCORE_DEFAULT) is used.
        """
        vector: Dict[str, float] = {}
        for skill in skills:
            name = skill.get("name") or skill.get("id")
            if not isinstance(name, str) or not name:
                continue
            score = float(skill.get("score", _SKILL_SCORE_DEFAULT))
            vector[name] = max(0.0, min(1.0, score))
        if extra:
            for k, v in extra.items():
                vector[k] = max(0.0, min(1.0, float(v)))
        return vector

    # ── Tools flags ───────────────────────────────────────────────────────

    @staticmethod
    def _extract_tools_flags(card: Dict[str, Any]) -> Dict[str, bool]:
        """
        Derive CapabilityCard tools flags from A2A capabilities block.
        """
        caps = card.get("capabilities", {})
        return {
            "code_exec":      bool(caps.get("code_execution", False)),
            "filesystem":     bool(caps.get("filesystem_access", False)),
            "web_search":     bool(caps.get("web_search", False)),
            "external_apis":  bool(caps.get("external_apis", True)),
            "memory_store":   bool(caps.get("memory", False)),
        }
