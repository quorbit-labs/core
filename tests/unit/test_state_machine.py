"""
Unit tests — QUORBIT Sprint 6 — D18, D19, D20, L2

Coverage:
  - Valid state transitions succeed
  - Invalid state transitions raise InvalidStateTransitionError
  - atomic_transition() CAS semantics: succeeds/fails on state match
  - atomic_transition() logs to MerkleLog
  - Concurrent transitions — only one succeeds
  - SOFT_QUARANTINED agent: can receive priority<=3, rejected for priority>3
  - Active agent can receive any priority
  - PROBATIONARY agent cannot receive any task
  - TaskSchema validation: valid/invalid schemas
  - TaskSchema checkpoint_data preserved through round-trip
  - D20: get_shard() reads QUORBIT_SHARD_SALT from env
"""

from __future__ import annotations

import json
import os
import threading
import uuid

import pytest

from backend.app.bus.state_machine import LockAcquisitionError, StateMachine
from backend.app.bus.registry import (
    AgentRegistry,
    AgentState,
    InvalidStateTransitionError,
)
from backend.app.bus.task_schema import (
    RetryPolicy,
    TaskSchema,
    TimeoutPolicy,
    ValidationError,
)


# ── FakeRedis ─────────────────────────────────────────────────────────────────


class FakeRedis:
    """
    In-memory Redis double covering all operations used by StateMachine,
    MerkleLog, and AgentRegistry in these tests.

    Thread-safety: a threading.Lock guards nx=True set operations so that
    concurrent-transition tests behave correctly.
    """

    def __init__(self):
        self._data: dict = {}
        self._lists: dict = {}
        self._sets: dict = {}
        self._hashes: dict = {}
        self._nx_lock = threading.Lock()

    # ── String ops ────────────────────────────────────────────────────────

    def get(self, key: str):
        return self._data.get(key)

    def set(self, key: str, value, nx: bool = False, px=None, ex=None):
        if nx:
            with self._nx_lock:
                if key in self._data:
                    return None          # already exists — NX fails
                self._data[key] = str(value)
                return True
        self._data[key] = str(value)
        return True

    def setnx(self, key: str, value):
        if key not in self._data:
            self._data[key] = str(value)
            return 1
        return 0

    def delete(self, *keys):
        for k in keys:
            self._data.pop(k, None)
            self._lists.pop(k, None)
            self._sets.pop(k, None)
            self._hashes.pop(k, None)
        return len(keys)

    def exists(self, *keys):
        return sum(
            1 for k in keys
            if k in self._data or k in self._lists or k in self._sets or k in self._hashes
        )

    def incr(self, key: str) -> int:
        val = int(self._data.get(key, 0)) + 1
        self._data[key] = str(val)
        return val

    def expire(self, key, seconds):
        return 1

    # ── Lua eval (lock release pattern only) ──────────────────────────────

    def eval(self, script: str, num_keys: int, *args):
        key, expected_token = args[0], args[1]
        if self._data.get(key) == expected_token:
            del self._data[key]
            return 1
        return 0

    # ── Hash ops ──────────────────────────────────────────────────────────

    def hset(self, key: str, mapping: dict = None, **kwargs):
        if key not in self._hashes:
            self._hashes[key] = {}
        if mapping:
            self._hashes[key].update(mapping)
        self._hashes[key].update(kwargs)
        return len(mapping or {}) + len(kwargs)

    def hgetall(self, key: str) -> dict:
        return dict(self._hashes.get(key, {}))

    def hincrby(self, key: str, field: str, amount: int = 1) -> int:
        if key not in self._hashes:
            self._hashes[key] = {}
        val = int(self._hashes[key].get(field, 0)) + amount
        self._hashes[key][field] = str(val)
        return val

    # ── Set ops ───────────────────────────────────────────────────────────

    def sadd(self, key: str, *members):
        if key not in self._sets:
            self._sets[key] = set()
        added = sum(1 for m in members if m not in self._sets[key])
        self._sets[key].update(members)
        return added

    def srem(self, key: str, *members):
        if key not in self._sets:
            return 0
        removed = sum(1 for m in members if m in self._sets[key])
        self._sets[key] -= set(members)
        return removed

    def scard(self, key: str) -> int:
        return len(self._sets.get(key, set()))

    def smembers(self, key: str):
        return set(self._sets.get(key, set()))

    # ── List ops ──────────────────────────────────────────────────────────

    def rpush(self, key: str, *values):
        if key not in self._lists:
            self._lists[key] = []
        for v in values:
            self._lists[key].append(str(v))
        return len(self._lists[key])

    def lrange(self, key: str, start: int, end: int):
        lst = self._lists.get(key, [])
        if end == -1:
            return lst[start:]
        return lst[start: end + 1]

    def lindex(self, key: str, index: int):
        lst = self._lists.get(key, [])
        try:
            return lst[index]
        except IndexError:
            return None

    def llen(self, key: str) -> int:
        return len(self._lists.get(key, []))

    # ── Scan ──────────────────────────────────────────────────────────────

    def scan_iter(self, pattern: str = "*"):
        prefix = pattern.rstrip("*")
        for k in list(self._data.keys()):
            if k.startswith(prefix):
                yield k
        for k in list(self._hashes.keys()):
            if k.startswith(prefix):
                yield k

    # ── Pipeline ──────────────────────────────────────────────────────────

    def pipeline(self):
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, redis: FakeRedis):
        self._redis = redis
        self._cmds: list = []

    def set(self, key, value, nx=False, px=None, ex=None):
        self._cmds.append(("set", key, value, nx, px, ex))
        return self

    def setnx(self, key, value):
        self._cmds.append(("setnx", key, value))
        return self

    def hset(self, key, mapping=None, **kwargs):
        self._cmds.append(("hset", key, mapping or {}))
        return self

    def expire(self, key, seconds):
        self._cmds.append(("expire", key, seconds))
        return self

    def delete(self, *keys):
        self._cmds.append(("delete", *keys))
        return self

    def sadd(self, key, *members):
        self._cmds.append(("sadd", key, *members))
        return self

    def srem(self, key, *members):
        self._cmds.append(("srem", key, *members))
        return self

    def rpush(self, key, *values):
        self._cmds.append(("rpush", key, *values))
        return self

    def execute(self):
        results = []
        for cmd in self._cmds:
            if cmd[0] == "set":
                results.append(self._redis.set(cmd[1], cmd[2], nx=cmd[3]))
            elif cmd[0] == "setnx":
                results.append(self._redis.setnx(cmd[1], cmd[2]))
            elif cmd[0] == "hset":
                results.append(self._redis.hset(cmd[1], mapping=cmd[2]))
            elif cmd[0] == "expire":
                results.append(self._redis.expire(cmd[1], cmd[2]))
            elif cmd[0] == "delete":
                results.append(self._redis.delete(*cmd[1:]))
            elif cmd[0] == "sadd":
                results.append(self._redis.sadd(cmd[1], *cmd[2:]))
            elif cmd[0] == "srem":
                results.append(self._redis.srem(cmd[1], *cmd[2:]))
            elif cmd[0] == "rpush":
                results.append(self._redis.rpush(cmd[1], *cmd[2:]))
            else:
                results.append(None)
        self._cmds.clear()
        return results


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_sm(fake_redis=None, with_merkle=False):
    """Build a StateMachine pointing at a FakeRedis."""
    from backend.app.audit.merkle_log import MerkleLog

    fr = fake_redis or FakeRedis()
    ml = MerkleLog(fr) if with_merkle else None
    return StateMachine(fr, merkle_log=ml), fr


