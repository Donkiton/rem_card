from __future__ import annotations

from types import SimpleNamespace
from dataclasses import replace
import os
from pathlib import Path

import pytest


def _lease(root, role="nurse", *, held=True):
    return SimpleNamespace(
        held=held,
        role=role,
        store=SimpleNamespace(root=root),
    )


def _simulate_fresh_path_import_state(monkeypatch):
    from rem_card.app import unified_preflight

    monkeypatch.setattr(unified_preflight, "_loaded_static_baza_roots", lambda: {})


@pytest.mark.parametrize('role', ['doctor', 'nurse'])
def test_explicit_restart_continuation_keeps_role_without_granting_admission(role):
    from rem_card.app.unified_preflight import attach_startup_request, build_startup_request, take_emergency_role_after_chooser_ready
    shell = SimpleNamespace()
    attach_startup_request(shell, build_startup_request(resume_role=role))
    assert take_emergency_role_after_chooser_ready(shell) == role
    assert take_emergency_role_after_chooser_ready(shell) is None


def test_invalid_copy_is_rejected_before_static_path_restart(tmp_path, monkeypatch):
    from rem_card.app import unified_preflight as p
    monkeypatch.setenv('REMCARD_EMERGENCY_DB_ROOT', str(tmp_path / 'local'))
    monkeypatch.setattr(p, '_loaded_static_baza_roots', lambda: {'paths': str(tmp_path / 'central')})
    monkeypatch.setattr(p, '_prepare_local_only_emergency_decision', lambda *a: SimpleNamespace(
        allowed=False, status='no_valid_standby', user_message='Копия устарела'))
    with pytest.raises(p.LocalOnlyStartupError) as error:
        p.prepare_local_only_runtime_context(role='nurse', central_root=str(tmp_path / 'central'),
            central_failure=p.CENTRAL_FAILURE_UNREACHABLE,
            confirm_startup=lambda *a: pytest.fail('invalid copy offered'),
            confirm_password=lambda *a: pytest.fail('invalid copy asked password'))
    assert error.value.status == 'no_valid_standby'


@pytest.mark.parametrize('cached_paths', [False, True])
def test_new_session_is_authorized_and_persisted_before_restart(tmp_path, monkeypatch, cached_paths):
    from rem_card.app import unified_preflight as p, emergency_startup
    local = tmp_path / 'local'
    central = tmp_path / 'central'
    calls = []
    leases = []
    monkeypatch.setenv('REMCARD_EMERGENCY_DB_ROOT', str(local))
    monkeypatch.setattr(p, '_loaded_static_baza_roots', lambda: {'paths': str(central)} if cached_paths else {})
    decision = SimpleNamespace(allowed=True, status='standby_available', user_message='Свежая копия',
        active_session_metadata=None, password_settings_db_path=str(local / 'settings.db'),
        standby_metadata=SimpleNamespace(source_remote_db_path=str(central / 'archiv' / 'rao_journal.db')))
    monkeypatch.setattr(p, '_prepare_local_only_emergency_decision', lambda *a: decision)
    monkeypatch.setattr(emergency_startup, 'start_or_resume_emergency_session',
        lambda *a, **kw: calls.append('persist') or SimpleNamespace(runtime_context=_emergency_context(local)))
    acquire = p._acquire_local_only_lease
    def track(*args):
        lease = acquire(*args)
        leases.append(lease)
        return lease
    monkeypatch.setattr(p, '_acquire_local_only_lease', track)
    with pytest.raises(p.LocalOnlyRestartRequired):
        p.prepare_local_only_runtime_context(role='nurse', central_root=str(central),
            central_failure=p.CENTRAL_FAILURE_UNREACHABLE, restart_after_activation=True,
            confirm_startup=lambda *a: calls.append('offer') or True,
            confirm_password=lambda *a: calls.append('password') or True)
    assert calls == ['offer', 'password', 'persist']
    assert len(leases) == 1 and not leases[0].held


def test_plain_role_hint_keeps_chooser_and_emergency_hint_dispatches_once():
    from rem_card.app.unified_preflight import (
        attach_startup_request,
        build_startup_request,
        complete_startup_request,
        get_startup_request,
        take_emergency_role_after_chooser_ready,
    )

    shell = SimpleNamespace()
    attach_startup_request(shell, build_startup_request(role="doctor"))
    assert take_emergency_role_after_chooser_ready(shell) is None

    attach_startup_request(
        shell,
        build_startup_request(role="nurse", emergency_startup_request="request.json"),
    )
    assert take_emergency_role_after_chooser_ready(shell) == "nurse"
    assert take_emergency_role_after_chooser_ready(shell) is None
    complete_startup_request(shell)
    assert get_startup_request(shell).emergency_startup_request == ""


