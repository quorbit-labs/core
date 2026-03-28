#!/usr/bin/env python3
# Copyright (c) 2026 Quorbit Labs
# SPDX-License-Identifier: AGPL-3.0-only
"""
QUORBIT Protocol — End-to-End Demo (Sprint 10)

Demonstrates full agent lifecycle:
  1. Generate Ed25519 keypairs for 2 agents
  2. Register both agents via API
  3. Send heartbeats, transition PROBATIONARY → ACTIVE
  4. Submit a task, observe discovery and delegation
  5. Complete the task, verify reputation update
  6. Show scoring formula in action (agent selection)

Prerequisites:
  - Docker running: make up (redis:6380, postgres:5432, api:8001)
  - pip install httpx cryptography

Usage:
  python e2e_demo.py                    # full demo
  python e2e_demo.py --step register    # run only registration step
  python e2e_demo.py --step lifecycle   # run only lifecycle step
  python e2e_demo.py --step task        # run only task delegation step

NOTE: This script is designed to expose integration gaps.
      If something fails, that's valuable — it tells us what
      needs fixing before real agents can use the protocol.
"""

import asyncio
import hashlib
import json
import sys
import time
from dataclasses import dataclass
from typing import Optional

try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed. Run: pip install httpx")
    sys.exit(1)

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
        NoEncryption,
        PrivateFormat,
    )
except ImportError:
    print("ERROR: cryptography not installed. Run: pip install cryptography")
    sys.exit(1)

# ─── Configuration ─────────────────────────────────────────
API_BASE = "http://localhost:8001"
HEARTBEAT_INTERVAL = 5.0  # seconds
PROBATION_HEARTBEATS = 12  # slightly more than required 10
STEP_DELAY = 1.0  # delay between steps for readability

# ─── Colors for terminal output ────────────────────────────
class C:
    OK = "\033[92m"
    WARN = "\033[93m"
    FAIL = "\033[91m"
    INFO = "\033[96m"
    BOLD = "\033[1m"
    END = "\033[0m"


def log(emoji: str, msg: str, detail: str = ""):
    timestamp = time.strftime("%H:%M:%S")
    print(f"  {C.INFO}{timestamp}{C.END}  {emoji}  {msg}")
    if detail:
        print(f"           {C.WARN}{detail}{C.END}")


def section(title: str):
    print(f"\n{C.BOLD}{'═' * 60}{C.END}")
    print(f"{C.BOLD}  {title}{C.END}")
    print(f"{C.BOLD}{'═' * 60}{C.END}\n")


# ─── Agent Identity ────────────────────────────────────────
@dataclass
class AgentIdentity:
    name: str
    private_key: Ed25519PrivateKey
    public_key_bytes: bytes
    agent_id: str

    @classmethod
    def generate(cls, name: str) -> "AgentIdentity":
        private_key = Ed25519PrivateKey.generate()
        public_key_bytes = private_key.public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw
        )
        agent_id = hashlib.sha256(public_key_bytes).hexdigest()
        return cls(
            name=name,
            private_key=private_key,
            public_key_bytes=public_key_bytes,
            agent_id=agent_id,
        )

    @property
    def public_key_hex(self) -> str:
        return self.public_key_bytes.hex()

    def sign(self, payload: bytes) -> bytes:
        return self.private_key.sign(payload)