def _valid_task(**overrides) -> dict:
    """Minimal valid task dict."""
    base = {
        "task_id": str(uuid.uuid4()),
        "type": "analysis",
        "priority": 5,
        "ttl_seconds": 60,
        "payload": {"text": "hello"},
        "required_capabilities": {"coding": 0.7},
    }
    base.update(overrides)
    return base


# ── TestValidTransitions ──────────────────────────────────────────────────────


class TestValidTransitions:
    def test_probationary_to_active(self):
        sm, fr = _make_sm()
        result = sm.atomic_transition("a1", AgentState.PROBATIONARY, AgentState.ACTIVE)
        assert result is True
        assert fr.get("bus:state:a1") == "ACTIVE"

    def test_active_to_degraded(self):
        sm, fr = _make_sm()
        fr.set("bus:state:a1", "ACTIVE")
        result = sm.atomic_transition("a1", AgentState.ACTIVE, AgentState.DEGRADED)
        assert result is True
        assert fr.get("bus:state:a1") == "DEGRADED"

    def test_active_to_soft_quarantined(self):
        sm, fr = _make_sm()
        fr.set("bus:state:a1", "ACTIVE")
        result = sm.atomic_transition(
            "a1", AgentState.ACTIVE, AgentState.SOFT_QUARANTINED
        )
        assert result is True
        assert fr.get("bus:state:a1") == "SOFT_QUARANTINED"

    def test_degraded_to_active(self):
        sm, fr = _make_sm()
        fr.set("bus:state:a1", "DEGRADED")
        result = sm.atomic_transition("a1", AgentState.DEGRADED, AgentState.ACTIVE)
        assert result is True

    def test_degraded_to_isolated(self):
        sm, fr = _make_sm()
        fr.set("bus:state:a1", "DEGRADED")
        result = sm.atomic_transition("a1", AgentState.DEGRADED, AgentState.ISOLATED)
        assert result is True

    def test_isolated_to_quarantined(self):
        sm, fr = _make_sm()
        fr.set("bus:state:a1", "ISOLATED")
        result = sm.atomic_transition("a1", AgentState.ISOLATED, AgentState.QUARANTINED)
        assert result is True

    def test_soft_quarantined_to_quarantined(self):
        sm, fr = _make_sm()
        fr.set("bus:state:a1", "SOFT_QUARANTINED")
        result = sm.atomic_transition(
            "a1", AgentState.SOFT_QUARANTINED, AgentState.QUARANTINED
        )
        assert result is True

    def test_quarantined_to_probationary(self):
        sm, fr = _make_sm()
        fr.set("bus:state:a1", "QUARANTINED")
        result = sm.atomic_transition(
            "a1", AgentState.QUARANTINED, AgentState.PROBATIONARY
        )
        assert result is True

    def test_soft_quarantined_to_active(self):
        sm, fr = _make_sm()
        fr.set("bus:state:a1", "SOFT_QUARANTINED")
        result = sm.atomic_transition(
            "a1", AgentState.SOFT_QUARANTINED, AgentState.ACTIVE
        )
        assert result is True