def test_emergency_hint_requires_supported_role():
    from rem_card.app.unified_preflight import build_startup_request

    with pytest.raises(ValueError, match="doctor or nurse"):
        build_startup_request(role="operblock", emergency_startup_request="request.json")


def test_admitted_preflight_reuses_guard_without_second_role_lock(tmp_path, monkeypatch):
    from rem_card.app import main, runtime_paths
    from rem_card.app.unified_preflight import prepare_admitted_runtime_context

    runtime = object()
    observed = {}

    def prepare(args, active_local_case, splash, *, acquire_role_lock=True):
        observed.update(
            role=args.role,
            marker=args.emergency_startup_request,
            active_local_case=active_local_case,
            acquire_role_lock=acquire_role_lock,
        )
        return runtime, "", None

    monkeypatch.setattr(runtime_paths, "resolve_baza_dir", lambda: str(tmp_path))
    monkeypatch.setattr(main, "_has_active_local_operblock_case_before_network_probe", lambda role: False)
    monkeypatch.setattr(main, "_prepare_runtime_context_for_startup", prepare)

    result = prepare_admitted_runtime_context(
        role="nurse",
        central_lease=_lease(tmp_path),
        emergency_startup_request="request.json",
    )

    assert result is runtime
    assert observed == {
        "role": "nurse",
        "marker": "request.json",
        "active_local_case": False,
        "acquire_role_lock": False,
    }


@pytest.mark.parametrize(
    "lease",
    [
        lambda root: _lease(root, held=False),
        lambda root: _lease(root, role="doctor"),
        lambda root: _lease(root / "another"),
    ],
)
def test_admitted_preflight_rejects_invalid_central_admission(tmp_path, monkeypatch, lease):
    from rem_card.app import runtime_paths
    from rem_card.app.unified_preflight import prepare_admitted_runtime_context

    monkeypatch.setattr(runtime_paths, "resolve_baza_dir", lambda: str(tmp_path))
    with pytest.raises(RuntimeError, match="SessionLease"):
        prepare_admitted_runtime_context(role="nurse", central_lease=lease(tmp_path))


def test_bootstrap_cleanup_failure_cannot_open_fallback_runtime(monkeypatch):
    from rem_card.app import main

    class FailedBootstrap(RuntimeError):
        cleanup_failed = True
        runtime_container = object()

    fallback_calls = []
    monkeypatch.setattr(
        main,
        "_try_operblock_offline_startup_after_network_failure",
        lambda *args, **kwargs: fallback_calls.append("operblock"),
    )
    monkeypatch.setattr(
        main,
        "_try_emergency_startup_after_network_failure",
        lambda *args, **kwargs: fallback_calls.append("emergency"),
    )

    def failed_bootstrap(*, role, runtime_context=None):
        raise FailedBootstrap("cleanup incomplete")

    with pytest.raises(FailedBootstrap) as caught:
        main._bootstrap_container_with_emergency_fallback(
            failed_bootstrap,
            role="nurse",
            emergency_runtime_context=None,
        )

    assert caught.value.runtime_container is not None
    assert fallback_calls == []


def test_admitted_bootstrap_retains_central_lease_and_no_legacy_lock(tmp_path, monkeypatch):
    from rem_card.app import runtime_paths
    from rem_card.app.unified_preflight import bootstrap_admitted_container

    lease = _lease(tmp_path)
    runtime = object()
    container = object()
    observed = {}

    def direct_bootstrap(**kwargs):
        observed.update(kwargs)
        return container

    monkeypatch.setattr(runtime_paths, "resolve_baza_dir", lambda: str(tmp_path))

    result = bootstrap_admitted_container(
        direct_bootstrap,
        role="nurse",
        central_lease=lease,
        runtime_context=runtime,
        emergency_startup_request="request.json",
    )

    assert result == (container, runtime)
    assert lease.held is True
    assert observed == {"role": "nurse", "runtime_context": runtime}


