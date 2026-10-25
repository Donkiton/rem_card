"""First-entry admission/seed must stay fenced across the mandatory DB upgrade."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


_STARTUP_CHECK = r'''
import json
import os
import pathlib
import sqlite3
import sys
import threading
import time

from _local_rem_card_bootstrap import bootstrap_local_rem_card
bootstrap_local_rem_card()

from PySide6 import QtCore
OriginalSettings = QtCore.QSettings
OriginalSettings.setDefaultFormat(OriginalSettings.IniFormat)
for scope in (OriginalSettings.UserScope, OriginalSettings.SystemScope):
    OriginalSettings.setPath(OriginalSettings.IniFormat, scope, os.environ['REMCARD_CI_SETTINGS_DIR'])

class IsolatedSettings(OriginalSettings):
    def __init__(self, *args, **kwargs):
        if len(args) >= 2 and isinstance(args[0], str) and isinstance(args[1], str):
            super().__init__(OriginalSettings.IniFormat, OriginalSettings.UserScope, *args, **kwargs)
        else:
            super().__init__(*args, **kwargs)

QtCore.QSettings = IsolatedSettings
from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])
app.setQuitOnLastWindowClosed(False)

from rem_card.app import bootstrap as boot
from rem_card.app import role_admission as admission
from rem_card.app import replica_provenance
from rem_card.app import startup_db_guard as guard
from rem_card.app import unified_db_schema as schema
from rem_card.app.db_runtime_context import build_network_runtime_context
from rem_card.app.local_replica_sync import LocalReplicaSync
from rem_card.app.startup_check_worker import startup_check_runner
from rem_card.app.unified_database_setup import prepare_database_root
from rem_card.ui.shared.startup_check import role_startup_check_runner

baza = pathlib.Path(os.environ['REMCARD_BAZA_DIR'])
version, role = int(sys.argv[1]), sys.argv[2]
baza.mkdir(parents=True)
ok, reason = prepare_database_root(str(baza))
assert ok, reason
context = build_network_runtime_context()
medical = pathlib.Path(context.medical_db_path)
assert medical.is_relative_to(baza)
assert pathlib.Path(context.settings_db_path).is_relative_to(baza)
with sqlite3.connect(medical) as connection:
    connection.execute('DROP INDEX idx_order_audit_admission_id')
    if version == 26:
        connection.execute('DROP INDEX idx_admin_committed_planned_expr_order_id')
    connection.execute('DELETE FROM schema_migrations WHERE version > ?', (version,))
    connection.execute('UPDATE meta SET value=? WHERE key=?', (version, schema.SCHEMA_FASTPATH_META_KEY))
    connection.execute("INSERT INTO patients(full_name) VALUES('synthetic before upgrade')")
    patient_id = connection.execute('SELECT last_insert_rowid()').fetchone()[0]
    connection.execute(
        'INSERT INTO admissions(patient_id,bed_number,history_number,admission_datetime) VALUES(?,1,?,?)',
        (patient_id, 'SYNTHETIC-UPGRADE', '2026-10-17 08:00:00'),
    )
    connection.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('db_cycle_started_at','synthetic-upgrade-cycle')")
policy_path = pathlib.Path(context.medical_client_policy_path)
policy = json.loads(policy_path.read_text(encoding='utf-8'))
policy['min_client_version'] = '5.1.6'
policy_path.write_text(json.dumps(policy), encoding='utf-8')

admission.invalidate_role_admission()
runner = role_startup_check_runner(threading.Event(), lambda _message: None)
with startup_check_runner(runner):
    result = guard.run_startup_db_guard(role)
assert result.ok, result.technical_reason
receipt, _ = admission.recent_admission(str(medical))
assert receipt and receipt['snapshot'], 'The real guard must provide an old-schema seed'
assert receipt['state']['migration'] == version
assert receipt['snapshot']['state']['schema_revision'] == str(version)
with sqlite3.connect(receipt['snapshot']['path']) as old_seed:
    assert old_seed.execute('SELECT MAX(version) FROM schema_migrations').fetchone() == (version,)

# Keep normal sync paused only long enough to inspect publication/freshness.
# No source/schema/cursor validation or worker response is replaced.
pending = []
original_start = LocalReplicaSync.start
def paused_start(replica):
    pending.append(replica)
LocalReplicaSync.start = paused_start
# These background schedulers do not participate in admission or replica sync.
boot.Container._create_emergency_standby_scheduler = lambda *_args: None
boot.Container._create_emergency_restore_probe_scheduler = lambda *_args: None

container = None
try:
    container = boot.bootstrap(role, runtime_context=context)
    manager = container.db_manager
    replica = manager._local_replica
    assert replica is not None and pending == [replica]
    assert pathlib.Path(replica.local_db_path).is_relative_to(pathlib.Path(os.environ['LOCALAPPDATA']))
    with sqlite3.connect(medical) as central:
        assert central.execute('SELECT MAX(version) FROM schema_migrations').fetchone() == (28,)
        assert schema.is_unified_schema_ready(central)
    assert json.loads(policy_path.read_text(encoding='utf-8'))['min_client_version'] == '5.1.11'
    assert replica._read_local_state()['schema_revision'] == str(version)
    assert replica.last_sync_ok_ts == 0
    assert not replica.is_ready(max_stale_sec=60)
    assert not manager._should_read_from_local()

    manager.run_write_operation(
        lambda cursor: cursor.execute('UPDATE patients SET full_name=? WHERE id=?',
                                      ('synthetic after upgrade', patient_id)),
        source='test_mandatory_upgrade_visibility',
    )
    required = manager._required_local_replica_cursor
    with sqlite3.connect(medical) as central:
        source_cursor = central.execute('SELECT MAX(id) FROM change_log').fetchone()[0]
    assert required == source_cursor and required > receipt['snapshot']['state']['change_cursor']
    assert not manager._should_read_from_local()
    row = manager.fetch_one_remcard('SELECT full_name FROM patients WHERE id=?', (patient_id,))
    assert row[0] == 'synthetic after upgrade'

    responses = []
    original_sync = replica._worker_client.sync
    def observed_sync(**kwargs):
        response = original_sync(**kwargs)
        responses.append((dict(kwargs.get('local_state') or {}), dict(response)))
        return response
    replica._worker_client.sync = observed_sync
    LocalReplicaSync.start = original_start
    original_start(replica)
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline and not manager._should_read_from_local():
        app.processEvents()
        time.sleep(0.01)
    assert replica.is_ready(max_stale_sec=60), replica.health_snapshot()
    assert manager._should_read_from_local(), replica.health_snapshot()
    assert responses
    first_request, first_response = responses[0]
    assert first_request['schema_revision'] == str(version)
    assert first_response['status'] == 'snapshot_ready', first_response
    assert first_response['sync_mode'] == 'full', first_response
    assert first_response['full_reason'] == 'schema_mismatch', first_response
    assert first_response['state']['schema_revision'] == '28'
    assert first_response['source_identity'] == admission.file_identity(str(medical))
    assert replica.last_state['change_cursor'] >= required
    assert replica._read_local_state()['schema_revision'] == '28'
    with sqlite3.connect(replica.local_db_path) as local:
        assert schema.is_unified_schema_ready(local)
        assert local.execute('SELECT full_name FROM patients WHERE id=?', (patient_id,)).fetchone() == (
            'synthetic after upgrade',
        )
    provenance = replica_provenance.read_baseline(replica.local_db_path, admission.file_identity(str(medical)))
    assert provenance and provenance['state']['schema_revision'] == '28'
    assert provenance['state']['change_cursor'] >= required
    assert manager.fetch_one_remcard('SELECT full_name FROM patients WHERE id=?', (patient_id,))[0] == (
        'synthetic after upgrade'
    )
    with manager.snapshot_read_scope('test_mandatory_upgrade_read_source'):
        assert manager.current_snapshot_read_source() == 'local_replica'
    report = {
        'old_schema': version, 'role': role, 'new_schema': 28,
        'seed_was_ready': False, 'sync_mode': first_response['sync_mode'],
        'full_reason': first_response.get('full_reason'), 'required_cursor_observed': True,
    }
finally:
    LocalReplicaSync.start = original_start
    if container is not None:
        assert container.settings_service.stop_deferred_release_retry(3)
        assert container.data_service.shutdown(timeout=5)
        assert container.db_manager.close(timeout_sec=5)
    admission.invalidate_role_admission()
    from rem_card.services.settings.settings_service import reset_settings_service
    reset_settings_service()
print('MANDATORY_UPGRADE_CHECK ' + json.dumps(report))
'''


@pytest.mark.parametrize("version", [26, 27])
@pytest.mark.parametrize("role", ["doctor", "nurse"])
def test_old_preflight_seed_stays_not_ready_until_real_sync_of_upgraded_database(tmp_path, version, role):
    source = Path(__file__).resolve().parents[1]
    profile = tmp_path / "profile"
    profile.mkdir()
    temp = tmp_path / "temp"
    temp.mkdir()
    environment = dict(os.environ)
    environment.pop("REMCARD_STARTUP_GUARD_QUICKCHECK_OK", None)
    environment.pop("REMCARD_INTERNAL_DEV_BAZA_PIN", None)
    environment.pop("REMCARD_VERSION_FILE", None)
    environment.update({
        # Этот сценарий проверяет обязательную миграцию релиза 5.1.11.
        "REMCARD_APP_VERSION": "5.1.11",
        "PYTHONPATH": str(source), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
        "QT_QPA_PLATFORM": "offscreen",
        "APPDATA": str(tmp_path / "roaming"), "LOCALAPPDATA": str(tmp_path / "local"),
        "USERPROFILE": str(profile), "HOMEDRIVE": profile.drive,
        "HOMEPATH": str(profile)[len(profile.drive):], "ProgramData": str(tmp_path / "programdata"),
        "TEMP": str(temp), "TMP": str(temp), "REMCARD_LOCAL_LOGS_DIR": str(tmp_path / "logs"),
        "REMCARD_CI_SETTINGS_DIR": str(tmp_path / "qsettings"),
        "REMCARD_BAZA_DIR": str(tmp_path / "Baza"), "REMCARD_DEV_BAZA_DIR": str(tmp_path / "Baza"),
        "REMCARD_DATA_PATH_CONFIG": str(tmp_path / "data_path.json"),
        "REMCARD_DEV_DATABASE_CONFIG": str(tmp_path / "dev_paths.json"),
        "REMCARD_TEST_INSTANCE_NAMESPACE": str(tmp_path), "REMCARD_EMERGENCY_DB_ROOT": str(tmp_path / "emergency"),
        "REMCARD_LOCAL_METRICS_SYNC": "1", "REMCARD_METRICS_AGGREGATION_ENABLED": "0",
        "REMCARD_LOCAL_FIRST_SYNC": "1", "REMCARD_INCREMENTAL_REPLICA": "1", "REMCARD_LOCAL_OUTBOX_SYNC": "0",
        "REMCARD_DAILY_CENTRAL_BACKUP_ENABLED": "0", "REMCARD_RUNTIME_AUTO_BACKUPS": "0",
        "REMCARD_APPLY_SETTINGS_RELEASE_SNAPSHOT": "0",
    })
    result = subprocess.run(
        [sys.executable, "-B", "-c", _STARTUP_CHECK, str(version), role],
        cwd=source, env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=75,
    )
    assert result.returncode == 0, (result.stdout + result.stderr)[-12000:]
    reports = [line.removeprefix("MANDATORY_UPGRADE_CHECK ") for line in result.stdout.splitlines()
               if line.startswith("MANDATORY_UPGRADE_CHECK ")]
    assert len(reports) == 1
    report = json.loads(reports[0])
    assert report["old_schema"] == version and report["new_schema"] == 28
    assert report["role"] == role and report["sync_mode"] == "full"
    assert report["seed_was_ready"] is False and report["required_cursor_observed"] is True
