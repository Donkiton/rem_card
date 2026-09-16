"""Patient review and persistent wait, owned by the emergency main window."""
from __future__ import annotations

import os
from collections.abc import Callable

from PySide6.QtCore import QEvent, QObject, QTimer, Qt, Slot
from PySide6.QtWidgets import QApplication, QDialog, QPushButton

from rem_card.app.emergency_workflow import authorize_patient_merge, resume_local_work
from rem_card.app.logger import logger
from rem_card.ui.shared.async_call import AsyncCallThread
from rem_card.ui.shared.emergency_dialogs import EmergencyActionDialog, NonClosableEmergencyDialog
from rem_card.ui.shared.emergency_exit_dialog import (
    EmergencyExitPreflightDialog,
    EmergencyNetworkRestoredDialog,
)


READY_STATUSES = {"merge_ready_mode_a", "remote_changed_conflict_pending"}
RECOVERY_REMINDER_MS = 5 * 60 * 1000
PROBE_TIMEOUT_MS = 90 * 1000


class EmergencyWaitingDialog(NonClosableEmergencyDialog):
    def __init__(self, controller):
        super().__init__("Аварийная работа приостановлена", "Сохраняем локальные данные…", controller.window)
        self.setWindowModality(Qt.ApplicationModal)
        self.check_button = QPushButton("Проверить связь")
        self.resume_button = QPushButton("Продолжить работу")
        self.close_button = QPushButton("Закрыть RemCard")
        for button in (self.check_button, self.resume_button, self.close_button):
            button.setObjectName("DialogOkBtn")
            self.button_layout.addWidget(button)
        self.content_layout.addLayout(self.button_layout)
        self.check_button.clicked.connect(controller.check_now)
        self.resume_button.clicked.connect(controller.resume)
        self.close_button.clicked.connect(controller.close_waiting)
        self.setMinimumWidth(620)


