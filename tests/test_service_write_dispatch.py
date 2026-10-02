"""Контракт очереди и синхронных callbacks у трёх клинических сервисов."""

from types import SimpleNamespace

import pytest

from rem_card.services.fluid_service import FluidService
from rem_card.services.patient_service import PatientService
from rem_card.services.patient_bed_management.service import PatientBedManagementService


@pytest.fixture(params=[PatientService, FluidService, PatientBedManagementService])
def submit(request):
    def call(data_service, operation, on_success=None, on_error=None):
        return request.param.enqueue_write(
            SimpleNamespace(data_service=data_service), "save:test", operation, on_success, on_error,
        )
    return call


@pytest.mark.parametrize("queue_result", [True, False, None])
def test_queue_owns_execution_and_receives_original_callbacks(submit, queue_result):
    received = []
    executed = []
    operation = lambda: executed.append(True)
    success = lambda value: None
    error = lambda exc: None
    queue = SimpleNamespace(enqueue_write=lambda **kwargs: received.append(kwargs) or queue_result)
    assert submit(queue, operation, success, error) is None
    assert executed == []
    assert received == [{
        "description": "save:test", "operation": operation,
        "on_success": success, "on_error": error,
    }]


def test_rejected_queue_does_not_run_synchronous_fallback(submit):
    executed = []
    failure = RuntimeError("queue rejected")
    def reject(**kwargs):
        raise failure
    with pytest.raises(RuntimeError) as caught:
        submit(SimpleNamespace(enqueue_write=reject), lambda: executed.append(True))
    assert caught.value is failure
    assert executed == []


def test_synchronous_success_preserves_result_identity_and_return_contract(submit):
    payload = object()
    received = []
    assert submit(None, lambda: payload, received.append) is None
    assert received == [payload]


@pytest.mark.parametrize("handle", [True, False])
def test_operation_error_is_reported_or_propagated_once(submit, handle):
    failure = ValueError("operation failed")
    errors = []
    successes = []
    def operation():
        raise failure
    if handle:
        assert submit(None, operation, successes.append, errors.append) is None
        assert errors == [failure]
    else:
        with pytest.raises(ValueError) as caught:
            submit(None, operation, successes.append)
        assert caught.value is failure
    assert successes == []


def test_success_callback_error_is_not_reported_as_failed_write(submit):
    committed = []
    errors = []
    failure = RuntimeError("UI callback failed")
    def on_success(value):
        assert value == "committed"
        raise failure
    with pytest.raises(RuntimeError) as caught:
        submit(None, lambda: committed.append(True) or "committed", on_success, errors.append)
    assert caught.value is failure
    assert committed == [True]
    assert errors == []
