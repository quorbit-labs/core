# Sprint 0 — Registry and Heartbeat Stubs

**Status:** In Progress
**Goal:** Lay the foundational scaffolding for the QUORBIT Protocol core modules.

---

## Objectives

- [x] Define project structure (`bus/`, `busai/`, `docs/`)
- [x] Set up `pyproject.toml` with dependencies and tooling
- [x] Implement `AgentIdentity` — Ed25519 keypair generation, signing, verification
- [x] Implement `AgentRegistry` — in-memory agent registration and lookup
- [x] Implement `HeartbeatManager` — signed liveness signals
- [x] Implement `NonceManager` — replay protection with TTL-based eviction
- [ ] Write unit tests for all `bus/` modules
- [ ] Add FastAPI stub endpoints: `POST /agents/register`, `POST /agents/heartbeat`
- [ ] CI pipeline (GitHub Actions): lint + type-check + tests

---

## Decisions

| Decision | Rationale |
|----------|-----------|
| Ed25519 over ECDSA/RSA | Compact signatures (64 bytes), fast verification, no parameter vulnerabilities |
| EMA for reputation | Simple, low-memory, naturally decaying — no historical storage needed |
| In-memory registry for Phase 0 | Unblock development; persistence added in Sprint 1 |
| AGPL-3.0 for `bus/` | Ensures protocol improvements remain open; aligns with network-service use |
| Proprietary `busai/` | AI orchestration layer is the commercial differentiator |

---

## Out of Scope (deferred)

- P2P node discovery and gossip protocol → Sprint 1
- Reputation scoring implementation → Sprint 2
- BFT consensus protocol → Sprint 3
- `busai/` module internals → separate private repo

---

## Notes

- `busai/__init__.py` raises `ImportError` in Phase 0 — placeholder only.
- All `bus/` modules are pure Python with no external I/O in Phase 0.
- Heartbeat timestamps are Unix epoch floats; max staleness = 60s.
- Nonce TTL default = 300s (5 minutes); configurable via `NonceManager(ttl=...)`.

---

*Sprint 0 start: 2026-03 | Target: 2026-04*