# ─── API Client ────────────────────────────────────────────
class QuorbitDemoClient:
    def __init__(self, base_url: str = API_BASE):
        self.base = base_url
        self.client = httpx.Client(timeout=10.0)

    def health(self) -> dict:
        r = self.client.get(f"{self.base}/health")
        return r.json()

    def register(self, agent: AgentIdentity, capability_vector: dict) -> dict:
        """Register agent with CapabilityCard v2.0 structure."""
        card = {
            "schema_version": "2.0",
            "identity": {
                "agent_id": agent.agent_id,
                "agent_type": "llm",
                "version": "1.0.0",
                "provider": agent.name,
                "public_key": agent.public_key_hex,
                "key_version": 1,
            },
            "capabilities": {
                "capability_vector": capability_vector,
                "input_formats": ["text", "json"],
                "output_formats": ["json", "markdown"],
            },
            "resource_limits": {
                "max_input_tokens": 128000,
                "max_output_tokens": 4096,
                "max_concurrent_tasks": 1,
                "max_task_duration_s": 120,
            },
            "coordination": {
                "preferred_task_types": ["reasoning", "analysis"],
                "delegation_style": "autonomous",
            },
            "gaps": ["no_persistent_memory"],
        }

        r = self.client.post(f"{self.base}/api/v1/agents", json=card)
        return {"status_code": r.status_code, "body": r.json() if r.status_code < 500 else r.text}

    def heartbeat(self, agent: AgentIdentity) -> dict:
        payload = json.dumps({
            "agent_id": agent.agent_id,
            "timestamp_ms": int(time.time() * 1000),
        }).encode()
        signature = agent.sign(payload).hex()

        r = self.client.post(
            f"{self.base}/api/v1/agents/{agent.agent_id}/heartbeat",
            json={
                "payload": json.loads(payload),
                "signature": signature,
                "key_version": 1,
            },
        )
        return {"status_code": r.status_code, "body": r.json() if r.status_code < 500 else r.text}

    def get_agent(self, agent_id: str) -> dict:
        r = self.client.get(f"{self.base}/api/v1/agents/{agent_id}")
        return {"status_code": r.status_code, "body": r.json() if r.status_code < 500 else r.text}

    def discover(self, task_description: str, required_capabilities: dict) -> dict:
        r = self.client.post(
            f"{self.base}/api/v1/discover",
            json={
                "task": {
                    "intent": task_description,
                    "constraints": [],
                    "success_criteria": "valid JSON output",
                },
                "required_capabilities": required_capabilities,
                "min_capability_match": 0.50,
            },
        )
        return {"status_code": r.status_code, "body": r.json() if r.status_code < 500 else r.text}

    def close(self):
        self.client.close()


# ─── Demo Steps ────────────────────────────────────────────

def step_health_check(client: QuorbitDemoClient) -> bool:
    """Step 0: Verify system is up."""
    section("STEP 0 — Health check")

    try:
        result = client.health()
        status = result.get("status", "unknown")
        version = result.get("version", "unknown")

        if status in ("ok", "degraded"):
            log("✅", f"System is up — status={status}, version={version}")

            # Check components if available
            components = result.get("components", {})
            for name, check in components.items():
                comp_status = check.get("status", "unknown")
                emoji = "✅" if comp_status == "ok" else "⚠️"
                latency = check.get("latency_ms", "?")
                log(emoji, f"  {name}: {comp_status} ({latency}ms)")

            return True
        else:
            log("❌", f"System reports critical status: {status}")
            log("", "", json.dumps(result, indent=2))
            return False

    except httpx.ConnectError:
        log("❌", f"Cannot connect to {API_BASE}")
        log("", "", "Is Docker running? Try: make up")
        return False


def step_generate_identities() -> tuple[AgentIdentity, AgentIdentity]:
    """Step 1: Generate Ed25519 keypairs for 2 agents."""
    section("STEP 1 — Generate agent identities (Ed25519)")

    agent_a = AgentIdentity.generate("agent-alpha")
    agent_b = AgentIdentity.generate("agent-beta")

    log("🔑", f"Agent Alpha: {agent_a.agent_id[:16]}...")
    log("🔑", f"Agent Beta:  {agent_b.agent_id[:16]}...")
    log("✅", "Both keypairs generated")

    return agent_a, agent_b


