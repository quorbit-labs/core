-- QUORBIT Protocol — pgvector Schema Migration 001
-- Requires: PostgreSQL >= 14, pgvector >= 0.5.0
--
-- Tables:
--   task_history       — agent task embeddings for routing (cosine sim)
--   reputation_history — append-only reputation event log
--
-- Index strategy:
--   - ivfflat index on task_history.embedding  (ANN, fast approximate search)
--   - ivfflat index on reputation_history.embedding (optional analytics)
--   - B-tree index on (agent_id, timestamp DESC) for time-range queries

-- ── Extension ─────────────────────────────────────────────────────────────────

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ── task_history ──────────────────────────────────────────────────────────────
-- Stores one row per completed task per agent.
-- embedding: 384-dim unit-normalised vector (all-MiniLM-L6-v2 compatible)
-- outcome:   e.g. "completed_on_time", "abandoned", "validated", "flagged"

CREATE TABLE IF NOT EXISTS task_history (
    id          BIGSERIAL    PRIMARY KEY,
    agent_id    UUID         NOT NULL,
    embedding   vector(384)  NOT NULL,
    outcome     TEXT         NOT NULL,
    timestamp   DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW()),

    CONSTRAINT task_history_unique_event
        UNIQUE (agent_id, timestamp)
);

-- Cosine similarity index (ANN via IVFFlat)
-- lists = 100 is appropriate for up to ~1M rows; tune for dataset size.
CREATE INDEX IF NOT EXISTS idx_task_history_embedding_cosine
    ON task_history
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- Fast lookup by agent for time-range queries
CREATE INDEX IF NOT EXISTS idx_task_history_agent_ts
    ON task_history (agent_id, timestamp DESC);

-- Fast lookup by outcome (for analytics)
CREATE INDEX IF NOT EXISTS idx_task_history_outcome
    ON task_history (outcome);

-- ── reputation_history ────────────────────────────────────────────────────────
-- Append-only ledger of every reputation change event.
-- score:  combined reputation at the time of the event [0.0–1.0]
-- delta:  signed score change that produced this entry
-- reason: event name, e.g. "completed_on_time", "fabricated_result"
-- embedding: optional embedding of the reason/context (for analytics)

CREATE TABLE IF NOT EXISTS reputation_history (
    id          BIGSERIAL        PRIMARY KEY,
    agent_id    UUID             NOT NULL,
    score       DOUBLE PRECISION NOT NULL CHECK (score >= 0.0 AND score <= 1.0),
    delta       DOUBLE PRECISION NOT NULL,
    reason      TEXT             NOT NULL,
    timestamp   DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW()),
    embedding   vector(384)          -- nullable: not all events have an embedding

    -- No UNIQUE constraint: multiple events may occur in the same millisecond.
);

-- Fast time-range queries per agent (primary access pattern)
CREATE INDEX IF NOT EXISTS idx_reputation_history_agent_ts
    ON reputation_history (agent_id, timestamp DESC);

-- Cosine similarity on reputation embeddings (analytics / similarity search)
CREATE INDEX IF NOT EXISTS idx_reputation_history_embedding_cosine
    ON reputation_history
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 50)
    WHERE embedding IS NOT NULL;

-- ── Helpful views ─────────────────────────────────────────────────────────────

-- Latest reputation score per agent
CREATE OR REPLACE VIEW agent_reputation_latest AS
SELECT DISTINCT ON (agent_id)
    agent_id,
    score,
    delta,
    reason,
    timestamp
FROM reputation_history
ORDER BY agent_id, timestamp DESC;

-- Agent task performance summary
CREATE OR REPLACE VIEW agent_task_summary AS
SELECT
    agent_id,
    COUNT(*)                                            AS total_tasks,
    COUNT(*) FILTER (WHERE outcome = 'completed_on_time') AS on_time,
    COUNT(*) FILTER (WHERE outcome = 'completed_late')    AS late,
    COUNT(*) FILTER (WHERE outcome = 'abandoned')         AS abandoned,
    COUNT(*) FILTER (WHERE outcome = 'validated')         AS validated,
    COUNT(*) FILTER (WHERE outcome = 'flagged')           AS flagged,
    MAX(timestamp)                                      AS last_task_at
FROM task_history
GROUP BY agent_id;
