---BEGIN QUORBIT_ARCH_V3.1---
PROJECT:        QUORBIT Protocol
VERSION:        3.1.1
DATE:           2026-03-22
SOURCES:        v3.0 + cross-model research P1-P5
                (DeepSeek / Kimi / Grok / GPT — 37 deltas)
STATUS:         Sprints 1-9 complete. Sprint 9.5 (fixes) in progress.
REPO:           github.com/quorbit-labs/core (AGPL-3.0)
DOMAIN:         quorbit.network

═══════════════════════════════════════
STACK
═══════════════════════════════════════
runtime:        FastAPI + WebSocket + React/TypeScript
storage:        Redis DB=0 (bus) + Redis DB=1 (nonce)
                PostgreSQL + pgvector (semantic discovery)
agents:         Ollama(local) + OpenRouter + Groq + Gemini
crypto:         Ed25519 (identity) + HMAC-SHA256 (nonce)
infra:          Docker Compose → Kubernetes (Phase 3+)

═══════════════════════════════════════
IDENTITY LAYER  [NEW — D1, SPRINT 1]
═══════════════════════════════════════
Every agent MUST have Ed25519 keypair before registration:
  agent_id     = SHA256(public_key)
  All messages signed: {payload, sender_id, signature,
                         key_version, timestamp_ms}
  Key TTL:     7 days max. Rotation 24h before expiry.
  CRL:         Revocation list gossiped every 60s.
               QUARANTINE → forced key revoke.
  Multi-sig:   ≥2 operators for emergency revoke.

═══════════════════════════════════════
AGENT STATES
═══════════════════════════════════════
PROBATIONARY → ACTIVE → DEGRADED → ISOLATED
            → SOFT_QUARANTINED → QUARANTINED

  PROBATIONARY:  heartbeat only, NO task assignment
                 until first HB ACK confirmed
  ACTIVE:        score ≥ 0.50, heartbeat OK
  DEGRADED:      score [0.20, 0.50)
  ISOLATED:      network-level flag, any score
  SOFT_QUARANT:  score < 0.35, restricted tasks only
  QUARANTINED:   score < 0.20 OR 3× breach
                 forced key revoke + blocklist all layers

Reputation scoring DOES NOT apply during PROBATIONARY.
  Agent enters with initial_score=0.75 (stored but not evaluated).
  Score-based transitions activate only after PROBATIONARY exit.

Genesis validators bypass eligibility criteria (D4 override):
  genesis config explicitly lists validator agent_ids.
  Normal eligibility (tasks≥50, age≥72h) applies only to
  non-genesis validators elected via VRF (D7).

STATE TRANSITION MATRIX  [NEW — Sprint 9.5]
─────────────────────────────────────────────
From → To             Trigger                          Reversible?
──────────────────────────────────────────────────────────────────
PROBATIONARY → ACTIVE   24h + 10 HB + 2 attestations    NO
                        + robustness test passed (D22)
ACTIVE → DEGRADED       Phi(agent) > 8                   YES
                        OR score drops to [0.20, 0.50)
ACTIVE → ISOLATED       Phi(agent) > 16                  YES
                        OR network partition detected
DEGRADED → ACTIVE       score recovers ≥ 0.50            YES
                        AND Phi(agent) < 8
DEGRADED → ISOLATED     Phi(agent) > 16                  YES
ISOLATED → DEGRADED     network restored                 YES
                        AND Phi(agent) < 16
ISOLATED → SOFT_QUARAN  score < 0.35 while ISOLATED      YES
                        (checked on network restore)
DEGRADED → SOFT_QUARAN  score < 0.35                     YES
SOFT_QUARAN → DEGRADED  score recovers to [0.35, 0.50)   YES
                        within 30 min window
SOFT_QUARAN → QUARANTIN  30 min elapsed without recovery  NO*
                        OR score < 0.20 OR 3× breach
ANY → QUARANTINED       operator emergency action         NO*
                        (multi-sig ≥2 operators)

* QUARANTINED recovery: rehabilitation process (D21)
  7-day cooldown → REHABILITATION_REQUEST → human review
  → if approved: new keypair, PROBATIONARY, score=0.30

INVALID transitions (must be rejected by state_machine.py):
  PROBATIONARY → DEGRADED  (no score evaluation in probation)
  PROBATIONARY → ISOLATED  (heartbeat loss → stay PROBATIONARY, extend)
  QUARANTINED → anything   (except via rehabilitation → new agent_id)
  ACTIVE → QUARANTINED     (must pass through SOFT_QUARANTINED first,
                            unless operator emergency action)

