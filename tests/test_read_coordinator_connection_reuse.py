from __future__ import annotations

import sqlite3
import threading
import time
from collections import Counter
from contextlib import nullcontext
from datetime import datetime
from types import SimpleNamespace

import pytest

from rem_card.data.dao import db_manager as db_manager_module
from rem_card.data.dao.db_manager import DatabaseManager
from rem_card.services import read_coordinator as coordinator_module
from rem_card.services.read_coordinator import OrdersRefreshCancelled, ReadCoordinator


SHIFT = datetime(2026, 10, 28, 8)


class _ReadConnection(sqlite3.Connection):
    def close(self):
        self.closed = True
        super().close()


class _SqliteOrdersService:
    """Real DBManager reads against a disposable SQLite database."""

    def __init__(self, path, entities):
        self.path = path
        self.calls = Counter()
        self.connections = []
        self.statements = []
        self.after_probe = lambda: None
        self.failure_stage = None
        self.full_version = None
        self.db = object.__new__(DatabaseManager)
        self.db.db_path = str(path)
        self.db._thread_state = threading.local()
        self.db._local_replica = None
        self.db._last_seen_change_cursor = 0
        self.db._in_current_thread_remcard_transaction = lambda: False
        self.db.write_controller = SimpleNamespace(connection_guard=lambda _conn: nullcontext())
        self.db._open_readonly_central_connection = self._open
        self.orders_dao = SimpleNamespace(db=self.db)
        with sqlite3.connect(path) as conn:
            conn.executescript(
                "CREATE TABLE change_log (id INTEGER PRIMARY KEY, entity_name TEXT, "
                "entity_id INTEGER, admission_id INTEGER, action TEXT, changed_at TEXT, "
                "changed_by TEXT, version INTEGER);"
                "CREATE TABLE admin_rows (id INTEGER, order_id INTEGER, planned_time TEXT, "
                "status TEXT, is_committed INTEGER);"
                "INSERT INTO admin_rows VALUES (1, 1, '2026-10-28 10:00:00', 'done', 1);"
            )
            for cursor, entity in enumerate(entities, 11):
                conn.execute(
                    "INSERT INTO change_log VALUES (?, ?, 1, 17, 'update', NULL, NULL, NULL)",
                    (cursor, entity),
                )
            if not entities:
                conn.execute(
                    "INSERT INTO change_log VALUES (10, 'administrations', 1, 17, 'update', NULL, NULL, NULL)"
                )
        self.order = SimpleNamespace(id=1, is_committed=1, draft_sort_order=None)

    def _open(self):
        # Model costly connection setup, then count the actual SQLite opens.
        time.sleep(0.01)
        self.calls['open'] += 1
        conn = sqlite3.connect(
            self.path.as_uri() + '?mode=ro', uri=True, isolation_level=None,
            factory=_ReadConnection,
        )
        conn.row_factory = sqlite3.Row
        conn.closed = False
        conn.set_trace_callback(self.statements.append)
        self.connections.append(conn)
        return conn

    def get_latest_change_id(self, *, admission_id, include_global):
        self.calls['probe'] += 1
        cursor = self.db.get_latest_change_id(admission_id, include_global)
        self.after_probe()
        if self.failure_stage == 'probe':
            raise RuntimeError('probe failed')
        return cursor

    def get_changes_since(self, last_change_id, *, admission_id, include_global):
        self.calls['changes'] += 1
        if self.failure_stage == 'changes':
            raise RuntimeError('changes failed')
        return self.db.fetch_changes_since(last_change_id, admission_id, include_global)

    def get_latest_administrations(self, **_kwargs):
        self.calls['delta_rows'] += 1
        return self.db._fetch_all_central('SELECT * FROM admin_rows')

    def build_orders_snapshot(self, admission_id, shift_date, **_kwargs):
        self.calls['full'] += 1
        # The early read scope must be gone before the full builder selects
        # its own source and conservative cursor.
        assert self.db._scoped_central_read_connection() is None
        with self.db.snapshot_read_scope('test_orders_full'):
            cursor = self.db.get_latest_change_id(admission_id, False)
            rows = self.db._fetch_all_central('SELECT * FROM admin_rows')
        return self.snapshot(cursor if self.full_version is None else self.full_version, rows)

    def snapshot(self, cursor, rows=()):
        return {
            'admission_id': 17, 'shift_date': SHIFT, 'only_committed': True,
            'orders': [self.order], 'admin_rows': [dict(row) for row in rows],
            'has_any_draft': False, 'has_any_orders': True,
            'has_any_administrations': bool(rows), 'change_id': cursor,
        }

    def assert_closed(self):
        assert all(conn.closed for conn in self.connections)
        assert not any(statement.lstrip().upper().startswith('BEGIN') for statement in self.statements)


