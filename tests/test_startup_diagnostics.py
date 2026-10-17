import pytest
from rem_card.app import startup_diagnostics as diag
from rem_card.app import main as app_main


def test_span_preserves_result_exception_and_records_deltas(monkeypatch):
    events = []
    monkeypatch.setattr(diag, 'event', lambda stage, **fields: events.append((stage, fields)))
    samples = iter([{'process_cpu_ms': 2, 'read_bytes': 10}, {'process_cpu_ms': 5, 'read_bytes': 30}])
    monkeypatch.setattr(diag, '_sample', lambda: next(samples))
    error = ValueError('must not be logged')
    with pytest.raises(ValueError) as caught:
        with diag.span('test'):
            raise error
    assert caught.value is error
    end = events[-1][1]
    assert end['process_cpu_ms_delta'] == 3
    assert end['read_bytes_delta'] == 20
    assert end['outcome'] == 'ValueError'
    assert 'must not be logged' not in repr(events)


def test_first_role_and_elapsed_are_per_attempt(monkeypatch):
    events = []
    monkeypatch.setattr(diag, '_attempt_number', 0)
    monkeypatch.setattr(diag, '_attempt', {})
    monkeypatch.setattr(diag, 'event', lambda stage, **fields: events.append((stage, fields)))
    diag.role_requested('doctor', 'one')
    diag.role_ready()
    assert events[-1][1]['first_role'] is True
    assert events[-1][1]['request_to_ready_ms'] >= 0
    diag.role_requested('nurse', 'two')
    diag.role_ready()
    assert events[-1][1]['first_role'] is False
    assert events[-1][1]['session_id'] == 'two'


def test_logging_failure_does_not_escape(monkeypatch):
    from rem_card.app import local_metrics
    def broken(*args, **kwargs):
        raise OSError('disk failure')
    monkeypatch.setattr(local_metrics, 'record_metric', broken)
    @diag.measured('test')
    def operation():
        return 42
    assert operation() == 42


def test_quick_check_keeps_exact_sql_and_result():
    from rem_card.app.sqlite_shared import run_quick_check
    class Connection:
        def execute(self, sql):
            assert sql == 'PRAGMA quick_check'
            return self
        def fetchone(self):
            return ('ok',)
    assert run_quick_check(Connection()) == (True, 'ok')


def test_startup_failure_category_preserves_structured_user_message():
    failure = type(
        "StructuredStartupFailure",
        (),
        {
            "technical_reason": "startup probe failed",
            "user_message": "База данных повреждена; требуется восстановление.",
        },
    )()

    assert app_main._startup_failure_category(failure) == "corruption"


def test_startup_db_profile_lock_timeout_is_locked_busy():
    from rem_card.app.startup_db_guard import StartupGuardResult, _lock_timeout_user_message

    reason = "Could not acquire db_profile lock: synthetic/db.lock"
    failure = StartupGuardResult(ok=False, technical_reason=reason, user_message=_lock_timeout_user_message(reason))
    assert app_main._startup_failure_category(failure) == "locked_busy"


def test_startup_failure_category_keeps_plain_reason_fallback():
    failure = type(
        "StructuredStartupFailure",
        (),
        {
            "technical_reason": "unable to open database file",
            "user_message": "База временно недоступна.",
        },
    )()

    assert app_main._startup_failure_category(failure) == "network_unavailable"


def test_role_theme_selection_updates_active_crash_marker(monkeypatch):
    from rem_card.services import crash_reports

    roles = []
    monkeypatch.setattr(crash_reports, "update_crash_session_role", roles.append)
    monkeypatch.setattr(app_main, "_install_no_button_focus_rect_style", lambda _app: None)
    monkeypatch.setattr(app_main, "full_runtime_theme_enabled", lambda: False)
    monkeypatch.setattr(app_main, "_apply_basic_app_theme", lambda _app: None)

    app_main._apply_app_theme(object(), "nurse")
    app_main._apply_app_theme(object(), "system")

    assert roles == ["nurse"]