def test_settings_admission_runs_shared_preflight_but_bootstraps_without_clinical_role(tmp_path, monkeypatch):
    from rem_card.app import runtime_paths
    from rem_card.app.unified_preflight import bootstrap_admitted_container

    observed = {}

    def direct_bootstrap(**kwargs):
        observed.update(kwargs)
        return object()

    monkeypatch.setattr(runtime_paths, "resolve_baza_dir", lambda: str(tmp_path))

    bootstrap_admitted_container(
        direct_bootstrap,
        role="settings",
        central_lease=_lease(tmp_path, role="settings"),
        runtime_context=None,
    )

    assert observed["role"] is None
    assert observed["runtime_context"] is None


def test_admitted_bootstrap_propagates_partial_owner_without_fallback(tmp_path, monkeypatch):
    from rem_card.app import main, runtime_paths
    from rem_card.app.unified_preflight import bootstrap_admitted_container

    owner = object()

    class FailedBootstrap(RuntimeError):
        cleanup_failed = True
        runtime_container = owner

    monkeypatch.setattr(runtime_paths, "resolve_baza_dir", lambda: str(tmp_path))
    monkeypatch.setattr(
        main,
        "_bootstrap_container_with_emergency_fallback",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("fallback must not run")),
    )

    with pytest.raises(FailedBootstrap) as caught:
        bootstrap_admitted_container(
            lambda **kwargs: (_ for _ in ()).throw(FailedBootstrap("incomplete cleanup")),
            role="nurse",
            central_lease=_lease(tmp_path),
            runtime_context=object(),
        )

    assert caught.value.runtime_container is owner
    assert caught.value.cleanup_failed is True


def test_central_admitted_emergency_context_keeps_restore_path_enabled(tmp_path, monkeypatch):
    from rem_card.app import runtime_paths
    from rem_card.app.unified_preflight import bootstrap_admitted_container

    runtime = _emergency_context(tmp_path / "local-emergency")
    restore_scheduler = object()
    container = SimpleNamespace(emergency_restore_probe_scheduler=restore_scheduler)
    monkeypatch.setattr(runtime_paths, "resolve_baza_dir", lambda: str(tmp_path / "central"))

    result, selected = bootstrap_admitted_container(
        lambda **kwargs: container,
        role="nurse",
        central_lease=_lease(tmp_path / "central"),
        runtime_context=runtime,
    )

    assert result is container
    assert selected is runtime
    assert result.emergency_restore_probe_scheduler is restore_scheduler


def _emergency_context(local_root, session_id="session-1"):
    from rem_card.app.db_runtime_context import build_emergency_runtime_context

    return build_emergency_runtime_context(str(local_root / "active" / session_id))


def test_local_only_emergency_uses_local_root_and_never_resolves_central(tmp_path, monkeypatch):
    from rem_card.app import emergency_startup, runtime_paths, unified_preflight
    from rem_card.app.unified_preflight import (
        CENTRAL_FAILURE_UNREACHABLE,
        prepare_local_only_runtime_context,
    )

    central_root = tmp_path / "central-unavailable"
    local_root = tmp_path / "local-emergency"
    context = _emergency_context(local_root)
    decision = SimpleNamespace(
        allowed=True,
        status="active_session_available",
        user_message="Открыть локальную сессию?",
        active_session_metadata=SimpleNamespace(
            base_remote_db_path=str(central_root / "archiv" / "rao_journal.db"),
        ),
        password_settings_db_path=context.settings_db_path,
    )
    calls = []
    _simulate_fresh_path_import_state(monkeypatch)
    monkeypatch.setenv("REMCARD_EMERGENCY_DB_ROOT", str(local_root))
    monkeypatch.setattr(
        runtime_paths,
        "resolve_baza_dir",
        lambda: (_ for _ in ()).throw(AssertionError("central root must not be resolved")),
    )
    monkeypatch.setattr(
        unified_preflight,
        "_prepare_local_only_emergency_decision",
        lambda role, root=None: calls.append(("prepare", role, root)) or decision,
    )
    monkeypatch.setattr(
        emergency_startup,
        "start_or_resume_emergency_session",
        lambda value, root=None: calls.append(("start", root)) or SimpleNamespace(runtime_context=context),
    )

    admission = prepare_local_only_runtime_context(
        role="nurse",
        central_root=str(central_root),
        central_failure=CENTRAL_FAILURE_UNREACHABLE,
        confirm_startup=lambda message, button: calls.append(("confirm", message, button)) or True,
        confirm_password=lambda path: (_ for _ in ()).throw(AssertionError("active session needs no password")),
    )
    try:
        assert admission.local_lease.held is True
        assert admission.role == "nurse"
        assert admission.local_root == str(local_root)
        assert admission.central_root == str(central_root)
        assert admission.runtime_context is context
        assert calls[0] == ("prepare", "nurse", str(local_root))
        assert calls[-1] == ("start", str(local_root))
    finally:
        admission.local_lease.release()


