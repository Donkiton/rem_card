import pytest
from rem_card.app import startup_diagnostics as diag


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
