"""ChangeWriteCoordinationMixin for the doctor orders widget."""

from PySide6.QtWidgets import QHeaderView
from rem_card.app.logger import logger
from rem_card.services.order_service import OrderConflictError
from rem_card.ui.shared.custom_message_box import CustomMessageBox
import sqlite3
import time


class ChangeWriteCoordinationMixin:
    def _apply_table_header_layout(self):
        if not hasattr(self, "table_view"):
            return
        header = self.table_view.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.resizeSection(0, 350)

    def _reset_change_batch(self, *, stop_timer: bool):
        if stop_timer and self._change_batch_timer.isActive():
            self._change_batch_timer.stop()
        self._pending_change_context_key = None
        self._pending_change_reload = False
        self._pending_change_invalidated = False
        self._pending_change_count = 0

    def _flush_change_batch(self):
        if self._is_closing:
            self._reset_change_batch(stop_timer=False)
            return
        pending_context_key = self._pending_change_context_key
        should_reload = bool(self._pending_change_reload)
        batch_count = int(self._pending_change_count or 0)
        self._reset_change_batch(stop_timer=False)
        current_context_key = self._current_context_key()
        if pending_context_key is None or current_context_key is None or pending_context_key != current_context_key:
            logger.info(
                "[OrdersWidget] discard debounced change batch pending_context=%s current_context=%s",
                pending_context_key,
                current_context_key,
            )
            return
        logger.info(
            "[OrdersWidget] flush debounced change batch count=%s reload=%s context_key=%s",
            batch_count,
            int(should_reload),
            pending_context_key,
        )
        if should_reload:
            self._request_snapshot(
                force=False,
                source="refresh",
                priority="MEDIUM",
                invalidate_reason=None,
            )

    def _should_show_soft_update(self, source: str) -> bool:
        return False

    def _schedule_soft_update_state(self, *, source: str):
        self._soft_update_timer.stop()
        self._clear_soft_update_state()

    def _show_soft_update_if_needed(self):
        return

    def _set_refresh_status(self, text: str):
        label = getattr(self, "_refresh_status_label", None)
        if label is None:
            return
        value = str(text or "").strip()
        label.setText(value)
        label.setVisible(bool(value))

    def _clear_soft_update_state(self):
        self._soft_update_timer.stop()
        self._soft_update_message = ""
        self._set_refresh_status("")

    @staticmethod
    def _is_foreign_key_error(exc: Exception) -> bool:
        if isinstance(exc, sqlite3.IntegrityError) and "foreign key" in str(exc).lower():
            return True
        return "foreign key" in str(exc).lower()

    def _patient_error_label(self, admission_id) -> str:
        try:
            patient = self.service.get_patient(int(admission_id)) if self.service else None
        except Exception:
            patient = None
        if not patient:
            return f"госпитализация #{admission_id}"

        name = str(getattr(patient, "full_name", "") or "").strip() or f"госпитализация #{admission_id}"
        details = []
        history = str(getattr(patient, "history_number", "") or "").strip()
        bed = getattr(patient, "bed_number", None)
        if history:
            details.append(f"ИБ {history}")
        if bed not in (None, ""):
            details.append(f"койка {bed}")
        return f"{name} ({', '.join(details)})" if details else name

    @staticmethod
    def _description_target_admission_id(description: str, fallback):
        parts = str(description or "").split(":")
        if len(parts) >= 2 and parts[0] == "orders_clear_drafts":
            try:
                return int(parts[1])
            except Exception:
                return fallback
        return fallback

    def _format_foreign_key_write_error(self, description: str, exc: Exception) -> str:
        target_admission_id = self._description_target_admission_id(description, self.admission_id)
        patient_label = self._patient_error_label(target_admission_id)

        if str(description or "").startswith("orders_clear_drafts:"):
            action = "очистка черновиков назначений при открытии или смене карты"
            reason = (
                "в карте есть назначение, которое база считает черновиком, "
                "но к нему уже привязаны сохраненные отметки выполнения."
            )
            next_step = "Нажмите «Исправить карту»: программа проверит назначения этого пациента и сохранит такие строки корректно."
        else:
            action = "сохранение данных карты"
            reason = "одна из сохраняемых строк ссылается на запись, которую база не нашла."
            next_step = "Обновите карту. Если сообщение повторится, передайте разработчику это окно."

        return (
            f"Не удалось выполнить действие: {action}.\n\n"
            f"Пациент: {patient_label}\n"
            f"Причина: {reason}\n\n"
            f"{next_step}\n\n"
            f"Техническая деталь: {exc}"
        )

    def _repair_order_draft_integrity(self, admission_id, shift_date):
        if not self.service or admission_id is None:
            return

        target_admission_id = int(admission_id)
        target_shift_date = shift_date
        result_holder = {}

        def operation():
            result_holder["result"] = self.service.repair_order_draft_integrity(
                target_admission_id,
                target_shift_date,
            )

        def on_success():
            result = result_holder.get("result") or {}
            rescued = int(result.get("rescued_orders", 0) or 0)
            removed_admins = int(result.get("removed_admins", 0) or 0)
            self.request_refresh(force=True)
            self._show_info(
                "Проверка назначений выполнена.\n\n"
                f"Исправлено назначений: {rescued}\n"
                f"Удалено черновых отметок выполнения: {removed_admins}"
            )

        def on_error(exc):
            self._show_warning(
                "Автоматическое исправление не выполнено.\n\n"
                f"Пациент: {self._patient_error_label(target_admission_id)}\n"
                f"Причина: {exc}"
            )

        self._enqueue_write(
            f"orders_repair_integrity:{target_admission_id}",
            operation=operation,
            on_success=on_success,
            on_error=on_error,
            show_error=False,
        )

    def _show_write_error(self, description: str, exc: Exception):
        if isinstance(exc, OrderConflictError):
            self._show_warning(
                "Назначения были изменены на другом рабочем месте. "
                "Несохранённые изменения отклонены, загружается актуальный лист. "
                "После обновления повторите нужное изменение."
            )
            return
        if not self._is_foreign_key_error(exc):
            self._show_warning(f"Ошибка сохранения: {exc}")
            return

        target_admission_id = self._description_target_admission_id(description, self.admission_id)
        result = CustomMessageBox.warning_with_actions(
            self,
            "Проверка назначений",
            self._format_foreign_key_write_error(description, exc),
            [
                ("Исправить карту", 1),
                ("Закрыть", CustomMessageBox.Cancel),
            ],
        )
        if result == 1:
            self._repair_order_draft_integrity(target_admission_id, self.shift_date)

    def _enqueue_write(
        self,
        description: str,
        operation,
        on_success=None,
        on_error=None,
        *,
        block_ui: bool = True,
        show_error: bool = True,
        perf_click_id: int | None = None,
        pass_result_to_success: bool = False,
    ):
        if self._is_closing or not self.service:
            return

        queued_at = time.perf_counter()
        logger.info(
            "[OrdersClick] write_enqueue role=doctor admission_id=%s description=%s block_ui=%s",
            self.admission_id,
            description,
            int(bool(block_ui)),
        )
        if block_ui and hasattr(self, "frame_container"):
            self.frame_container.setEnabled(False)

        def _on_success(result):
            if block_ui and hasattr(self, "frame_container"):
                self.frame_container.setEnabled(True)
            logger.info(
                "[OrdersClick] write_success role=doctor admission_id=%s description=%s elapsed_ms=%s",
                self.admission_id,
                description,
                round((time.perf_counter() - queued_at) * 1000.0, 1),
            )
            self._perf_mark_click(perf_click_id, "write_ok")
            if on_success:
                if pass_result_to_success:
                    on_success(result)
                else:
                    on_success()

        def _on_error(exc):
            if block_ui and hasattr(self, "frame_container"):
                self.frame_container.setEnabled(True)
            logger.info(
                "[OrdersClick] write_error role=doctor admission_id=%s description=%s elapsed_ms=%s error=%s",
                self.admission_id,
                description,
                round((time.perf_counter() - queued_at) * 1000.0, 1),
                exc,
            )
            self._perf_mark_click(perf_click_id, "write_error", extra=str(exc))
            try:
                if on_error:
                    on_error(exc)
            finally:
                if show_error:
                    self._show_write_error(description, exc)

        self.service.enqueue_write(
            description=description,
            operation=operation,
            on_success=_on_success,
            on_error=_on_error,
        )
