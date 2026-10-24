"""Synthetic full Qt role entry with the production child, guard and bootstrap.

Run cold, warm, then small_changes with the same dedicated --owned-root. Use a
fresh runtime root for each repeat. The same harness/runtime can target immutable
old sources. Schema preparation is outside entry timing. This local offscreen
source benchmark does not measure frozen executable startup or SMB latency.
"""
from __future__ import annotations
import os, sys, pathlib, json, time, argparse, sqlite3, shutil, multiprocessing, hashlib
SOURCE = os.environ.get('REMCARD_BENCH_SOURCE_ROOT')
if '--source-root' in sys.argv:
    SOURCE = sys.argv[sys.argv.index('--source-root') + 1]
    os.environ['REMCARD_BENCH_SOURCE_ROOT'] = SOURCE
if SOURCE:
    sys.path.insert(0, SOURCE)
    from _local_rem_card_bootstrap import bootstrap_local_rem_card
    bootstrap_local_rem_card()
    import rem_card
    if pathlib.Path(rem_card.__path__[0]).resolve() != pathlib.Path(SOURCE).resolve():
        raise RuntimeError('The requested source checkout was not bound to rem_card')

def _parse_arguments():
    p = argparse.ArgumentParser()
    p.add_argument('--source-root', required=True)
    p.add_argument('--owned-root', required=True)
    p.add_argument('--fixture', required=True)
    p.add_argument('--role', default='doctor', choices=['doctor', 'nurse'])
    p.add_argument('--scenario', default='cold', choices=['cold', 'warm', 'small_changes'])
    p.add_argument('--output', required=True)
    p.add_argument('--prepare-only', action='store_true')
    p.add_argument('--version-file')
    return p.parse_args()


def _prepare_runtime():
    a = _parse_arguments()
    src = pathlib.Path(a.source_root).resolve()
    owned = pathlib.Path(a.owned_root).resolve()
    fixture = pathlib.Path(a.fixture).resolve()
    output = pathlib.Path(a.output).resolve()
    if owned == pathlib.Path(owned.anchor) or owned == src or src in owned.parents:
        raise ValueError('Use a dedicated benchmark runtime directory outside the source checkout')
    if output == fixture or output.is_relative_to(src):
        raise ValueError('Benchmark output must not overwrite its source or fixture')
    marker = owned / '.remcard-role-entry-benchmark.json'
    fixture_sha256 = hashlib.sha256(fixture.read_bytes()).hexdigest()
    identity = {'purpose': 'synthetic RemCard role entry benchmark', 'fixture_sha256': fixture_sha256, 'source_root': str(src)}
    if owned.exists() and any(owned.iterdir()):
        if not marker.is_file() or json.loads(marker.read_text(encoding='utf-8')) != identity:
            raise ValueError('Refusing an existing directory without this benchmark fixture/source marker')
    owned.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(identity, indent=2), encoding='utf-8')
    baza = owned / 'Baza'
    logs = owned / ('logs_' + a.scenario)
    logs.mkdir(exist_ok=True)
    os.environ.update(
        REMCARD_BENCH_SOURCE_ROOT=str(src),
        PYTHONDONTWRITEBYTECODE='1',
        PYTHONPATH=str(src),
        LOCALAPPDATA=str(owned / 'appdata'),
        APPDATA=str(owned / 'appdata_roaming'),
        REMCARD_LOCAL_LOGS_DIR=str(logs),
        REMCARD_BAZA_DIR=str(baza),
        REMCARD_DEV_BAZA_DIR=str(baza),
        REMCARD_DATA_PATH_CONFIG=str(owned / 'data_path.json'),
        REMCARD_DEV_DATABASE_CONFIG=str(owned / 'dev_paths.json'),
        REMCARD_LOCAL_METRICS_SYNC='1',
        REMCARD_METRICS_AGGREGATION_ENABLED='0',
        REMCARD_TEST_INSTANCE_NAMESPACE=str(owned),
        REMCARD_EMERGENCY_DB_ROOT=str(owned / 'emergency'),
        QT_QPA_PLATFORM='offscreen',
    )
    profile = owned / 'profile'
    profile.mkdir(exist_ok=True)
    os.environ.update(
        USERPROFILE=str(profile),
        HOMEDRIVE=profile.drive,
        HOMEPATH=str(profile)[len(profile.drive):],
        ProgramData=str(owned / 'programdata'),
        TEMP=str(owned / 'temp'),
        TMP=str(owned / 'temp'),
    )
    (owned / 'temp').mkdir(exist_ok=True)
    if a.version_file:
        os.environ['REMCARD_VERSION_FILE'] = str(pathlib.Path(a.version_file).resolve())
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(src))
    from _local_rem_card_bootstrap import bootstrap_local_rem_card
    bootstrap_local_rem_card()
    if not (baza / 'archiv' / 'rao_journal.db').exists():
        from rem_card.app.unified_database_setup import prepare_database_root
        baza.mkdir(parents=True, exist_ok=True)
        ok, msg = prepare_database_root(str(baza))
        if not ok:
            raise RuntimeError('Synthetic root setup failed: ' + msg)
        shutil.copyfile(fixture, baza / 'archiv' / 'rao_journal.db')
        # Prepare the candidate's own supported schema outside the timed entry.
        # The canonical clinical rows/history remain the same across versions.
        from rem_card.app.unified_db_schema import ensure_unified_schema
        with sqlite3.connect(baza / 'archiv' / 'rao_journal.db') as connection:
            ensure_unified_schema(connection, allow_local_schema_upgrades=True)
    if a.prepare_only:
        print('PREPARED', str(baza))
        return
    medical = baza / 'archiv' / 'rao_journal.db'
    from rem_card.app.startup_snapshot_cache import cache_paths
    master, receipt_file = cache_paths(str(medical))
    cache_present_before = pathlib.Path(receipt_file).is_file()
    if a.scenario == 'cold' and cache_present_before:
        raise ValueError('Cold benchmark requires a new runtime directory without a persisted cache')
    if a.scenario != 'cold' and (not cache_present_before):
        raise ValueError('Warm benchmark requires a previous completed cold entry on this source/runtime')
    if a.scenario == 'small_changes':
        with sqlite3.connect(medical) as con:
            con.execute('UPDATE vitals SET pulse=pulse+1 WHERE id=1')
            con.execute('UPDATE clinical_events SET data=\'{"state":"benchmark_change"}\' WHERE id=1')
    return a, src, owned, fixture, output, logs, medical, cache_present_before