def test_local_only_never_bypasses_authoritative_maintenance_rejection(tmp_path, monkeypatch):
    from rem_card.app import emergency_paths
    from rem_card.app.unified_preflight import LocalOnlyStartupError, prepare_local_only_runtime_context

    monkeypatch.setattr(
        emergency_paths,
        "resolve_emergency_root",
        lambda *args: (_ for _ in ()).throw(AssertionError("local files must not be inspected")),
    )
    with pytest.raises(LocalOnlyStartupError, match="недоступности") as caught:
        prepare_local_only_runtime_context(
            role="nurse",
            central_root=str(tmp_path / "central"),
            central_failure="maintenance",
            confirm_startup=lambda *_: True,
        )
    assert caught.value.status == "central_failure_not_unreachable"


def test_local_only_requires_restart_when_static_paths_use_central_root(tmp_path, monkeypatch):
    import sys

    from rem_card.app.unified_preflight import LocalOnlyRestartRequired, require_fresh_local_only_import_state

    local_root = tmp_path / "local-emergency"
    central_root = tmp_path / "central"
    monkeypatch.setenv("REMCARD_EMERGENCY_DB_ROOT", str(local_root))
    monkeypatch.setitem(sys.modules, "rem_card.app.paths", SimpleNamespace(BAZA_DIR=str(central_root)))

    with pytest.raises(LocalOnlyRestartRequired) as caught:
        require_fresh_local_only_import_state(role="nurse")

    assert caught.value.status == "local_restart_required"
    assert caught.value.local_root == str(local_root)
    assert caught.value.loaded_roots == {"rem_card.app.paths": str(central_root)}


def test_local_only_accepts_static_paths_already_bound_to_same_local_root(tmp_path, monkeypatch):
    import sys

    from rem_card.app.unified_preflight import require_fresh_local_only_import_state

    local_root = tmp_path / "local-emergency"
    monkeypatch.setitem(sys.modules, "rem_card.app.paths", SimpleNamespace(BAZA_DIR=str(local_root)))

    assert require_fresh_local_only_import_state(role="doctor", local_root=str(local_root)) == str(local_root)


def test_local_runtime_path_escape_is_rejected_and_local_lease_released(tmp_path, monkeypatch):
    from rem_card.app import emergency_startup, unified_preflight
    from rem_card.app.unified_preflight import (
        CENTRAL_FAILURE_UNREACHABLE,
        LocalOnlyStartupError,
        prepare_local_only_runtime_context,
    )

    local_root = tmp_path / "local-emergency"
    escaped = replace(_emergency_context(local_root), medical_db_path=str(tmp_path / "outside.db"))
    decision = SimpleNamespace(
        allowed=True,
        status="active_session_available",
        user_message="Открыть?",
        active_session_metadata=SimpleNamespace(
            base_remote_db_path=str(tmp_path / "central" / "archiv" / "rao_journal.db"),
        ),
        password_settings_db_path=escaped.settings_db_path,
    )
    lease_holder = {}
    _simulate_fresh_path_import_state(monkeypatch)
    monkeypatch.setenv("REMCARD_EMERGENCY_DB_ROOT", str(local_root))
    monkeypatch.setattr(unified_preflight, "_prepare_local_only_emergency_decision", lambda *args: decision)
    monkeypatch.setattr(
        emergency_startup,
        "start_or_resume_emergency_session",
        lambda *args, **kwargs: SimpleNamespace(runtime_context=escaped),
    )
    from rem_card.app import unified_preflight

    original_acquire = unified_preflight._acquire_local_only_lease

    def acquire(root, role):
        lease = original_acquire(root, role)
        lease_holder["lease"] = lease
        return lease

    monkeypatch.setattr(unified_preflight, "_acquire_local_only_lease", acquire)

    with pytest.raises(LocalOnlyStartupError, match="вне аварийного") as caught:
        prepare_local_only_runtime_context(
            role="doctor",
            central_root=str(tmp_path / "central"),
            central_failure=CENTRAL_FAILURE_UNREACHABLE,
            confirm_startup=lambda *_: True,
        )
    assert caught.value.status == "local_runtime_path_escape"
    assert lease_holder["lease"].held is False


