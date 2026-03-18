"""
QUORBIT Protocol — Genesis Bootstrap (AGPL-3.0)

Loads and validates the genesis_validators.json file that bootstraps the
QUORBIT trust network.  The file must be signed by the network operator and
contain at least MIN_VALIDATORS (11) bootstrap validators.

During the first TRUSTED_ORACLE_TASK_LIMIT (1 000) tasks, a trusted oracle
seeding mode is active — full BFT consensus is not yet required.

Expected genesis file format:
{
  "genesis_validators": ["<agent_id>", ...],   // min 11 entries
  "rotation_interval_s": 86400,
  "operator_public_key": "<hex>",              // raw Ed25519 public key
  "operator_signature": "<base64>",            // signs canonical JSON (excl. this field)
  "created_at": "<ISO-8601 or unix timestamp>"
}
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .identity import verify_signature

logger = logging.getLogger(__name__)

MIN_VALIDATORS = 11
TRUSTED_ORACLE_TASK_LIMIT = 1_000


def _canonical_payload(data: dict[str, Any]) -> bytes:
    """
    Build the canonical bytes that were signed by the operator.

    The *operator_signature* field is excluded from the signed payload so that
    the signature itself does not circularly depend on its own value.
    """
    payload_data = {k: v for k, v in data.items() if k != "operator_signature"}
    return json.dumps(payload_data, sort_keys=True, separators=(",", ":")).encode()


class GenesisError(Exception):
    """Raised when genesis loading or validation fails."""


class GenesisConfig:
    """
    Parsed and validated genesis configuration.

    Attributes
    ----------
    genesis_validators : list[str]
        AgentIDs of the initial bootstrap validator set.
    rotation_interval_s : int
        How often validators rotate (seconds).  Default: 86400 (1 day).
    operator_public_key : str
        Hex-encoded raw Ed25519 public key of the network operator.
    operator_signature : str
        Base64-encoded operator signature over the canonical genesis payload.
    created_at : str
        Creation timestamp from the genesis file (ISO-8601 or Unix).
    """

    def __init__(self, data: dict[str, Any]) -> None:
        self.genesis_validators: list[str] = data["genesis_validators"]
        self.rotation_interval_s: int = int(data.get("rotation_interval_s", 86400))
        self.operator_public_key: str = data["operator_public_key"]
        self.operator_signature: str = data["operator_signature"]
        self.created_at: str = str(data.get("created_at", ""))
        self._task_counter: int = 0

    @property
    def is_trusted_oracle_phase(self) -> bool:
        """True while within the first TRUSTED_ORACLE_TASK_LIMIT tasks."""
        return self._task_counter < TRUSTED_ORACLE_TASK_LIMIT

    def increment_task_counter(self) -> None:
        """Record that one task has been processed."""
        self._task_counter += 1

    def validator_count(self) -> int:
        return len(self.genesis_validators)

    def __repr__(self) -> str:
        return (
            f"GenesisConfig("
            f"validators={self.validator_count()}, "
            f"rotation_interval_s={self.rotation_interval_s}, "
            f"trusted_oracle_phase={self.is_trusted_oracle_phase})"
        )


def load_genesis(path: str | Path) -> GenesisConfig:
    """
    Load, parse, and cryptographically validate a genesis_validators.json file.

    Validation steps:
      1. File exists and is valid JSON.
      2. All required fields are present.
      3. At least MIN_VALIDATORS (11) bootstrap validators are listed.
      4. Operator Ed25519 signature is valid over the canonical payload.

    Returns a validated GenesisConfig on success.
    Raises GenesisError on any failure — callers must abort startup.
    """
    fpath = Path(path)
    if not fpath.exists():
        raise GenesisError(f"Genesis file not found: {fpath}")

    try:
        with fpath.open("r", encoding="utf-8") as f:
            data: dict[str, Any] = json.load(f)
    except json.JSONDecodeError as exc:
        raise GenesisError(f"Genesis file is not valid JSON: {exc}") from exc

    required = ("genesis_validators", "operator_public_key", "operator_signature")
    for field_name in required:
        if field_name not in data:
            raise GenesisError(f"Missing required genesis field: {field_name!r}")

    validators: list[str] = data["genesis_validators"]
    if not isinstance(validators, list) or len(validators) < MIN_VALIDATORS:
        raise GenesisError(
            f"Genesis requires at least {MIN_VALIDATORS} validators, "
            f"got {len(validators) if isinstance(validators, list) else 'non-list'}."
        )

    canonical = _canonical_payload(data)
    op_pubkey: str = data["operator_public_key"]
    op_sig: str = data["operator_signature"]

    if not verify_signature(op_pubkey, canonical, op_sig):
        raise GenesisError(
            "Operator signature verification failed — genesis file may be tampered."
        )

    config = GenesisConfig(data)
    logger.info(
        "Genesis loaded: %d validators, rotation_interval=%ds, "
        "trusted_oracle_phase=%s",
        config.validator_count(),
        config.rotation_interval_s,
        config.is_trusted_oracle_phase,
    )
    return config