def _write_report(stats, logs, output):
    phases = []
    for f in logs.glob('metrics*.jsonl'):
        for ln, line in enumerate(f.open(encoding='utf-8'), 1):
            try:
                j = json.loads(line)
            except:
                continue
            if j.get('metric') == 'startup_diagnostic':
                row = {k: j[k] for k in ('ts', 'pid', 'stage', 'phase', 'elapsed_ms', 'request_to_ready_ms', 'outcome', 'check_mode', 'reason', 'target', 'thread_id', 'mode', 'sync_mode', 'cache_hit', 'full_scan_age_sec', 'error_class') if k in j}
                phases.append(row)
            elif j.get('metric') in {'role_initial_data_observed_ready', 'role_entry_transition_ms'}:
                row = {k: j[k] for k in ('ts', 'pid', 'elapsed_ms', 'includes_transition') if k in j}
                phases.append({'stage': j['metric'], 'phase': 'end', **row})
    stats['phases'] = phases
    stats['phase_telemetry_fields'] = 'technical timings, state labels and process ids only; no clinical values or identifiers'
    stats['real_startup_child_pids'] = sorted({x['pid'] for x in phases if x.get('pid') != os.getpid() and x.get('stage') in ('startup_snapshot_copy', 'startup_admission_probe')})
    if not stats['real_startup_child_pids']:
        raise RuntimeError('Benchmark did not execute a real startup child')
    stats['compiled_guard_enabled_in_source_harness'] = True
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: stats[k] for k in ('scenario', 'request_to_ready_ms', 'process_to_chooser_ms', 'qt_max_heartbeat_gap_ms', 'shutdown_lease_released')}), flush=True)


