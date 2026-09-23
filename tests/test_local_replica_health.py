from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path


_TEST_ROOT = Path(tempfile.mkdtemp(prefix="remcard_replica_health_tests_"))
os.environ["REMCARD_BAZA_DIR"] = str(_TEST_ROOT / "baza")
os.environ["REMCARD_DEV_BAZA_DIR"] = str(_TEST_ROOT / "dev_baza")
os.environ["REMCARD_LOCAL_LOGS_DIR"] = str(_TEST_ROOT / "logs")
os.environ["REMCARD_DATA_PATH_CONFIG"] = str(_TEST_ROOT / "remcard_data_path.json")
os.environ["REMCARD_CRASH_OUTBOX_DIR"] = str(_TEST_ROOT / "crash_outbox")
os.environ["REMCARD_STYLE_SETTINGS_PATH"] = str(_TEST_ROOT / "style.ini")
os.environ["REMCARD_CI_SETTINGS_DIR"] = str(_TEST_ROOT / "settings")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["LOCALAPPDATA"] = str(_TEST_ROOT / "local_appdata")

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

import pytest  # noqa: E402

from rem_card.app.local_replica_sync import LocalReplicaSync  # noqa: E402
from rem_card.app.local_replica_worker import LocalReplicaWriterBusy  # noqa: E402
from rem_card.services.crash_reports import validate_crash_payload  # noqa: E402
from rem_card.services.local_replica_health import (  # noqa: E402
    LocalReplicaRoleHealth,
    build_local_replica_health_path,
)
import rem_card.services.local_replica_health as health_module  # noqa: E402