def test_local_only_operblock_session_gets_no_network_path(tmp_path, monkeypatch):
    from rem_card.app import operblock_offline_store
    from rem_card.app.db_runtime_context import build_operblock_offline_runtime_context
    from rem_card.app.unified_preflight import CENTRAL_FAILURE_UNREACHABLE, prepare_local_only_runtime_context

    local_root = tmp_path / "operblock-local"
    context = build_operblock_offline_runtime_context(str(local_root / "active"))
    observed = {}
    _simulate_fresh_path_import_state(monkeypatch)
    monkeypatch.setattr(operblock_offline_store, "get_operblock_offline_root", lambda: str(local_root))

    def start(**kwargs):
        observed.update(kwargs)
        return SimpleNamespace(runtime_context=context)

    monkeypatch.setattr(operblock_offline_store, "start_or_resume_operblock_offline_session", start)
    admission = prepare_local_only_runtime_context(
        role="operblock_planned",
        central_root=str(tmp_path / "central"),
        central_failure=CENTRAL_FAILURE_UNREACHABLE,
        confirm_startup=lambda *_: True,
        confirm_password=lambda *_: (_ for _ in ()).throw(AssertionError("operblock has no password")),
    )
    try:
        assert admission.mode == "opblock_offline"
        assert observed["network_db_path"] is None
        assert observed["root"] == str(local_root)
    finally:
        admission.local_lease.release()


def test_bootstrap_local_only_disables_central_probe_but_keeps_local_services(tmp_path, monkeypatch):
    from rem_card.app.unified_preflight import (
        LOCAL_ONLY_STATE_PROPERTY,
        LocalOnlyRuntimeAdmission,
        bootstrap_local_only,
    )

    local_root = tmp_path / "local-emergency"
    context = _emergency_context(local_root)
    lease = _lease(local_root)
    lease.store.root = local_root
    scheduler = SimpleNamespace(stop_calls=[], stop=lambda timeout: scheduler.stop_calls.append(timeout) or True)
    standby_scheduler = SimpleNamespace(
        stop_calls=[],
        stop=lambda timeout: standby_scheduler.stop_calls.append(timeout) or True,
    )
    data_service = SimpleNamespace(
        restore_assigned=[],
        standby_assigned=[],
        set_emergency_standby_scheduler=lambda value: data_service.standby_assigned.append(value),
        set_emergency_restore_probe_scheduler=lambda value: data_service.restore_assigned.append(value),
    )
    container = SimpleNamespace(
        data_service=data_service,
        emergency_restore_probe_scheduler=scheduler,
        emergency_standby_scheduler=standby_scheduler,
    )
    shell = SimpleNamespace(properties={}, setProperty=lambda key, value: shell.properties.update({key: value}))
    admission = LocalOnlyRuntimeAdmission(
        role="nurse",
        central_root=str(tmp_path / "central"),
        local_root=str(local_root),
        runtime_context=context,
        local_lease=lease,
    )
    observed = {}
    _simulate_fresh_path_import_state(monkeypatch)

    result = bootstrap_local_only(
        lambda **kwargs: observed.update(kwargs) or container,
        admission=admission,
        shell=shell,
    )

    assert result is container
    assert observed == {"role": "nurse", "runtime_context": context}
    assert standby_scheduler.stop_calls == [1.0]
    assert scheduler.stop_calls == [1.0]
    assert container.emergency_restore_probe_scheduler is None
    assert container.emergency_standby_scheduler is None
    assert data_service.standby_assigned == [None]
    assert data_service.restore_assigned == [None]
    assert data_service.central_access_allowed is False
    assert shell.properties[LOCAL_ONLY_STATE_PROPERTY]["central_reacquire_required"] is True


