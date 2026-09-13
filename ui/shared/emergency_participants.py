"""Close sibling emergency windows only after their local writes have drained."""
from PySide6.QtCore import QObject, QTimer, Slot
from PySide6.QtWidgets import QApplication

from rem_card.app.logger import logger
from rem_card.ui.shared.async_call import AsyncCallThread


class EmergencyPeerMonitor(QObject):
    def __init__(self, window, participant):
        super().__init__(window)
        self.window = window
        self.participant = participant
        self.worker = None
        self.draining = False
        self.pause_token = None
        self.had_peers = False
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.poll)
        self.timer.start()

    @Slot()
    def poll(self):
        if self.window._is_closing or self.draining:
            return
        controller = getattr(self.window, "_emergency_workflow", None)
        if controller is None or controller.busy:
            return
        try:
            if self.participant.finish_lock is not None:
                ready = self.participant.peers_ready()
                if ready and self.had_peers and controller.waiting:
                    controller.check_now()
                self.had_peers = not ready
                return
            if not self.participant.another_window_is_finishing():
                self._restore_local_work()
                return
            orders = self.window._doctor_orders_widget_for_close()
            if orders is not None and orders.has_drafts():
                self.window.statusBar().showMessage(
                    "В другом окне завершают аварийную работу. Сохраните или отмените черновик назначений."
                )
                return
            self.window.stack.setEnabled(False)
            self.draining = True
            if controller.pause_token is not None:
                self._drained({"ok": True})
                return
            self.worker = AsyncCallThread(self.window.container.data_service.pause_emergency_work,
                                          timeout_sec=5.0, parent=self)
            self.worker.succeeded.connect(self._drained)
            self.worker.failed.connect(self._failed)
            self.worker.start()
        except Exception as exc:
            logger.warning("Emergency peer coordination failed: %s", exc)

    @Slot(object)
    def _drained(self, result):
        if self.window._is_closing:
            return
        if not result.get("ok"):
            self._failed("Локальное сохранение ещё не завершено")
            return
        controller = self.window._emergency_workflow
        if controller.pause_token is None:
            self.pause_token = result
        if not self.participant.another_window_is_finishing():
            self.draining = False
            self._restore_local_work()
            return
        if controller.waiting:
            controller.waiting.finish_with_code(0)
        if not self.window.close():
            self.draining = False
            self._restore_local_work()

    @Slot(object)
    def _failed(self, error):
        logger.warning("Emergency peer could not close: %s", error)
        self.draining = False
        self._restore_local_work()
        self.window.statusBar().showMessage("Ожидаем завершения локального сохранения перед выходом.")

    def _restore_local_work(self):
        if self.pause_token is not None:
            try:
                result = self.window.container.data_service.resume_emergency_work(self.pause_token)
            except Exception as exc:
                logger.warning("Could not resume peer after cancelled finish: %s", exc)
                return
            if not result.get("ok"):
                return
            self.pause_token = None
        controller = self.window._emergency_workflow
        if controller.pause_token is None and not controller.waiting:
            self.window.stack.setEnabled(True)


class NetworkEmergencyMonitor(QObject):
    """A window already open online must also join a newly authorized local session."""
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.redirecting = False
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.poll)
        self.timer.start()

    @Slot()
    def poll(self):
        if self.window._is_closing or self.redirecting or self.window._runtime_outage_handling:
            return
        from pathlib import Path
        from rem_card.app.emergency_metadata import read_json_file
        from rem_card.app.emergency_paths import active_dir, resolve_emergency_root
        from rem_card.app.emergency_remote_identity import remote_identity_paths_match
        from rem_card.app.emergency_workflow import RESUMABLE_STATUSES

        runtime = self.window.container.runtime_context
        if runtime.mode != "network":
            return
        directory = Path(active_dir(resolve_emergency_root()))
        if not directory.is_dir():
            return
        try:
            for metadata in directory.glob("*/emergency_session.json"):
                session = read_json_file(str(metadata))
                if session.get("status") not in RESUMABLE_STATUSES:
                    continue
                if not remote_identity_paths_match(session.get("base_remote_db_path", ""), runtime.medical_db_path):
                    continue
                orders = self.window._doctor_orders_widget_for_close()
                if orders is not None and orders.has_drafts():
                    self.window.statusBar().showMessage(
                        "На этом ПК открыта аварийная сессия. Сохраните или отмените черновик для перехода к локальным данным."
                    )
                    return
                self.redirecting = True
                self.window.stack.setEnabled(False)
                self.window.statusBar().showMessage("На этом ПК открыта аварийная сессия. Перезапускаем роль с локальными данными.")
                application = QApplication.instance()
                application.setProperty("remcard_restart_requested", True)
                if not self.window.close():
                    application.setProperty("remcard_restart_requested", False)
                    self.redirecting = False
                    self.window.stack.setEnabled(True)
                return
        except Exception as exc:
            logger.warning("Could not check local emergency session for network window: %s", exc)