def _run_role_entry(a, src, owned, fixture, output, logs, medical, cache_present_before):
    from PySide6 import QtCore
    OriginalSettings = QtCore.QSettings
    OriginalSettings.setDefaultFormat(OriginalSettings.IniFormat)
    for scope in (OriginalSettings.UserScope, OriginalSettings.SystemScope):
        OriginalSettings.setPath(OriginalSettings.IniFormat, scope, str(owned / 'qsettings'))

    class IsolatedSettings(OriginalSettings):

        def __init__(self, *args, **kwargs):
            if len(args) >= 2 and isinstance(args[0], str) and isinstance(args[1], str):
                super().__init__(OriginalSettings.IniFormat, OriginalSettings.UserScope, *args, **kwargs)
            else:
                super().__init__(*args, **kwargs)
    QtCore.QSettings = IsolatedSettings
    from PySide6.QtCore import QSettings, QTimer, QEvent, qVersion
    from PySide6.QtWidgets import QApplication, QMessageBox
    QSettings.setDefaultFormat(QSettings.IniFormat)
    for scope in (QSettings.UserScope, QSettings.SystemScope):
        QSettings.setPath(QSettings.IniFormat, scope, str(owned / 'qsettings'))
    from rem_card.app.gui_gc import disable_automatic_gui_gc, install_gui_gc
    disable_automatic_gui_gc()
    from rem_card.app import startup_diagnostics as diagnostics, unified_shortcuts
    unified_shortcuts.migrate_known_legacy_shortcuts_once = lambda: None
    recorded = []
    original_event = diagnostics.event

    def event(stage, **fields):
        recorded.append({'stage': stage, 'at': time.perf_counter(), **{k: v for k, v in fields.items() if k in ('phase', 'elapsed_ms', 'outcome', 'request_to_ready_ms', 'role')}})
        return original_event(stage, **fields)
    diagnostics.event = event
    errors = []

    def dialog_error(*args, **kwargs):
        errors.append('unexpected_modal:' + str(args[1] if len(args) > 1 else ''))
        return QMessageBox.Ok
    QMessageBox.warning = dialog_error
    QMessageBox.critical = dialog_error
    started = time.perf_counter()
    diagnostics.start()
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    install_gui_gc(app)
    probe_results = []
    from rem_card.app import startup_check_worker as worker_module
    original_probe = worker_module.run_startup_probe

    def observed_probe(*args, **kwargs):
        began = time.perf_counter()
        result = original_probe(*args, **kwargs)
        if result[0] and result[1] != 'ok':
            payload = json.loads(result[1])
            probe_results.append({'mode': kwargs.get('admission_mode'), 'sync_mode': payload.get('sync_mode'), 'has_snapshot': bool(payload.get('snapshot')), 'full_checked_at_epoch': payload.get('full_checked_at_epoch'), 'elapsed_ms': round((time.perf_counter() - began) * 1000, 3)})
        return result
    worker_module.run_startup_probe = observed_probe
    from rem_card.app import main as legacy_main
    legacy_main.is_compiled = lambda: True
    from rem_card.ui.unified_window import UnifiedWindow
    window = UnifiedWindow()
    window._check_updates = lambda: None
    original_error = window._error

    def error(exc, **kw):
        errors.append(type(exc).__name__ + ':' + str(exc))
        return original_error(exc, **kw)
    window._error = error
    window._exit_update_checked = True
    window._suppress_exit_update = True
    beats = []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: beats.append(time.perf_counter()))
    timer.start()

    def wait(predicate, limit):
        end = time.monotonic() + limit
        while not predicate() and time.monotonic() < end:
            app.processEvents()
            app.sendPostedEvents(None, QEvent.DeferredDelete)
            time.sleep(0.002)
        if not predicate():
            raise TimeoutError('Synthetic role-entry wait timed out')
    window.show()
    window.initialize()
    wait(lambda: errors or any((x['stage'] == 'chooser_ready' for x in recorded)), 60)
    if errors:
        raise RuntimeError('Chooser failed: ' + repr(errors))
    chooser = time.perf_counter()
    wait(lambda: not window._workers, 30)
    requested = time.perf_counter()
    window.enter_role(a.role)
    wait(lambda: errors or any((x['stage'] == 'role_ready' for x in recorded)), 120)
    if errors:
        raise RuntimeError('Role entry failed: ' + repr(errors))
    ready = time.perf_counter()
    from rem_card.app.main import _initial_w1_state
    if not (window.container is not None and window.role_window is not None and (not window._busy) and _initial_w1_state(window.role_window).get('ready')):
        raise RuntimeError('role_ready without initialized data/runtime')
    stats = {
        'scope': 'full UnifiedWindow chooser/enter_role -> real read-only multiprocessing child -> guard -> bootstrap -> initial data and transition; local synthetic files, offscreen Qt, no real SMB',
        'timing_boundary': 'request_to_ready_ms starts immediately before enter_role and includes actual initial-data/transition readiness; process_to_* starts before QApplication setup, after imports/fixture preparation, and is not OS process-launch latency; cold means absent persistent cache, not flushed OS file cache',
        'source_root': str(src),
        'scenario': a.scenario,
        'role': a.role,
        'runtime': sys.version,
        'sqlite': sqlite3.sqlite_version,
        'qt': qVersion(),
        'metrics_log_dir': str(logs),
        'process_to_chooser_ms': round((chooser - started) * 1000, 3),
        'request_to_ready_ms': round((ready - requested) * 1000, 3),
        'process_to_role_ready_ms': round((ready - started) * 1000, 3),
        'qt_heartbeat_ticks': len(beats),
        'qt_max_heartbeat_gap_ms': round(max((b - a for a, b in zip(beats, beats[1:])), default=0) * 1000, 3),
        'ready_data_observed': True,
        'events': [{k: v for k, v in x.items() if k != 'at'} for x in recorded],
        'source_bytes': medical.stat().st_size,
        'source_fixture_sha256': hashlib.sha256(fixture.read_bytes()).hexdigest(),
        'cache_present_before': cache_present_before,
        'startup_probes': probe_results,
    }
    window.request_application_exit(confirmed=True)
    wait(lambda: window.container is None and window._closing, 60)
    stats['shutdown_lease_released'] = window.lease is None
    timer.stop()
    from rem_card.app.local_metrics import flush_metrics
    flush_metrics(timeout=2)
    _write_report(stats, logs, output)
    window.close()
    window.deleteLater()
    app.processEvents()


def main():
    prepared = _prepare_runtime()
    if prepared is not None:
        _run_role_entry(*prepared)


if __name__ == '__main__':
    multiprocessing.freeze_support()
    main()
