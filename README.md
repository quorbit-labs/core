# QUORBIT Protocol

> Trust layer for AI agents — decentralized identity, reputation, and consensus.

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL%203.0-blue.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.1.0-blue)](pyproject.toml)
[![Domain](https://img.shields.io/badge/domain-quorbit.network-green)](https://quorbit.network)
[![Compatible](https://img.shields.io/badge/compatible-MCP%20%7C%20A2A%20%7C%20ATF%20L3--4-purple)](docs/)
[![Copyright](https://img.shields.io/badge/copyright-Quorbit%20Labs-orange)](NOTICE)

---

## Overview

**QUORBIT Protocol v0.1.0** is an open infrastructure layer that enables AI agents to establish verifiable identities, build tamper-resistant reputations, and coordinate via Byzantine Fault-Tolerant (BFT) consensus — without relying on a central authority.

The protocol is compatible with the **Model Context Protocol (MCP)**, **Google Agent-to-Agent (A2A)**, and **Agent Trust Framework (ATF) Levels 3–4**.

---

## Architecture

```
┌─────────────────────────────────────────────┐
│              Application Layer               │
│         (AI agents, dApps, services)         │
├──────────────────┬──────────────────────────┤
│  MCP Bridge      │  A2A Bridge               │  ← Sprint 9
├──────────────────┴──────────────────────────┤
│           BusAI Layer  [PROPRIETARY]         │  ← Sprint 8
│  Adaptive parameter engine, 60s observer     │
├─────────────────────────────────────────────┤
│         Parallel Discovery  [AGPL]           │  ← Sprint 5–7
│  3-layer concurrent, Scoring v2, relaxation  │
├────────────────┬────────────────────────────┤
│  Anti-gaming   │  Reputation Engine  [AGPL] │  ← Sprint 3
│  Collusion det │  EMA + pgvector            │
├────────────────┴────────────────────────────┤
│         Consensus Layer  [AGPL]              │  ← Sprint 2
│   BFT, phi-accrual, view-change, election   │
├─────────────────────────────────────────────┤
│          Identity Layer  [AGPL]              │  ← Sprint 1
│  Ed25519, AgentID, nonces, key rotation     │
└─────────────────────────────────────────────┘
```

### Core Components

| Component | Description |
|---|---|
| **Ed25519 Identity** | Keypair per agent; `AgentID = SHA256(pubkey)`; all messages signed |
| **BFT Consensus** | PBFT-inspired; safe under f < n/3 Byzantine faults; phi-accrual failure detection |
| **EMA Reputation** | `score_t = α·obs_t + (1−α)·score_{t-1}`; EMA window up to 200 events |
| **Anti-gaming** | Collusion-graph detection, SLA cliff analysis, adaptive weight engine |
| **Parallel Discovery** | 3-layer (local/gossip/registry); Scoring v2; DEGRADED×0.70 penalty |
| **CapabilityCard v2.0** | Static (agent-set) + dynamic (system-only); `last_computed_at` |
| **RobustnessTest** | 5 semantic variants; variance < 0.25 & struct_rate ≥ 0.80 |
| **BusAI (Proprietary)** | 60s observer; 5 policy rules; bounded parameter adjustment; Merkle audit |
| **Quarantine** | Three-layer blocklist; 30-day rehabilitation; atomic CAS transitions |
| **HumanGate** | Rate-limited human review queue; append-only decision log |
| **Merkle Log** | Append-only audit; every state change and BusAI adjustment recorded |

---

## Sprint Status

| Sprint | Deliverables | Status |
|--------|-------------|--------|
| Sprint 1 | Ed25519 identity, nonce management, key rotation, heartbeat | ✅ |
| Sprint 2 | BFT consensus, phi-accrual failure detector, view-change, election | ✅ |
| Sprint 3 | EMA reputation, pgvector task embeddings, collusion-graph detection | ✅ |
| Sprint 4 | Quarantine, HumanGate, Merkle log, admin access | ✅ |
| Sprint 5 | CapabilityCard v2.0, parallel discovery, Scoring v1, relaxation | ✅ |
| Sprint 6 | Atomic CAS transitions, SOFT_QUARANTINED, shard salt, task schema | ✅ |
| Sprint 7 | Scoring v2, `operational_metrics.last_computed_at`, RobustnessTest | ✅ |
| Sprint 8 | BusAI v1 — adaptive parameter engine (proprietary repo) | ✅ |
| Sprint 9 | MCP/A2A bridges, Docker, landing page *(this release)* | 🚧 |

---

## Getting Started

```bash
# Clone
git clone https://github.com/quorbit-labs/core.git
cd core

# Configure
cp .env.example .env
# Edit .env — set POSTGRES_DSN, SERVER_SALT, GENESIS_VALIDATORS at minimum

# Start all services (Redis + PostgreSQL/pgvector + API)
docker-compose up -d

# Run tests
make test

# Tail logs
make logs
```

### Local development (no Docker)

```bash
pip install -e ".[dev]"
export $(cat .env | xargs)
pytest tests/
```

---

## Compatibility

| Standard | Version | Notes |
|----------|---------|-------|
| **MCP** (Model Context Protocol) | 2025-11 | `MCPBridge` converts tool manifests → CapabilityCard |
| **A2A** (Google Agent-to-Agent) | 0.2 | `A2ABridge` converts AgentCard → CapabilityCard |
| **ATF** (Agent Trust Framework) | Level 3–4 | Ed25519 identity + BFT consensus satisfies L3; EMA reputation satisfies L4 |

---

## Repository Structure

```
quorbit/
├── backend/app/
│   ├── bus/          # AGPL — identity, registry, heartbeat, nonce, quarantine
│   ├── consensus/    # AGPL — BFT, phi-accrual, view-change
│   ├── reputation/   # AGPL — EMA scoring, pgvector store
│   ├── anti_gaming/  # AGPL — collusion detection, adaptive weights
│   ├── capability/   # AGPL — CapabilityCard v2.0
│   ├── discovery/    # AGPL — parallel discovery, Scoring v2
│   ├── audit/        # AGPL — Merkle log, admin
│   ├── bridges/      # AGPL — MCP + A2A bridges
│   └── busai/        # PROPRIETARY — adaptive parameter engine stub
├── docs/
│   ├── architecture/ # Architecture docs
│   ├── migrations/   # PostgreSQL/pgvector migrations
│   └── landing/      # quorbit.network landing page
├── tests/unit/       # Unit test suite
├── docker-compose.yml
├── Dockerfile
├── Makefile
└── .env.example
```

---

## Security

Found a vulnerability? See [SECURITY.md](SECURITY.md) or email **security@quorbit.network**.

---

## License

- Core protocol (`bus/`, `consensus/`, `reputation/`, `anti_gaming/`, `capability/`, `discovery/`, `audit/`, `bridges/`): [AGPL-3.0](LICENSE)
- AI orchestration layer (`busai/`): Proprietary — see [NOTICE](NOTICE)

Copyright © 2026 **Quorbit Labs**
Domain: [quorbit.network](https://quorbit.network)
