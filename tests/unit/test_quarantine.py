"""
Unit tests — QUORBIT Quarantine, Blocklist & HumanGate (Sprint 4)

Coverage:
  - Quarantine blocks agent across all layers
  - Blocklist propagation (local + Redis + Registry)
  - Rehabilitation requires 30-day minimum
  - HumanGate rate limiting
  - HumanGate opaque scores (threshold not revealed)
  - Auto-quarantine on score < 0.20
  - Auto-quarantine on breach count ≥ 3
  - Operator requirement for approve/reject
  - PROBATIONARY task assignment restrictions
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from backend.app.bus.quarantine import (
    QuarantineManager,
    QuarantineError,
    RehabilitationError,
    SCORE_THRESHOLD,
    BREACH_THRESHOLD,
    REHABILITATION_SECONDS,
    BLOCKLIST_PREFIX,
)
from backend.app.bus.human_gate import (
    HumanGateManager,
    HumanGateError,
    RateLimitExceededError,
    STATUS_PENDING,
    STATUS_APPROVED,
    STATUS_REJECTED,
)
from backend.app.bus.registry import AgentState


# ── Fake Redis ─────────────────────────────────────────────────────────────────


class FakeRedis:
    """Minimal in-memory Redis double for unit testing."""

    def __init__(self):
        self._store: dict = {}
        self._lists: dict = {}
        self._sets: dict = {}
        self._ttls: dict = {}

    # Strings
    def get(self, key):
        return self._store.get(key)

    def set(self, key, value, ex=None):
        self._store[key] = value
        if ex:
            self._ttls[key] = ex

    def setnx(self, key, value):
        if key not in self._store:
            self._store[key] = value
            return 1
        return 0

    def delete(self, *keys):
        for k in keys:
            self._store.pop(k, None)
            self._lists.pop(k, None)
            self._sets.pop(k, None)

    def exists(self, key):
        return int(key in self._store)

    def incr(self, key):
        val = int(self._store.get(key, 0)) + 1
        self._store[key] = str(val)
        return val

    def expire(self, key, seconds):
        self._ttls[key] = seconds

    # Lists
    def rpush(self, key, *values):
        if key not in self._lists:
            self._lists[key] = []
        for v in values:
            self._lists[key].append(v)
        return len(self._lists[key])

    def lrange(self, key, start, end):
        lst = self._lists.get(key, [])
        if end == -1:
            return lst[start:]
        return lst[start:end + 1]

    def llen(self, key):
        return len(self._lists.get(key, []))

    def lindex(self, key, index):
        lst = self._lists.get(key, [])
        try:
            return lst[index]
        except IndexError:
            return None

    def lrem(self, key, count, value):
        lst = self._lists.get(key, [])
        new_lst = [x for x in lst if x != value]
        removed = len(lst) - len(new_lst)
        self._lists[key] = new_lst
        return removed

    # Hashes
    def hset(self, key, field=None, value=None, mapping=None):
        if key not in self._store:
            self._store[key] = {}
        if mapping:
            self._store[key].update(mapping)
        elif field is not None:
            self._store[key][field] = value

    def hget(self, key, field):
        return self._store.get(key, {}).get(field)

    def hgetall(self, key):
        val = self._store.get(key, {})
        return val if isinstance(val, dict) else {}

    def hincrby(self, key, field, amount):
        d = self._store.setdefault(key, {})
        current = int(d.get(field, 0))
        d[field] = str(current + amount)
        return current + amount

    # Sets
    def sadd(self, key, *values):
        if key not in self._sets:
            self._sets[key] = set()
        self._sets[key].update(values)

    def sismember(self, key, value):
        return value in self._sets.get(key, set())

    def smembers(self, key):
        return self._sets.get(key, set())

    def scard(self, key):
        return len(self._sets.get(key, set()))

    def srem(self, key, *values):
        s = self._sets.get(key, set())
        for v in values:
            s.discard(v)

    # Pipeline
    def pipeline(self):
        return FakePipeline(self)

    def scan_iter(self, pattern="*"):
        prefix = pattern.rstrip("*")
        for k in list(self._store.keys()):
            if k.startswith(prefix):
                yield k


class FakePipeline:
    def __init__(self, redis):
        self._redis = redis
        self._cmds = []

    def set(self, key, value, ex=None):
        self._cmds.append(("set", key, value, ex))
        return self

    def delete(self, *keys):
        self._cmds.append(("delete", keys))
        return self

    def rpush(self, key, *values):
        self._cmds.append(("rpush", key, values))
        return self

    def hset(self, key, field=None, value=None, mapping=None):
        self._cmds.append(("hset", key, field, value, mapping))
        return self

    def lrem(self, key, count, value):
        self._cmds.append(("lrem", key, count, value))
        return self

    def execute(self):
        results = []
        for cmd in self._cmds:
            if cmd[0] == "set":
                self._redis.set(cmd[1], cmd[2], ex=cmd[3])
                results.append(True)
            elif cmd[0] == "delete":
                self._redis.delete(*cmd[1])
                results.append(True)
            elif cmd[0] == "rpush":
                self._redis.rpush(cmd[1], *cmd[2])
                results.append(True)
            elif cmd[0] == "hset":
                self._redis.hset(cmd[1], field=cmd[2], value=cmd[3], mapping=cmd[4])
                results.append(True)
            elif cmd[0] == "lrem":
                self._redis.lrem(cmd[1], cmd[2], cmd[3])
                results.append(True)
        self._cmds.clear()
        return results


# ── Registry mock factory ─────────────────────────────────────────────────────


def make_registry(fake_redis: FakeRedis, agent_id: str = "agent_abc"):
    registry = MagicMock()
    registry.get_state.return_value = AgentState.ACTIVE
    registry.set_state.return_value = None
    registry.revoke_key.return_value = None
    registry.last_quarantine_at.return_value = None
    return registry


# ── Quarantine tests ──────────────────────────────────────────────────────────


class TestQuarantineBlocksAgent:
    def setup_method(self):
        self.redis = FakeRedis()
        self.registry = make_registry(self.redis)
        self.qm = QuarantineManager(self.registry, self.redis)

    def test_quarantine_blocks_agent_in_redis(self):
        self.qm.propagate_quarantine("agent_x", reason="test")
        assert self.redis.exists(f"{BLOCKLIST_PREFIX}agent_x")

    def test_quarantine_blocks_agent_locally(self):
        self.qm.propagate_quarantine("agent_x", reason="test")
        assert "agent_x" in self.qm._local_blocklist

    def test_is_blocked_returns_true_after_quarantine(self):
        self.qm.propagate_quarantine("agent_x", reason="test")
        assert self.qm.is_blocked("agent_x") is True

    def test_is_blocked_returns_false_for_clean_agent(self):
        assert self.qm.is_blocked("clean_agent") is False

    def test_quarantine_calls_revoke_key(self):
        self.qm.propagate_quarantine("agent_x", reason="policy")
        self.registry.revoke_key.assert_called_once()
        call_kwargs = self.registry.revoke_key.call_args
        assert "agent_x" in str(call_kwargs)

    def test_quarantine_sets_state_to_quarantined(self):
        self.qm.propagate_quarantine("agent_x", reason="test")
        self.registry.set_state.assert_called_with(
            "agent_x", AgentState.QUARANTINED, force=True
        )

    def test_quarantine_is_idempotent(self):
        self.qm.propagate_quarantine("agent_x", reason="test")
        self.qm.propagate_quarantine("agent_x", reason="test")
        assert self.qm.is_blocked("agent_x") is True


class TestBlocklistPropagation:
    def setup_method(self):
        self.redis = FakeRedis()
        self.registry = make_registry(self.redis)
        self.qm = QuarantineManager(self.registry, self.redis)

    def test_is_blocked_detects_redis_only_entry(self):
        # Simulate a blocklist entry added by a different node (no local cache)
        self.redis.set(f"{BLOCKLIST_PREFIX}remote_agent", "test:1234")
        # Local cache is empty
        assert "remote_agent" not in self.qm._local_blocklist
        # is_blocked should find it via Redis
        assert self.qm.is_blocked("remote_agent") is True

    def test_is_blocked_syncs_local_cache_from_redis(self):
        self.redis.set(f"{BLOCKLIST_PREFIX}remote_agent", "test:1234")
        self.qm.is_blocked("remote_agent")
        # Local cache should now be populated
        assert "remote_agent" in self.qm._local_blocklist

    def test_is_blocked_detects_quarantined_registry_state(self):
        self.registry.get_state.return_value = AgentState.QUARANTINED
        assert self.qm.is_blocked("state_agent") is True

    def test_blocklist_reason_is_stored(self):
        self.qm.propagate_quarantine("agent_x", reason="score_low")
        reason = self.qm.get_blocklist_reason("agent_x")
        assert reason is not None
        assert "score_low" in reason

    def test_sync_local_cache_on_init(self):
        # Pre-populate Redis
        self.redis.set(f"{BLOCKLIST_PREFIX}pre_blocked", "init_test:0")
        # New manager syncs on init
        qm2 = QuarantineManager(self.registry, self.redis)
        assert "pre_blocked" in qm2._local_blocklist


class TestAutoQuarantineTriggers:
    def setup_method(self):
        self.redis = FakeRedis()
        self.registry = make_registry(self.redis)
        self.qm = QuarantineManager(self.registry, self.redis)

    def test_auto_quarantine_on_low_score(self):
        result = self.qm.check_and_quarantine("low_rep", score=0.10)
        assert result is True
        assert self.qm.is_blocked("low_rep") is True

    def test_no_auto_quarantine_above_threshold(self):
        result = self.qm.check_and_quarantine("ok_agent", score=0.50)
        assert result is False
        assert self.qm.is_blocked("ok_agent") is False

    def test_auto_quarantine_at_exact_threshold(self):
        # score == SCORE_THRESHOLD (0.20) should NOT trigger (strict <)
        result = self.qm.check_and_quarantine("edge_agent", score=SCORE_THRESHOLD)
        assert result is False

    def test_auto_quarantine_on_breach_count(self):
        agent = "breach_agent"
        for _ in range(BREACH_THRESHOLD):
            self.qm.record_breach(agent)
        result = self.qm.check_and_quarantine(agent, score=0.50)
        assert result is True
        assert self.qm.is_blocked(agent) is True

    def test_breach_counter_increments(self):
        agent = "counter_agent"
        self.qm.record_breach(agent)
        self.qm.record_breach(agent)
        assert self.qm.get_breach_count(agent) == 2


class TestRehabilitationRequires30d:
    def setup_method(self):
        self.redis = FakeRedis()
        self.registry = make_registry(self.redis)
        self.qm = QuarantineManager(self.registry, self.redis)

    def test_rehabilitation_fails_before_30d(self):
        # quarantined just now
        self.registry.last_quarantine_at.return_value = time.time()
        with pytest.raises(RehabilitationError, match="days"):
            self.qm.rehabilitate("agent_x", operator_id="op1")

    def test_rehabilitation_fails_with_no_quarantine_record(self):
        self.registry.last_quarantine_at.return_value = None
        with pytest.raises(RehabilitationError):
            self.qm.rehabilitate("agent_x", operator_id="op1")

    def test_rehabilitation_succeeds_after_30d(self):
        past = time.time() - (REHABILITATION_SECONDS + 1)
        self.registry.last_quarantine_at.return_value = past
        # Block first
        self.qm.propagate_quarantine("agent_x", reason="test")
        # Rehabilitate
        self.qm.rehabilitate("agent_x", operator_id="op1")
        assert self.qm.is_blocked("agent_x") is False

    def test_rehabilitation_resets_breach_count(self):
        past = time.time() - (REHABILITATION_SECONDS + 1)
        self.registry.last_quarantine_at.return_value = past
        self.qm.propagate_quarantine("agent_x", reason="test")
        for _ in range(3):
            self.qm.record_breach("agent_x")
        self.qm.rehabilitate("agent_x", operator_id="op1")
        assert self.qm.get_breach_count("agent_x") == 0

    def test_is_eligible_false_before_30d(self):
        self.registry.last_quarantine_at.return_value = time.time()
        assert self.qm.is_eligible_for_rehabilitation("agent_x") is False

    def test_is_eligible_true_after_30d(self):
        past = time.time() - (REHABILITATION_SECONDS + 100)
        self.registry.last_quarantine_at.return_value = past
        assert self.qm.is_eligible_for_rehabilitation("agent_x") is True


# ── HumanGate tests ───────────────────────────────────────────────────────────


class TestHumanGateRateLimit:
    def setup_method(self):
        self.redis = FakeRedis()
        # Rate limit: 3 per window
        self.hg = HumanGateManager(self.redis, rate_limit=3, rate_window_seconds=3600)

    def test_submit_within_limit_succeeds(self):
        # First submission should work
        self.hg.submit("agent_a", reason="test")
        assert self.hg.get_status("agent_a") == STATUS_PENDING

    def test_rate_limit_exceeded_raises(self):
        agent = "rate_test_agent"
        # Exhaust the rate limit (first submission goes to pending,
        # subsequent calls re-check and increment counter)
        # We need to simulate fresh calls — clear meta between calls
        for i in range(3):
            # Clear pending status so it re-enters queue each time (testing counter)
            self.redis.hset(f"humangate:meta:{agent}", mapping={"status": "approved"})
            self.redis.incr(f"humangate:rate:{agent}")

        # Now the count is 4 (1 from setup above + 3 manual), exceeds limit=3
        with pytest.raises(RateLimitExceededError):
            self.hg.submit(agent, reason="overflow")

    def test_rate_limit_reset_via_busai(self):
        self.hg.set_rate_limit(10, 1800)
        config = self.hg.get_rate_limit()
        assert config["rate_limit"] == 10
        assert config["rate_window_seconds"] == 1800

    def test_invalid_rate_limit_raises(self):
        with pytest.raises(ValueError):
            self.hg.set_rate_limit(0, 3600)

    def test_duplicate_submit_is_idempotent(self):
        self.hg.submit("agent_b", reason="first")
        # Second submit while still pending is a no-op
        self.hg.submit("agent_b", reason="second")
        queue = self.hg.pending_queue()
        # agent_b appears exactly once
        assert queue.count("agent_b") == 1


class TestHumanGateOpaqueScores:
    def setup_method(self):
        self.redis = FakeRedis()
        self.hg = HumanGateManager(self.redis, rate_limit=100, rate_window_seconds=3600)

    def test_submit_does_not_reveal_threshold(self):
        """Submitting an agent must not expose the internal threshold."""
        # submit() should not raise with a reason that mentions a score
        self.hg.submit("agent_c", reason="score_borderline")
        # No threshold value should appear in the log records
        log = self.hg.get_log()
        for record in log:
            assert "threshold" not in str(record).lower()

    def test_rate_limit_error_does_not_reveal_limit(self):
        """RateLimitExceededError message must not reveal the numeric limit."""
        agent = "rate_probe_agent"
        for _ in range(100):
            self.redis.incr(f"humangate:rate:{agent}")
        try:
            self.hg.submit(agent, reason="probe")
        except RateLimitExceededError as exc:
            # The error should NOT contain the numeric limit value
            assert str(self.hg._rate_limit) not in str(exc)

    def test_approve_requires_operator(self):
        self.hg.submit("agent_d", reason="test")
        with pytest.raises(HumanGateError, match="Operator"):
            self.hg.approve("agent_d", operator_id="")

    def test_reject_requires_operator(self):
        self.hg.submit("agent_e", reason="test")
        with pytest.raises(HumanGateError, match="Operator"):
            self.hg.reject("agent_e", operator_id="")

    def test_approve_changes_status(self):
        self.hg.submit("agent_f", reason="ambiguous")
        self.hg.approve("agent_f", operator_id="op_alice")
        assert self.hg.get_status("agent_f") == STATUS_APPROVED

    def test_reject_changes_status(self):
        self.hg.submit("agent_g", reason="suspicious")
        self.hg.reject("agent_g", operator_id="op_bob")
        assert self.hg.get_status("agent_g") == STATUS_REJECTED

    def test_approve_removes_from_pending_queue(self):
        self.hg.submit("agent_h", reason="test")
        assert "agent_h" in self.hg.pending_queue()
        self.hg.approve("agent_h", operator_id="op_carol")
        assert "agent_h" not in self.hg.pending_queue()

    def test_all_decisions_logged(self):
        self.hg.submit("agent_i", reason="test")
        self.hg.approve("agent_i", operator_id="op_dave")
        log = self.hg.get_log()
        events = [r["event"] for r in log]
        assert "submit" in events
        assert "approve" in events

    def test_approve_not_pending_raises(self):
        with pytest.raises(HumanGateError):
            self.hg.approve("never_submitted", operator_id="op_x")


# ── Registry PROBATIONARY restrictions ───────────────────────────────────────


class TestProbationaryRestrictions:
    """Test that PROBATIONARY agents cannot receive task assignments."""

    def setup_method(self):
        self.redis = FakeRedis()
        self.registry = MagicMock()

    def _make_registry_with_state(self, state: AgentState):
        """Return a real-enough mock registry with fixed state."""
        from backend.app.bus.registry import AgentRegistry
        # Use the real can_receive_tasks and assign_task methods via a mock
        from unittest.mock import patch
        registry = MagicMock(spec=AgentRegistry)
        registry.get_state.return_value = state
        registry.can_receive_tasks.side_effect = lambda aid: (
            state not in (AgentState.PROBATIONARY, AgentState.QUARANTINED)
        )
        return registry

    def test_probationary_cannot_receive_tasks(self):
        from backend.app.bus.registry import AgentRegistry, AgentState

        redis_mock = FakeRedis()
        # Write PROBATIONARY state
        redis_mock.set("bus:state:prob_agent", AgentState.PROBATIONARY.value)
        redis_mock.hset(
            "bus:capability:prob_agent",
            mapping={"name": "p", "endpoint": "", "registered_at": "0", "last_seen": "0", "tasks_completed": "0"},
        )

        import sys
        with patch("backend.app.bus.registry.redis") as mock_redis_mod:
            mock_redis_mod.Redis.from_url.return_value = redis_mock
            registry = AgentRegistry.__new__(AgentRegistry)
            registry._redis = redis_mock
            registry._salt = b"\x00" * 32
            registry._num_shards = 10

        assert registry.can_receive_tasks("prob_agent") is False

    def test_active_can_receive_tasks(self):
        from backend.app.bus.registry import AgentRegistry, AgentState

        redis_mock = FakeRedis()
        redis_mock.set("bus:state:active_agent", AgentState.ACTIVE.value)
        redis_mock.hset(
            "bus:capability:active_agent",
            mapping={"name": "a", "endpoint": "", "registered_at": "0", "last_seen": "0", "tasks_completed": "0"},
        )

        registry = AgentRegistry.__new__(AgentRegistry)
        registry._redis = redis_mock
        registry._salt = b"\x00" * 32
        registry._num_shards = 10

        assert registry.can_receive_tasks("active_agent") is True

    def test_quarantined_cannot_receive_tasks(self):
        from backend.app.bus.registry import AgentRegistry, AgentState

        redis_mock = FakeRedis()
        redis_mock.set("bus:state:quar_agent", AgentState.QUARANTINED.value)
        redis_mock.hset(
            "bus:capability:quar_agent",
            mapping={"name": "q", "endpoint": "", "registered_at": "0", "last_seen": "0", "tasks_completed": "0"},
        )

        registry = AgentRegistry.__new__(AgentRegistry)
        registry._redis = redis_mock
        registry._salt = b"\x00" * 32
        registry._num_shards = 10

        assert registry.can_receive_tasks("quar_agent") is False

    def test_assign_task_raises_for_probationary(self):
        from backend.app.bus.registry import AgentRegistry, AgentState

        redis_mock = FakeRedis()
        redis_mock.set("bus:state:prob2", AgentState.PROBATIONARY.value)
        redis_mock.hset(
            "bus:capability:prob2",
            mapping={"name": "p", "endpoint": "", "registered_at": "0", "last_seen": "0", "tasks_completed": "0"},
        )

        registry = AgentRegistry.__new__(AgentRegistry)
        registry._redis = redis_mock
        registry._salt = b"\x00" * 32
        registry._num_shards = 10

        with pytest.raises(ValueError, match="PROBATIONARY"):
            registry.assign_task("prob2")
