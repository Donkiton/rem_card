"""One application window. Each role owns a fresh, fully drained runtime."""
from __future__ import annotations

import os
from pathlib import Path
import time
import uuid

from PySide6.QtCore import QSettings, Qt, QTimer, QFileSystemWatcher, QThread, QObject
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMainWindow, QStackedWidget, QWidget, QVBoxLayout, QLabel, QPushButton, QMessageBox, QDialog

from rem_card.app.version import APP_DISPLAY_TITLE, APP_VERSION
from rem_card.app.unified_runtime import CentralUnavailable, CompatibilityError, check_client_compatibility, lifecycle_event, SessionShutdown
from rem_card.ui.shared.async_call import AsyncCallThread
from rem_card.ui.shared.unified_entry_pages import WelcomePage, StartupPage


class UnifiedWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        QApplication.instance().setProperty("unified_entry", True)
        self.unified_controller = self
        self.setWindowTitle(APP_DISPLAY_TITLE)
        self.setMinimumSize(1200, 780)
        self.settings = QSettings("MyHospital", "RemCardUnified")
        self.stack = QStackedWidget()
        from rem_card.ui.shared.unified_chrome import EntryChrome
        self.entry_chrome = EntryChrome(self, self.stack)
        self.setCentralWidget(self.entry_chrome)
        from rem_card.ui.shared.window_transition import WindowTransition
        self._transition = WindowTransition(self)
        self.loading = StartupPage()
        self.welcome = WelcomePage()
        self.stack.addWidget(self.loading)
        self.stack.addWidget(self.welcome)
        self.welcome.role_selected.connect(self.enter_role)
        self.welcome.settings_requested.connect(self.open_settings)
        self.welcome.about_requested.connect(self.about)
        self.welcome.update_requested.connect(self.update_application)
        self.root = ""
        self.store = None
        self.lease = None
        self.exclusive = None
        self.role_window = None
        self.container = None
        self.role = ""
        self.session_id = ""
        self._busy = False
        self._leaving = False
        self._shutdown = None
        self._pending_exit = False
        self._closing = False
        self._return_to_control = False
        self._restart = False
        self._candidate = None
        self._update_requested = False
        self._exit_update_checked = False
        self._suppress_exit_update = False
        self._maintenance_mutating = False
        self._owned_containers = []
        self._retired_widgets = []
        self._role_threads = []
        self._workers = set()
        self._maintenance_state = {}
        self._last_generation = -1
        self._exclusive_pending = False
        self._compatibility_error = ""
        self._central_unavailable = False
        self._local_only = False
        self._requires_fresh_runtime = False
        self._leave_notice = ""
        self._maintenance_deadline = None
        self._maintenance_operation = ""
        self._last_state_request = False
        self._institution = {"full_name": "", "short_name": ""}
        self._role_environment = None
        self._control_page = None
        self._restore_geometry("shell")
        self.statusBar().hide()
        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self.refresh_access)
        self._watcher.fileChanged.connect(self.refresh_access)
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(30000)
        self._status_timer.timeout.connect(self.refresh_access)
        self._countdown = QTimer(self)
        self._countdown.setInterval(250)
        self._countdown.timeout.connect(self._tick_maintenance)

    def _async(self, fn, done, failed=None):
        worker = AsyncCallThread(fn)
        self._workers.add(worker)
        worker.succeeded.connect(done)
        worker.failed.connect(failed or self._error)
        worker.finished.connect(lambda w=worker: self._workers.discard(w))
        worker.start()

    @property
    def _emergency_workflow(self):
        if self._local_only:
            return None
        return getattr(self.role_window, "_emergency_workflow", None)

    def show_loading_indicator(self, *args, **kwargs):
        if self.role_window:
            return self.role_window.show_loading_indicator(*args, **kwargs)

    def hide_loading_indicator(self, *args, **kwargs):
        if self.role_window:
            return self.role_window.hide_loading_indicator(*args, **kwargs)

    def _error(self, exc):
        if isinstance(exc, CentralUnavailable):
            self._central_unavailable = True
            self._compatibility_error = ""
        elif isinstance(exc, CompatibilityError):
            self._compatibility_error = str(exc)
            self._check_updates()
        self._busy = False
        self.loading.set_error(str(exc))
        local_allowed = self._central_unavailable and not self._compatibility_error
        self.welcome.set_access_state(str(exc), not local_allowed)
        self.stack.setCurrentWidget(self.welcome)
        lifecycle_event("unified_operation_failed", session_id=self.session_id, role=self.role,
                        error_class=type(exc).__name__)

    def initialize(self):
        from rem_card.app.runtime_paths import resolve_baza_dir, is_compiled, read_configured_baza_dir
        icon_dir = Path(__file__).resolve().parents[1] / "icon"
        icon = icon_dir / "remcardicon.ico"
        self.setWindowIcon(QIcon(str(icon)))
        self.loading.set_stage(0)
        try:
            if is_compiled() and not read_configured_baza_dir():
                self.change_database(first_run=True)
                return
            self.root = resolve_baza_dir()
        except Exception as exc:
            self._error(exc)
            self.change_database(first_run=True)
            return
        self.loading.complete_stage(0)
        self._configure_store()
        self.loading.set_stage(1)
        self._async(lambda: check_client_compatibility(self.root, APP_VERSION), self._initial_compatible, self._initial_incompatible)

    def _initial_incompatible(self, exc):
        self._error(exc)
        self._check_updates()

    def _configure_store(self):
        from rem_card.app.unified_access import MaintenanceStore

        self.store = MaintenanceStore(self.root)
        self._last_generation = -1
        # Watch parent as well: state file is atomically replaced, and control may be created later.
        if self._watcher.directories():
            self._watcher.removePaths(self._watcher.directories())
        self._watcher.addPath(self.root)
        self._status_timer.start()

    def _initial_compatible(self, _):
        self._compatibility_error = ""
        self._central_unavailable = False
        self.loading.complete_stage(1)
        self.loading.set_stage(2)
        cached = self.settings.value("institution/" + self.root, {})
        if isinstance(cached, dict):
            self._set_institution(cached)
        def prepared(institution):
            self._set_institution(institution)
            self.loading.complete_stage(2)
            self.loading.set_stage(3)
            # Clinical QObject services are constructed on role admission.
            def imports():
                from rem_card.app import bootstrap  # noqa: F401
                from rem_card.ui import main_window  # noqa: F401
            self._async(imports, self._ready)
        from rem_card.app.unified_runtime import read_institution
        self._async(lambda: read_institution(self.root), prepared)

    def _ready(self, _=None):
        self.loading.complete_stage(3)
        self.loading.set_stage(4)
        self.loading.complete_stage(4)
        self.stack.setCurrentWidget(self.welcome)
        self.refresh_access()
        self._check_updates()
        from rem_card.app.unified_shortcuts import migrate_known_legacy_shortcuts_once
        self._async(migrate_known_legacy_shortcuts_once, lambda _: None,
                    lambda exc: lifecycle_event("unified_shortcut_migration_failed", error_class=type(exc).__name__))
        from rem_card.app.unified_preflight import take_emergency_role_after_chooser_ready
        emergency_role = take_emergency_role_after_chooser_ready(self)
        if emergency_role:
            QTimer.singleShot(0, lambda: self.enter_role(emergency_role))

    def _check_updates(self):
        if self._local_only or self._requires_fresh_runtime:
            return
        from rem_card.app.main import _find_startup_update_candidate
        self._async(_find_startup_update_candidate, self._update_found,
                    lambda exc: lifecycle_event("unified_update_check_failed", error_class=type(exc).__name__))

    def _update_found(self, candidate):
        self._candidate = candidate
        self.welcome.set_update_available(candidate.version if candidate else "")

    def _set_institution(self, value):
        if not isinstance(value, dict):
            return
        self._institution = {k: str(value.get(k) or "") for k in ("full_name", "short_name")}
        for page in (self.welcome, self.loading):
            page.set_institution(self._institution["full_name"], self._institution["short_name"])
        self.settings.setValue("institution/" + self.root, self._institution)

    def enter_role(self, role):
        from rem_card.app.roles import ROLE_KEYS
        if role not in (*ROLE_KEYS, "settings") or not self.root:
            return
        if self._closing or self._busy or self._leaving or self.container is not None:
            return
        if self._requires_fresh_runtime:
            self._restart_for_storage_boundary()
            return
        self._save_geometry("shell")
        self._busy = True
        self._leave_notice = ""
        self._return_to_control = False
        self.role = role
        self.session_id = uuid.uuid4().hex
        self.stack.setCurrentWidget(self.loading)
        self.loading.reset()
        self.loading.complete_stage(0)
        self.loading.set_stage(1, "Проверка доступа к рабочему месту…")
        def admission():
            from rem_card.app.unified_access import SessionLease, MaintenanceStateError
            lease = SessionLease(self.root, role)
            try:
                acquired = lease.acquire()
            except MaintenanceStateError as exc:
                if lease.store.read().get("error") == "root_unavailable":
                    raise CentralUnavailable("Общая база недоступна. Возможен только локальный аварийный режим.") from exc
                raise
            if not acquired:
                raise RuntimeError("Вход закрыт: идут технические работы или база занята обслуживанием.")
            try:
                check_client_compatibility(self.root, APP_VERSION)
            except Exception:
                lease.release()
                raise
            return lease
        def start_admission():
            if self._workers:
                QTimer.singleShot(100, start_admission)
            else:
                self._async(admission, self._admitted, self._admission_failed)
        start_admission()

    def _configure_role_environment(self):
        keys = ("REMCARD_UI_ROLE", "REMCARD_LOCAL_FIRST_SYNC", "REMCARD_LOCAL_OUTBOX_SYNC",
                "REMCARD_BAZA_DIR", "REMCARD_UNIFIED_LOCAL_ONLY")
        self._role_environment = {key: os.environ.get(key) for key in keys}
        if self.role == "settings":
            os.environ.pop("REMCARD_UI_ROLE", None)
        else:
            os.environ["REMCARD_UI_ROLE"] = self.role
        if self.role.startswith("operblock"):
            os.environ["REMCARD_LOCAL_FIRST_SYNC"] = "0"
            os.environ["REMCARD_LOCAL_OUTBOX_SYNC"] = "0"

    def _admission_failed(self, exc):
        if self.lease or self.container is not None or getattr(exc, "cleanup_failed", False) or getattr(exc, "runtime_container", None) is not None:
            self._admitted_failed(exc)
            return
        if not isinstance(exc, (OSError, CentralUnavailable)) or self.role == "settings":
            self._error(exc)
            return
        self._central_unavailable = True
        self._local_only = True
        try:
            from rem_card.app.unified_preflight import prepare_local_only_runtime_context, bootstrap_local_only, CENTRAL_FAILURE_UNREACHABLE
            admission = prepare_local_only_runtime_context(
                role=self.role, central_root=self.root, central_failure=CENTRAL_FAILURE_UNREACHABLE)
            self.lease = admission.local_lease
            self._configure_role_environment()
            self.container = bootstrap_local_only(admission=admission, shell=self)
            from rem_card.app.main import _apply_app_theme
            _apply_app_theme(QApplication.instance(), self.role)
            self._finish_admission()
            self.statusBar().show()
            self.statusBar().showMessage("Локальный аварийный режим. Общая база отключена. Для подключения вернитесь к выбору ролей.")
        except (Exception, SystemExit) as failure:
            self._admitted_failed(failure)

    def _admitted(self, lease):
        self.lease = lease
        self._central_unavailable = False
        self._local_only = False
        try:
            self._configure_role_environment()
            from rem_card.app.bootstrap import bootstrap
            from rem_card.app.main import _apply_app_theme
            _apply_app_theme(QApplication.instance(), self.role if self.role != "settings" else "doctor")
            self.loading.set_stage(3, "Подготовка рабочего места…")
            QApplication.processEvents()
            from rem_card.app.unified_preflight import get_startup_request, prepare_admitted_runtime_context, bootstrap_admitted_container
            request = get_startup_request(self)
            marker = request.emergency_startup_request if self.role == request.role else ""
            runtime_context = prepare_admitted_runtime_context(
                role=self.role, central_lease=lease, emergency_startup_request=marker)
            self.container, _ = bootstrap_admitted_container(
                bootstrap, role=self.role, central_lease=lease, runtime_context=runtime_context,
                emergency_startup_request=marker)
            self._finish_admission()
        except (Exception, SystemExit) as exc:
            self._admitted_failed(exc)

    def _finish_admission(self):
        from rem_card.app.unified_preflight import complete_startup_request
        self._owned_containers.append(self.container)
        self.container.data_service.set_runtime_session(self.session_id, self.role)
        settings_service = self.container.settings_service
        settings_service.invalidate_cache()
        self._set_institution(settings_service.get_app_setting("institution", "identity", default={}))
        self._create_role_window()
        complete_startup_request(self)

    def _admitted_failed(self, exc):
        restart_required = getattr(exc, "status", "") == "local_restart_required"
        if isinstance(exc, SystemExit):
            exc = RuntimeError("Открытие рабочего места отменено.")
        self._error(exc)
        partial = getattr(exc, "runtime_container", None)
        if self.container is None and partial is not None:
            self.container = partial
        if self.container is not None:
            self.request_role_exit(force=True)
        elif self.lease:
            if getattr(exc, "cleanup_failed", False):
                self._busy = True
                self.welcome.set_access_state("Подготовка не завершена; освобождение базы не подтверждено. Требуется завершить процесс.", True)
            else:
                self.lease.release()
                self.lease = None
                self._restore_role_environment()
        if self.container is None and self.lease is None:
            self._local_only = False
            self._local_only_runtime_state = None
            self.setProperty("remcard_local_only_runtime", None)
            self._restore_role_environment()
            if restart_required:
                self._restart_for_storage_boundary()

    def _restart_for_storage_boundary(self):
        self._requires_fresh_runtime = True
        self._candidate = None
        self.welcome.set_update_available("")
        self._restart = True
        self._suppress_exit_update = True
        self._busy = False
        self.close()

    def _restore_role_environment(self):
        for key, value in (self._role_environment or {}).items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._role_environment = None

    def _create_role_window(self):
        from rem_card.ui.main_window import MainWindow

        shell = self
        class EmbeddedRoleWindow(MainWindow):
            def _apply_restored_maximization(self):
                # Legacy per-role timers must never resize the unified shell.
                return

            def _maybe_migrate_operblock_offline_after_release(self):
                if shell._local_only:
                    return
                return super()._maybe_migrate_operblock_offline_after_release()

            def _request_shared_emergency_finish(self, *args, **kwargs):
                if shell._local_only:
                    QMessageBox.information(shell, "Локальный режим", "Для подключения к общей базе вернитесь к выбору ролей и войдите снова после восстановления сети.")
                    return False
                return super()._request_shared_emergency_finish(*args, **kwargs)

            def _activate_container(self, container):
                if container is not None and not any(c is container for c in shell._owned_containers):
                    shell._owned_containers.append(container)
                super()._activate_container(container)
                shell.container = container
                if container is not None:
                    container.data_service.set_runtime_session(shell.session_id, shell.role)

            def _replace_operblock_container_after_runtime_drop(self, local_container, role_key):
                for widget in (self._operblock_widgets or {}).values():
                    if widget not in shell._retired_widgets:
                        shell._retired_widgets.append(widget)
                shell._capture_role_threads()
                return super()._replace_operblock_container_after_runtime_drop(local_container, role_key)

            def _acquire_role_lock(self, role_key):
                self._last_active_role_key = role_key
                return bool(shell.lease)

            def _schedule_maintenance(self):
                # DbManager manages safe periodic backups. No detached UI cleanup on role switch.
                return

            def show_roles(self):
                shell.request_role_exit()

            def closeEvent(self, event):
                event.ignore()
                if getattr(self, "_runtime_outage_handling", False):
                    shell._suppress_exit_update = True
                    shell._pending_exit = True
                    shell.request_role_exit(force=True)
                else:
                    shell.request_application_exit()

        self.role_window = EmbeddedRoleWindow(self.container, role=None if self.role == "settings" else self.role)
        self.role_window.setWindowFlags(Qt.Widget)
        self.role_window.title_bar.window_ptr = self
        self.stack.addWidget(self.role_window)
        if self.role == "settings":
            self.role_window.on_settings_clicked()
        else:
            self.role_window.prepare_initial_role_ui_for_startup()
            if not self.role_window._initial_role_ui_ready:
                raise RuntimeError("Не удалось подготовить выбранную роль.")
            self.role_window.start_initial_role_refresh()
            self.role_window.wake_initial_role_monitor()
            from rem_card.app.main import _wait_for_initial_w1, _startup_w1_wait_ms
            from rem_card.app.logger import logger
            _wait_for_initial_w1(QApplication.instance(), self.role_window, logger,
                                 time.perf_counter(), _startup_w1_wait_ms())
        for index in range(5):
            self.loading.complete_stage(index)
        self._animate_page(self.role_window, self.role, True, self._role_transition_ready,
                           maximize_default=self.role != "settings")

    def _role_transition_ready(self):
        self._busy = False
        self._compatibility_error = ""
        lifecycle_event("role_enter_ready", session_id=self.session_id, role=self.role)
        self.refresh_access()
        if self._pending_exit and not self._leaving:
            self.request_role_exit()

    def request_role_exit(self, force=False):
        if self._leaving:
            return
        interrupted_transition = self._transition.running
        if interrupted_transition:
            if not force:
                return
            self._transition.cancel()
        if self.container is None:
            self.show_roles()
            return
        if not force:
            from rem_card.ui.shared.custom_message_box import CustomMessageBox
            dialog = CustomMessageBox("Выход из роли", "Вернуться к выбору ролей? Несохранённый ввод будет потерян.",
                                      "warning", parent=self,
                                      action_buttons=[("Вернуться к ролям", QMessageBox.Yes), ("Остаться", QMessageBox.No)])
            buttons = dialog.findChildren(QPushButton, "DialogOkBtn")
            for button in buttons:
                button.setAutoDefault(False)
            if buttons:
                buttons[-1].setDefault(True)
                buttons[-1].setFocus()
            reply = dialog.exec()
            dialog.deleteLater()
            if reply != QMessageBox.Yes:
                self._pending_exit = False
                self._restart = False
                self._update_requested = False
                return
        self._leaving = True
        self._busy = True
        if not interrupted_transition:
            self._save_geometry(self.role)
        lifecycle_event("role_leave_requested", session_id=self.session_id, role=self.role, forced=bool(force))
        containers = list(self._owned_containers)
        containers.extend(self.role_window.iter_runtime_containers() if self.role_window else [self.container])
        for container in containers:
            data_service = getattr(container, "data_service", None)
            if data_service is not None:
                data_service.set_shutting_down()
        if self.role_window:
            self._capture_role_threads()
            self.role_window._is_closing = True
            for widget in (self.role_window.doctor_main, self.role_window.nurse_main, self.role_window.operblock_main):
                if widget is not None and hasattr(widget, "shutdown"):
                    widget.shutdown()
            for timer in self.role_window.findChildren(QTimer):
                timer.stop()
            watchdog = getattr(self.role_window, "_hard_ui_watchdog", None)
            if watchdog:
                watchdog.stop(timeout_sec=0.5)
        # Close modal input forms without applying their values. Preserve the runtime until drain.
        for widget in QApplication.topLevelWidgets():
            if isinstance(widget, QDialog) and widget is not self:
                widget.reject()
        if self._pending_exit:
            self.loading.set_stage(4, "Завершение сохранений и закрытие программы…")
            self.stack.setCurrentWidget(self.loading)
        else:
            self.welcome.set_access_state("Завершение сохранений и освобождение базы…", True)
            self._animate_page(self.welcome, "shell", False, lambda: None)
        self._shutdown = SessionShutdown(containers, session_id=self.session_id, role=self.role)
        self._wait_before_drain()

    def _capture_role_threads(self):
        # Capture parentless workers too, before widget.shutdown clears its attributes.
        for root in [self.role_window, *self._retired_widgets]:
            if root is None:
                continue
            for owner in [root, *root.findChildren(QObject)]:
                for value in [owner, *vars(owner).values()]:
                    if isinstance(value, QThread) and value not in self._role_threads:
                        self._role_threads.append(value)

    def _role_threads_running(self):
        from shiboken6 import isValid
        return any(isValid(thread) and thread.isRunning() for thread in self._role_threads)

    def _wait_before_drain(self):
        # Settings saves and detached UI reads must finish before closing their DB.
        if self._workers or any(w.isRunning() for w in AsyncCallThread._keepalive_threads):
            QTimer.singleShot(100, self._wait_before_drain)
            return
        if self._role_threads_running():
            self.welcome.set_access_state("Ожидание завершения фоновой задачи. База ещё не освобождена.", True)
            QTimer.singleShot(100, self._wait_before_drain)
            return
        self._async(self._shutdown.run, self._drained)

    def _drained(self, result):
        if self._transition.running:
            QTimer.singleShot(40, lambda: self._drained(result))
            return
        if not result["ok"]:
            self.welcome.set_access_state("; ".join(result["blocked"]), True)
            QTimer.singleShot(2000, self._retry_drain)
            return
        # Workers belonging to the role may still own an archive/read connection.
        active = list(AsyncCallThread._keepalive_threads)
        own = self._workers
        if any(w not in own and w.isRunning() for w in active):
            self.welcome.set_access_state("Ожидание завершения чтения данных…", True)
            QTimer.singleShot(500, lambda: self._drained(result))
            return
        if self._role_threads_running():
            self.welcome.set_access_state("Ожидание фоновых задач рабочего места…", True)
            QTimer.singleShot(500, lambda: self._drained(result))
            return
        failures = []
        for container in self._shutdown.containers:
            data = getattr(container, "data_service", None)
            outcomes = getattr(data, "write_outcomes", lambda: [])()
            failures.extend(item for item in outcomes if item.get("state") == "failed")
        if failures:
            self._leave_notice = f"Не выполнено сохранений: {len(failures)}. Результаты записаны в журнал диагностики. Проверьте данные после входа."
        if self.role_window:
            self.stack.removeWidget(self.role_window)
            self.role_window.deleteLater()
            self.role_window = None
        for widget in self._retired_widgets:
            widget.deleteLater()
        self._retired_widgets.clear()
        self._owned_containers.clear()
        self._role_threads.clear()
        self.container = None
        if self._local_only:
            self._suppress_exit_update = True
            self._requires_fresh_runtime = True
            if not self._pending_exit:
                self._restart = True
                self._pending_exit = True
        self._local_only = False
        self._local_only_runtime_state = None
        self.setProperty("remcard_local_only_runtime", None)
        self._restore_role_environment()
        if self.lease:
            self.lease.release()
            self.lease = None
        self.role = ""
        self._shutdown = None
        self._leaving = self._busy = False
        if self._pending_exit:
            self.close()
        elif self._return_to_control and self._control_page is not None:
            self.stack.setCurrentWidget(self._control_page)
            self.refresh_access()
        else:
            self.show_roles()

    def _retry_drain(self):
        if self._shutdown:
            self._async(self._shutdown.run, self._drained)

    def show_roles(self):
        if self.container is not None:
            self.request_role_exit()
            return
        self.stack.setCurrentWidget(self.welcome)
        self.refresh_access()

    def open_settings(self):
        if not self.root:
            self.change_database(first_run=True)
        elif self._maintenance_state.get("state", "open") != "open" or self._compatibility_error:
            self.open_maintenance()
        else:
            self.enter_role("settings")

    def refresh_access(self, *_):
        if self._local_only or self._requires_fresh_runtime or self._closing or self.store is None or self._last_state_request:
            return
        self._last_state_request = True
        def failure(exc):
            self._last_state_request = False
            self.welcome.set_access_state("Состояние доступа неизвестно. Проверьте соединение.", True)
            self._central_unavailable = True
            if self.container and not self._leaving and not self._local_only:
                self.request_role_exit(force=True)
        self._async(self.store.read, self._access_received, failure)

    def _access_received(self, state):
        self._last_state_request = False
        if self._closing:
            return
        generation = state.get("generation")
        if isinstance(generation, int):
            if generation < self._last_generation:
                return
            self._last_generation = generation
        self._maintenance_state = state
        blocked = state.get("state") != "open"
        if state.get("state") != "unknown" and self.store.control_dir.is_dir() and str(self.store.control_dir) not in self._watcher.directories():
            self._watcher.addPath(str(self.store.control_dir))
        if not self._leaving:
            message = self._compatibility_error or ("Идут технические работы. Попробуйте позже." if blocked else self._leave_notice)
            entry_blocked = blocked or bool(self._compatibility_error)
            if self._central_unavailable and state.get("state") not in {"draining", "maintenance"} and not self._compatibility_error:
                message = "Общая база недоступна. Выберите роль для локального аварийного режима."
                entry_blocked = False
            self.welcome.set_access_state(message, entry_blocked)
        if blocked and self.container and not self._leaving:
            if state.get("state") == "unknown":
                if not self._local_only:
                    self.request_role_exit(force=True)
                return
            operation = state.get("operation_id", "")
            if operation != self._maintenance_operation:
                self._maintenance_operation = operation
                self._maintenance_deadline = time.monotonic() + 60
                self._countdown.start()
                lifecycle_event("maintenance_received", session_id=self.session_id, role=self.role,
                                operation_id=operation)
        elif not blocked:
            self._maintenance_deadline = None
            self._countdown.stop()
            if not self._local_only:
                self.statusBar().hide()
        if self._control_page is not None:
            text = "Доступ открыт" if not blocked else "Вход закрыт. Ожидание освобождения базы…"
            if self.exclusive and state.get("state") == "maintenance":
                text = ("Сеансы единого входа завершены, доступ к базе удерживается для обслуживания. "
                        "Старые версии RemCard должны быть закрыты на всех ПК: они не поддерживают этот режим.")
            elif state.get("state") == "unknown":
                text = "Состояние базы неизвестно. Обслуживание не подтверждено."
            self._maintenance_label.setText(text)
            self._maintenance_button.setText("Включить технические работы" if not blocked else "Завершить технические работы")
            if blocked and state.get("state") != "unknown" and self.container is None and not self.exclusive and not self._exclusive_pending:
                self._exclusive_pending = True
                self._async(lambda: self.store.try_exclusive(expected_generation=state["generation"],
                            operation_id=state["operation_id"], owner_token=state["owner_token"]), self._exclusive_ready,
                            self._exclusive_failed)

    def _exclusive_failed(self, exc):
        self._exclusive_pending = False
        lifecycle_event("maintenance_exclusive_failed", error_class=type(exc).__name__)
        self._maintenance_label.setText("Не удалось подтвердить освобождение базы. Повторная проверка…")

    def _exclusive_ready(self, lease):
        self._exclusive_pending = False
        self.exclusive = lease
        if lease:
            self.refresh_access()
        else:
            QTimer.singleShot(1500, self.refresh_access)

    def _tick_maintenance(self):
        if self._maintenance_deadline is None:
            return
        seconds = max(0, int(self._maintenance_deadline - time.monotonic()) + 1)
        self.statusBar().show()
        self.statusBar().showMessage(f"Технические работы через {seconds} с. Сохраните изменения. Несохранённый ввод будет потерян.")
        if seconds <= 0:
            self._countdown.stop()
            self.request_role_exit(force=True)

    def open_maintenance(self):
        if self._local_only or self._requires_fresh_runtime:
            QMessageBox.information(self, "Локальный режим", "Для обслуживания общей базы завершите локальную роль и повторно подключитесь через выбор ролей.")
            return
        if self._control_page is None:
            page = QWidget()
            layout = QVBoxLayout(page)
            self._maintenance_label = QLabel("Проверка состояния…")
            self._maintenance_label.setWordWrap(True)
            layout.addWidget(self._maintenance_label)
            self._maintenance_button = QPushButton("Включить технические работы")
            self._maintenance_button.clicked.connect(self._toggle_maintenance)
            layout.addWidget(self._maintenance_button)
            path_button = QPushButton("Путь к базе данных…")
            path_button.clicked.connect(lambda: self.change_database())
            layout.addWidget(path_button)
            back = QPushButton("Назад")
            back.clicked.connect(self._back_from_control)
            layout.addWidget(back)
            layout.addStretch()
            self.stack.addWidget(page)
            self._control_page = page
        self.stack.setCurrentWidget(self._control_page)
        self.refresh_access()

    def _back_from_control(self):
        if self.role_window and not self._leaving:
            self.stack.setCurrentWidget(self.role_window)
        else:
            self.show_roles()

    def _toggle_maintenance(self):
        if self._local_only or self._requires_fresh_runtime:
            return
        if self._exclusive_pending or self._maintenance_mutating:
            return
        self._maintenance_mutating = True
        state = self._maintenance_state
        if state.get("state") == "open":
            self._async(lambda: self.store.begin(expected_generation=state.get("generation")), self._maintenance_started, self._maintenance_change_failed)
        else:
            self._async(lambda: self.store.finish(expected_generation=state.get("generation"),
                        operation_id=state.get("operation_id"), owner_token=state.get("owner_token")), self._maintenance_finished, self._maintenance_change_failed)

    def _maintenance_change_failed(self, exc):
        self._maintenance_mutating = False
        self._maintenance_label.setText("Изменение не подтверждено: " + str(exc))
        self.refresh_access()

    def _maintenance_finished(self, state):
        self._maintenance_mutating = False
        self.exclusive = None
        self._access_received(state)

    def _maintenance_started(self, state):
        self._maintenance_mutating = False
        self._return_to_control = True
        self._access_received(state)
        if self.container and self.role == "settings":
            self.request_role_exit(force=True)
        elif self.role_window:
            self.stack.setCurrentWidget(self.role_window)

    def change_database(self, first_run=False):
        if self._local_only or self._requires_fresh_runtime:
            QMessageBox.information(self, "Локальный режим", "Завершите локальную роль перед изменением пути к общей базе.")
            return
        from rem_card.ui.shared.unified_settings_dialogs import DatabasePathDialog
        from rem_card.app.runtime_paths import write_configured_baza_dir, is_compiled, save_dev_baza_dir
        dialog = DatabasePathDialog(self.root, self)
        if dialog.exec() != QDialog.Accepted:
            if first_run:
                self.welcome.set_access_state("Для начала работы выберите папку базы в настройках.", True)
                self.stack.setCurrentWidget(self.welcome)
            return
        raw_path = dialog.path_edit.text().strip()
        if not raw_path:
            QMessageBox.warning(self, "Путь к базе", "Укажите папку базы данных.")
            return
        selected = str(Path(raw_path).absolute())
        def validate():
            from rem_card.app.unified_database_setup import prepare_database_root
            ok, message = prepare_database_root(selected)
            if not ok:
                raise RuntimeError(message)
            check_client_compatibility(selected, APP_VERSION)
            if is_compiled():
                write_configured_baza_dir(selected)
            else:
                save_dev_baza_dir(selected)
            return selected
        def saved(path):
            if first_run:
                self.root = path
                self.loading.complete_stage(0)
                os.environ["REMCARD_BAZA_DIR"] = path
                from rem_card.app.runtime_paths import DEV_RUNTIME_BAZA_PIN_ENV
                os.environ[DEV_RUNTIME_BAZA_PIN_ENV] = str(os.getpid())
                self._configure_store()
                self._initial_compatible(None)
            elif QMessageBox.question(self, "Путь сохранён", "Перезапустить RemCard для подключения к выбранной базе?") == QMessageBox.Yes:
                self._restart = True
                self.close()
        self._async(validate, saved)

    def edit_institution(self):
        if self.container is None or self._leaving:
            return
        from rem_card.ui.shared.unified_settings_dialogs import InstitutionDialog
        dialog = InstitutionDialog(**self._institution, parent=self)
        if dialog.exec() == QDialog.Accepted:
            value = {"full_name": dialog.full_name.text().strip(), "short_name": dialog.short_name.text().strip()}
            service = self.container.settings_service
            self._async(lambda: service.set_app_setting("institution", "identity", value), lambda _: self._set_institution(value))

    def about(self):
        from rem_card.ui.shared.unified_settings_dialogs import EntryInformationDialog
        dialog = EntryInformationDialog("О программе", f"RemCard {APP_VERSION}\nРеанимационная карта\n\nЕдиное приложение для специалистов отделения.", self)
        dialog.exec()
        dialog.deleteLater()

    def update_application(self):
        if self._local_only or self._requires_fresh_runtime:
            return
        if self._candidate is None:
            return
        self._pending_exit = True
        self._update_requested = True
        self.close()

    def _exit_update_found(self, candidate):
        self._exit_update_checked = True
        self._update_found(candidate)
        if candidate is not None:
            self._update_requested = QMessageBox.question(
                self, "Обновление RemCard", f"Доступна версия {candidate.version}. Обновить перед выходом?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes) == QMessageBox.Yes
        self.close()

    def _exit_update_failed(self, exc):
        lifecycle_event("unified_exit_update_check_failed", error_class=type(exc).__name__)
        self._exit_update_checked = True
        self.close()

    def _save_geometry(self, key):
        if self._transition.running:
            return
        self.settings.setValue(key + "/geometry", self.saveGeometry())
        self.settings.setValue(key + "/maximized", self.isMaximized() or getattr(self, '_is_custom_maximized', False))
        normal = (getattr(self, '_custom_normal_geometry', self.normalGeometry())
                  if getattr(self, '_is_custom_maximized', False) else self.normalGeometry())
        self.settings.setValue(key + "/normal_rect", normal)

    def _animate_page(self, page, key, role_mode, finished, maximize_default=False):
        from PySide6.QtCore import QRect
        normal = self.settings.value(key + "/normal_rect")
        if not isinstance(normal, QRect) or not normal.isValid():
            probe = QWidget()
            geometry = self.settings.value(key + "/geometry")
            restored = bool(geometry) and probe.restoreGeometry(geometry)
            normal = probe.normalGeometry() if restored else QRect()
            probe.deleteLater()
        screen = QApplication.screenAt(normal.center()) if normal.isValid() else self.screen()
        available = (screen or self.screen()).availableGeometry()
        if not normal.isValid():
            normal = QRect(0, 0, min(1360, available.width()), min(820, available.height()))
            normal.moveCenter(available.center())
        normal.setWidth(min(available.width(), max(self.minimumWidth(), normal.width())))
        normal.setHeight(min(available.height(), max(self.minimumHeight(), normal.height())))
        normal.moveLeft(max(available.left(), min(normal.left(), available.right() - normal.width() + 1)))
        normal.moveTop(max(available.top(), min(normal.top(), available.bottom() - normal.height() + 1)))
        maximized = self.settings.value(key + "/maximized", maximize_default, type=bool)

        def prepare():
            self.entry_chrome.set_role_mode(role_mode)
            self.stack.setCurrentWidget(page)

        self._transition.start(available if maximized else normal, prepare, finished,
                               maximized=maximized, normal_rect=normal)

    def _restore_geometry(self, key, maximize_default=False):
        self._is_custom_maximized = False
        maximized = self.settings.value(key + "/maximized", maximize_default, type=bool)
        geometry = self.settings.value(key + "/geometry")
        if geometry:
            self.restoreGeometry(geometry)
        else:
            screen = self.screen().availableGeometry()
            self.resize(min(1360, screen.width()), min(820, screen.height()))
        if maximized:
            self.showMaximized()
        else:
            self.showNormal()

    def request_application_exit(self, confirmed=False):
        if self._leaving:
            self._pending_exit = True
            return
        if not confirmed:
            from rem_card.ui.shared.custom_message_box import CustomMessageBox
            if CustomMessageBox.question(self, "Выход из программы", "Выйти из программы?") != QMessageBox.Yes:
                self._pending_exit = False
                return
        self._pending_exit = True
        if self.container is not None:
            self.request_role_exit(force=True)
        else:
            self.close()

    def closeEvent(self, event):
        if self.container is not None:
            event.ignore()
            self.request_application_exit()
            return
        if self._busy or self._leaving:
            event.ignore()
            return
        if not self._exit_update_checked and not self._update_requested and not self._restart and not self._suppress_exit_update and self.root:
            event.ignore()
            self._closing = True
            self._status_timer.stop()
            self._exit_update_checked = True
            from rem_card.app.main import _find_startup_update_candidate
            self._async(_find_startup_update_candidate, self._exit_update_found, self._exit_update_failed)
            return
        self._status_timer.stop()
        self._closing = True
        if self._workers or self._exclusive_pending:
            event.ignore()
            QTimer.singleShot(100, self.close)
            return
        if not self.entry_chrome.role_mode:
            self._save_geometry("shell")
        self._status_timer.stop()
        if self.exclusive:
            self.exclusive.release()
        if self._restart:
            from rem_card.app.main import _launch_requested_restart
            launched = _launch_requested_restart()
        elif self._candidate and self._update_requested:
            from rem_card.app.update_launcher import launch_unified_update
            launched = launch_unified_update(self._candidate, wait_for_parent=True)
        else:
            launched = True
        if not launched:
            event.ignore()
            self._closing = self._restart = self._update_requested = self._pending_exit = False
            self._status_timer.start()
            QMessageBox.warning(self, "RemCard", "Не удалось запустить перезапуск или обновление. Программа остаётся открытой.")
            return
        event.accept()
        QApplication.quit()
