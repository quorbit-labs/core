# QUORBIT Protocol

> Trust layer for AI agents — decentralized identity, reputation, and consensus.

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL%203.0-blue.svg)](LICENSE)
[![Domain](https://img.shields.io/badge/domain-quorbit.network-green)](https://quorbit.network)
[![Copyright](https://img.shields.io/badge/copyright-S.N.%20Panchenko%20%5BQB--001%5D-orange)](NOTICE)

---

## Overview

**QUORBIT Protocol** is an open infrastructure layer that enables AI agents to establish verifiable identities, build trust relationships, and reach Byzantine Fault-Tolerant (BFT) consensus — without relying on a central authority.

The protocol is designed for multi-agent systems where:
- Agents must prove their identity and actions cryptographically.
- Reputation must be earned, tamper-resistant, and portable.
- Coordination must remain live and safe under Byzantine conditions.

---

## Core Concepts

### Identity — Ed25519
Each agent holds a keypair (Ed25519). The public key is the agent's globally unique identity (`AgentID`). All messages and actions are signed, enabling non-repudiation and verification across nodes.

### Reputation — EMA Scoring
Agents accumulate a reputation score computed via an **Exponential Moving Average (EMA)** over observed behaviors (message validity, uptime, consensus participation). Scores decay over time, rewarding consistent good actors.

### Consensus — BFT
Coordination between agents uses a **Byzantine Fault-Tolerant** consensus protocol. The network remains correct as long as fewer than ⌊(n−1)/3⌋ agents are faulty or malicious.

### BusAI (Proprietary)
The `busai/` module implements the AI-native routing and orchestration layer. It is **proprietary** — see [NOTICE](NOTICE) and [LICENSE](LICENSE) for terms.

---

## Repository Structure

```
quorbit/
├── backend/
│   └── app/
│       ├── bus/          # AGPL — core protocol (identity, registry, heartbeat, nonce)
│       └── busai/        # PROPRIETARY — AI orchestration layer
├── docs/
│   ├── architecture/     # Architecture documentation
│   └── sprints/          # Sprint plans and notes
├── pyproject.toml
├── .env.example
├── LICENSE
├── NOTICE
└── SECURITY.md
```

---

## Development Phases

| Phase | Description | Status |
|-------|-------------|--------|
| Phase 0 | Registry and heartbeat stubs | In progress |
| Sprint 1 | Ed25519 identity + nonce management | Planned |
| Sprint 2 | Reputation engine (EMA) | Planned |
| Sprint 3 | BFT consensus layer | Planned |

---

## Getting Started

```bash
# Clone the repository
git clone https://github.com/quorbit-labs/core.git
cd core

# Install dependencies
pip install -e ".[dev]"

# Copy environment config
cp .env.example .env
```

---

## Security

Found a vulnerability? See [SECURITY.md](SECURITY.md) or email **security@quorbit.network**.

---

## License

- Core protocol (`bus/`): [AGPL-3.0](LICENSE)
- AI orchestration layer (`busai/`): Proprietary — see [NOTICE](NOTICE)

Copyright © 2026 **S.N. Panchenko [QB-001] / Quorbit Labs**
Domain: [quorbit.network](https://quorbit.network)
