from __future__ import annotations

import unittest
import threading
from types import MethodType, SimpleNamespace
from unittest.mock import patch

from rem_card.app.network_write_worker import NetworkWriteWorkerTimeout
from rem_card.data.dao.db_manager import DatabaseManager
from rem_card.services.data_service import DataService


class _Signal:
    def __init__(self):
        self.values = []

    def emit(self, value):
        self.values.append(value)


class _Queue:
    @staticmethod
    def active_count():
        return 0

    @staticmethod
    def pending_count():
        return 0


def _failure_harness():
    harness = SimpleNamespace(
        _last_failure_category="",
        _unconfirmed_write_count=0,
        _unknown_active_write=False,
        _runtime_role="operblock",
        _queue=_Queue(),
        _outage_signal_emitted=False,
        network_outage_detected=_Signal(),
        blocked_info=None,
    )

    def block_new_writes_for_runtime_outage(self, info):
        self.blocked_info = dict(info)

    harness.block_new_writes_for_runtime_outage = MethodType(
        block_new_writes_for_runtime_outage,
        harness,
    )
    return harness


class NetworkWriteOutcomeHandlingTest(unittest.TestCase):
    def test_interactive_network_metadata_enables_isolated_worker(self):
        harness = SimpleNamespace(
            _runtime_role="operblock",
            _is_interactive_opblock_write=MethodType(
                DataService._is_interactive_opblock_write,
                SimpleNamespace(_runtime_role="operblock"),
            ),
        )

        metadata = DataService._opblock_interactive_write_metadata(
            harness,
            "operblock_save_vital:17",
            {"request_id": "request-17"},
        )

        self.assertTrue(metadata["interactive"])
        self.assertTrue(metadata["isolated_worker"])
        self.assertEqual(metadata["role"], "operblock")
        self.assertEqual(metadata["request_id"], "request-17")

    def test_unknown_result_is_not_marked_as_failed(self):
        harness = SimpleNamespace(
            _unknown_active_write=False,
            _unconfirmed_write_count=0,
        )
        harness._mark_operblock_write_failed = MethodType(
            DataService._mark_operblock_write_failed,
            harness,
        )
        exc = NetworkWriteWorkerTimeout(
            operation_id="operation-unknown",
            source="operblock_save",
            timeout_sec=7.0,
            phase="confirm",
            outcome_unknown=True,
        )

        with (
            patch("rem_card.app.operblock_offline_store.mark_operblock_write_outcome_unknown") as mark_unknown,
            patch("rem_card.app.operblock_offline_store.mark_operblock_write_failed") as mark_failed,
            patch("rem_card.services.data_service.record_metric"),
        ):
            DataService._mark_operblock_write_outcome(
                harness,
                "operation-unknown",
                "operblock_save",
                exc,
            )

        mark_unknown.assert_called_once()
        mark_failed.assert_not_called()
        self.assertTrue(harness._unknown_active_write)
        self.assertEqual(harness._unconfirmed_write_count, 0)

    def test_local_coordination_timeout_does_not_start_outage_transition(self):
        harness = _failure_harness()
        exc = NetworkWriteWorkerTimeout(
            operation_id="operation-local-busy",
            source="operblock_save",
            timeout_sec=0.5,
            phase="local_coordination",
            outcome_unknown=False,
        )

        category = DataService._handle_database_access_failure(
            harness,
            exc,
            source="operblock_save",
            write_description="operblock_save",
        )

        self.assertEqual(category, "locked_busy")
        self.assertIsNone(harness.blocked_info)
        self.assertEqual(harness._unconfirmed_write_count, 0)

    def test_confirmed_rollback_timeout_blocks_writes_without_unknown_warning(self):
        harness = _failure_harness()
        exc = NetworkWriteWorkerTimeout(
            operation_id="operation-rolled-back",
            source="operblock_save",
            timeout_sec=7.0,
            phase="execute",
            outcome_unknown=False,
        )

        category = DataService._handle_database_access_failure(
            harness,
            exc,
            source="operblock_save",
            write_description="operblock_save",
        )

        self.assertEqual(category, "network_unavailable")
        self.assertIsNotNone(harness.blocked_info)
        self.assertEqual(harness._unconfirmed_write_count, 0)
        self.assertFalse(harness._unknown_active_write)
        self.assertEqual(len(harness.network_outage_detected.values), 1)

    def test_network_worker_does_not_hold_parent_read_lock_while_waiting(self):
        manager = DatabaseManager.__new__(DatabaseManager)
        manager._thread_state = threading.local()
        manager._central_io_lock = threading.Lock()
        manager._local_replica = None
        manager._prefer_central_reads_until = 0.0

        class _Client:
            @staticmethod
            def execute(operation, **_kwargs):
                self.assertFalse(manager._central_io_lock.locked())
                return operation(SimpleNamespace())

        manager._network_write_worker_client = lambda: _Client()
        with patch("rem_card.data.dao.db_manager.record_metric"):
            result = DatabaseManager._run_isolated_network_write(
                manager,
                lambda _cursor: "ok",
                source="operblock_save",
                metadata={
                    "operation_id": "operation-read-lock",
                    "timeout_ms": 7000,
                },
            )

        self.assertEqual(result, "ok")

    def test_emergency_runtime_never_uses_network_worker(self):
        manager = DatabaseManager.__new__(DatabaseManager)
        manager.runtime_context = SimpleNamespace(mode="operblock_emergency")

        self.assertFalse(
            DatabaseManager._should_use_network_write_worker(
                manager,
                {
                    "isolated_worker": True,
                    "operation_id": "operation-emergency",
                },
            )
        )

    def test_repeated_replica_timeout_starts_network_outage_transition(self):
        harness = _failure_harness()
        harness._shutting_down = False
        harness._network_outage_detected = False
        harness._handle_database_access_failure = MethodType(
            DataService._handle_database_access_failure,
            harness,
        )

        DataService._handle_local_replica_sync_failure(
            harness,
            {
                "consecutive_failures": 2,
                "last_sync_error": (
                    "Обновление локальной реплики превысило безопасный "
                    "тайм-аут 6.0 с."
                ),
                "last_sync_error_class": "LocalReplicaWorkerTimeout",
            },
        )

        self.assertIsNotNone(harness.blocked_info)
        self.assertEqual(
            harness.blocked_info["source"],
            "local_replica_sync",
        )

    def test_replica_schema_error_does_not_start_network_outage(self):
        harness = _failure_harness()
        harness._shutting_down = False
        harness._network_outage_detected = False
        harness._handle_database_access_failure = MethodType(
            DataService._handle_database_access_failure,
            harness,
        )

        DataService._handle_local_replica_sync_failure(
            harness,
            {
                "consecutive_failures": 3,
                "last_sync_error": "no such table: change_log",
                "last_sync_error_class": "LocalReplicaWorkerError",
            },
        )

        self.assertIsNone(harness.blocked_info)


if __name__ == "__main__":
    unittest.main()