def _coordinator(service):
    coordinator = ReadCoordinator(service)
    context = coordinator.make_orders_context(
        source_db='live', admission_id=17, shift_date=SHIFT,
        role='nurse', mode='live', variant='committed',
    )
    frozen = coordinator._finalize_snapshot(
        snapshot=service.snapshot(10), scope='orders_tab', tab_name='orders',
        cache_key=context.cache_key(), context_hash=context.hash(), role=context.role,
        mode=context.mode, source_db=context.source_db, variant=context.variant,
        load_strategy='test', load_trace_id='test-10', source='test', stale=False,
        invalidate_reason=None,
    )
    coordinator._store_orders_tab(context, frozen)
    return coordinator, context


@pytest.mark.parametrize('entities,expected_version,expected_opens,expected_delta,expected_full', [
    ([], 10, 1, 0, 0),
    (['administrations'], 11, 1, 1, 0),
    (['administrations', 'vitals'], 12, 2, 0, 1),
])
def test_orders_reads_reuse_one_connection_without_skipping_authoritative_probe(
    tmp_path, entities, expected_version, expected_opens, expected_delta, expected_full,
):
    service = _SqliteOrdersService(tmp_path / 'orders.db', entities)
    coordinator, context = _coordinator(service)

    result = coordinator.load_orders_tab(context, source='click')

    assert result['version'] == expected_version
    assert service.calls == Counter(
        open=expected_opens, probe=1, changes=int(bool(entities)),
        delta_rows=expected_delta, full=expected_full,
    )
    assert len([sql for sql in service.statements if 'MAX(id)' in sql]) == 1 + expected_full
    service.assert_closed()
    assert coordinator._orders_reads_inflight == {}


@pytest.mark.parametrize('full_version', [None, 11])
def test_newer_unsupported_write_after_delayed_probe_keeps_its_cursor_floor(tmp_path, full_version):
    service = _SqliteOrdersService(tmp_path / 'orders.db', ['administrations'])
    service.full_version = full_version
    coordinator, context = _coordinator(service)
    probed, release = threading.Event(), threading.Event()
    results, errors = [], []

    def after_probe():
        assert not service.db._scoped_central_read_connection().in_transaction
        probed.set()
        assert release.wait(3)

    service.after_probe = after_probe

    def load():
        try:
            results.append(coordinator.load_orders_tab(context, source='click'))
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=load)
    worker.start()
    try:
        assert probed.wait(2)
        # Rollback-journal SQLite must allow this writer to commit while the
        # reused connection remains open; no long read transaction is added.
        with sqlite3.connect(service.path, timeout=0.2) as writer:
            writer.execute(
                "INSERT INTO change_log VALUES (12, 'orders', 1, 17, 'update', NULL, NULL, NULL)"
            )
    finally:
        release.set()
        worker.join(4)

    assert not worker.is_alive()
    if full_version is None:
        assert errors == []
        assert [snapshot['version'] for snapshot in results] == [12]
        assert coordinator.get_cached_tab(context)['version'] == 12
    else:
        assert results == []
        assert len(errors) == 1
        assert isinstance(errors[0], OrdersRefreshCancelled)
        assert 'below required version' in str(errors[0])
        assert coordinator.get_cached_tab(context)['version'] == 10
    assert service.calls == Counter(open=2, probe=1, changes=1, full=1)
    service.assert_closed()
    assert coordinator._orders_reads_inflight == {}


