from __future__ import annotations

from PySide6.QtWidgets import QDialog
from rem_card.app.logger import logger
from rem_card.services.concurrency import DataConflictError
from rem_card.services.operblock_service import OperBlockConflictError
from rem_card.services.operblock_service import OperBlockSourceMovementChangedError
from rem_card.ui.shared.custom_message_box import CustomMessageBox
from typing import Any
import weakref
from rem_card.ui.operblock_view.operblock_admission_dialogs import (
    OccupyTableDialog,
    OperBlockQueueDialog,
)
from rem_card.ui.operblock_view.operblock_helpers import (
    _operblock_table_display_name,
)


class OperBlockCaseActionsMixin:
    def _open_rao_queue_dialog(self, table_code: str, table_name: str):
        if self._is_closing or self._write_pending or self.is_view_only_mode():
            return
        dialog = OperBlockQueueDialog(self.operblock_service.list_waiting_rao_handoffs, self)
        dialog.refresh_rows()
        if dialog.exec() != QDialog.Accepted or dialog.selected_handoff_id is None:
            return
        try:
            initial_data = self.operblock_service.get_rao_handoff_form_data(
                dialog.selected_handoff_id,
                table_code,
            )
        except Exception as exc:
            CustomMessageBox.warning(self, "Очередь", str(exc))
            return
        self._open_occupy_dialog(table_code, table_name, initial_data=initial_data)

    def _open_occupy_dialog(
        self,
        table_code: str,
        table_name: str,
        *,
        initial_data: dict[str, Any] | None = None,
    ):
        if self._is_closing or self._write_pending:
            return
        if self.is_view_only_mode():
            return
        dialog = OccupyTableDialog(
            table_code,
            table_name,
            self,
            initial_data=dict(initial_data or {}),
        )
        dialog_ref = weakref.ref(dialog)

        def save():
            form = dialog_ref()
            if form is None:
                return
            try:
                payload = form.get_data()
            except Exception as exc:
                CustomMessageBox.warning(form, "Ошибка", str(exc))
                return
            form.set_saving(True)
            self._write_pending = True

            def operation():
                return self.operblock_service.create_operation_case(payload)

            self._enqueue_write(
                f"operblock_create_operation_case:{table_code}",
                operation,
                on_success=lambda result: self._on_occupy_success(dialog_ref, result),
                on_error=lambda exc: self._on_occupy_error(dialog_ref, exc),
            )

        dialog.save_button.clicked.connect(save)
        dialog.exec()

    def _open_edit_patient_dialog(self, operation_case_id: int):
        if self._is_closing or self._write_pending:
            return
        if self.is_view_only_mode():
            return
        try:
            initial_data = self.operblock_service.get_operation_case_form_data(int(operation_case_id))
        except Exception as exc:
            CustomMessageBox.warning(self, "Редактировать пациента", str(exc))
            self.refresh_board(force=True)
            return
        table_code = str((initial_data or {}).get("table_code") or "")
        table_name = str((initial_data or {}).get("table_name") or "") or _operblock_table_display_name(table_code)
        expected_operation_case_revision = (initial_data or {}).get("operation_case_revision")
        expected_admission_revision = (initial_data or {}).get("admission_revision")
        dialog = OccupyTableDialog(
            table_code,
            table_name,
            self,
            mode="edit",
            initial_data=dict(initial_data or {}),
            operation_case_id=int(operation_case_id),
        )
        dialog_ref = weakref.ref(dialog)

        def save():
            form = dialog_ref()
            if form is None:
                return
            try:
                payload = form.get_data()
            except Exception as exc:
                CustomMessageBox.warning(form, "Ошибка", str(exc))
                return
            form.set_saving(True)
            self._write_pending = True

            def operation():
                return self.operblock_service.update_operation_case_form_data(
                    int(operation_case_id),
                    payload,
                    expected_operation_case_revision=expected_operation_case_revision,
                    expected_admission_revision=expected_admission_revision,
                )

            self._enqueue_write(
                f"operblock_update_operation_case:{int(operation_case_id)}",
                operation,
                on_success=lambda result: self._on_patient_edit_success(dialog_ref, result),
                on_error=lambda exc: self._on_patient_edit_error(dialog_ref, exc),
            )

        dialog.save_button.clicked.connect(save)
        dialog.exec()

    def _on_occupy_success(self, dialog_ref, result):
        self._write_pending = False
        dialog = dialog_ref()
        if dialog is not None:
            dialog.set_saving(False)
            dialog.accept()
        self.refresh_board(force=True)

    def _on_occupy_error(self, dialog_ref, exc: Exception):
        self._write_pending = False
        dialog = dialog_ref()
        if dialog is not None:
            dialog.set_saving(False)
        CustomMessageBox.warning(self, "Ошибка сохранения", str(exc))
        self.refresh_board(force=True)

    def _on_patient_edit_success(self, dialog_ref, result):
        self._write_pending = False
        dialog = dialog_ref()
        if dialog is not None:
            dialog.set_saving(False)
            dialog.accept()
        self.refresh_board(force=True)
        case_id = int((result or {}).get("operation_case_id") or 0)
        if self._current_operation_case_id and int(self._current_operation_case_id) == case_id:
            self.refresh_protocol(force=True)

    def _on_patient_edit_error(self, dialog_ref, exc: Exception):
        self._write_pending = False
        dialog = dialog_ref()
        if dialog is not None:
            dialog.set_saving(False)
        title = "Конфликт данных" if isinstance(exc, (DataConflictError, OperBlockConflictError)) else "Ошибка сохранения"
        CustomMessageBox.warning(self, title, str(exc))
        self.refresh_board(force=True)
        if self._current_operation_case_id:
            self.refresh_protocol(force=True)

    def _undo_last_action(self):
        if not self._current_operation_case_id or self._write_pending:
            return
        case_id = int(self._current_operation_case_id)

        def operation():
            return self.operblock_service.undo_last_action(case_id)

        self._write_pending = True
        self._apply_protocol_controls_state()
        self._enqueue_write(
            f"operblock_undo_last:{case_id}",
            operation,
            on_success=lambda result: self._on_undo_last_success(result),
            on_error=lambda exc: self._on_stage_action_error(exc),
        )

    def _on_undo_last_success(self, result):
        self._write_pending = False
        message = str((result or {}).get("message") or "Последнее действие отменено.")
        CustomMessageBox.information(self, "Отмена действия", message)
        self.refresh_protocol(force=True)
        self.refresh_board(force=True)

    def _confirm_release_current_case(self):
        if self._current_operation_case_id:
            self._confirm_release_case(self._current_operation_case_id)

    def _confirm_release_case(self, operation_case_id: int):
        if self.is_view_only_mode():
            return
        if self._write_pending:
            return
        reply = CustomMessageBox.question(
            self,
            "Освободить стол",
            "Действительно освободить операционный стол?\n"
            "Пациент будет перенесён в архив операционных пациентов.",
            CustomMessageBox.Yes | CustomMessageBox.No,
            CustomMessageBox.No,
        )
        if reply != CustomMessageBox.Yes:
            return
        handoff_id = None
        try:
            candidates = self.operblock_service.find_late_binding_candidates(
                int(operation_case_id)
            )
        except Exception as exc:
            logger.error(
                "operblock release late handoff lookup failed case_id=%s: %s",
                operation_case_id,
                exc,
                exc_info=True,
            )
            candidates = []
        if len(candidates) == 1:
            candidate = candidates[0]
            patient = dict(candidate.get("patient_snapshot") or {})
            reply = CustomMessageBox.question(
                self,
                "Связать с картой РАО",
                "Перед освобождением стола найдена исходная карта РАО с теми же "
                "номером истории, ФИО и датой рождения:\n\n"
                f"{patient.get('full_name') or 'ФИО не указано'}\n"
                f"История: {patient.get('history_number') or 'не указана'}\n\n"
                "Связать случай и вернуть пациента на зарезервированную койку?",
                CustomMessageBox.Yes | CustomMessageBox.No,
                CustomMessageBox.Yes,
            )
            if reply == CustomMessageBox.Yes:
                handoff_id = int(candidate["id"])
        self._enqueue_release_case(
            int(operation_case_id),
            handoff_id=handoff_id,
        )

    def _enqueue_release_case(
        self,
        operation_case_id: int,
        *,
        handoff_id: int | None,
        preserve_source_movement: bool = False,
    ) -> None:
        self._write_pending = True

        def operation():
            return self.operblock_service.release_operation_table(
                operation_case_id,
                handoff_id=handoff_id,
                preserve_source_movement=preserve_source_movement,
            )

        self._enqueue_write(
            (
                f"operblock_release_operation_table:{operation_case_id}:preserve_source"
                if preserve_source_movement
                else f"operblock_release_operation_table:{operation_case_id}"
            ),
            operation,
            on_success=lambda _result: self._on_release_case_success(operation_case_id),
            on_error=lambda exc: self._on_release_case_error(
                operation_case_id,
                handoff_id,
                preserve_source_movement,
                exc,
            ),
        )

    def _on_release_case_success(self, operation_case_id: int):
        self._write_pending = False
        self.refresh_board(force=True)
        if self._current_operation_case_id == int(operation_case_id):
            self._show_board()
        parent = self.window()
        migrate = getattr(parent, "_maybe_migrate_operblock_offline_after_release", None)
        if callable(migrate):
            migrate()

    def _on_release_case_error(
        self,
        operation_case_id: int,
        handoff_id: int | None,
        preserve_source_movement: bool,
        exc: Exception,
    ):
        self._write_pending = False
        if isinstance(exc, OperBlockSourceMovementChangedError) and not preserve_source_movement:
            CustomMessageBox.warning(self, "Освободить стол", str(exc))
            self._enqueue_release_case(
                int(operation_case_id),
                handoff_id=handoff_id,
                preserve_source_movement=True,
            )
            return
        CustomMessageBox.warning(self, "Освободить стол", str(exc))
        self.refresh_board(force=True)
        if self._current_operation_case_id:
            self.refresh_protocol(force=True)