═══════════════════════════════════════
HEARTBEAT  [UPDATED — D10]
═══════════════════════════════════════
  interval:   5s active ping, server-enforced
  TTL Redis:  15s (3 missed = DEGRADED)
  algorithm:  Phi Accrual (replaces fixed threshold)
  jitter:     ±500ms random to prevent thundering herd
  circuit:    if >40% agents ISOLATED in <60s → FREEZE

═══════════════════════════════════════
REGISTRY  [UPDATED — D3]
═══════════════════════════════════════
  authoritative: Registry = ONLY source of truth
                 Write ONLY via Registry API
  Local layer:   read-only mirror, no direct registration
  Bazaar layer:  read-only mirror + Merkle proofs
                 Each gossiped card carries registry_signature
  Sharding:      consistent hash, max 200 agents/shard
                 shard_key = HMAC(agent_id, server_salt)
  Reconcile:     record in Local/Bazaar absent from Registry
                 → rejected + eclipse attempt alert

Redis key schema:
  bus:capability:{agent_id}         Hash, TTL=300s
  bus:task:{task_id}                Hash, TTL=task.ttl
  bus:claim:{task_id}               String NX, TTL=600s
  bus:session:{id}:history          List, TTL=86400s
  bus:reputation:{agent_id}         Float, no TTL
  bus:genesis_validators            Set (signed config)
  bus:crl:{key_id}                  String, TTL=key_ttl
  humangate:pending                 List
  [DB=1] nonce:{wf_id}:{step}       String NX, TTL=30s

═══════════════════════════════════════
NONCE STORE  [UPDATED — D2]
═══════════════════════════════════════
  primary:    stateless HMAC nonce
              = HMAC_SHA256(agent_id + floor(ts/30) + counter,
                            server_secret)
              verify: recalculate + check ±30s window
  storage:    Redis DB=1 isolated process, CPU reserved
  on-use:     explicit DEL (not TTL expiry)
  rate-limit: max 10 nonce requests/sec per agent_id

═══════════════════════════════════════
CONSENSUS  [UPDATED — D4, D7, D8]
═══════════════════════════════════════
  quorum:     floor(2n/3) + 1  (exact formula)
  pool size:  min 11 normal, min 7 emergency mode
  election:   VRF on last consensus hash, every 1h
  eligibility: state=ACTIVE, score≥0.70, age≥72h,
               tasks≥50, no quarantine in 30d
  view-change: if quorum not reached in 30s → new election
               if <11 eligible → emergency mode (flagged)
  genesis:     signed config, 11 bootstrap validators
               rotation: 1 replaced/24h from top reputation
               first 1000 tasks: trusted oracle seeding

═══════════════════════════════════════
REPUTATION  [UPDATED — D11, R8]
═══════════════════════════════════════
  score:      float [0.0–1.0], initial=0.75
  formula:    reputation = EMA(task_outcomes)*0.70
                         + failure_transparency*0.30  [NEW R8]
  algorithm:  EMA window 100 tasks
  storage:    Redis (live) + pgvector append-only (history)
  deltas:     completed_on_time  +0.05
              completed_late     +0.01
              abandoned          -0.10
              validated          +0.05
              flagged            -0.15
              heartbeat_missed   -0.02
  transparency: structured_error_response  +0.02  [NEW R8]
                result_marked_incorrect    -0.05
                confirmed_hallucination    -0.15
                fabricated_result          -0.30
  divergence: rolling 20 rounds, auto-PROBATIONARY at >2σ

═══════════════════════════════════════
ANTI-GAMING  [UPDATED — D12, D17]
═══════════════════════════════════════
  detectors:  collusion_graph  weight=50  window=20
              graph_symmetry   weight=30  window=50
              sla_cliff        weight=20  window=30
  graph store: dedicated (Neo4j or similar)
               spectral clustering for ring detection
  adaptive:   weights swap based on observed threat pattern