def test_emergency_snapshot_must_belong_to_selected_central_root(tmp_path, monkeypatch):
    from rem_card.app import unified_preflight
    from rem_card.app.unified_preflight import (
        CENTRAL_FAILURE_UNREACHABLE,
        LocalOnlyStartupError,
        prepare_local_only_runtime_context,
    )

    local_root = tmp_path / "local-emergency"
    decision = SimpleNamespace(
        allowed=True,
        status="active_session_available",
        user_message="Открыть?",
        active_session_metadata=SimpleNamespace(
            base_remote_db_path=str(tmp_path / "other-central" / "archiv" / "rao_journal.db"),
        ),
    )
    monkeypatch.setenv("REMCARD_EMERGENCY_DB_ROOT", str(local_root))
    _simulate_fresh_path_import_state(monkeypatch)
    monkeypatch.setattr(unified_preflight, "_prepare_local_only_emergency_decision", lambda *args: decision)
    confirmed = []

    with pytest.raises(LocalOnlyStartupError) as caught:
        prepare_local_only_runtime_context(
            role="nurse",
            central_root=str(tmp_path / "selected-central"),
            central_failure=CENTRAL_FAILURE_UNREACHABLE,
            confirm_startup=lambda *_: confirmed.append(True) or True,
        )

    assert caught.value.status == "local_snapshot_central_mismatch"
    assert confirmed == []


def test_local_standby_validation_does_not_construct_network_context(tmp_path, monkeypatch):
    from rem_card.app import emergency_standby, emergency_startup, emergency_workflow, unified_preflight

    local_root = tmp_path / "local-emergency"
    metadata = SimpleNamespace(
        medical_db_path=str(local_root / "standby" / "rao_journal_standby.db"),
        settings_db_path=str(local_root / "standby" / "remcard_settings_standby.db"),
        source_remote_db_path=str(tmp_path / "central" / "archiv" / "rao_journal.db"),
        updated_at="2026-01-01T12:00:00", created_at="2026-01-01T12:00:00",
    )

    status = emergency_standby.EmergencyStandbyRefreshResult(ok=True, status="valid", reason="ok", metadata=metadata)
    monkeypatch.setattr(emergency_startup, "find_resumable_active_session", lambda store: (None, "no resumable active session"))
    monkeypatch.setattr(emergency_startup, "_standby_metadata_matches_files", lambda *args: (True, "ok"))
    monkeypatch.setattr(emergency_workflow, "validate_emergency_patient_source", lambda path: "")
    monkeypatch.setattr(emergency_standby.EmergencyStandbyManager, "validate_standby", lambda self: status)
    monkeypatch.setattr(
        emergency_standby,
        "build_network_runtime_context",
        lambda: (_ for _ in ()).throw(AssertionError("network context must not be constructed")),
    )
    monkeypatch.setattr(
        emergency_standby.EmergencyStandbyManager,
        "__init__",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network-aware constructor must not run")),
    )

    decision = unified_preflight._prepare_local_only_emergency_decision("nurse", str(local_root))

    assert decision.allowed is True
    assert decision.standby_metadata is metadata


def test_local_bootstrap_configures_environment_before_lazy_import(tmp_path, monkeypatch):
    from rem_card.app import unified_preflight
    from rem_card.app.unified_preflight import LocalOnlyRuntimeAdmission, bootstrap_local_only

    local_root = tmp_path / "local-emergency"
    context = _emergency_context(local_root)
    admission = LocalOnlyRuntimeAdmission(
        role="doctor",
        central_root=str(tmp_path / "central"),
        local_root=str(local_root),
        runtime_context=context,
        local_lease=_lease(local_root, role="doctor"),
    )
    container = SimpleNamespace(data_service=SimpleNamespace(), emergency_restore_probe_scheduler=None)
    _simulate_fresh_path_import_state(monkeypatch)

    def load_bootstrap():
        assert __import__("os").environ["REMCARD_UNIFIED_LOCAL_ONLY"] == "1"
        assert __import__("os").environ["REMCARD_BAZA_DIR"] == str(local_root)
        assert __import__("os").environ["REMCARD_LOCAL_FIRST_SYNC"] == "0"
        assert __import__("os").environ["REMCARD_LOCAL_OUTBOX_SYNC"] == "0"
        return lambda **kwargs: container

    monkeypatch.setattr(unified_preflight, "_load_local_bootstrap", load_bootstrap)

    assert bootstrap_local_only(admission=admission) is container


