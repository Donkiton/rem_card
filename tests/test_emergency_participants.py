from pathlib import Path
import logging
from types import SimpleNamespace

import pytest

from rem_card.app.emergency_participants import (
    EmergencyParticipant, EmergencySessionBusy, active_participants,
    exclusive_emergency_merge, finish_in_progress,
)


def test_doctor_and_nurse_share_finish_barrier(tmp_path):
    nurse = EmergencyParticipant(tmp_path, "nurse")
    doctor = EmergencyParticipant(tmp_path, "doctor")
    try:
        assert len(active_participants(tmp_path)) == 2
        assert not nurse.peers_ready()
        doctor.request_finish()
        assert nurse.another_window_is_finishing()
        assert finish_in_progress(tmp_path)
        with pytest.raises(EmergencySessionBusy):
            EmergencyParticipant(tmp_path, "doctor")
        with pytest.raises(EmergencySessionBusy):
            nurse.request_finish()
        with pytest.raises(EmergencySessionBusy):
            with exclusive_emergency_merge(tmp_path):
                pytest.fail("merge started while windows were open")
        nurse.release()
        assert doctor.peers_ready()
        doctor.cancel_finish()
        assert not finish_in_progress(tmp_path)
    finally:
        doctor.release()
        nurse.release()
    assert active_participants(tmp_path) == []


def test_merge_gate_blocks_a_new_window_until_completion(tmp_path):
    with exclusive_emergency_merge(tmp_path):
        assert finish_in_progress(tmp_path)
        with pytest.raises(EmergencySessionBusy):
            EmergencyParticipant(tmp_path, "nurse")
    participant = EmergencyParticipant(tmp_path, "doctor")
    participant.release()
    assert not finish_in_progress(tmp_path)


def test_peer_lock_is_not_deleted_by_another_participant(tmp_path):
    one = EmergencyParticipant(tmp_path, "nurse")
    two = EmergencyParticipant(tmp_path, "doctor")
    try:
        lock_path = Path(tmp_path) / "participants" / one.filename
        before = lock_path.read_bytes()
        assert not two.peers_ready()
        assert lock_path.read_bytes() == before
    finally:
        one.release()
        two.release()


def test_merge_gate_releases_after_failure(tmp_path):
    with pytest.raises(ValueError, match="test failure"):
        with exclusive_emergency_merge(tmp_path):
            raise ValueError("test failure")
    assert not finish_in_progress(tmp_path)


@pytest.mark.parametrize("drained", [False, True])
def test_participant_is_released_only_after_successful_database_shutdown(tmp_path, drained):
    from rem_card.app.main import _shutdown_window_resources

    participant = EmergencyParticipant(tmp_path, "nurse")
    events = []
    def shutdown():
        assert active_participants(tmp_path)
        events.append("drain")
        return drained
    def close_db():
        assert active_participants(tmp_path)
        events.append("close")
        return True
    container = SimpleNamespace(data_service=SimpleNamespace(shutdown=shutdown),
                                db_manager=SimpleNamespace(close=close_db), emergency_participant=participant)
    try:
        assert _shutdown_window_resources(SimpleNamespace(container=container), logging.getLogger(__name__)) is drained
        assert bool(active_participants(tmp_path)) is not drained
        assert events == (["drain", "close"] if drained else ["drain"])
    finally:
        participant.release()


@pytest.mark.parametrize("drained", [False, True])
def test_finalizer_keeps_network_role_lock_until_writes_have_drained(monkeypatch, drained):
    from rem_card.app import main, logger

    events = []
    monkeypatch.setattr(main, "_shutdown_window_resources", lambda *args: events.append("drain") or drained)
    monkeypatch.setattr(logger, "finalize_crash_handler", lambda **kwargs: None)
    window = SimpleNamespace(release_role_lock=lambda: events.append("release"))
    state = main._StartupRuntimeState(window=window, logger=logging.getLogger(__name__), exit_code=1,
                                     role_lock=None, emergency_runtime_context=None)
    main._finalize_startup_application(args=SimpleNamespace(role="doctor"), server=None,
                                     server_listening=False, server_name="test", QLocalServer=None, state=state)
    assert events == (["drain", "release"] if drained else ["drain"])