class EmergencyWorkflowController(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.store = window._emergency_store_for_runtime()
        self.session_id = window.container.runtime_context.emergency_session_id
        self.data_service = window.container.data_service
        self.scheduler = getattr(window.container, "emergency_restore_probe_scheduler", None)
        self.waiting: EmergencyWaitingDialog | None = None
        self.preflight: EmergencyExitPreflightDialog | None = None
        self.recovery_notice: EmergencyNetworkRestoredDialog | None = None
        self.pause_token = None
        self.worker: AsyncCallThread | None = None
        self._worker_done: Callable | None = None
        self._worker_failed_done: Callable | None = None
        self.busy = False
        self.review_offered = False
        self.recovery_notified = False
        self.last_status: dict = {}
        self._probe_context = ""
        self._probe_baseline_ts = 0.0
        self._preflight_from_recovery_notice = False
        self._network_ready = False
        self._notice_show_scheduled = False
        self._notice_deferred = False
        self._shutting_down = False

        self._probe_timeout_timer = QTimer(self)
        self._probe_timeout_timer.setSingleShot(True)
        self._probe_timeout_timer.setInterval(PROBE_TIMEOUT_MS)
        self._probe_timeout_timer.timeout.connect(self._probe_timed_out)
        self._recovery_reminder_timer = QTimer(self)
        self._recovery_reminder_timer.setSingleShot(True)
        self._recovery_reminder_timer.setInterval(RECOVERY_REMINDER_MS)
        self._recovery_reminder_timer.timeout.connect(self._recovery_reminder_due)
        self._notice_defer_timer = QTimer(self)
        self._notice_defer_timer.setSingleShot(True)
        self._notice_defer_timer.timeout.connect(self._show_recovery_notice_if_current)
        window.installEventFilter(self)

        metadata = self.store.read_active_session(self.session_id)
        self.recovery_required = bool(getattr(metadata, "merge_recovery_required", False)) or metadata.status in {
            "merge_pending", "merging",
        }
        self.recovery_retry_requested = False
        if metadata.status != "active":
            window.stack.setEnabled(False)
            QTimer.singleShot(0, self, lambda: self.begin_wait(password_required=False))

    def eventFilter(self, obj, event):
        if obj is self.window and event.type() == QEvent.Close:
            # MainWindow can still ignore this event while resolving drafts.
            # Check its committed close state after closeEvent has run.
            QTimer.singleShot(0, self, self._stop_if_window_is_closing)
        return super().eventFilter(obj, event)

    @Slot()
    def _stop_if_window_is_closing(self):
        if self.window._is_closing:
            self._stop_timers_and_notices()

    def _message(self, title, message):
        EmergencyActionDialog.ask(self.waiting or self.preflight or self.window, title, str(message), [("Понятно", 1)])

    def _run(self, fn, done, failed_done=None):
        if self.busy:
            return False
        self.busy = True
        self._worker_done = done
        self._worker_failed_done = failed_done
        if self.waiting:
            self.waiting.check_button.setEnabled(False)
            self.waiting.resume_button.setEnabled(False)
        worker = AsyncCallThread(fn, parent=self)
        self.worker = worker
        # Bound QObject slots force delivery back to the controller's GUI thread.
        worker.succeeded.connect(self._worker_succeeded)
        worker.failed.connect(self._worker_failed)
        worker.start()
        return True

    @Slot(object)
    def _worker_succeeded(self, value):
        self._finish_worker(value, failed=False)

    @Slot(object)
    def _worker_failed(self, value):
        self._finish_worker(value, failed=True)

    def _finish_worker(self, value, *, failed: bool):
        done = self._worker_done
        failed_done = self._worker_failed_done
        worker = self.worker
        self._worker_done = None
        self._worker_failed_done = None
        self.worker = None
        self.busy = False
        if worker is not None:
            worker.deleteLater()
        if self.window._is_closing:
            return
        if self.waiting:
            self.waiting.check_button.setEnabled(True)
            self.waiting.resume_button.setEnabled(not self.recovery_required)
        if failed:
            logger.warning("Emergency workflow failed: %s", value)
            if failed_done is not None:
                failed_done(value)
            else:
                self._message(
                    "Действие не завершено",
                    "Локальные данные сохранены. Не удалось завершить действие. "
                    "Проверьте указанную причину и повторите попытку.",
                )
            return
        try:
            if done is not None:
                done(value)
        except Exception as exc:
            logger.exception("Emergency workflow result could not be recorded: %s", exc)
            self._message(
                "Действие не завершено",
                "Не удалось сохранить состояние аварийной сессии. "
                "Работа остаётся приостановленной. Повторите проверку или закройте программу.",
            )

    def begin_wait(self, *, password_required=True, from_recovery_notice=False):
        if self.waiting:
            self.waiting.show()
            self.waiting.raise_()
            return
        if self.preflight:
            self.preflight.show()
            self.preflight.raise_()
            return
        orders = self.window._doctor_orders_widget_for_close()
        if orders is not None and orders.has_drafts():
            self._message("Несохранённые назначения", "Сначала сохраните или отмените черновик назначений.")
            if from_recovery_notice:
                self._defer_recovery_notice()
            return
        if not password_required:
            if self._request_shared_finish(dialog=None):
                self._begin_pause_after_authorized()
            return

        self._preflight_from_recovery_notice = bool(from_recovery_notice)
        dialog = EmergencyExitPreflightDialog(self._verify_emergency_password, parent=self.window)
        self.preflight = dialog
        dialog.approved.connect(self._preflight_approved)
        dialog.cancelled.connect(self._preflight_cancelled)
        dialog.retry_requested.connect(lambda: self._request_fresh_probe("preflight"))
        dialog.show()
        dialog.raise_()
        dialog.password_edit.setFocus(Qt.OtherFocusReason)
        self._request_fresh_probe("preflight")

    def _verify_emergency_password(self, password: str) -> bool:
        if os.environ.get("REMCARD_EMERGENCY_PASSWORD_AUTO_ACCEPT") == "1":
            return True
        from rem_card.app.emergency_password import verify_emergency_password

        runtime_context = getattr(self.window.container, "runtime_context", None)
        settings_db_path = str(getattr(runtime_context, "settings_db_path", "") or "")
        if settings_db_path:
            return verify_emergency_password(password, settings_db_path=settings_db_path, readonly=True)
        return verify_emergency_password(password, runtime_context=runtime_context, readonly=True)

    def _request_shared_finish(self, dialog: EmergencyExitPreflightDialog | None) -> bool:
        request_shared_finish = getattr(self.window, "_request_shared_emergency_finish", None)
        if not callable(request_shared_finish):
            return True
        try:
            result = request_shared_finish()
            if result is False:
                raise RuntimeError("другие окна ещё не подтвердили завершение")
            return True
        except Exception as exc:
            logger.warning("Shared emergency finish could not be requested: %s", exc)
            message = (
                "Не удалось подготовить другие окна этой аварийной сессии к завершению. "
                "Локальные данные остаются сохранёнными; повторите попытку."
            )
            if dialog is not None:
                dialog.set_action_error(message)
            else:
                self._message("Завершение пока недоступно", message)
            return False

    @Slot()
    def _preflight_approved(self):
        dialog = self.preflight
        if dialog is None or not dialog.probe_ready:
            return
        if not self._request_shared_finish(dialog):
            return
        self._probe_timeout_timer.stop()
        self.preflight = None
        dialog.finish_with_code(QDialog.Accepted)
        dialog.deleteLater()
        self._preflight_from_recovery_notice = False
        self._begin_pause_after_authorized()

    @Slot()
    def _preflight_cancelled(self):
        dialog = self.preflight
        was_recovery = self._preflight_from_recovery_notice
        self._probe_timeout_timer.stop()
        self._probe_context = ""
        self.preflight = None
        self._preflight_from_recovery_notice = False
        if dialog is not None:
            dialog.deleteLater()
        if was_recovery:
            self._defer_recovery_notice()

    def _begin_pause_after_authorized(self):
        self._stop_recovery_notice()
        self.window.stack.setEnabled(False)
        self.waiting = EmergencyWaitingDialog(self)
        self.waiting.show()
        self._run(
            lambda: self.data_service.pause_emergency_work(timeout_sec=5.0),
            self._paused,
            self._pause_failed,
        )

    def _pause_failed(self, exc):
        if self.waiting:
            self.waiting.message_label.setText(
                "Не удалось дождаться сохранения локальных данных. Запись снова разрешена.\n"
                f"Причина: {self._safe_detail(exc)}\n"
                "Нажмите «Проверить связь», чтобы повторить, или «Продолжить работу» для возврата."
            )

    def _paused(self, token):
        if not token.get("ok"):
            reason = self._pause_reason_text(str(token.get("reason") or "unknown"), token)
            self.waiting.message_label.setText(
                f"Сохранение и приостановка не завершены. {reason}\n"
                "Нажмите «Проверить связь», чтобы повторить, или «Продолжить работу» для возврата."
            )
            return
        self.pause_token = token
        if self.recovery_required:
            self.waiting.resume_button.setEnabled(False)
            self.waiting.message_label.setText(
                "Нужно установить результат предыдущего переноса. Локальные данные сохранены.\n"
                "После восстановления связи нажмите «Проверить связь», чтобы завершить проверку. "
                "До этого продолжение локальной работы недоступно."
            )
            return
        self.store.mark_session_status(self.session_id, "waiting")
        self.waiting.message_label.setText(
            "Локальные данные сохранены. Ввод данных приостановлен.\n\n"
            "После восстановления связи просмотрите изменения и выберите пациентов для переноса. "
            "Можно закрыть программу: ожидание продолжится при следующем запуске на этом ПК."
        )
        self.check_now()

    @staticmethod
    def _pause_reason_text(reason: str, payload: dict) -> str:
        if reason == "write_queue_not_drained":
            return f"В очереди ещё есть записи: {int(payload.get('pending_count') or 0)}."
        if reason == "maintenance_not_quiescent":
            return "Фоновое сохранение ещё выполняется."
        if reason == "monitor_pause_timeout":
            return "Фоновая проверка данных не успела остановиться."
        if reason == "shutting_down":
            return "RemCard уже завершает работу."
        return f"Причина: {reason}."

    def check_now(self):
        if self.busy:
            return
        if self.pause_token is None:
            self._run(
                lambda: self.data_service.pause_emergency_work(timeout_sec=5.0),
                self._paused,
                self._pause_failed,
            )
            return
        self.review_offered = False
        self.recovery_retry_requested = self.recovery_required
        self._request_fresh_probe("waiting")

    def _request_fresh_probe(self, context: str):
        if self.scheduler is None:
            self._show_probe_unavailable(context, "Служба проверки связи недоступна. Перезапустите RemCard.")
            return
        try:
            before = dict(self.scheduler.get_status() or {})
        except Exception as exc:
            self._show_probe_unavailable(context, f"Не удалось получить состояние проверки: {self._safe_detail(exc)}")
            return
        self._probe_context = str(context)
        self._probe_baseline_ts = max(
            float(before.get("last_probe_ts") or 0.0),
            float(self.last_status.get("last_probe_ts") or 0.0),
        )
        if context == "preflight" and self.preflight:
            self.preflight.set_checking()
        elif context == "waiting" and self.waiting:
            self.waiting.message_label.setText("Проверяем доступ к основной базе. Локальные данные сохранены.")
        try:
            accepted = bool(self.scheduler.request_probe(reason=f"emergency_{context}_manual"))
        except Exception as exc:
            accepted = False
            detail = self._safe_detail(exc)
        else:
            detail = "служба проверки остановлена"
        if not accepted:
            self._probe_context = ""
            self._show_probe_unavailable(context, f"Проверка не запущена: {detail}. Повторите попытку.")
            return
        self._probe_timeout_timer.start()
        try:
            current = dict(self.scheduler.get_status() or {})
        except Exception:
            current = {}
        self._consume_requested_probe_status(current)

    def _consume_requested_probe_status(self, payload: dict) -> bool:
        if not self._probe_context:
            return False
        try:
            probe_ts = float(payload.get("last_probe_ts") or 0.0)
        except (TypeError, ValueError):
            probe_ts = 0.0
        if probe_ts <= self._probe_baseline_ts or payload.get("running") or payload.get("pending"):
            return False
        context = self._probe_context
        status = str(payload.get("status") or "")
        if status.startswith("round_success_") or status in {"local_write_busy", "local_maintenance_busy"}:
            # One manual request begins a stability sequence. Every successful
            # round is fresh, but only the final stable status may authorize.
            self._probe_baseline_ts = probe_ts
            self._probe_timeout_timer.start()
            message = self._status_message(payload, context=context)
            if context == "preflight" and self.preflight:
                self.preflight.set_probe_result(ready=False, message=message, allow_retry=False)
            elif context == "waiting" and self.waiting:
                self.waiting.message_label.setText(message)
            return True
        self._probe_context = ""
        self._probe_timeout_timer.stop()
        ready = self._status_is_ready(payload)
        message = self._status_message(payload, context=context)
        if context == "preflight" and self.preflight:
            self.preflight.set_probe_result(ready=ready, message=message)
            if ready and os.environ.get("REMCARD_EMERGENCY_PASSWORD_AUTO_ACCEPT") == "1":
                QTimer.singleShot(0, self.preflight, self.preflight.approved.emit)
        elif context == "waiting" and self.waiting:
            self.waiting.message_label.setText(message)
            if ready:
                self._handle_ready_when_waiting()
        return True

    @Slot()
    def _probe_timed_out(self):
        context = self._probe_context
        if not context:
            return
        try:
            current = dict(self.scheduler.get_status() or {}) if self.scheduler is not None else {}
        except Exception:
            current = {}
        if self._consume_requested_probe_status(current):
            return
        self._probe_context = ""
        self._show_probe_unavailable(
            context,
            (
                "Проверка основной базы не завершилась вовремя. "
                "Локальная работа не приостановлена; повторите проверку."
                if context == "preflight"
                else "Проверка основной базы не завершилась вовремя. Локальные данные сохранены, "
                "работа остаётся приостановленной; повторите проверку."
            ),
        )

    def _show_probe_unavailable(self, context: str, message: str):
        if context == "preflight" and self.preflight:
            self.preflight.set_probe_result(ready=False, message=message, allow_retry=True)
        elif context == "waiting" and self.waiting:
            self.waiting.message_label.setText(message)

    @staticmethod
    def _status_is_ready(payload: dict) -> bool:
        status = str(payload.get("status") or "")
        if not bool(payload.get("network_stable")) or status not in READY_STATUSES:
            return False
        return status != "merge_ready_mode_a" or bool(payload.get("merge_ready"))

    @classmethod
    def _status_message(cls, payload: dict, *, context: str = "preflight") -> str:
        status = str(payload.get("status") or "")
        detail = cls._safe_detail(payload.get("error") or payload.get("reason") or "")
        if cls._status_is_ready(payload):
            if context == "waiting":
                return "Основная база успешно проверена и готова. Готовим просмотр локальных изменений."
            return (
                "Основная база успешно проверена и готова.\n"
                "Введите аварийный пароль и подтвердите переход к просмотру изменений."
            )
        if status == "session_lock_active":
            return (
                "Основная база доступна, но другое окно RemCard ещё использует её.\n"
                "Закройте обычные окна врача, медсестры или оперблока на других ПК, затем повторите проверку.\n"
                f"Причина проверки: {detail or 'активна блокировка рабочей сессии'}."
            )
        if status == "db_lock_active":
            return (
                "Основная база сейчас занята записью или обслуживанием. Дождитесь завершения операции "
                "и повторите проверку."
            )
        if status == "emergency_merge_lock_active":
            return (
                "Для основной базы уже выполняется аварийный перенос. Дождитесь его завершения "
                "и повторите проверку."
            )
        if status == "probe_file_unavailable":
            return (
                "Не удалось проверить безопасную запись в служебную папку основной базы. "
                "Проверьте сетевой доступ и права, затем повторите проверку."
            )
        if status == "remote_identity_mismatch":
            return (
                "Обнаружена другая или заменённая основная база. Перенос заблокирован. "
                "Не продолжайте завершение и обратитесь к администратору."
            )
        if status in {"client_policy_block", "client_policy_unavailable"}:
            return (
                "Основная база отклонила эту версию RemCard или её правила недоступны. "
                "Обновите RemCard либо обратитесь к администратору."
            )
        if status.startswith("round_success_"):
            current = int(payload.get("consecutive_successes") or 0)
            required = int(payload.get("success_rounds_required") or 0)
            return f"Связь появилась. Выполняется проверка устойчивости: {current} из {required}."
        if status == "running":
            return "Проверяем основную базу. Дождитесь результата проверки."
        if status.startswith("network_") or status in {"disabled", "idle", "exception"}:
            suffix = f" Причина: {detail}." if detail and detail != "probe_started" else ""
            return (
                "Основная база пока недоступна. Локальная работа остаётся доступной. "
                "Проверьте сеть и повторите проверку." + suffix
            )
        suffix = f" Причина: {detail}." if detail else ""
        return "Основная база ещё не готова к безопасному переносу. Повторите проверку." + suffix

    @staticmethod
    def _safe_detail(value, limit: int = 220) -> str:
        return " ".join(str(value or "").split())[:limit]

    def on_status(self, payload):
        if self._shutting_down:
            return
        self.last_status = dict(payload or {})
        ready = self._status_is_ready(self.last_status)
        consumed = self._consume_requested_probe_status(self.last_status)
        status = str(self.last_status.get("status") or "")
        if not ready and (status in {"running", "local_write_busy", "local_maintenance_busy"}
                          or status.startswith("round_success_")):
            return
        if not ready:
            self._network_ready = False
            self.recovery_notified = False
            self._notice_deferred = False
            self._recovery_reminder_timer.stop()
            self._dismiss_recovery_notice()
            if self.preflight and not consumed:
                self.preflight.set_probe_result(
                    ready=False,
                    message=self._status_message(self.last_status, context="preflight"),
                )
            if self.waiting and not consumed:
                self.waiting.message_label.setText(self._status_message(self.last_status, context="waiting"))
            return
        if not ready:
            return

        newly_ready = not self._network_ready
        self._network_ready = True
        if self.recovery_required:
            if self.recovery_retry_requested:
                self.recovery_retry_requested = False
                self._restart_after_close()
            return
        if self.waiting:
            if not consumed:
                self.waiting.message_label.setText(self._status_message(self.last_status, context="waiting"))
                self._handle_ready_when_waiting()
            return
        if self.preflight:
            return
        if newly_ready and not self._notice_deferred:
            self._schedule_recovery_notice()

    def _handle_ready_when_waiting(self):
        if self.busy or self.review_offered or self.pause_token is None or not self.waiting:
            return
        peers_ready = getattr(self.window, "_shared_emergency_peers_ready", None)
        if callable(peers_ready):
            try:
                ready = bool(peers_ready())
            except Exception as exc:
                logger.warning("Shared emergency peer readiness check failed: %s", exc)
                ready = False
            if not ready:
                self.waiting.message_label.setText(
                    "Ожидаем сохранения и закрытия других окон этой аварийной сессии…\n"
                    "Локальные данные в этом окне уже сохранены."
                )
                return
        self.review_offered = True
        self._run(self._build_review, self._show_review, self._review_failed)

    def _review_failed(self, exc):
        self.review_offered = True
        if self.waiting:
            self.waiting.message_label.setText(
                "Не удалось подготовить просмотр изменений. Локальные данные сохранены.\n"
                f"Причина: {self._safe_detail(exc)}\n"
                "Повторите проверку связи."
            )

    def _schedule_recovery_notice(self):
        if self._notice_show_scheduled or self.recovery_notice or self.waiting or self.preflight:
            return
        if not self._session_is_active() or not self._network_ready:
            return
        self._notice_show_scheduled = True
        self._notice_defer_timer.start(0)

    @Slot()
    def _show_recovery_notice_if_current(self):
        self._notice_show_scheduled = False
        if (
            self._shutting_down
            or self.recovery_notice is not None
            or self.waiting is not None
            or self.preflight is not None
            or not self._network_ready
            or not self._session_is_active()
        ):
            return
        dialog = EmergencyNetworkRestoredDialog(parent=self.window)
        self.recovery_notice = dialog
        self.recovery_notified = True
        dialog.go_now.connect(self._recovery_go_now)
        dialog.stayed.connect(self._recovery_stayed)
        dialog.show()
        dialog.raise_()

    @Slot()
    def _recovery_go_now(self):
        dialog = self.recovery_notice
        self.recovery_notice = None
        if dialog is not None:
            dialog.deleteLater()
        self._notice_deferred = False
        self.begin_wait(from_recovery_notice=True)

    @Slot()
    def _recovery_stayed(self):
        dialog = self.recovery_notice
        self.recovery_notice = None
        if dialog is not None:
            dialog.deleteLater()
        self._defer_recovery_notice()

    def _defer_recovery_notice(self):
        if self._shutting_down or not self._network_ready or not self._session_is_active():
            return
        self._notice_deferred = True
        self._recovery_reminder_timer.start()

    @Slot()
    def _recovery_reminder_due(self):
        if self._shutting_down or not self._network_ready or not self._session_is_active():
            self._notice_deferred = False
            return
        self._notice_deferred = False
        self._schedule_recovery_notice()

    def _session_is_active(self) -> bool:
        if self.window._is_closing:
            return False
        runtime_context = getattr(self.window.container, "runtime_context", None)
        if str(getattr(runtime_context, "mode", "") or "") != "emergency":
            return False
        try:
            return str(self.store.read_active_session(self.session_id).status or "") == "active"
        except Exception:
            return False

    def _dismiss_recovery_notice(self):
        dialog = self.recovery_notice
        self.recovery_notice = None
        if dialog is not None:
            dialog.finish_with_code(QDialog.Rejected)
            dialog.deleteLater()

    def _stop_recovery_notice(self):
        self._notice_deferred = False
        self._recovery_reminder_timer.stop()
        self._notice_defer_timer.stop()
        self._notice_show_scheduled = False
        self._dismiss_recovery_notice()

    def _stop_timers_and_notices(self):
        self._shutting_down = True
        self._probe_timeout_timer.stop()
        self._stop_recovery_notice()

    def _build_review(self):
        from rem_card.app.emergency_remote_identity import validate_remote_identity_error
        from rem_card.app.emergency_review import build_emergency_review

        session = self.store.read_active_session(self.session_id)
        remote_path = str(self.last_status.get("remote_db_path") or session.base_remote_db_path)
        identity_error = validate_remote_identity_error(session, remote_path)
        if identity_error:
            raise ValueError(identity_error)
        return build_emergency_review(
            base_db_path=session.base_snapshot_path,
            local_db_path=session.local_db_path,
            remote_db_path=remote_path,
        )

    def _show_review(self, review):
        from rem_card.app.emergency_review import review_selection
        from rem_card.ui.shared.emergency_review_dialog import EmergencyReviewDialog

        self.waiting.message_label.setText(
            "Связь восстановлена. Изменения ещё не перенесены.\n"
            "Просмотрите их или закройте программу, сохраняя ожидание."
        )
        dialog = EmergencyReviewDialog(review, parent=self.waiting)
        if dialog.exec() != QDialog.Accepted:
            self.waiting.message_label.setText(
                "Просмотр изменений отменён. Локальные данные сохранены. "
                "Нажмите «Проверить связь», когда будете готовы повторить."
            )
            return
        selected = list(dialog.selected_admission_ids)
        if not selected:
            self.store.mark_session_discarded(
                self.session_id,
                reason="user_declined_all_patients",
                requested_by_role=self.window._current_role_key(),
            )
            self.window._pending_emergency_discard = {"store": self.store, "session_id": self.session_id}
            self._restart_after_close()
            return
        self._run(
            lambda: review_selection(review, selected),
            self._selection_ready,
            self._selection_failed,
        )

    def _selection_failed(self, exc):
        self.review_offered = True
        self._message(
            "Проверка выбора не завершена",
            "Локальные данные сохранены. Не удалось проверить выбранных пациентов: "
            f"{self._safe_detail(exc)}. Повторите проверку связи.",
        )

    def _selection_ready(self, selection):
        if selection.get("blockers"):
            self.review_offered = True
            self._message(
                "Проверьте выбор пациентов",
                "\n".join(
                    str(item.get("message", "Проверьте данные")) if isinstance(item, dict) else str(item)
                    for item in selection["blockers"]
                ),
            )
            return
        try:
            authorize_patient_merge(self.store, self.session_id, selection)
            self.scheduler.mark_merge_ready()
        except Exception as exc:
            logger.warning("Emergency approval could not be saved: %s", exc)
            self.review_offered = True
            self._message(
                "Перенос не начат",
                "Не удалось подтвердить готовность основной базы. "
                "Локальные данные сохранены. Повторите проверку связи.",
            )
            return
        self._restart_after_close()

    def _restart_after_close(self):
        self._stop_timers_and_notices()
        if self.waiting:
            self.waiting.finish_with_code(0)
        restart = getattr(self.window, "_restart_after_emergency_workflow", None)
        if callable(restart) and restart():
            return
        QApplication.instance().setProperty("remcard_restart_requested", True)
        if not self.window.close():
            self._shutting_down = False
            QApplication.instance().setProperty("remcard_restart_requested", False)
            if self.waiting:
                self.waiting.show()

    def resume(self):
        if self.busy:
            return
        if self.recovery_required:
            self._message("Проверка переноса", "Сначала установите результат предыдущего переноса.")
            return
        if self.pause_token is not None:
            try:
                result = self.data_service.resume_emergency_work(self.pause_token)
            except Exception as exc:
                logger.warning("Emergency writes could not be resumed: %s", exc)
                result = {"ok": False, "reason": str(exc)}
            if not result.get("ok"):
                self._message(
                    "Работа остаётся приостановленной",
                    "Не удалось возобновить запись данных. Закройте и повторно запустите RemCard. "
                    f"Причина: {self._safe_detail(result.get('reason'))}.",
                )
                return
        self.pause_token = None
        try:
            resume_local_work(self.store, self.session_id)
        except Exception as exc:
            logger.warning("Emergency resume decision could not be saved: %s", exc)
            self._run(
                lambda: self.data_service.pause_emergency_work(timeout_sec=5.0),
                self._paused,
                self._pause_failed,
            )
            return
        cancel_shared_finish = getattr(self.window, "_cancel_shared_emergency_finish", None)
        if callable(cancel_shared_finish):
            try:
                cancel_shared_finish()
            except Exception as exc:
                logger.warning("Shared emergency finish request could not be cancelled: %s", exc)
        self.review_offered = False
        self.recovery_notified = True
        if self.waiting:
            self.waiting.finish_with_code(0)
            self.waiting.deleteLater()
        self.waiting = None
        self.window.stack.setEnabled(True)

    def close_waiting(self):
        if self.busy:
            self._message("Подготовка данных", "Дождитесь завершения текущей проверки.")
            return
        if not self.recovery_required:
            try:
                self.store.mark_session_status(self.session_id, "waiting")
            except Exception as exc:
                logger.warning("Emergency wait state could not be saved: %s", exc)
                self._message(
                    "Действие не завершено",
                    "Не удалось сохранить режим ожидания. Проверьте доступ к локальной папке данных.",
                )
                return
        self._stop_timers_and_notices()
        if self.waiting:
            self.waiting.finish_with_code(0)
        self.window.close()