def test_local_bootstrap_suppresses_scheduler_factories_before_container_init(tmp_path, monkeypatch):
    import sys

    from rem_card.app.unified_preflight import LocalOnlyRuntimeAdmission, bootstrap_local_only

    local_root = tmp_path / "local-emergency"
    context = _emergency_context(local_root)
    calls = []
    _simulate_fresh_path_import_state(monkeypatch)

    class FakeContainer:
        def _create_emergency_standby_scheduler(self, role):
            calls.append(("standby", role))
            return object()

        def _create_emergency_restore_probe_scheduler(self, role):
            calls.append(("restore", role))
            return object()

        def __init__(self, role):
            self.data_service = SimpleNamespace()
            self.emergency_standby_scheduler = self._create_emergency_standby_scheduler(role)
            self.emergency_restore_probe_scheduler = self._create_emergency_restore_probe_scheduler(role)

    def fake_bootstrap(*, role, runtime_context):
        assert runtime_context is context
        return FakeContainer(role)

    module_name = "remcard_test_local_bootstrap"
    fake_bootstrap.__module__ = module_name
    monkeypatch.setitem(sys.modules, module_name, SimpleNamespace(Container=FakeContainer))
    admission = LocalOnlyRuntimeAdmission(
        role="nurse",
        central_root=str(tmp_path / "central"),
        local_root=str(local_root),
        runtime_context=context,
        local_lease=_lease(local_root),
    )

    container = bootstrap_local_only(fake_bootstrap, admission=admission)

    assert calls == []
    assert container.emergency_standby_scheduler is None
    assert container.emergency_restore_probe_scheduler is None
    # The scoped patch is restored for any later central bootstrap.
    assert FakeContainer("nurse").emergency_restore_probe_scheduler is not None
    assert calls == [("standby", "nurse"), ("restore", "nurse")]