# ── TestInvalidTransitions ────────────────────────────────────────────────────


class TestInvalidTransitions:
    def test_active_to_probationary_raises(self):
        sm, fr = _make_sm()
        fr.set("bus:state:a1", "ACTIVE")
        with pytest.raises(InvalidStateTransitionError):
            sm.atomic_transition("a1", AgentState.ACTIVE, AgentState.PROBATIONARY)

    def test_probationary_to_degraded_raises(self):
        sm, fr = _make_sm()
        with pytest.raises(InvalidStateTransitionError):
            sm.atomic_transition("a1", AgentState.PROBATIONARY, AgentState.DEGRADED)

    def test_quarantined_to_active_raises(self):
        sm, fr = _make_sm()
        fr.set("bus:state:a1", "QUARANTINED")
        with pytest.raises(InvalidStateTransitionError):
            sm.atomic_transition("a1", AgentState.QUARANTINED, AgentState.ACTIVE)

    def test_probationary_to_isolated_raises(self):
        sm, fr = _make_sm()
        with pytest.raises(InvalidStateTransitionError):
            sm.atomic_transition("a1", AgentState.PROBATIONARY, AgentState.ISOLATED)

    def test_active_to_quarantined_raises(self):
        sm, fr = _make_sm()
        fr.set("bus:state:a1", "ACTIVE")
        with pytest.raises(InvalidStateTransitionError):
            sm.atomic_transition("a1", AgentState.ACTIVE, AgentState.QUARANTINED)

    def test_force_bypasses_invalid_transition(self):
        sm, fr = _make_sm()
        fr.set("bus:state:a1", "ACTIVE")
        # ACTIVE → PROBATIONARY is invalid but force=True overrides
        result = sm.atomic_transition(
            "a1", AgentState.ACTIVE, AgentState.PROBATIONARY, force=True
        )
        assert result is True
        assert fr.get("bus:state:a1") == "PROBATIONARY"