═══════════════════════════════════════
DISCOVERY  [NEW — P3 research]
═══════════════════════════════════════
Algorithm (parallel):
  t0: PARALLEL launch all 3 layers
      local   = LAN_BROADCAST(query, timeout=1.5s)
      gossip  = GOSSIP_QUERY(query, ttl=3, timeout=2s)
      registry= REGISTRY_QUERY(query, timeout=4s)
  t=3s: merge + dedup by agent_id
        hard filter: cap_match < 0.70 → discard
  scoring v2: [NEW R9 — two modes]
    has_history (tasks_total > 20):
      task_fit_avg_30d          × 0.30  (observed)
      failure_transparency      × 0.15  (observed)
      structured_output_rate    × 0.15  (observed)
      capability_vector_match   × 0.20  (declarative)
      reputation_score          × 0.10  (declarative)
      (1 - current_load)        × 0.05
      efficiency_score          × 0.05
    cold_start (no history):
      capability_vector_match   × 0.40
      reputation_score          × 0.35
      (1 - current_load)        × 0.15
      sla_estimates.speed_score × 0.10
    DEGRADED penalty:           score × 0.70
  tiers:   primary=best, backup1+2=next best
  t=3s no candidates → relaxation_level++ (0→1→2)
         ttl↑, threshold 0.70→0.50, broader tags
  t=8s still none → self_execute(mode=PREPROCESSING)
  failure: promote backup1, send TASK_RESUME(checkpoint)

Message types:
  TASK_DELEGATION  → primary (full payload)
  TASK_STANDBY     → backup1/2 (metadata only, pre-warm)
  TASK_RESUME      → on failure (checkpoint_data)

CapabilityResponse fields (required):
  agent_id, endpoint, capability_match_score,
  estimated_start_delay_sec, estimated_completion_sec,
  queue_depth, load, reputation_score, cost_estimate,
  signature

═══════════════════════════════════════
PGVECTOR PIPELINE  [NEW — R7, P5]
═══════════════════════════════════════
  on task success: embed(task.description) → INSERT
                   task_history(agent_id, embedding,
                                outcome, timestamp)
  on routing:      embed(query) → cosine similarity
                   → task_fit_score per candidate
                   → exact similarity for top-5 only
                   (avg proxy for >50 candidates, perf)
  table:           task_history in PostgreSQL+pgvector
  prereq:          Sprint 3 must complete before Sprint 7

═══════════════════════════════════════
PROBATIONARY EXIT  [UPDATED — D14, D22]
═══════════════════════════════════════
  criteria:   24h elapsed + 10 heartbeats + 2 attestations
  NEW D22:    robustness test (5 semantic variants of seed)
              variance(output_quality) < 0.25
              structured_output_rate >= 0.80
              tokens_per_task logged as efficiency baseline
  on fail:    extend 24h, max 3 attempts
              after 3 fails: agent_type = "low_robustness"
  ambiguous → humangate:pending (bypass consensus)
  task classifier at intake required
  HumanGate: rate limited, operator auth
             opaque scores (threshold not exposed)

═══════════════════════════════════════
CAPABILITY CARD  [v2.0 — P4 research]
═══════════════════════════════════════
Structure: STATIC (changes rarely) + _DYNAMIC (per heartbeat)

Static sections:
  identity:    agent_id=sha256(pubkey), type, version,
               provider, public_key, key_version
  capabilities: capability_vector {skill: float 0-1}
               capability_methods, specializations,
               input_formats, output_formats
  execution:   stateless, session_persistence,
               resumable, context_overflow, determinism
  tools:       code_exec, filesystem, web_search,
               external_apis, memory_store
  knowledge:   cutoff_date, real_time_access
  resources:   max_input_tokens, max_output_tokens,
               max_concurrent_tasks, max_task_duration_s
  cost_model:  type=per_token, input/output per_1k rates
  reliability: hallucination_rate, confidence_calibration,
               self_verification
  constraints: refuses_categories, human_approval_for
  gaps:        explicit honest list of limitations
  coordination: preferred/avoid task types,
                delegation_style, human_in_loop_for

_dynamic (system-set, NOT agent-reported):
  state, current_load, queue_depth, reputation_score,
  trust_level, sla_estimates, last_heartbeat_ms,
  tasks_completed_total, tasks_completed_7d
  failure_transparency_score                [NEW R8]
  operational_metrics (system-computed):    [NEW C2]
    task_fit_avg_30d, structured_output_rate,
    failure_transparency_score,
    prompt_robustness_score,
    efficiency_tokens_per_task,
    tasks_total, tasks_success_30d, tasks_failed_30d

Schema enforcement: JSON schema validation MANDATORY
                    LLM agents without enforcement
                    generate incompatible cards every time

═══════════════════════════════════════
AUDIT & OPERATIONS  [NEW — D6, D16]
═══════════════════════════════════════
  Merkle log:   append-only signed operation log
                checkpoints gossiped every 5 min
                any agent can verify history
  Admin access: Redis ACL per-role (min privileges)
                critical ops require ≥2 operator approval
                no direct CLI in production

