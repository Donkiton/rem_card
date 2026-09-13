from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace


from rem_card.app.emergency_merge_mode_a import EmergencyModeAMergeService
from rem_card.app.emergency_restore_probe import (
    EmergencyRestoreProbe,
    EmergencyRestoreProbeScheduler,
    _first_existing_lock,
)
from rem_card.app.role_session_lock import RoleSessionLock


def _write_lock(path: Path, *, host: str, pid: int, nonce: str, timestamp: float | None = None) -> dict:
    payload = {
        "timestamp": time.time() if timestamp is None else timestamp,
        "role": path.stem,
        "pid": pid,
        "host": host,
        "owner_id": f"{host}:{pid}:{path.stem}",
        "nonce": nonce,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _age(path: Path, seconds: float = 300.0) -> None:
    old = time.time() - seconds
    os.utime(path, (old, old))


def test_restore_probe_reclaims_proven_dead_local_lock(monkeypatch, tmp_path):
    lock_path = tmp_path / "session_locks" / "nurse.lock"
    _write_lock(lock_path, host=socket.gethostname(), pid=23188, nonce="dead-local")
    monkeypatch.setattr(RoleSessionLock, "_is_pid_alive_local", staticmethod(lambda _pid: False))

    assert _first_existing_lock(str(lock_path.parent), (lock_path.name,)) == ""
    assert not lock_path.exists()


def test_continuous_success_keeps_network_stable_beyond_first_window(monkeypatch, tmp_path):
    from rem_card.app import emergency_restore_probe as module

    clock = [100.0]
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    monkeypatch.setattr(module, "record_metric", lambda *args, **kwargs: None)
    probe = EmergencyRestoreProbe(role="nurse", runtime_context=SimpleNamespace(mode="emergency", baza_dir=""),
                                  store=SimpleNamespace(resolve_root=lambda: str(tmp_path)),
                                  success_rounds_required=3, stability_window_sec=60)
    session = SimpleNamespace(emergency_session_id="session", base_last_change_id=1)
    validation = SimpleNamespace(last_change_id=1, fingerprint={})
    context = SimpleNamespace(medical_db_path=str(tmp_path / "remote.db"), settings_db_path="")
    monkeypatch.setattr(probe, "_run_probe_round", lambda: probe._record_success(
        "merge_ready_mode_a", "mode_a_remote_unchanged", session, validation, context))
    for count in range(25):
        clock[0] = 100.0 + count * 15
        result = probe.run_probe_once()
        assert result["network_stable"] is (count >= 2)
    clock[0] += 80
    result = probe.run_probe_once()
    assert not result["network_stable"]
    assert result["consecutive_successes"] == 1


def test_restore_probe_preserves_live_and_unknown_local_locks(monkeypatch, tmp_path):
    lock_path = tmp_path / "session_locks" / "doctor.lock"
    _write_lock(lock_path, host=socket.gethostname(), pid=1234, nonce="live-local", timestamp=time.time() - 600)
    _age(lock_path)
    monkeypatch.setattr(RoleSessionLock, "_is_pid_alive_local", staticmethod(lambda _pid: True))

    assert _first_existing_lock(str(lock_path.parent), (lock_path.name,)) == str(lock_path)
    assert lock_path.exists()

    monkeypatch.setattr(RoleSessionLock, "_is_pid_alive_local", staticmethod(lambda _pid: None))
    assert _first_existing_lock(str(lock_path.parent), (lock_path.name,)) == str(lock_path)
    assert lock_path.exists()


def test_restore_probe_uses_foreign_heartbeat_and_reclaims_expired_lock(tmp_path):
    lock_path = tmp_path / "session_locks" / "nurse.lock"
    _write_lock(lock_path, host="another-workstation", pid=4567, nonce="foreign", timestamp=time.time() - 600)

    assert _first_existing_lock(str(lock_path.parent), (lock_path.name,)) == str(lock_path)
    assert lock_path.exists()

    _age(lock_path)
    assert _first_existing_lock(str(lock_path.parent), (lock_path.name,)) == ""
    assert not lock_path.exists()


def test_restore_probe_preserves_invalid_and_empty_lock_files(tmp_path):
    lock_dir = tmp_path / "session_locks"
    invalid = lock_dir / "doctor.lock"
    invalid.parent.mkdir(parents=True)
    invalid.write_text("", encoding="utf-8")
    _age(invalid)

    assert _first_existing_lock(str(lock_dir), (invalid.name,)) == str(invalid)
    assert invalid.exists()

    empty_object = lock_dir / "nurse.lock"
    empty_object.write_text("{}", encoding="utf-8")
    _age(empty_object)
    assert _first_existing_lock(str(lock_dir), (empty_object.name,)) == str(empty_object)
    assert empty_object.exists()


def test_stale_cleanup_preserves_lock_replaced_during_recheck(monkeypatch, tmp_path):
    lock_path = tmp_path / "session_locks" / "nurse.lock"
    stale = _write_lock(lock_path, host=socket.gethostname(), pid=23188, nonce="stale")
    replacement = dict(stale, pid=os.getpid(), nonce="replacement", timestamp=time.time())
    checker = RoleSessionLock(str(lock_path), role="nurse", owner_id="checker", stale_timeout_sec=75.0)
    monkeypatch.setattr(checker, "_is_pid_alive_local", lambda _pid: False)
    reads = 0

    def read_with_replacement():
        nonlocal reads
        reads += 1
        if reads == 1:
            return stale
        lock_path.write_text(json.dumps(replacement), encoding="utf-8")
        return replacement

    monkeypatch.setattr(checker, "_read_payload", read_with_replacement)

    assert checker.is_held_by_other() is True
    assert json.loads(lock_path.read_text(encoding="utf-8"))["nonce"] == "replacement"


def test_mode_a_final_recheck_uses_same_stale_lock_policy(monkeypatch, tmp_path):
    lock_path = tmp_path / "session_locks" / "nurse.lock"
    _write_lock(lock_path, host=socket.gethostname(), pid=23188, nonce="mode-a-stale")
    monkeypatch.setattr(RoleSessionLock, "_is_pid_alive_local", staticmethod(lambda _pid: False))
    service = EmergencyModeAMergeService.__new__(EmergencyModeAMergeService)

    status = service._recheck_session_locks(SimpleNamespace(session_locks_dir=str(lock_path.parent)))

    assert status["ok"] is True
    assert status["active_locks"] == []
    assert not lock_path.exists()


def test_doctor_probe_reserves_shared_network_emergency_marker(monkeypatch, tmp_path):
    from rem_card.app import runtime_paths

    monkeypatch.setattr(runtime_paths, "is_compiled", lambda: True)
    probe = EmergencyRestoreProbe.__new__(EmergencyRestoreProbe)
    probe.role = "doctor"
    probe._network_emergency_role_lock = None
    context = SimpleNamespace(session_locks_dir=str(tmp_path / "session_locks"))
    session = SimpleNamespace(emergency_session_id="shared-session")

    try:
        probe._ensure_network_emergency_role_marker(context, session)
        marker = Path(context.session_locks_dir) / "nurse_emergency.lock"
        payload = json.loads(marker.read_text(encoding="utf-8"))
        assert payload["owner_role"] == "doctor"
        assert "shared-session" in payload["owner_id"]
    finally:
        probe.release_network_emergency_role_marker()


def test_doctor_probe_does_not_erase_other_live_emergency_marker(monkeypatch, tmp_path):
    from rem_card.app import runtime_paths

    monkeypatch.setattr(runtime_paths, "is_compiled", lambda: True)
    marker = tmp_path / "session_locks" / "nurse_emergency.lock"
    _write_lock(marker, host=socket.gethostname(), pid=os.getpid(), nonce="other-live-session")
    probe = EmergencyRestoreProbe.__new__(EmergencyRestoreProbe)
    probe.role = "doctor"
    probe._network_emergency_role_lock = None

    probe._ensure_network_emergency_role_marker(
        SimpleNamespace(session_locks_dir=str(marker.parent)),
        SimpleNamespace(emergency_session_id="current-session"),
    )

    assert probe._network_emergency_role_lock is None
    assert json.loads(marker.read_text(encoding="utf-8"))["nonce"] == "other-live-session"


def test_manual_probe_request_interrupts_backoff_without_concurrent_runs():
    class Probe:
        enabled = True

        def __init__(self):
            self.calls = 0
            self.active = 0
            self.max_active = 0
            self.second_call = threading.Event()

        def run_probe_once(self):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                self.calls += 1
                if self.calls >= 2:
                    self.second_call.set()
                time.sleep(0.03)
                return {"status": "session_lock_active", "last_probe_ts": time.time()}
            finally:
                self.active -= 1

        def get_status(self):
            return {"status": "session_lock_active", "last_probe_ts": time.time()}

        def is_network_stable(self):
            return False

        def release_network_emergency_role_marker(self):
            return None

    probe = Probe()
    scheduler = EmergencyRestoreProbeScheduler(probe, interval_sec=30.0, failure_backoff_sec=30.0)
    try:
        assert scheduler.start() is True
        deadline = time.time() + 1.0
        while probe.calls < 1 and time.time() < deadline:
            time.sleep(0.01)
        assert probe.calls == 1

        requested_at = time.monotonic()
        assert scheduler.request_probe("manual") is True
        assert probe.second_call.wait(0.5)
        assert time.monotonic() - requested_at < 0.5
        assert probe.max_active == 1
    finally:
        assert scheduler.stop(timeout=1.0) is True


def test_pid_query_keeps_current_process_without_signalling_it():
    assert RoleSessionLock._is_pid_alive_local(os.getpid()) is True
    assert EmergencyRestoreProbe.is_enabled_for_runtime("doctor", "emergency") is True
    assert EmergencyRestoreProbe.is_enabled_for_runtime("nurse", "emergency") is True