# ── TestAtomicTransition ──────────────────────────────────────────────────────


class TestAtomicTransition:
    def test_cas_fails_when_state_mismatch(self):
        """Transition returns False when from_state doesn't match actual state."""
        sm, fr = _make_sm()
        fr.set("bus:state:a1", "ACTIVE")  # agent is ACTIVE
        # Caller claims it's PROBATIONARY → CAS should fail
        result = sm.atomic_transition("a1", AgentState.PROBATIONARY, AgentState.ACTIVE)
        assert result is False
        # State must not have changed
        assert fr.get("bus:state:a1") == "ACTIVE"

    def test_cas_succeeds_when_state_matches(self):
        sm, fr = _make_sm()
        fr.set("bus:state:a1", "ACTIVE")
        result = sm.atomic_transition("a1", AgentState.ACTIVE, AgentState.DEGRADED)
        assert result is True
        assert fr.get("bus:state:a1") == "DEGRADED"

    def test_lock_released_after_successful_transition(self):
        sm, fr = _make_sm()
        sm.atomic_transition("a1", AgentState.PROBATIONARY, AgentState.ACTIVE)
        # After the transition the lock key must be gone
        assert fr.get("bus:lock:state:a1") is None

    def test_lock_released_after_cas_failure(self):
        sm, fr = _make_sm()
        fr.set("bus:state:a1", "ACTIVE")
        sm.atomic_transition("a1", AgentState.PROBATIONARY, AgentState.ACTIVE)
        assert fr.get("bus:lock:state:a1") is None

    def test_lock_acquired_while_held_raises(self):
        """If a lock already exists, acquisition should raise LockAcquisitionError."""
        sm, fr = _make_sm()
        # Pre-place a lock as if another process holds it
        fr.set("bus:lock:state:a1", "held-by-other", nx=True)
        with pytest.raises(LockAcquisitionError):
            sm.atomic_transition("a1", AgentState.PROBATIONARY, AgentState.ACTIVE)

    def test_atomic_transition_logs_to_merkle(self):
        sm, fr = _make_sm(with_merkle=True)
        sm.atomic_transition("a1", AgentState.PROBATIONARY, AgentState.ACTIVE)
        entries = fr.lrange("audit:merkle:entries", 0, -1)
        assert len(entries) == 1
        entry = json.loads(entries[0])
        payload = json.loads(entry["data"])
        assert entry["operation"] == "state_transition"
        assert payload["agent_id"] == "a1"
        assert payload["from_state"] == "PROBATIONARY"
        assert payload["to_state"] == "ACTIVE"

    def test_failed_cas_does_not_log_to_merkle(self):
        sm, fr = _make_sm(with_merkle=True)
        fr.set("bus:state:a1", "ACTIVE")
        # CAS failure: claim it is PROBATIONARY
        sm.atomic_transition("a1", AgentState.PROBATIONARY, AgentState.ACTIVE)
        entries = fr.lrange("audit:merkle:entries", 0, -1)
        assert len(entries) == 0

    def test_concurrent_transitions_only_one_succeeds(self):
        """
        Two threads compete to do PROBATIONARY → ACTIVE.
        Exactly one should succeed; the other gets a CAS failure or lock error.
        """
        sm, fr = _make_sm()
        results = []
        errors = []

        def try_transition():
            try:
                r = sm.atomic_transition(
                    "agent_x", AgentState.PROBATIONARY, AgentState.ACTIVE
                )
                results.append(r)
            except LockAcquisitionError:
                errors.append("lock")

        t1 = threading.Thread(target=try_transition)
        t2 = threading.Thread(target=try_transition)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Exactly one True result (or the other got a lock error — both are valid)
        true_count = results.count(True)
        assert true_count == 1 or (true_count == 0 and len(errors) >= 1)
        assert fr.get("bus:state:agent_x") == "ACTIVE"


