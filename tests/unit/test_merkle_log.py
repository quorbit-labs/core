"""
Unit tests — QUORBIT Merkle Append-Only Log & Admin Access Model (Sprint 4)

Coverage:
  - append() creates a valid chain entry
  - Chain verification passes on valid log
  - Tampered entry is detected by verify_chain()
  - Checkpoint is generated and reflects the latest hash
  - Admin multi-sig enforcement
  - Admin action logging → Merkle log
  - Session recording
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import pytest

from backend.app.audit.merkle_log import (
    MerkleLog,
    MerkleEntry,
    GENESIS_HASH,
    ENTRIES_KEY,
    CHECKPOINT_KEY,
    _compute_hash,
)
from backend.app.audit.admin import (
    AdminManager,
    AdminRole,
    InsufficientOperatorsError,
    UnauthorizedOperatorError,
    CRITICAL_OPERATIONS,
    MIN_CRITICAL_OPERATORS,
)


# ── Fake Redis (shared with test_quarantine.py pattern) ───────────────────────


class FakeRedis:
    def __init__(self):
        self._store: dict = {}
        self._lists: dict = {}
        self._sets: dict = {}

    def get(self, key):
        return self._store.get(key)

    def set(self, key, value, ex=None):
        self._store[key] = value

    def delete(self, *keys):
        for k in keys:
            self._store.pop(k, None)
            self._lists.pop(k, None)

    def exists(self, key):
        return int(key in self._store)

    def incr(self, key):
        val = int(self._store.get(key, 0)) + 1
        self._store[key] = str(val)
        return val

    def expire(self, key, seconds):
        pass

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

    def hset(self, key, field=None, value=None, mapping=None):
        if key not in self._store:
            self._store[key] = {}
        if mapping:
            self._store[key].update(mapping)
        elif field is not None:
            if not isinstance(self._store.get(key), dict):
                self._store[key] = {}
            self._store[key][field] = value

    def hget(self, key, field):
        d = self._store.get(key)
        if isinstance(d, dict):
            return d.get(field)
        return None

    def hgetall(self, key):
        d = self._store.get(key, {})
        return d if isinstance(d, dict) else {}

    def hincrby(self, key, field, amount):
        d = self._store.setdefault(key, {})
        if not isinstance(d, dict):
            self._store[key] = {}
            d = self._store[key]
        current = int(d.get(field, 0))
        d[field] = str(current + amount)
        return current + amount

    def sadd(self, key, *values):
        if key not in self._sets:
            self._sets[key] = set()
        self._sets[key].update(values)

    def sismember(self, key, value):
        return value in self._sets.get(key, set())

    def smembers(self, key):
        return self._sets.get(key, set())

    def srem(self, key, *values):
        s = self._sets.get(key, set())
        for v in values:
            s.discard(v)

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

    def rpush(self, key, *values):
        self._cmds.append(("rpush", key, values))
        return self

    def set(self, key, value, ex=None):
        self._cmds.append(("set", key, value))
        return self

    def execute(self):
        for cmd in self._cmds:
            if cmd[0] == "rpush":
                self._redis.rpush(cmd[1], *cmd[2])
            elif cmd[0] == "set":
                self._redis.set(cmd[1], cmd[2])
        self._cmds.clear()
        return []


# ── MerkleLog tests ───────────────────────────────────────────────────────────


class TestAppendCreatesChain:
    def setup_method(self):
        self.redis = FakeRedis()
        self.log = MerkleLog(self.redis)

    def test_append_returns_merkle_entry(self):
        entry = self.log.append("test_op", "data1")
        assert isinstance(entry, MerkleEntry)

    def test_first_entry_uses_genesis_hash(self):
        entry = self.log.append("test_op", "data1")
        assert entry.prev_hash == GENESIS_HASH

    def test_second_entry_links_to_first(self):
        e1 = self.log.append("op1", "data1")
        e2 = self.log.append("op2", "data2")
        assert e2.prev_hash == e1.hash

    def test_entry_hash_is_correct(self):
        entry = self.log.append("my_op", "my_data")
        expected = _compute_hash(
            GENESIS_HASH, "my_op", "my_data", entry.timestamp_ms
        )
        assert entry.hash == expected

    def test_log_length_increments(self):
        assert self.log.length() == 0
        self.log.append("op1", "d1")
        assert self.log.length() == 1
        self.log.append("op2", "d2")
        assert self.log.length() == 2

    def test_entries_are_persisted_in_redis(self):
        self.log.append("op", "data")
        raw = self.redis.lrange(ENTRIES_KEY, 0, -1)
        assert len(raw) == 1

    def test_entry_contains_operation_and_data(self):
        entry = self.log.append("quarantine", '{"agent": "abc"}')
        assert entry.operation == "quarantine"
        assert entry.data == '{"agent": "abc"}'

    def test_timestamp_ms_is_positive_integer(self):
        entry = self.log.append("op", "data")
        assert isinstance(entry.timestamp_ms, int)
        assert entry.timestamp_ms > 0


class TestChainVerification:
    def setup_method(self):
        self.redis = FakeRedis()
        self.log = MerkleLog(self.redis)

    def test_empty_log_is_valid(self):
        assert self.log.verify_chain() is True

    def test_single_entry_chain_is_valid(self):
        self.log.append("op1", "data1")
        assert self.log.verify_chain() is True

    def test_multi_entry_chain_is_valid(self):
        for i in range(10):
            self.log.append(f"op{i}", f"data{i}")
        assert self.log.verify_chain() is True

    def test_chain_verification_after_many_appends(self):
        for i in range(50):
            self.log.append("bulk_op", f"payload_{i}")
        assert self.log.verify_chain() is True

    def test_get_entries_returns_correct_count(self):
        for i in range(5):
            self.log.append(f"op{i}", f"d{i}")
        entries = self.log.get_entries()
        assert len(entries) == 5

    def test_get_entries_slice(self):
        for i in range(10):
            self.log.append(f"op{i}", f"d{i}")
        entries = self.log.get_entries(start=2, end=4)
        assert len(entries) == 3


class TestTamperedEntryDetected:
    def setup_method(self):
        self.redis = FakeRedis()
        self.log = MerkleLog(self.redis)

    def test_tampered_operation_field_detected(self):
        self.log.append("original_op", "data")
        # Tamper with the stored entry
        raw = self.redis.lrange(ENTRIES_KEY, 0, -1)[0]
        d = json.loads(raw)
        d["operation"] = "tampered_op"
        self.redis._lists[ENTRIES_KEY][0] = json.dumps(d)
        # verify_chain should detect the hash mismatch
        assert self.log.verify_chain() is False

    def test_tampered_data_field_detected(self):
        self.log.append("op", "original_data")
        raw = self.redis.lrange(ENTRIES_KEY, 0, -1)[0]
        d = json.loads(raw)
        d["data"] = "malicious_data"
        self.redis._lists[ENTRIES_KEY][0] = json.dumps(d)
        assert self.log.verify_chain() is False

    def test_tampered_prev_hash_detected(self):
        self.log.append("op1", "d1")
        self.log.append("op2", "d2")
        # Tamper with the second entry's prev_hash
        raw = self.redis.lrange(ENTRIES_KEY, 1, 1)[0]
        d = json.loads(raw)
        d["prev_hash"] = "a" * 64
        self.redis._lists[ENTRIES_KEY][1] = json.dumps(d)
        assert self.log.verify_chain() is False

    def test_tampered_hash_field_directly_detected(self):
        self.log.append("op", "data")
        raw = self.redis.lrange(ENTRIES_KEY, 0, -1)[0]
        d = json.loads(raw)
        d["hash"] = "f" * 64  # wrong hash
        self.redis._lists[ENTRIES_KEY][0] = json.dumps(d)
        assert self.log.verify_chain() is False

    def test_deleted_middle_entry_breaks_chain(self):
        for i in range(5):
            self.log.append(f"op{i}", f"d{i}")
        # Remove the second entry (index 1) — shifts the chain
        del self.redis._lists[ENTRIES_KEY][1]
        assert self.log.verify_chain() is False


class TestCheckpointGeneration:
    def setup_method(self):
        self.redis = FakeRedis()
        self.log = MerkleLog(self.redis)

    def test_empty_log_has_no_checkpoint(self):
        assert self.log.get_checkpoint() is None

    def test_append_creates_checkpoint(self):
        self.log.append("op", "data")
        cp = self.log.get_checkpoint()
        assert cp is not None

    def test_checkpoint_contains_hash(self):
        entry = self.log.append("op", "data")
        cp = self.log.get_checkpoint()
        assert cp["hash"] == entry.hash

    def test_checkpoint_contains_timestamp_ms(self):
        self.log.append("op", "data")
        cp = self.log.get_checkpoint()
        assert "timestamp_ms" in cp
        assert cp["timestamp_ms"] > 0

    def test_checkpoint_updates_on_each_append(self):
        e1 = self.log.append("op1", "d1")
        cp1 = self.log.get_checkpoint()
        e2 = self.log.append("op2", "d2")
        cp2 = self.log.get_checkpoint()
        assert cp1["hash"] == e1.hash
        assert cp2["hash"] == e2.hash
        assert cp1["hash"] != cp2["hash"]

    def test_checkpoint_hash_matches_chain_tip(self):
        for i in range(5):
            self.log.append(f"op{i}", f"d{i}")
        entries = self.log.get_entries()
        tip_hash = entries[-1].hash
        cp = self.log.get_checkpoint()
        assert cp["hash"] == tip_hash

    def test_checkpoint_can_be_used_for_gossip(self):
        """Two logs with the same entries should produce the same checkpoint."""
        redis2 = FakeRedis()
        log2 = MerkleLog(redis2)

        # Append the same sequence to both logs at the same timestamps
        for i in range(3):
            e = self.log.append(f"op{i}", f"data{i}")
            # Manually replicate to log2 with the same timestamps and hashes
            raw = json.dumps(e.to_dict())
            redis2.rpush(ENTRIES_KEY, raw)
            redis2.set(CHECKPOINT_KEY, json.dumps({
                "hash": e.hash,
                "timestamp_ms": e.timestamp_ms,
            }))

        cp1 = self.log.get_checkpoint()
        cp2 = log2.get_checkpoint()
        assert cp1["hash"] == cp2["hash"]


# ── AdminManager tests ────────────────────────────────────────────────────────


class TestAdminMultiSig:
    def setup_method(self):
        self.redis = FakeRedis()
        merkle = MerkleLog(self.redis)
        self.admin = AdminManager(self.redis, merkle)
        # Register some operators
        self.admin.register_operator("op_alice")
        self.admin.register_operator("op_bob")

    def test_critical_op_requires_two_operators(self):
        with pytest.raises(InsufficientOperatorsError):
            self.admin.require_multi_sig("force_quarantine", ["op_alice"])

    def test_critical_op_with_two_operators_succeeds(self):
        # Should not raise
        self.admin.require_multi_sig("force_quarantine", ["op_alice", "op_bob"])

    def test_non_critical_op_requires_one_operator(self):
        self.admin.require_multi_sig("update_heartbeat", ["op_alice"])

    def test_non_critical_op_no_operators_raises(self):
        with pytest.raises(InsufficientOperatorsError):
            self.admin.require_multi_sig("update_heartbeat", [])

    def test_unregistered_operator_raises(self):
        with pytest.raises(UnauthorizedOperatorError):
            self.admin.require_multi_sig("force_quarantine", ["op_alice", "intruder"])

    def test_duplicate_operators_counted_once(self):
        with pytest.raises(InsufficientOperatorsError):
            # Same operator twice — deduplicated to 1
            self.admin.require_multi_sig("force_quarantine", ["op_alice", "op_alice"])

    def test_all_critical_operations_defined(self):
        assert "change_genesis_validators" in CRITICAL_OPERATIONS
        assert "force_quarantine" in CRITICAL_OPERATIONS
        assert "change_consensus_parameters" in CRITICAL_OPERATIONS

    def test_is_critical_operation(self):
        assert AdminManager.is_critical_operation("force_quarantine") is True
        assert AdminManager.is_critical_operation("read_logs") is False


class TestAdminActionLogging:
    def setup_method(self):
        self.redis = FakeRedis()
        self.merkle = MerkleLog(self.redis)
        self.admin = AdminManager(self.redis, self.merkle)
        self.admin.register_operator("op_alice")

    def test_log_admin_action_appends_to_actions(self):
        self.admin.log_admin_action("op_alice", "read_state", {})
        log = self.admin.get_action_log()
        assert len(log) == 1

    def test_log_admin_action_writes_to_merkle_log(self):
        self.admin.log_admin_action("op_alice", "force_quarantine", {"agent": "abc"})
        assert self.merkle.length() == 1

    def test_log_contains_operator_and_operation(self):
        self.admin.log_admin_action("op_alice", "test_action", {"key": "val"})
        log = self.admin.get_action_log()
        record = log[0]
        assert record["operator_id"] == "op_alice"
        assert record["operation"] == "test_action"

    def test_merkle_log_contains_admin_prefix(self):
        self.admin.log_admin_action("op_alice", "do_thing", {})
        entries = self.merkle.get_entries()
        assert entries[0].operation == "admin:do_thing"

    def test_session_recording(self):
        session_id = self.admin.start_session("op_alice")
        self.admin.log_admin_action("op_alice", "act1", {}, session_id=session_id)
        self.admin.log_admin_action("op_alice", "act2", {}, session_id=session_id)
        session = self.admin.get_session(session_id)
        assert session is not None
        assert int(session.get("actions_count", 0)) == 2

    def test_get_acl_command_returns_string(self):
        acl = AdminManager.get_acl_command(AdminRole.HEARTBEAT_WRITER)
        assert "heartbeat_writer" in acl

    def test_merkle_chain_valid_after_admin_actions(self):
        for i in range(5):
            self.admin.log_admin_action("op_alice", f"action_{i}", {"i": i})
        assert self.merkle.verify_chain() is True