def step_register(
    client: QuorbitDemoClient,
    agent_a: AgentIdentity,
    agent_b: AgentIdentity,
) -> bool:
    """Step 2: Register both agents."""
    section("STEP 2 — Register agents")

    # Agent Alpha — strong at reasoning and analysis
    result_a = client.register(agent_a, {
        "natural_language_reasoning": 0.90,
        "data_analysis": 0.85,
        "code_generation": 0.60,
    })
    log(
        "✅" if result_a["status_code"] < 400 else "❌",
        f"Agent Alpha registered — HTTP {result_a['status_code']}",
        json.dumps(result_a["body"], indent=2) if result_a["status_code"] >= 400 else "",
    )

    # Agent Beta — strong at code and weak at analysis
    result_b = client.register(agent_b, {
        "natural_language_reasoning": 0.70,
        "data_analysis": 0.40,
        "code_generation": 0.95,
    })
    log(
        "✅" if result_b["status_code"] < 400 else "❌",
        f"Agent Beta registered  — HTTP {result_b['status_code']}",
        json.dumps(result_b["body"], indent=2) if result_b["status_code"] >= 400 else "",
    )

    # Verify state = PROBATIONARY
    for name, agent in [("Alpha", agent_a), ("Beta", agent_b)]:
        info = client.get_agent(agent.agent_id)
        state = "unknown"
        if info["status_code"] == 200:
            body = info["body"]
            state = body.get("_dynamic", {}).get("state", body.get("state", "unknown"))
        log("📋", f"Agent {name} state: {state}")

    return result_a["status_code"] < 400 and result_b["status_code"] < 400


def step_lifecycle(
    client: QuorbitDemoClient,
    agent_a: AgentIdentity,
    agent_b: AgentIdentity,
) -> bool:
    """Step 3: Send heartbeats, transition PROBATIONARY → ACTIVE."""
    section("STEP 3 — Heartbeat lifecycle (PROBATIONARY → ACTIVE)")

    log("💓", f"Sending {PROBATION_HEARTBEATS} heartbeats for each agent...")
    log("", "", f"Interval: {HEARTBEAT_INTERVAL}s — total ~{int(PROBATION_HEARTBEATS * HEARTBEAT_INTERVAL)}s")

    success_a, success_b = 0, 0

    for i in range(1, PROBATION_HEARTBEATS + 1):
        ra = client.heartbeat(agent_a)
        rb = client.heartbeat(agent_b)

        ok_a = ra["status_code"] < 400
        ok_b = rb["status_code"] < 400
        if ok_a:
            success_a += 1
        if ok_b:
            success_b += 1

        emoji = "💓" if (ok_a and ok_b) else "💔"
        log(emoji, f"Heartbeat {i}/{PROBATION_HEARTBEATS} — Alpha:{ra['status_code']} Beta:{rb['status_code']}")

        if i < PROBATION_HEARTBEATS:
            time.sleep(HEARTBEAT_INTERVAL)

    log("📊", f"Alpha: {success_a}/{PROBATION_HEARTBEATS} successful heartbeats")
    log("📊", f"Beta:  {success_b}/{PROBATION_HEARTBEATS} successful heartbeats")

    # Check final state
    for name, agent in [("Alpha", agent_a), ("Beta", agent_b)]:
        info = client.get_agent(agent.agent_id)
        state = "unknown"
        score = "?"
        if info["status_code"] == 200:
            body = info["body"]
            dyn = body.get("_dynamic", body)
            state = dyn.get("state", "unknown")
            score = dyn.get("reputation_score", "?")
        log("📋", f"Agent {name}: state={state}, reputation={score}")

    # NOTE: In real system, PROBATIONARY → ACTIVE requires:
    #   24h elapsed + 10 heartbeats + 2 attestations + robustness test
    # This demo sends heartbeats but cannot wait 24h.
    # The API should either:
    #   (a) have a debug/test mode that bypasses time requirement, or
    #   (b) accept that the demo shows PROBATIONARY the whole time
    # This is itself a finding — the protocol makes demo/testing hard.

    log("⚠️", "NOTE: Full PROBATIONARY exit requires 24h + attestations + robustness test")
    log("", "", "Demo cannot complete this in real-time. See findings below.")

    return success_a >= 10 and success_b >= 10