def test_real_local_only_bootstrap_from_synthetic_standby_never_touches_central(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys
    import textwrap

    from scripts.regression_checks.emergency_standby import _prepare_emergency_store_fixture

    store, metadata = _prepare_emergency_store_fixture(str(tmp_path / "fixture"))
    central_root = tmp_path / "forbidden-central"
    metadata = replace(
        metadata,
        source_remote_db_path=str(central_root / "archiv" / "rao_journal.db"),
        source_settings_db_path=str(central_root / "settings" / "remcard_settings.db"),
    )
    store.write_standby_metadata(metadata)
    script = textwrap.dedent(
        r"""
        import builtins
        import os
        import sqlite3
        import sys

        local_root = os.path.abspath(sys.argv[1])
        forbidden_root = os.path.abspath(sys.argv[2])
        forbidden_token = os.path.normcase(forbidden_root).replace("\\", "/")

        def is_forbidden(value):
            try:
                raw = os.fspath(value)
            except TypeError:
                raw = value
            if isinstance(raw, bytes):
                raw = os.fsdecode(raw)
            return forbidden_token in os.path.normcase(str(raw)).replace("\\", "/")

        def audit(event, args):
            if event == "open" or event.startswith("os.") or event.startswith("sqlite3."):
                if any(is_forbidden(value) for value in args):
                    raise AssertionError(f"central access forbidden: {event} {args!r}")

        sys.addaudithook(audit)
        for name in ("stat", "lstat", "listdir", "scandir", "access"):
            original = getattr(os, name)
            def guarded(path, *args, _original=original, _name=name, **kwargs):
                if is_forbidden(path):
                    raise AssertionError(f"central access forbidden: os.{_name}({path!r})")
                return _original(path, *args, **kwargs)
            setattr(os, name, guarded)
        original_connect = sqlite3.connect
        def guarded_connect(database, *args, **kwargs):
            if is_forbidden(database):
                raise AssertionError(f"central access forbidden: sqlite3.connect({database!r})")
            return original_connect(database, *args, **kwargs)
        sqlite3.connect = guarded_connect

        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["REMCARD_EMERGENCY_DB_ROOT"] = local_root
        os.environ["REMCARD_BACKGROUND_INTEGRITY_ENABLED"] = "0"
        os.environ["REMCARD_STARTUP_QUICKCHECK_BACKGROUND_ENABLED"] = "0"
        from rem_card.app.unified_preflight import (
            CENTRAL_FAILURE_UNREACHABLE,
            LocalOnlyStartupError,
            LocalOnlyRestartRequired,
            bootstrap_local_only,
            prepare_local_only_runtime_context,
        )

        # A standby is not permission for a doctor to start an emergency session.
        try:
            prepare_local_only_runtime_context(
                role="doctor", central_root=forbidden_root,
                central_failure=CENTRAL_FAILURE_UNREACHABLE,
                confirm_startup=lambda *_: (_ for _ in ()).throw(AssertionError("doctor offered activation")),
            )
        except LocalOnlyStartupError as exc:
            assert exc.status == "role_not_allowed", exc
        else:
            raise AssertionError("doctor activated standby")

        try:
            prepare_local_only_runtime_context(
                role="nurse", central_root=forbidden_root,
                central_failure=CENTRAL_FAILURE_UNREACHABLE,
                confirm_startup=lambda *_: True, confirm_password=lambda *_: True,
                restart_after_activation=True,
            )
        except LocalOnlyRestartRequired:
            pass
        else:
            raise AssertionError("new activation must request restart")

        admission = prepare_local_only_runtime_context(
            role="nurse",
            central_root=forbidden_root,
            central_failure=CENTRAL_FAILURE_UNREACHABLE,
            confirm_startup=lambda *_: True,
            confirm_password=lambda *_: (_ for _ in ()).throw(AssertionError("active session asked password")),
        )
        container = None
        try:
            container = bootstrap_local_only(admission=admission)
            assert container.runtime_context.mode == "emergency"
            assert container.emergency_standby_scheduler is None
            assert container.emergency_restore_probe_scheduler is None
            assert os.path.commonpath((container.db_manager.db_path, local_root)) == local_root
            from rem_card.app import paths
            assert os.path.normcase(paths.BAZA_DIR) == os.path.normcase(local_root)
            row = container.db_manager._remcard_conn.execute("SELECT COUNT(*) FROM patients").fetchone()
            assert int(row[0]) >= 1
            active_path = container.db_manager.db_path
            from rem_card.app.unified_runtime import SessionShutdown
            result = SessionShutdown([container], role="nurse").run()
            assert result["ok"], result
            container = None
        finally:
            if container is not None:
                try:
                    container.data_service.shutdown()
                finally:
                    container.db_manager.close()
            admission.local_lease.release()
        doctor = prepare_local_only_runtime_context(
            role="doctor", central_root=forbidden_root,
            central_failure=CENTRAL_FAILURE_UNREACHABLE,
            confirm_startup=lambda *_: True,
            confirm_password=lambda *_: (_ for _ in ()).throw(AssertionError("doctor asked password for active session")),
        )
        try:
            assert doctor.runtime_context.medical_db_path == active_path
            doctor_container = bootstrap_local_only(admission=doctor)
            assert doctor_container.db_manager.db_path == active_path
            assert SessionShutdown([doctor_container], role="doctor").run()["ok"]
        finally:
            doctor.local_lease.release()
        print("LOCAL_ONLY_REAL_BOOTSTRAP_OK")
        """
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(Path(__file__).resolve().parents[2]), environment.get("PYTHONPATH", "")))
    )
    result = subprocess.run(
        [sys.executable, "-c", script, store.resolve_root(), str(central_root)],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "LOCAL_ONLY_REAL_BOOTSTRAP_OK" in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="Windows UNC path semantics")
@pytest.mark.parametrize("role", ["doctor", "nurse", "operblock_emergency", "operblock_planned"])
@pytest.mark.parametrize("configured", [r"\\localhost\RemCardTest", "\\\\localhost\\RemCardTest\\", "//LOCALHOST/remcardtest"])
def test_central_admission_accepts_equivalent_unc_share_roots(monkeypatch, role, configured):
    from rem_card.app import runtime_paths
    from rem_card.app.unified_preflight import _require_central_admission

    monkeypatch.setattr(runtime_paths, "resolve_baza_dir", lambda: configured)
    _require_central_admission(_lease(Path(r"\\localhost\RemCardTest"), role=role), role)


@pytest.mark.skipif(os.name != "nt", reason="Windows UNC path semantics")
@pytest.mark.parametrize("protected", [r"\\localhost\Other", r"\\other\RemCardTest", r"\\localhost\RemCardTest\child"])
def test_central_admission_rejects_different_unc_roots(monkeypatch, protected):
    from rem_card.app import runtime_paths
    from rem_card.app.unified_preflight import _require_central_admission

    monkeypatch.setattr(runtime_paths, "resolve_baza_dir", lambda: r"\\localhost\RemCardTest")
    with pytest.raises(RuntimeError, match="does not protect"):
        _require_central_admission(_lease(Path(protected)), "nurse")