@pytest.mark.parametrize('failure_stage', ['probe', 'changes'])
def test_orders_connection_closes_after_probe_or_changelog_exception(tmp_path, failure_stage):
    service = _SqliteOrdersService(tmp_path / 'orders.db', ['vitals'])
    service.failure_stage = failure_stage
    coordinator, context = _coordinator(service)

    if failure_stage == 'probe':
        with pytest.raises(RuntimeError, match='probe failed'):
            coordinator.load_orders_tab(context, source='click')
        assert service.calls == Counter(open=1, probe=1)
    else:
        result = coordinator.load_orders_tab(context, source='click')
        assert result['version'] == 11
        assert service.calls == Counter(open=2, probe=1, changes=1, full=1)
    service.assert_closed()
    assert service.db._scoped_central_read_connection() is None
    assert coordinator._orders_reads_inflight == {}


def test_inflight_cancel_releases_connection_before_full_fallback(tmp_path):
    service = _SqliteOrdersService(tmp_path / 'orders.db', ['vitals'])
    coordinator, context = _coordinator(service)
    probed, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def after_probe():
        probed.set()
        assert release.wait(3)

    service.after_probe = after_probe

    def load():
        try:
            coordinator.load_orders_tab(context, source='click', cancel_check=cancelled.is_set)
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=load)
    worker.start()
    try:
        assert probed.wait(2)
        cancelled.set()
    finally:
        release.set()
        worker.join(4)

    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], OrdersRefreshCancelled)
    assert service.calls == Counter(open=1, probe=1)
    service.assert_closed()
    assert coordinator._orders_reads_inflight == {}


def test_connection_open_delay_remains_in_version_probe_telemetry(tmp_path, monkeypatch):
    service = _SqliteOrdersService(tmp_path / 'orders.db', [])
    coordinator, context = _coordinator(service)
    clock = [100.0]
    metrics = []
    original_open = service.db._open_readonly_central_connection

    def delayed_open():
        clock[0] += 5.0
        return original_open()

    service.db._open_readonly_central_connection = delayed_open
    monkeypatch.setattr(coordinator_module.time, 'perf_counter', lambda: clock[0])
    monkeypatch.setattr(
        coordinator_module, 'record_metric',
        lambda metric, value, **fields: metrics.append((metric, value, fields)),
    )

    result = coordinator.load_orders_tab(context, source='click')

    assert result['version'] == 10
    total = next(item for item in metrics if item[0] == 'orders_load_time_ms')
    assert total[1] == 5000.0
    assert total[2]['version_probe_ms'] == 5000.0
    service.assert_closed()


def test_scope_open_error_preserves_probe_retry_and_local_fallback(tmp_path, monkeypatch):
    service = _SqliteOrdersService(tmp_path / 'orders.db', [])
    coordinator, context = _coordinator(service)
    attempts, fallback_reads = [], []
    service.db._should_read_from_local = lambda: False
    service.db._last_seen_change_cursor = 10
    service.db._log_locked_read_throttled = lambda *_args: None
    service.db._local_replica = SimpleNamespace(
        fetch_one=lambda query, params: fallback_reads.append((query, params)) or (10,),
    )

    def locked_open():
        attempts.append(1)
        raise sqlite3.OperationalError('database is locked')

    service.db._open_readonly_central_connection = locked_open
    monkeypatch.setattr(db_manager_module, 'READ_LOCK_RETRY_DELAY_SEC', 0)

    result = coordinator.load_orders_tab(context, source='click')

    assert result['version'] == 10
    assert service.calls == Counter(probe=1)
    assert len(attempts) == 1 + db_manager_module.READ_LOCK_RETRIES
    assert len(fallback_reads) == 1
    assert service.db._scoped_central_read_connection() is None
    assert service.db._thread_state.central_read_scope_depth == 0
    assert coordinator._orders_reads_inflight == {}


def _stop_at_early_stage(monkeypatch, service, coordinator, stage, stop):
    if stage == 'probe':
        service.after_probe = stop
        return
    target, method = {
        'scope_open': (service.db, '_open_readonly_central_connection'),
        'changes': (service, 'get_changes_since'),
        'delta_rows': (service, 'get_latest_administrations'),
        'finalize': (coordinator, '_finalize_snapshot'),
        'resolve': (coordinator, '_resolve_orders_early_snapshot'),
        'store': (coordinator, '_store_orders_tab'),
    }[stage]
    original = getattr(target, method)

    def stopped(*args, **kwargs):
        if stage == 'store':
            # This is after the pre-store call-site checkpoint. The store
            # itself must check again after acquiring its cache lock.
            stop()
        result = original(*args, **kwargs)
        if stage != 'store':
            stop()
        return result

    monkeypatch.setattr(target, method, stopped)