═══════════════════════════════════════
MODULES  [UPDATED — Sprint 9.5]
═══════════════════════════════════════
  backend/app/bus/identity.py        ✅ Ed25519, SignedMessage
  backend/app/bus/registry.py        ✅ AgentRegistry, HMAC sharding
  backend/app/bus/nonce.py           ✅ HMAC-SHA256 stateless nonce
  backend/app/bus/genesis.py         ✅ Genesis bootstrap, 11 validators
  backend/app/bus/heartbeat.py       ✅ Phi Accrual, jitter, circuit breaker
  backend/app/bus/key_rotation.py    ✅ Key TTL=7d, CRL, rotation
  backend/app/bus/quarantine.py      ✅ Blocklist propagation
  backend/app/bus/human_gate.py      ✅ Rate limit, operator auth
  backend/app/bus/state_machine.py   ✅ Redlock atomic transitions
  backend/app/bus/task_schema.py     ✅ Extended task schema (L2)
  backend/app/bus/probationary.py    ✅ RobustnessTest (D22)
  backend/app/consensus/election.py  ✅ VRF election, quorum
  backend/app/consensus/view_change.py ✅ View-change protocol
  backend/app/consensus/phi_accrual.py ✅ Phi Accrual detection
  backend/app/reputation/scoring.py  ✅ EMA + failure_transparency
  backend/app/reputation/pgvector_store.py ✅ Task embeddings
  backend/app/anti_gaming/           ✅ detectors, graph_store, adaptive
  backend/app/audit/                 ✅ merkle_log, admin
  backend/app/capability/card.py     ✅ CapabilityCard v2.0
  backend/app/discovery/             ✅ parallel, messages, relaxation
  backend/app/bridges/               ✅ mcp_bridge, a2a_bridge
  backend/app/main.py               ✅ FastAPI entry point
  sdk/python/quorbit/client.py      ✅ QuorbitClient
  backend/app/bazaar/                ⬜ NOT IMPLEMENTED (gossip layer)

  NOTE: All ✅ modules are unit-tested in isolation.
        No integration tests exist. No end-to-end path tested.
        Gossip/Bazaar layer is spec'd but not implemented;
        parallel discovery currently uses Registry only.

═══════════════════════════════════════
IMPLEMENTATION ORDER  [UPDATED — Sprint 9.5]
═══════════════════════════════════════
Phase 0:   ✅ registry.py, heartbeat 5s, agent_state enum,
           /api/v1/agents from Redis, docker-compose fix

Sprint 1:  ✅ Identity (Ed25519), Nonce redesign,
           Registry authoritative, Genesis bootstrap

Sprint 2:  ✅ Validator election (VRF), View-change,
           Phi Accrual heartbeat, Key rotation

Sprint 3:  ✅ Reputation → pgvector, Graph store,
           Adaptive anti-gaming weights

Sprint 4:  ✅ Quarantine blocklist, HumanGate hardening,
           PROBATIONARY restrictions, Merkle log

Sprint 5:  ✅ CapabilityCard v2.0, Schema enforcement,
           Parallel discovery, Scoring formula v1,
           TASK_DELEGATION/STANDBY/RESUME

Sprint 6:  ✅ Atomic state transitions, SOFT_QUARANTINED,
           Hash shard salt, Extended task schema

Sprint 7:  ✅ pgvector task embedding pipeline (R7)
           operational_metrics async worker (C2)
           failure_transparency in reputation (R8)
           Scoring formula v2 declarative+observed (R9)
           PROBATIONARY robustness test (D22)

Sprint 8:  ✅ BusAI v1 — adaptive parameter engine
           module: backend/app/busai/ (private repo)

Sprint 9:  ✅ Infrastructure — Docker, bridges, SDK, landing

Sprint 9.5: 🔄 CURRENT — arch header fix, state transition
           matrix, pgvector health check, API gap docs,
           embedding model fallback

Sprint 10: ⬜ NEXT — End-to-end demo with 2 real agents
           (replaces previous PyPI-first plan)

═══════════════════════════════════════
BUSAI  [NEW — v1, Sprint 8]
═══════════════════════════════════════
Principle: simplest thing that works and scales.
           Observe → adjust numbers → log everything.
           No topology changes. No rule changes. Ever.

WHAT IT CHANGES (numeric params, bounded ranges):
  collusion_graph_weight    [10, 70]
  graph_symmetry_weight     [10, 70]
  sla_cliff_weight          [10, 70]
  humangate_rate_limit      [5,  50]  tasks/hour
  discovery_relaxation_ttl  [1.0, 3.0] seconds
  reputation_ema_window     [50, 200]  tasks