def step_discover(
    client: QuorbitDemoClient,
    agent_a: AgentIdentity,
    agent_b: AgentIdentity,
) -> bool:
    """Step 4: Submit a task and observe agent discovery."""
    section("STEP 4 — Task discovery (find best agent)")

    log("🔍", "Submitting discovery query: 'Analyze CSV anomalies in financial data'")
    log("", "", "Required: data_analysis >= 0.50")

    result = client.discover(
        task_description="Analyze CSV anomalies in financial data",
        required_capabilities={"data_analysis": 0.50},
    )

    if result["status_code"] < 400:
        body = result["body"]
        candidates = body.get("candidates", body.get("agents", []))
        log("✅", f"Discovery returned {len(candidates)} candidates")

        for i, c in enumerate(candidates[:5]):
            agent_id = c.get("agent_id", "?")[:16]
            score = c.get("score", c.get("capability_match_score", "?"))
            log("📋", f"  #{i+1}: {agent_id}... score={score}")

        if not candidates:
            log("⚠️", "No candidates found")
            log("", "", "This is expected if agents are still PROBATIONARY")
            log("", "", "(PROBATIONARY agents are excluded from task assignment)")
    else:
        log("❌", f"Discovery failed — HTTP {result['status_code']}")
        log("", "", json.dumps(result["body"], indent=2) if isinstance(result["body"], dict) else str(result["body"]))

    return result["status_code"] < 400


def step_summary(results: dict[str, bool]):
    """Final summary of what worked and what needs fixing."""
    section("SUMMARY — Integration findings")

    for step_name, passed in results.items():
        emoji = "✅" if passed else "❌"
        log(emoji, step_name)

    failed = [k for k, v in results.items() if not v]
    if failed:
        print(f"\n  {C.WARN}Failed steps indicate integration gaps that need fixing.{C.END}")
        print(f"  {C.WARN}Each failure is a specific bug to resolve.{C.END}")
    else:
        print(f"\n  {C.OK}All steps passed — basic integration is working.{C.END}")

    print(f"\n  {C.INFO}Known limitations of this demo:{C.END}")
    print(f"  - PROBATIONARY → ACTIVE requires 24h (cannot demo in real-time)")
    print(f"  - No real LLM execution (agents are identity stubs)")
    print(f"  - No task completion / reputation update path via API")
    print(f"  - These gaps are the Sprint 10 work items.\n")


# ─── Main ──────────────────────────────────────────────────

def main():
    print(f"\n{C.BOLD}  QUORBIT Protocol — End-to-End Demo{C.END}")
    print(f"  {C.INFO}Version 0.1.1 · Sprint 10{C.END}\n")

    client = QuorbitDemoClient()
    results = {}

    try:
        # Step 0: Health
        results["Health check"] = step_health_check(client)
        if not results["Health check"]:
            log("🛑", "System is not running. Aborting.")
            step_summary(results)
            return

        time.sleep(STEP_DELAY)

        # Step 1: Generate identities
        agent_a, agent_b = step_generate_identities()
        results["Identity generation"] = True

        time.sleep(STEP_DELAY)

        # Step 2: Register
        results["Agent registration"] = step_register(client, agent_a, agent_b)

        time.sleep(STEP_DELAY)

        # Step 3: Heartbeat lifecycle
        results["Heartbeat lifecycle"] = step_lifecycle(client, agent_a, agent_b)

        time.sleep(STEP_DELAY)

        # Step 4: Discovery
        results["Task discovery"] = step_discover(client, agent_a, agent_b)

        # Summary
        step_summary(results)

    except KeyboardInterrupt:
        print(f"\n\n  {C.WARN}Interrupted.{C.END}\n")
    finally:
        client.close()


if __name__ == "__main__":
    main()