@pytest.mark.parametrize('stage,changes,delta_rows', [
    ('scope_open', 0, 0), ('probe', 0, 0), ('changes', 1, 0),
    ('delta_rows', 1, 1), ('finalize', 1, 1), ('store', 1, 1),
])
@pytest.mark.parametrize('stop_kind', ['budget', 'cancel'])
def test_real_sqlite_delta_stops_before_further_reads_or_cache_publication(
    tmp_path, monkeypatch, stage, changes, delta_rows, stop_kind,
):
    service = _SqliteOrdersService(tmp_path / 'orders.db', ['administrations'])
    reader, context = _coordinator(service)
    base = reader.get_cached_tab(context)
    epoch = reader._orders_cache_epochs.get(context.cache_key(), 0)
    clock, records = [100.0], []
    cancelled = threading.Event()
    monkeypatch.setattr(
        coordinator_module, 'time',
        SimpleNamespace(perf_counter=lambda: clock[0], monotonic=time.monotonic),
    )
    monkeypatch.setattr(
        coordinator_module, 'record_metric',
        lambda name, value, **fields: records.append((name, value, fields)),
    )

    def stop():
        if stop_kind == 'budget':
            clock[0] += 6
        else:
            cancelled.set()

    _stop_at_early_stage(monkeypatch, service, reader, stage, stop)
    expected_reason = 'read_budget_exhausted' if stop_kind == 'budget' else 'cancel_check'
    with pytest.raises(OrdersRefreshCancelled, match=expected_reason):
        reader.load_orders_tab(context, source='click', timeout_sec=5, cancel_check=cancelled.is_set)

    # Only actual DBManager/SQLite reads before the stop point ran. Neither
    # a full fallback nor an admin read after an expired probe is allowed.
    assert service.calls == Counter(
        open=1, probe=int(stage != 'scope_open'), changes=changes, delta_rows=delta_rows,
    )
    assert reader.get_cached_tab(context) is base
    assert base['version'] == 10
    assert reader._orders_cache_epochs.get(context.cache_key(), 0) == epoch
    assert not any(name == 'orders_load_time_ms' for name, _value, _fields in records)
    assert reader._orders_active_refreshes == {}
    assert reader._orders_reads_inflight == {}
    service.assert_closed()


@pytest.mark.parametrize('stage', ['probe', 'resolve', 'store'])
@pytest.mark.parametrize('stop_kind', ['budget', 'cancel'])
def test_current_base_cannot_be_republished_after_early_stop(
    tmp_path, monkeypatch, stage, stop_kind,
):
    service = _SqliteOrdersService(tmp_path / 'orders.db', [])
    reader, context = _coordinator(service)
    reader.invalidate_tab(context, 'test_stale_base')
    stale = reader._orders_stale_snapshots[context.cache_key()]
    epoch = reader._orders_cache_epochs[context.cache_key()]
    clock, records = [100.0], []
    cancelled = threading.Event()
    monkeypatch.setattr(
        coordinator_module, 'time',
        SimpleNamespace(perf_counter=lambda: clock[0], monotonic=time.monotonic),
    )
    monkeypatch.setattr(
        coordinator_module, 'record_metric',
        lambda name, value, **fields: records.append((name, value, fields)),
    )

    def stop():
        if stop_kind == 'budget':
            clock[0] += 6
        else:
            cancelled.set()

    _stop_at_early_stage(monkeypatch, service, reader, stage, stop)
    with pytest.raises(OrdersRefreshCancelled):
        reader.load_orders_tab(context, source='click', timeout_sec=5, cancel_check=cancelled.is_set)

    assert service.calls == Counter(open=1, probe=1)
    assert reader.get_cached_tab(context) is None
    assert reader._orders_stale_snapshots[context.cache_key()] is stale
    assert stale['version'] == 10
    assert reader._orders_cache_epochs[context.cache_key()] == epoch
    assert not any(name == 'orders_load_time_ms' for name, _value, _fields in records)
    assert reader._orders_active_refreshes == {}
    assert reader._orders_reads_inflight == {}
    service.assert_closed()