# ── TestSoftQuarantinedTasks ──────────────────────────────────────────────────


class TestSoftQuarantinedTasks:
    """D19 — SOFT_QUARANTINED agents may only receive priority <= 3 tasks."""

    def _registry_with_state(self, state: AgentState) -> AgentRegistry:
        """Build an AgentRegistry backed by FakeRedis with agent pre-set to state."""
        fr = FakeRedis()
        reg = AgentRegistry.__new__(AgentRegistry)
        reg._redis = fr
        reg._salt = b"\x00" * 32
        reg._num_shards = 10
        # Register agent and force state
        fr.hset(f"bus:capability:agent1", mapping={"name": "test"})
        fr.set(f"bus:state:agent1", state.value)
        return reg

    def test_soft_quarantined_can_receive_low_priority(self):
        reg = self._registry_with_state(AgentState.SOFT_QUARANTINED)
        assert reg.can_receive_tasks("agent1", task_priority=1) is True
        assert reg.can_receive_tasks("agent1", task_priority=2) is True
        assert reg.can_receive_tasks("agent1", task_priority=3) is True

    def test_soft_quarantined_cannot_receive_high_priority(self):
        reg = self._registry_with_state(AgentState.SOFT_QUARANTINED)
        assert reg.can_receive_tasks("agent1", task_priority=4) is False
        assert reg.can_receive_tasks("agent1", task_priority=10) is False

    def test_soft_quarantined_no_priority_blocked(self):
        """No priority given → conservative: SOFT_QUARANTINED is blocked."""
        reg = self._registry_with_state(AgentState.SOFT_QUARANTINED)
        assert reg.can_receive_tasks("agent1") is False

    def test_soft_quarantined_assign_low_priority_succeeds(self):
        reg = self._registry_with_state(AgentState.SOFT_QUARANTINED)
        reg.assign_task("agent1", task_priority=3)  # must not raise

    def test_soft_quarantined_assign_high_priority_raises(self):
        reg = self._registry_with_state(AgentState.SOFT_QUARANTINED)
        with pytest.raises(ValueError):
            reg.assign_task("agent1", task_priority=5)

    def test_active_can_receive_any_priority(self):
        reg = self._registry_with_state(AgentState.ACTIVE)
        for p in range(1, 11):
            assert reg.can_receive_tasks("agent1", task_priority=p) is True

    def test_active_no_priority_allowed(self):
        reg = self._registry_with_state(AgentState.ACTIVE)
        assert reg.can_receive_tasks("agent1") is True

    def test_probationary_cannot_receive_any_task(self):
        reg = self._registry_with_state(AgentState.PROBATIONARY)
        for p in range(1, 11):
            assert reg.can_receive_tasks("agent1", task_priority=p) is False

    def test_quarantined_cannot_receive_any_task(self):
        reg = self._registry_with_state(AgentState.QUARANTINED)
        assert reg.can_receive_tasks("agent1", task_priority=1) is False

    def test_degraded_can_receive_all_priorities(self):
        """DEGRADED is not restricted — only penalised in scoring."""
        reg = self._registry_with_state(AgentState.DEGRADED)
        for p in range(1, 11):
            assert reg.can_receive_tasks("agent1", task_priority=p) is True


# ── TestTaskSchemaValidation ──────────────────────────────────────────────────