class _Clock:
    def __init__(self, value: float = 1_800_000_000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


class _FailedWorker:
    def sync(self, **_kwargs):
        raise RuntimeError("synthetic replica failure")

    def close(self):
        return None


class _DeferredWorker:
    def sync(self, **_kwargs):
        raise LocalReplicaWriterBusy()

    def close(self):
        return None


class _UnchangedWorker:
    def sync(self, **_kwargs):
        return {
            "status": "unchanged",
            "state": {
                "db_cycle": "cycle-a",
                "change_cursor": 0,
                "schema_revision": "24",
            },
        }

    def close(self):
        return None


def _tracker(
    tmp_path: Path,
    *,
    clock: _Clock,
    reports: list[dict] | None = None,
    delivery_requests: list[str] | None = None,
) -> LocalReplicaRoleHealth:
    reporter_callback = None
    if reports is not None:
        def capture_report(details):
            reports.append(dict(details))
            return True

        reporter_callback = capture_report
    delivery_callback = None
    if delivery_requests is not None:
        def capture_delivery(root):
            delivery_requests.append(str(root))
            return True

        delivery_callback = capture_delivery
    return LocalReplicaRoleHealth(
        state_path=str(tmp_path / "replica.db.health.json"),
        role="doctor",
        database_path=str(tmp_path / "baza" / "archiv" / "rao_journal.db"),
        client_id="pc-one",
        stale_after_sec=24 * 60 * 60,
        reporter=reporter_callback,
        delivery_requester=delivery_callback,
        clock=clock,
    )


def test_copy_install_and_unchanged_verification_are_persisted_separately(tmp_path):
    clock = _Clock()
    tracker = _tracker(tmp_path, clock=clock, reports=[])

    tracker.record_success("snapshot_ready")
    installed_at = clock.value
    clock.value += 3600
    tracker.record_success("unchanged")

    persisted = json.loads(tracker.state_path.read_text(encoding="utf-8"))
    assert persisted["actual_copy_installed_at"] == installed_at
    assert persisted["last_unchanged_verified_at"] == clock.value
    assert persisted["last_successful_outcome"] == "unchanged"


def test_recent_unchanged_confirmation_prevents_false_alarm_then_stale_reports(tmp_path):
    clock = _Clock()
    reports: list[dict] = []
    tracker = _tracker(tmp_path, clock=clock, reports=reports)
    tracker.record_success("unchanged")

    clock.value += (24 * 60 * 60) - 1
    assert not tracker.record_role_entry_failure(
        outcome="failed",
        local_copy_valid=True,
        error_class="RuntimeError",
    )
    assert reports == []

    clock.value += 2
    assert tracker.record_role_entry_failure(
        outcome="failed",
        local_copy_valid=True,
        error_class="RuntimeError",
    )
    assert len(reports) == 1
    assert reports[0]["never_confirmed"] is False


def test_published_confirmation_age_advances_without_disk_write(tmp_path):
    clock = _Clock()
    tracker = _tracker(tmp_path, clock=clock, reports=[])
    tracker.record_success("snapshot_ready")
    installed_at = tracker.snapshot()["actual_copy_installed_at"]

    clock.value += 7200
    snapshot = tracker.snapshot()

    assert snapshot["actual_copy_installed_at"] == installed_at
    assert snapshot["last_confirmed_age_sec"] == 7200


def test_repeated_unchanged_checks_throttle_sidecar_fsync(tmp_path, monkeypatch):
    clock = _Clock()
    tracker = _tracker(tmp_path, clock=clock, reports=[])
    writes: list[dict] = []

    def capture_write(_path, payload):
        writes.append(dict(payload))

    monkeypatch.setattr(health_module, "_atomic_write_json", capture_write)
    tracker.record_success("unchanged")
    clock.value += 2
    tracker.record_success("unchanged")
    clock.value += 59
    tracker.record_success("unchanged")

    assert len(writes) == 2
    assert writes[-1]["last_unchanged_verified_at"] == clock.value


def test_first_success_of_new_instance_is_persisted_even_within_throttle(tmp_path, monkeypatch):
    clock = _Clock()
    first = _tracker(tmp_path, clock=clock, reports=[])
    first.record_success("unchanged")
    clock.value += 2
    restarted = _tracker(tmp_path, clock=clock, reports=[])
    writes: list[dict] = []
    monkeypatch.setattr(
        health_module,
        "_atomic_write_json",
        lambda _path, payload: writes.append(dict(payload)),
    )

    restarted.record_success("unchanged")

    assert len(writes) == 1
    assert writes[0]["last_unchanged_verified_at"] == clock.value


def test_snapshot_install_and_incident_recovery_persist_immediately(tmp_path, monkeypatch):
    clock = _Clock()
    reports: list[dict] = []
    tracker = _tracker(tmp_path, clock=clock, reports=reports)
    writes: list[dict] = []
    monkeypatch.setattr(
        health_module,
        "_atomic_write_json",
        lambda _path, payload: writes.append(dict(payload)),
    )

    tracker.record_success("unchanged")
    first_count = len(writes)
    clock.value += 2
    tracker.record_success("snapshot_ready")
    assert len(writes) == first_count + 1

    clock.value += (24 * 60 * 60) + 1
    assert tracker.record_role_entry_failure(
        outcome="failed",
        local_copy_valid=True,
    )
    incident_count = len(writes)
    clock.value += 2
    tracker.record_success("unchanged")
    assert len(writes) == incident_count + 1
    assert writes[-1]["incident_active"] is False


def test_never_confirmed_old_file_reports_once_across_restarts_and_recovery_resets(tmp_path):
    clock = _Clock()
    old_replica = tmp_path / "replica.db"
    old_replica.write_bytes(b"old-copy-marker")
    os.utime(old_replica, (clock.value - 30, clock.value - 30))
    reports: list[dict] = []
    tracker = _tracker(tmp_path, clock=clock, reports=reports)

    assert tracker.record_role_entry_failure(
        outcome="deferred",
        local_copy_valid=True,
        error_class="LocalReplicaWriterBusy",
    )
    assert len(reports) == 1
    assert reports[0]["never_confirmed"] is True

    restarted = _tracker(tmp_path, clock=clock, reports=reports)
    assert not restarted.record_role_entry_failure(
        outcome="failed",
        local_copy_valid=True,
        error_class="RuntimeError",
    )
    assert len(reports) == 1

    restarted.record_success("unchanged")
    assert restarted.snapshot()["incident_active"] is False
    clock.value += (24 * 60 * 60) + 1
    assert restarted.record_role_entry_failure(
        outcome="failed",
        local_copy_valid=True,
        error_class="RuntimeError",
    )
    assert len(reports) == 2


@pytest.mark.parametrize(
    ("worker", "expected_outcome"),
    [(_FailedWorker(), "failed"), (_DeferredWorker(), "deferred")],
)
def test_only_first_sync_attempt_of_role_entry_can_report(
    tmp_path,
    worker,
    expected_outcome,
):
    clock = _Clock()
    reports: list[dict] = []
    tracker = _tracker(tmp_path, clock=clock, reports=reports)
    sync = LocalReplicaSync(
        central_db_path=str(tmp_path / "central.db"),
        local_db_path=str(tmp_path / "local.db"),
        worker_client=worker,
        role_entry_health=tracker,
    )

    assert not sync.sync_once()
    assert not sync.sync_once()
    assert [item["check_result"] for item in reports] == [expected_outcome]


def test_persistent_timestamp_does_not_make_replica_ready_without_current_verification(tmp_path):
    clock = _Clock()
    tracker = _tracker(tmp_path, clock=clock, reports=[])
    tracker.record_success("unchanged")
    local_db = tmp_path / "local.db"
    conn = sqlite3.connect(local_db)
    conn.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    sync = LocalReplicaSync(
        central_db_path=str(tmp_path / "central.db"),
        local_db_path=str(local_db),
        worker_client=_UnchangedWorker(),
        role_entry_health=tracker,
    )
    sync._ensure_local_conn()

    assert not sync.is_ready(max_stale_sec=60)
    assert sync.sync_once()
    assert sync.is_ready(max_stale_sec=60)
    sync.stop()


def test_default_report_is_local_crash_outbox_payload_without_log_collection(tmp_path, monkeypatch):
    outbox_root = tmp_path / "crash-spool"
    monkeypatch.setenv("REMCARD_CRASH_OUTBOX_DIR", str(outbox_root))
    clock = _Clock()
    tracker = LocalReplicaRoleHealth(
        state_path=str(tmp_path / "replica.health.json"),
        role="nurse",
        database_path=str(tmp_path / "baza" / "archiv" / "rao_journal.db"),
        client_id="pc-two",
        reporter=None,
        delivery_requester=None,
        clock=clock,
    )

    assert tracker.record_role_entry_failure(
        outcome="failed",
        local_copy_valid=False,
        error_class="TimeoutError",
        error="synthetic timeout",
    )
    payload_path = next((outbox_root / "outbox").glob("*.json"))
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert validate_crash_payload(payload) == (True, "ok")
    assert payload["event_type"] == "local_replica_stale"
    assert payload["details"]["failure_kind"] == "stale_local_replica"
    assert not list(outbox_root.rglob("logs_last_hour.txt"))


def test_delivery_uses_explicit_database_root_on_entry_and_after_capture(tmp_path):
    clock = _Clock()
    reports: list[dict] = []
    deliveries: list[str] = []
    tracker = _tracker(
        tmp_path,
        clock=clock,
        reports=reports,
        delivery_requests=deliveries,
    )

    assert tracker.request_pending_delivery()
    assert tracker.record_role_entry_failure(
        outcome="failed",
        local_copy_valid=False,
    )
    expected_root = str(tmp_path / "baza")
    assert deliveries == [expected_root, expected_root]


def test_health_sidecar_path_is_distinct_from_replica_file(tmp_path):
    replica_path = tmp_path / "replica.db"
    assert build_local_replica_health_path(str(replica_path)) == str(
        replica_path
    ) + ".health.json"


def test_malformed_typed_sidecar_cannot_turn_successful_sync_into_failure(tmp_path):
    clock = _Clock()
    tracker = _tracker(tmp_path, clock=clock, reports=[])
    tracker.state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "database_key": tracker.database_key,
                "role": "doctor",
                "actual_copy_installed_at": {"bad": "type"},
                "last_unchanged_verified_at": "not-a-number",
                "incident_reported_at": ["bad"],
                "incident_started_at": float("nan"),
                "incident_active": "yes",
            }
        ),
        encoding="utf-8",
    )
    local_db = tmp_path / "local.db"
    conn = sqlite3.connect(local_db)
    conn.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    sync = LocalReplicaSync(
        central_db_path=str(tmp_path / "central.db"),
        local_db_path=str(local_db),
        worker_client=_UnchangedWorker(),
        role_entry_health=tracker,
    )
    sync._ensure_local_conn()

    assert sync.sync_once()
    assert tracker.snapshot()["last_successful_outcome"] == "unchanged"
    sync.stop()


