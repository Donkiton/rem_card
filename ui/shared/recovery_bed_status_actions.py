from __future__ import annotations

from datetime import datetime
from typing import Callable

from PySide6.QtWidgets import QDialog

from rem_card.data.dto.remcard_dto import PatientStatus
from rem_card.ui.rem_card_sectors.outcome_dialogs import TransferOutcomeDialog
from rem_card.ui.shared.custom_message_box import CustomMessageBox


def open_recovery_transfer_dialog(parent, remcard_service, patient, on_finished: Callable[[], None] | None = None) -> bool:
    status_service = getattr(remcard_service, "status_service", None)
    admission_id = int(getattr(patient, "id", 0) or 0)
    if not status_service or not admission_id:
        CustomMessageBox.warning(parent, "Перевод", "Не удалось определить пациента или сервис статусов.")
        return False

    try:
        context = status_service.get_admission_outcome_context(admission_id)
    except Exception as exc:
        CustomMessageBox.warning(parent, "Перевод", f"Не удалось загрузить данные госпитализации: {exc}")
        return False

    dialog_parent = parent.window() if parent and parent.window() else parent
    dialog = TransferOutcomeDialog(context, datetime.now(), "", dialog_parent)
    if dialog.exec() != QDialog.Accepted:
        return False

    payload = dict(dialog.result_data or {})
    event_time = payload.get("event_time") or datetime.now()
    reason_text = payload.get("reason_text") or ""
    admission_details = payload.get("admission_details") or {}
    current_event = (
        status_service.get_current_status(admission_id)
        if hasattr(status_service, "get_current_status")
        else None
    )
    expected_active_event_id = int(getattr(current_event, "id", 0) or 0) if current_event else None
    expected_active_revision = int(getattr(current_event, "revision", 0) or 0) if current_event else None
    expected_admission_revision = int(context.get("revision") or 0) if context else None

    def finish():
        if on_finished:
            on_finished()

    def on_success(result):
        if not result:
            CustomMessageBox.warning(
                parent,
                "Перевод",
                "Не удалось зафиксировать перевод. Проверьте время перевода и текущий статус пациента.",
            )
        finish()

    def on_error(exc):
        CustomMessageBox.warning(parent, "Перевод", f"Ошибка фиксации перевода: {exc}")
        finish()

    if hasattr(status_service, "enqueue_change_status_with_outcome_details"):
        status_service.enqueue_change_status_with_outcome_details(
            admission_id,
            PatientStatus.TRANSFERRED,
            event_time,
            reason_type=None,
            reason_text=reason_text,
            user_id="W1",
            admission_details=admission_details,
            expected_active_event_id=expected_active_event_id,
            expected_active_revision=expected_active_revision,
            expected_admission_revision=expected_admission_revision,
            on_success=on_success,
            on_error=on_error,
        )
        return True

    try:
        result = status_service.change_status_with_outcome_details(
            admission_id,
            PatientStatus.TRANSFERRED,
            event_time,
            None,
            reason_text,
            "W1",
            admission_details,
            expected_active_event_id=expected_active_event_id,
            expected_active_revision=expected_active_revision,
            expected_admission_revision=expected_admission_revision,
        )
    except Exception as exc:
        on_error(exc)
        return True
    on_success(result)
    return True


def cancel_recovery_transfer(parent, remcard_service, patient, on_finished: Callable[[], None] | None = None) -> bool:
    status_service = getattr(remcard_service, "status_service", None)
    admission_id = int(getattr(patient, "id", 0) or 0)
    if not status_service or not admission_id:
        CustomMessageBox.warning(parent, "Отмена перевода", "Не удалось определить пациента или сервис статусов.")
        return False

    current_event = (
        status_service.get_current_status(admission_id)
        if hasattr(status_service, "get_current_status")
        else None
    )
    if getattr(current_event, "status", None) != PatientStatus.TRANSFERRED:
        CustomMessageBox.warning(parent, "Отмена перевода", "У пациента нет активного статуса перевода.")
        return False

    reply = CustomMessageBox.question(
        parent,
        "Отмена перевода",
        "Отменить перевод и вернуть пациента в статус «в отделении»?",
        CustomMessageBox.Yes | CustomMessageBox.No,
        CustomMessageBox.No,
    )
    if reply != CustomMessageBox.Yes:
        return False

    expected_active_event_id = int(getattr(current_event, "id", 0) or 0) if current_event else None
    expected_active_revision = int(getattr(current_event, "revision", 0) or 0) if current_event else None

    def finish():
        if on_finished:
            on_finished()

    def on_success(result):
        if not result:
            CustomMessageBox.warning(parent, "Отмена перевода", "Не удалось отменить перевод.")
        finish()

    def on_error(exc):
        CustomMessageBox.warning(parent, "Отмена перевода", f"Ошибка отмены перевода: {exc}")
        finish()

    if hasattr(status_service, "enqueue_rollback_last_status"):
        status_service.enqueue_rollback_last_status(
            admission_id,
            expected_active_event_id=expected_active_event_id,
            expected_active_revision=expected_active_revision,
            on_success=on_success,
            on_error=on_error,
        )
        return True

    try:
        result = status_service.rollback_last_status(
            admission_id,
            expected_active_event_id=expected_active_event_id,
            expected_active_revision=expected_active_revision,
        )
    except Exception as exc:
        on_error(exc)
        return True
    on_success(result)
    return True