class TestTaskSchemaValidation:
    def test_valid_schema_passes(self):
        task = TaskSchema.from_dict(_valid_task())
        task.validate()   # must not raise

    def test_from_dict_returns_task_schema(self):
        task = TaskSchema.from_dict(_valid_task())
        assert isinstance(task, TaskSchema)

    def test_task_id_preserved(self):
        tid = str(uuid.uuid4())
        task = TaskSchema.from_dict(_valid_task(task_id=tid))
        assert task.task_id == tid

    def test_invalid_uuid_raises(self):
        with pytest.raises(ValidationError, match="UUID"):
            TaskSchema.from_dict(_valid_task(task_id="not-a-uuid"))

    def test_empty_type_raises(self):
        with pytest.raises(ValidationError, match="type"):
            TaskSchema.from_dict(_valid_task(type=""))

    def test_whitespace_type_raises(self):
        with pytest.raises(ValidationError, match="type"):
            TaskSchema.from_dict(_valid_task(type="   "))

    def test_priority_zero_raises(self):
        with pytest.raises(ValidationError, match="priority"):
            TaskSchema.from_dict(_valid_task(priority=0))

    def test_priority_eleven_raises(self):
        with pytest.raises(ValidationError, match="priority"):
            TaskSchema.from_dict(_valid_task(priority=11))

    def test_priority_boundary_one_accepted(self):
        task = TaskSchema.from_dict(_valid_task(priority=1))
        assert task.priority == 1

    def test_priority_boundary_ten_accepted(self):
        task = TaskSchema.from_dict(_valid_task(priority=10))
        assert task.priority == 10

    def test_zero_ttl_raises(self):
        with pytest.raises(ValidationError, match="ttl_seconds"):
            TaskSchema.from_dict(_valid_task(ttl_seconds=0))

    def test_negative_ttl_raises(self):
        with pytest.raises(ValidationError, match="ttl_seconds"):
            TaskSchema.from_dict(_valid_task(ttl_seconds=-1))

    def test_payload_not_dict_raises(self):
        with pytest.raises(ValidationError, match="payload"):
            TaskSchema.from_dict(_valid_task(payload="bad"))

    def test_capability_above_one_raises(self):
        with pytest.raises(ValidationError):
            TaskSchema.from_dict(_valid_task(required_capabilities={"coding": 1.01}))

    def test_capability_below_zero_raises(self):
        with pytest.raises(ValidationError):
            TaskSchema.from_dict(_valid_task(required_capabilities={"coding": -0.01}))

    def test_capability_at_boundaries_accepted(self):
        task = TaskSchema.from_dict(
            _valid_task(required_capabilities={"a": 0.0, "b": 1.0})
        )
        assert task.required_capabilities["a"] == pytest.approx(0.0)
        assert task.required_capabilities["b"] == pytest.approx(1.0)

    def test_min_reputation_above_one_raises(self):
        with pytest.raises(ValidationError):
            TaskSchema.from_dict(_valid_task(min_reputation=1.1))

    def test_min_reputation_negative_raises(self):
        with pytest.raises(ValidationError):
            TaskSchema.from_dict(_valid_task(min_reputation=-0.1))

    def test_hard_timeout_less_than_soft_raises(self):
        with pytest.raises(ValidationError, match="hard_timeout"):
            TaskSchema.from_dict(_valid_task(
                timeout_policy={"soft_timeout_s": 30.0, "hard_timeout_s": 10.0}
            ))

    def test_equal_soft_hard_timeout_accepted(self):
        task = TaskSchema.from_dict(_valid_task(
            timeout_policy={"soft_timeout_s": 20.0, "hard_timeout_s": 20.0}
        ))
        assert task.timeout_policy.soft_timeout_s == pytest.approx(20.0)

    def test_negative_retries_raises(self):
        with pytest.raises(ValidationError, match="max_retries"):
            TaskSchema.from_dict(_valid_task(
                retry_policy={"max_retries": -1, "backoff_seconds": 5.0}
            ))

    def test_zero_retries_accepted(self):
        task = TaskSchema.from_dict(_valid_task(
            retry_policy={"max_retries": 0, "backoff_seconds": 0.0}
        ))
        assert task.retry_policy.max_retries == 0

    def test_negative_backoff_raises(self):
        with pytest.raises(ValidationError, match="backoff"):
            TaskSchema.from_dict(_valid_task(
                retry_policy={"max_retries": 3, "backoff_seconds": -1.0}
            ))

    def test_to_dict_round_trip(self):
        original = _valid_task(
            timeout_policy={"soft_timeout_s": 10.0, "hard_timeout_s": 30.0},
            retry_policy={"max_retries": 3, "backoff_seconds": 5.0},
        )
        task = TaskSchema.from_dict(original)
        d = task.to_dict()
        assert d["priority"] == original["priority"]
        assert d["timeout_policy"]["hard_timeout_s"] == pytest.approx(30.0)
        assert d["retry_policy"]["max_retries"] == 3


