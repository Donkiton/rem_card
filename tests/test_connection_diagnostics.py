from collections import OrderedDict
import json
import sqlite3
from types import SimpleNamespace

import pytest

from rem_card.app import connection_diagnostics as diagnostics, sqlite_shared
from rem_card.data.dao import db_manager


@pytest.fixture
def setup_probe(monkeypatch):
    clock = SimpleNamespace(now=0.0, cpu=0.0)
    monkeypatch.setattr(diagnostics, "time", SimpleNamespace(
        perf_counter=lambda: clock.now, monotonic=lambda: clock.now,
        thread_time=lambda: clock.cpu,
    ))
    monkeypatch.setattr(diagnostics, "_streams", OrderedDict())
    monkeypatch.setattr(diagnostics, "_first", set())
    events = []
    monkeypatch.setattr(diagnostics, "_emit", lambda payload: events.append(payload))
    monkeypatch.setattr(sqlite_shared, "_resolve_sqlite_profile_settings", lambda _: {
        "temp_store": "MEMORY", "cache_kb": 8192, "mmap_mb": 0,
        "journal_mode": "DELETE", "synchronous": "EXTRA",
    })
    calls = []

    class Connection:
        row_factory = None
        error = None
        delayed_sql = ""
        delay = 0.0

        def create_function(self, *args, **kwargs):
            calls.append(("function", args[0]))

        def execute(self, sql):
            calls.append(("execute", sql))
            if sql == self.delayed_sql:
                clock.now += self.delay
                if self.error is not None:
                    raise self.error

    connection = Connection()

    def connect(database_uri, **kwargs):
        calls.append(("connect", database_uri, kwargs))
        clock.now += .2
        clock.cpu += .01
        return connection

    monkeypatch.setattr(db_manager.sqlite3, "connect", connect)
    database = SimpleNamespace(
        db_path=r"\\private-server\patient-name\central.db", _closed=False,
        _remcard_conn=object(), _readonly_db_uri=lambda: "file:private-path?mode=ro",
    )
    return SimpleNamespace(clock=clock, events=events, connection=connection,
                           database=database, calls=calls)


def _open(probe):
    return db_manager.DatabaseManager._open_readonly_central_connection(probe.database)


def test_connection_setup_attributes_slow_pragma_without_extra_sql(setup_probe):
    probe = setup_probe
    probe.connection.delayed_sql = "PRAGMA query_only = ON"
    probe.connection.delay = 2.0

    assert _open(probe) is probe.connection

    summary = probe.events[0]
    assert summary["elapsed_ms"] == 2200.0
    assert summary["thread_cpu_ms"] == 10.0
    assert summary["phases"]["sqlite_connect"]["total_ms"] == 200.0
    assert summary["phases"]["pragma_query_only"]["total_ms"] == 2000.0
    assert summary["phases"]["configure_mutex_wait"]["total_ms"] == 0.0
    assert summary["outcome"] == "ok" and summary["operation_id"]
    assert [call[1] for call in probe.calls if call[0] == "execute"] == [
        "PRAGMA foreign_keys = ON", "PRAGMA query_only = ON",
        "PRAGMA temp_store = MEMORY", "PRAGMA cache_size = -8192",
        "PRAGMA mmap_size = 0", f"PRAGMA busy_timeout = {sqlite_shared.SQLITE_BUSY_TIMEOUT_MS}",
    ]
    assert probe.calls[0][2] == {
        "uri": True, "check_same_thread": True, "isolation_level": None, "timeout": 5.0,
    }
    encoded = json.dumps(summary)
    assert "private-server" not in encoded and "patient-name" not in encoded
    assert "private-path" not in encoded and "PRAGMA " not in encoded


def test_connection_setup_separates_local_mutex_wait_from_sqlite(setup_probe, monkeypatch):
    probe = setup_probe

    class DelayedLock:
        def __enter__(self):
            probe.clock.now += 1.5

        def __exit__(self, *_args):
            pass

    monkeypatch.setattr(sqlite_shared, "_SQLITE_NATIVE_MAINTENANCE_LOCK", DelayedLock())
    _open(probe)
    summary = probe.events[0]
    assert summary["phases"]["configure_mutex_wait"]["total_ms"] == 1500.0
    assert summary["phases"]["pragma_query_only"]["total_ms"] == 0.0
    assert summary["phases"]["connection_configure"]["total_ms"] == 1500.0


def test_connection_failure_preserves_original_exception_and_safe_codes(setup_probe):
    probe = setup_probe
    error = sqlite3.OperationalError("sensitive patient details and SQL values")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    error.sqlite_errorname = "SQLITE_BUSY"
    error.winerror = 5
    probe.connection.error = error
    probe.connection.delayed_sql = "PRAGMA foreign_keys = ON"

    with pytest.raises(sqlite3.OperationalError) as result:
        _open(probe)

    assert result.value is error
    summary = probe.events[0]
    assert summary["failure_phase"] == "pragma_foreign_keys"
    assert summary["sqlite_errorcode"] == sqlite3.SQLITE_BUSY
    assert summary["sqlite_errorname"] == "SQLITE_BUSY" and summary["winerror"] == 5
    assert "sensitive" not in json.dumps(summary)


def test_fast_connection_baseline_and_repeated_errors_are_bounded(setup_probe):
    probe = setup_probe
    for _ in range(12):
        _open(probe)
    assert len(probe.events) == 1
    probe.connection.error = sqlite3.OperationalError("private")
    probe.connection.delayed_sql = "PRAGMA foreign_keys = ON"
    for _ in range(100):
        with pytest.raises(sqlite3.OperationalError):
            _open(probe)
    assert len(probe.events) == 2
    probe.clock.now += diagnostics.REPEAT_INTERVAL_SEC
    with pytest.raises(sqlite3.OperationalError):
        _open(probe)
    assert probe.events[-1]["suppressed_before"] == 99
    assert len(probe.events) == 3


def test_broken_diagnostics_cannot_change_connection_result(setup_probe, monkeypatch):
    probe = setup_probe

    def broken(*_args, **_kwargs):
        raise RuntimeError("telemetry unavailable")

    monkeypatch.setattr(diagnostics, "_emit", broken)
    assert _open(probe) is probe.connection
    error = sqlite3.OperationalError("original connection failure")
    probe.connection.error = error
    probe.connection.delayed_sql = "PRAGMA foreign_keys = ON"
    with pytest.raises(sqlite3.OperationalError) as result:
        _open(probe)
    assert result.value is error
    assert diagnostics._current.get() is None


def test_metric_and_logger_failures_do_not_escape(monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("sink unavailable")

    monkeypatch.setattr(diagnostics, "record_metric", broken)
    monkeypatch.setattr(diagnostics, "logging", SimpleNamespace(getLogger=broken))
    diagnostics._emit({"outcome": "ok"})


def test_spawned_startup_connection_keeps_parent_session_correlation(setup_probe):
    from rem_card.app.startup_diagnostics import startup_attempt

    with startup_attempt("a" * 32, "nurse"):
        _open(setup_probe)

    summary = setup_probe.events[0]
    assert summary["startup_session_id"] == "a" * 32
    assert summary["role"] == "nurse"