def test_health_snapshot_does_no_io_and_does_not_wait_for_persistence(tmp_path, monkeypatch):
    clock = _Clock()
    tracker = _tracker(tmp_path, clock=clock, reports=[])
    entered_write = threading.Event()
    release_write = threading.Event()

    def blocked_write(_path, _payload):
        entered_write.set()
        assert release_write.wait(3)

    monkeypatch.setattr(health_module, "_atomic_write_json", blocked_write)
    writer = threading.Thread(
        target=tracker.record_success,
        args=("snapshot_ready",),
        daemon=True,
    )
    writer.start()
    assert entered_write.wait(1)

    started = time.perf_counter()
    snapshot = tracker.snapshot()
    elapsed = time.perf_counter() - started
    assert snapshot["last_successful_outcome"] == "snapshot_ready"
    assert elapsed < 0.1

    release_write.set()
    writer.join(timeout=2)
    assert not writer.is_alive()


def test_uninitialized_health_snapshot_does_not_read_sidecar(tmp_path, monkeypatch):
    tracker = _tracker(tmp_path, clock=_Clock(), reports=[])

    def forbidden_read(*_args, **_kwargs):
        raise AssertionError("snapshot attempted filesystem read")

    monkeypatch.setattr(Path, "read_text", forbidden_read)
    snapshot = tracker.snapshot()
    assert snapshot["actual_copy_installed_at"] == 0.0
