from collections import OrderedDict
import json
import queue
import sqlite3
from types import MethodType, SimpleNamespace

import pytest

from rem_card.app import db_wait_diagnostics as waits
from rem_card.app import read_operation_diagnostics as diagnostics
from rem_card.data.dao import db_manager


@pytest.fixture
def probe(monkeypatch):
    events = []
    calls = []
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(diagnostics, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(diagnostics, "_streams", OrderedDict())
    monkeypatch.setattr(diagnostics, "_window_started", float("-inf"))
    monkeypatch.setattr(diagnostics, "_window_emitted", 0)
    monkeypatch.setattr(diagnostics, "_window_suppressed", 0)
    monkeypatch.setattr(diagnostics, "_emit", lambda payload: events.append(payload))
    monkeypatch.setattr(waits, "_active", {})
    monkeypatch.setattr(waits, "_thread", object())
    monkeypatch.setattr(waits, "_emit", lambda *_: None)
    monkeypatch.setattr(db_manager, "is_database_unavailable_error", lambda _: False)

    class Connection:
        error_phase = ""
        error = None

        def cursor(self):
            calls.append(("cursor",))
            return self

        def execute(self, sql, params=()):
            calls.append(("execute", sql, params))
            if self.error_phase == "execute":
                raise self.error
            return self

        def fetchone(self):
            calls.append(("fetch_one",))
            if self.error_phase == "fetch_one":
                raise self.error
            return (7,)

        def fetchall(self):
            calls.append(("fetch_all",))
            if self.error_phase == "fetch_all":
                raise self.error
            return [(7,)]

        def close(self):
            calls.append(("close",))

    connection = Connection()

    def opened():
        calls.append(("open",))
        return connection

    manager = SimpleNamespace(
        db_path=r"\\private-server\SECRET_PATIENT\medical.db",
        _in_current_thread_remcard_transaction=lambda: False,
        _scoped_central_read_connection=lambda: None,
        _open_readonly_central_connection=opened,
        _confirmed_central_read_result=lambda result: result,
        _read_cancel_requested=db_manager.DatabaseManager._read_cancel_requested,
        _cancelled_read_exception=db_manager.DatabaseManager._cancelled_read_exception,
    )
    manager._fetch_all_with_cancel = MethodType(db_manager.DatabaseManager._fetch_all_with_cancel, manager)
    return SimpleNamespace(events=events, calls=calls, clock=clock, manager=manager, connection=connection)


@pytest.mark.parametrize("method,phase", [
    ("_fetch_one_central", "execute"), ("_fetch_one_central", "fetch_one"),
    ("_fetch_all_central", "execute"), ("_fetch_all_central", "fetch_all"),
])
def test_runtime_failure_after_open_keeps_codes_phase_and_exact_sql(probe, method, phase):
    error = sqlite3.OperationalError("SECRET_PATIENT private SQL and path")
    error.sqlite_errorcode = sqlite3.SQLITE_READONLY
    error.sqlite_errorname = "SQLITE_READONLY"
    error.winerror = 5
    probe.connection.error_phase, probe.connection.error = phase, error
    with pytest.raises(sqlite3.OperationalError) as caught:
        getattr(db_manager.DatabaseManager, method)(probe.manager, "SELECT SECRET_SQL", ("SECRET_PARAM",))
    assert caught.value is error
    assert [call for call in probe.calls if call[0] == "execute"] == [
        ("execute", "SELECT SECRET_SQL", ("SECRET_PARAM",)),
    ]
    assert probe.calls[0] == ("open",) and probe.calls[-1] == ("close",)
    event, = probe.events
    assert event["phase"] == phase
    assert event["source"] == ("fetch_one" if method == "_fetch_one_central" else "fetch_all")
    assert event["sqlite_errorcode"] == sqlite3.SQLITE_READONLY
    assert event["sqlite_errorname"] == "SQLITE_READONLY" and event["winerror"] == 5
    assert event["uri_mode_at_creation"] == "ro" and event["query_only_configured"] is True
    assert event["operation_id"] and event["session_id"]
    assert event["connection_id"].endswith(f":{id(probe.connection):x}")
    text = json.dumps(event)
    assert not any(secret in text for secret in ("SECRET_", "private-server", "private SQL", "medical.db"))


def test_external_scoped_connection_does_not_claim_readonly_or_close_owner(probe):
    error = sqlite3.OperationalError("private")
    probe.connection.error_phase, probe.connection.error = "execute", error
    probe.manager._scoped_central_read_connection = lambda: probe.connection
    with pytest.raises(sqlite3.OperationalError) as caught:
        db_manager.DatabaseManager._fetch_one_central(probe.manager, "SELECT 1")
    assert caught.value is error
    event, = probe.events
    assert event["connection_scope"] == "scoped"
    assert event["uri_mode_at_creation"] == "unknown" and event["query_only_configured"] is None
    assert ("open",) not in probe.calls and ("close",) not in probe.calls


@pytest.mark.parametrize("method,expected", [("_fetch_one_central", (7,)), ("_fetch_all_central", [(7,)])])
def test_healthy_read_has_no_event_or_additional_statements(probe, method, expected):
    assert getattr(db_manager.DatabaseManager, method)(probe.manager, "SELECT 7") == expected
    assert probe.events == []
    assert probe.calls == [("open",), ("cursor",), ("execute", "SELECT 7", ()),
                           ("fetch_one" if method == "_fetch_one_central" else "fetch_all",), ("close",)]


def test_notification_and_exception_chain_are_preserved(probe, monkeypatch):
    original = sqlite3.OperationalError("attempt to write a readonly database")
    probe.connection.error_phase, probe.connection.error = "execute", original
    wrapped = RuntimeError("same existing notification")
    notified = []
    monkeypatch.setattr(db_manager, "is_database_unavailable_error", lambda _: True)
    monkeypatch.setattr(db_manager, "notify_database_unavailable",
                        lambda exc, **fields: notified.append((exc, fields)) or wrapped)
    with pytest.raises(RuntimeError) as caught:
        db_manager.DatabaseManager._fetch_one_central(probe.manager, "SELECT 1")
    assert caught.value is wrapped and wrapped.__cause__ is original
    assert len(notified) == 1 and notified[0][0] is original
    assert notified[0][1]["context"] == "remcard_read_one"
    assert len(probe.events) == 1 and probe.calls[-1] == ("close",)


def test_repeated_errors_and_distinct_categories_have_bounded_output(probe, monkeypatch):
    operation = diagnostics.ReadOperationDiagnostics("private", "fetch_one")
    operation.phase = "execute"
    error = sqlite3.OperationalError("private")
    for _ in range(100):
        operation.report(error)
    assert len(probe.events) == 1
    probe.clock.now += diagnostics.REPEAT_INTERVAL_SEC
    operation.report(error)
    assert probe.events[-1]["suppressed_before"] == 99
    for index in range(1000):
        operation.resource = f"private-{index}"
        operation.report(error)
    assert len(probe.events) == 1 + diagnostics.MAX_EVENTS_PER_WINDOW
    assert len(diagnostics._streams) <= diagnostics.MAX_STREAMS
    monkeypatch.setattr(diagnostics, "MAX_EVENTS_PER_WINDOW", 1000)
    probe.clock.now += diagnostics.REPEAT_INTERVAL_SEC
    for index in range(300):
        operation.resource = f"other-{index}"
        operation.report(error)
    assert len(diagnostics._streams) == diagnostics.MAX_STREAMS


@pytest.mark.parametrize("broken", ["context", "codes", "permit", "emit"])
def test_broken_diagnostics_cannot_replace_read_error_or_skip_close(probe, monkeypatch, broken):
    def fail(*_args, **_kwargs):
        raise RuntimeError("diagnostics failed")
    target = {"context": "current_context", "codes": "write_error_fields", "permit": "_permit", "emit": "_emit"}[broken]
    monkeypatch.setattr(diagnostics, target, fail)
    error = sqlite3.OperationalError("original SQL error")
    probe.connection.error_phase, probe.connection.error = "execute", error
    with pytest.raises(sqlite3.OperationalError) as caught:
        db_manager.DatabaseManager._fetch_one_central(probe.manager, "SELECT 1")
    assert caught.value is error and probe.calls[-1] == ("close",)


def test_full_metric_queue_and_broken_logger_are_nonfatal_without_flush(monkeypatch):
    calls = []

    def full(name, value, **fields):
        assert "force_flush" not in fields
        calls.append((name, value))
        raise queue.Full()

    def broken_logger(*_args):
        raise RuntimeError("logger unavailable")

    monkeypatch.setattr(diagnostics, "record_metric", full)
    monkeypatch.setattr(diagnostics, "logging", SimpleNamespace(getLogger=broken_logger))
    diagnostics._emit({"phase": "execute"})
    assert calls == [("central_read_error", 1)]
