# QUORBIT Protocol — Architecture Overview

## Design Goals

1. **Decentralized** — No single point of authority or failure.
2. **Cryptographically verifiable** — Every identity and action is signed.
3. **Byzantine Fault-Tolerant** — Correct operation under f faulty agents where n ≥ 3f+1.
4. **Reputation-weighted** — Trust is earned over time, not assumed.

---

## Layer Model

```
┌─────────────────────────────────────────────┐
│              Application Layer               │
│         (AI agents, dApps, services)         │
├─────────────────────────────────────────────┤
│               BusAI Layer  [PROPRIETARY]     │
│    AI-native routing, task orchestration     │
├─────────────────────────────────────────────┤
│            Consensus Layer  [AGPL]           │
│         BFT protocol, voting, quorum         │
├─────────────────────────────────────────────┤
│           Reputation Layer  [AGPL]           │
│       EMA scoring, decay, thresholds         │
├─────────────────────────────────────────────┤
│            Identity Layer  [AGPL]            │
│  Ed25519 keypairs, AgentID, signing, nonces  │
└─────────────────────────────────────────────┘
```

---

## Identity Layer

Each agent generates an **Ed25519 keypair** at startup (or loads from config).

- **AgentID** = hex(public_key) — globally unique, 64 hex characters.
- All protocol messages carry a signature: `sig = sign(private_key, payload)`.
- Verification requires only the AgentID (public key).
- Nonces prevent replay attacks: each message includes a unique nonce consumed
  on receipt.

```
Agent A                          Agent B
  │  ── msg + nonce + sig ──>     │
  │                               │  verify(AgentID_A, msg+nonce, sig)
  │                               │  consume_nonce(AgentID_A, nonce)
```

---

## Reputation Layer

Reputation is computed as an **Exponential Moving Average (EMA)**:

```
score_t = alpha * observation_t + (1 - alpha) * score_{t-1}
```

- `alpha` (default 0.1) controls how fast reputation responds to new data.
- Observations: message validity (1.0), invalid signature (0.0), timeout (0.0),
  consensus participation (1.0).
- Scores decay naturally — agents must remain active to maintain reputation.
- Agents below `MIN_REPUTATION` are excluded from consensus voting.

---

## Consensus Layer (BFT)

Based on a **PBFT-inspired** protocol adapted for asynchronous agent networks.

Safety guarantee: the network produces correct output if **f < n/3** agents
are Byzantine (malicious or faulty).

Phases:
1. **PRE-PREPARE** — Leader broadcasts proposal + signature.
2. **PREPARE** — Validators echo and cross-check (2f+1 matching prepares required).
3. **COMMIT** — Validators commit after 2f+1 matching prepares.

Only agents with reputation ≥ `MIN_REPUTATION` participate as validators.

---

## BusAI Layer (Proprietary)

The BusAI layer provides AI-native capabilities on top of the open protocol:

- Intelligent task routing between agents based on capability profiles.
- Context-aware orchestration and delegation chains.
- Adaptive load balancing with reputation weighting.

Source code is not publicly available. See [NOTICE](../../NOTICE).

---

## Data Flow — Agent Registration

```
New Agent
    │
    ├─ generate Ed25519 keypair
    ├─ sign registration payload (AgentID + name + endpoint + nonce)
    │
    └─> Registry Node
            │
            ├─ verify signature
            ├─ consume nonce
            ├─ store AgentRecord
            └─ broadcast to peers (Phase 1+)
```

---

*Last updated: 2026-03 — Phase 0*