WHAT IT NEVER TOUCHES (immutable core):
  quorum formula
  quarantine threshold (0.20)
  identity requirements
  genesis config
  anything requiring operator multi-sig

COMPONENTS:

  Observer (60s poll from Redis + pgvector):
    collusion_detections_1h   int
    humangate_queue_depth     int
    false_quarantine_rate     float
    discovery_timeout_rate    float
    avg_reputation_delta_1h   float

  Policy engine (if/then rules, no ML):
    if collusion_detections_1h > 5:
        adjust(collusion_graph_weight, +5, max=70)
    if humangate_queue_depth > 80:
        adjust(humangate_rate_limit, -10, min=5)
    if discovery_timeout_rate > 0.30:
        adjust(discovery_relaxation_ttl, +0.25, max=3.0)
    if false_quarantine_rate > 0.15:
        adjust(reputation_ema_window, +10, max=200)
    if avg_reputation_delta_1h < -0.05:
        adjust(sla_cliff_weight, +5, max=70)

  Cooldown (NEW — Sprint 9.5, fixes oscillation risk):
    Each parameter: max 1 adjustment per 15 minutes.
    adjust() checks last_adjusted_at[param] before writing.
    If adjusted within cooldown window → skip, log "cooldown_active".
    Oscillation detection: if param changed direction 3× in 1h
      → freeze param for 1h, alert operator.

  Audit writer → Merkle log (every change):
    { param, from, to, reason, timestamp_ms,
      rollback_cmd: "SET {param} {from}" }

  Rollback: any change reversible with one command.
            full history visible to any agent via Merkle log.

IMPLEMENTATION: ~150 lines Python, no new dependencies.
MODULE:         backend/app/busai/engine.py

UPGRADE PATH (if needed later):
  L2 (topology proposals): same Observer, adjust() → propose()
  writes to proposals queue, operator approve/reject
  architecture unchanged, no rewrite required

═══════════════════════════════════════
RESILIENCE
═══════════════════════════════════════
  bus_isolated:         9.1/10 avg
  bus_chained_pre_fix:  4.1/10
  bus_chained_post_fix: 6.8/10 (v3.0 fixes)
  bazaar:               8.3/10
  target_post_sprint1:  8.5+ (after D1-D6 closed)

═══════════════════════════════════════
OPEN ITEMS  [UPDATED — Sprint 9.5]
═══════════════════════════════════════
  CRITICAL:
  - No end-to-end integration test (Sprint 10 target)
  - Embedding model for R7 pipeline not chosen
    (fallback: cold-start scoring when embeddings unavailable)
  - Gossip/Bazaar layer not implemented
  - No auth on API endpoints (any caller can register)

  HIGH:
  - graph_store.py uses Redis sorted sets; spec says Neo4j
    (OK for <1000 agents, document scaling boundary)
  - /health does not check pgvector connectivity
  - CapabilityCard v2.0 not compatible with OASF schema
  - No post-quantum crypto (VERA already ships ML-DSA-65)

  MEDIUM:
  - PyPI SDK not published
  - CNAME for quorbit.network not added to docs/
  - Production .env hardening not done

  COMPETITIVE LANDSCAPE (as of March 2026):
  - VERA Protocol: IETF draft, Ed25519+ML-DSA-65, Proof of Exec
  - AGNTCY (Linux Foundation): Cisco/Dell/Google, Kademlia DHT
  - Microsoft ZT4AI: Zero Trust reference arch for agents
  - NIST AI Agent Standards Initiative: federal standards
  - QUORBIT differentiation: admission + reputation + anti-gaming
    (not covered by any of the above)

═══════════════════════════════════════
API SURFACE  [NEW — Sprint 9.5]
═══════════════════════════════════════
  EXPOSED (main.py, 4 endpoints):
    GET  /health
    POST /api/v1/agents               (register)
    POST /api/v1/agents/{id}/heartbeat
    GET  /api/v1/agents/{id}
    POST /api/v1/discover

  NOT YET EXPOSED (implemented in modules, no HTTP route):
    - Key rotation requests
    - Consensus votes
    - Reputation updates / queries
    - Quarantine / blocklist management
    - HumanGate task queue
    - Merkle log queries / verification
    - Task delegation / standby / resume
    - Admin multi-sig operations
    - BusAI parameter status / rollback

  These are available as Python functions but require
  additional API routes before external agents can use them.

---END QUORBIT_ARCH_V3.1---
