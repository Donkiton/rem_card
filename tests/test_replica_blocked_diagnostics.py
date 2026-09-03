from unittest.mock import Mock

from rem_card.app import local_replica_sync as module
from rem_card.app.local_replica_sync import LocalReplicaSync
from rem_card.app.local_replica_worker import LocalReplicaSnapshotBusy, LocalReplicaWriterBusy


def test_prolonged_unreadable_gate_is_reported_without_degrading_database(tmp_path, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    metrics = []
    monkeypatch.setattr(module, "record_metric", lambda name, *a, **kw: metrics.append((name, kw)))
    worker = Mock()
    worker.sync.side_effect = LocalReplicaSnapshotBusy({"reason": "parse_error"})
    logger = Mock()
    sync = LocalReplicaSync(
        central_db_path=str(tmp_path / "central.db"), local_db_path=str(tmp_path / "local.db"),
        worker_client=worker, logger=logger,
    )
    failure_callback = Mock()
    sync.set_failure_callback(failure_callback)
    for seconds in (10, 20, 69):
        clock[0] = seconds
        assert not sync.sync_once()
    logger.warning.assert_not_called()
    for seconds in (70, 71, 300, 369, 370):
        clock[0] = seconds
        assert not sync.sync_once()
    warnings = [data for name, data in metrics if name == "local_replica_snapshot_blocked"]
    assert [item["blocked_sec"] for item in warnings] == [60.0, 360.0]
    assert all(item["reason"] == "parse_error" for item in warnings)
    assert logger.warning.call_count == 2
    assert not sync.health_snapshot()["degraded"]
    failure_callback.assert_not_called()

    # Unrelated contention and a successful attempt reset the warning window.
    worker.sync.side_effect = LocalReplicaWriterBusy()
    sync.sync_once()
    assert sync._snapshot_blocked_since is None
    worker.sync.side_effect = LocalReplicaSnapshotBusy({"reason": "parse_error"})
    sync.sync_once()
    assert sync._snapshot_blocked_since == 370
    worker.sync.side_effect = None
    worker.sync.return_value = {"status": "unchanged"}
    assert sync.sync_once()
    assert sync._snapshot_blocked_since is None