# ── TestTaskSchemaCheckpoint ──────────────────────────────────────────────────


class TestTaskSchemaCheckpoint:
    def test_no_checkpoint_by_default(self):
        task = TaskSchema.from_dict(_valid_task())
        assert task.checkpoint_data is None

    def test_checkpoint_data_preserved(self):
        checkpoint = {"step": 3, "partial_result": [1, 2, 3]}
        task = TaskSchema.from_dict(_valid_task(checkpoint_data=checkpoint))
        assert task.checkpoint_data == checkpoint
        assert task.checkpoint_data["step"] == 3

    def test_checkpoint_round_trip(self):
        checkpoint = {"iteration": 5, "state": "pending"}
        task = TaskSchema.from_dict(_valid_task(checkpoint_data=checkpoint))
        d = task.to_dict()
        assert d["checkpoint_data"] == checkpoint

    def test_checkpoint_not_dict_raises(self):
        with pytest.raises(ValidationError, match="checkpoint_data"):
            TaskSchema.from_dict(_valid_task(checkpoint_data="bad"))

    def test_checkpoint_none_explicit(self):
        task = TaskSchema.from_dict(_valid_task(checkpoint_data=None))
        assert task.checkpoint_data is None

    def test_checkpoint_empty_dict_accepted(self):
        task = TaskSchema.from_dict(_valid_task(checkpoint_data={}))
        assert task.checkpoint_data == {}


# ── TestShardSalt (D20) ───────────────────────────────────────────────────────


class TestShardSalt:
    def _build_registry(self, salt: bytes | None = None) -> AgentRegistry:
        fr = FakeRedis()
        reg = AgentRegistry.__new__(AgentRegistry)
        reg._redis = fr
        reg._num_shards = 16
        reg._salt = salt if salt is not None else b"\x00" * 32
        return reg

    def test_get_shard_returns_valid_range(self):
        reg = self._build_registry()
        for _ in range(20):
            sid = reg.get_shard(str(uuid.uuid4()))
            assert 0 <= sid < 16

    def test_get_shard_is_deterministic(self):
        reg = self._build_registry(salt=b"testsalt")
        agent = "deadbeef" * 8
        s1 = reg.get_shard(agent)
        s2 = reg.get_shard(agent)
        assert s1 == s2

    def test_different_salts_produce_different_shards(self):
        agent = "cafebabe" * 8
        reg_a = self._build_registry(salt=b"salt_a")
        reg_b = self._build_registry(salt=b"salt_b")
        # With overwhelming probability two different salts produce different shards
        # (could collide for a specific agent, so we test multiple agents)
        shards_a = [reg_a.get_shard(f"agent{i}") for i in range(20)]
        shards_b = [reg_b.get_shard(f"agent{i}") for i in range(20)]
        assert shards_a != shards_b

    def test_shard_salt_from_env(self, monkeypatch):
        """D20: registry reads QUORBIT_SHARD_SALT from environment."""
        monkeypatch.setenv("QUORBIT_SHARD_SALT", "env_test_salt")

        # Build via __init__ so env reading is triggered
        fr = FakeRedis()
        import backend.app.bus.registry as reg_mod

        original_redis_class = reg_mod.redis.Redis

        class FakeRedisClass:
            @staticmethod
            def from_url(*args, **kwargs):
                return fr

        monkeypatch.setattr(reg_mod.redis, "Redis", FakeRedisClass)

        reg = AgentRegistry(redis_url="redis://localhost:6379", num_shards=16)
        assert reg._salt == b"env_test_salt"

    def test_shard_salt_default_when_env_unset(self, monkeypatch):
        """Without env var, registry defaults to zero salt."""
        monkeypatch.delenv("QUORBIT_SHARD_SALT", raising=False)

        fr = FakeRedis()
        import backend.app.bus.registry as reg_mod

        class FakeRedisClass:
            @staticmethod
            def from_url(*args, **kwargs):
                return fr

        monkeypatch.setattr(reg_mod.redis, "Redis", FakeRedisClass)

        reg = AgentRegistry(redis_url="redis://localhost:6379")
        assert reg._salt == b"\x00" * 32
