from PySide6.QtWidgets import QMainWindow, QStackedWidget, QApplication, QVBoxLayout, QFrame, QMessageBox, QLabel, QWidget
from PySide6.QtCore import QSettings, Qt, QPoint, QEvent, QTimer, Slot, Signal, QEventLoop

from .shared.navigation_widgets import WelcomeWidget
from .shared.custom_title_bar import CustomTitleBar
from .styles.theme import CUSTOM_DIALOG_RADIUS, BG_MAIN

import os
import sys
import ctypes
import threading
import socket
import time

from rem_card.app.logger import logger
from rem_card.app.local_metrics import record_metric
from rem_card.app import operblock_startup_metrics
from rem_card.app.foreground_activity import foreground_activity_snapshot, should_defer_background_io
from rem_card.app.maintenance_activity import active_maintenance_snapshot, maintenance_task
from rem_card.app.paths import get_icon_dir, get_role_lock_path
from rem_card.app.role_session_lock import RoleSessionLock
from rem_card.app.sqlite_shared import active_sqlite_operation_snapshot
from rem_card.app.roles import (
    ROLE_DOCTOR,
    ROLE_KEYS,
    ROLE_NURSE,
    ROLE_OPERBLOCK,
    ROLE_OPERBLOCK_EMERGENCY,
    ROLE_OPERBLOCK_PLANNED,
    is_operblock_role,
    normalize_role_key,
    operblock_table_code_for_role,
    role_display_name,
)
from rem_card.app.version import APP_DISPLAY_TITLE

def _get_desktop_path():
    return os.path.join(os.environ["USERPROFILE"], "Desktop")


def _find_our_shortcut():
    if not getattr(sys, "frozen", False):
        return None

    try:
        from win32com.client import Dispatch  # type: ignore
    except Exception:
        return None

    desktop = _get_desktop_path()
    exe_path = os.path.abspath(sys.executable).lower()

    shell = Dispatch("WScript.Shell")

    for file in os.listdir(desktop):
        if not file.endswith(".lnk"):
            continue

        path = os.path.join(desktop, file)

        try:
            shortcut = shell.CreateShortcut(path)
            target = shortcut.Targetpath.lower()

            if target == exe_path:
                return path
        except Exception:
            continue

    return None


def _get_icon_path(name):
    path = os.path.join(get_icon_dir(), name)
    return path if os.path.exists(path) else None


def _apply_role_icon(role):
    if role not in ("doctor", "nurse"):
        return

    settings = QSettings("MyHospital", "RemCard")
    last_role = settings.value("shortcut_icon_role", "", type=str)
    if last_role == role:
        return

    icon_map = {
        "doctor": _get_icon_path("doctor.ico"),
        "nurse": _get_icon_path("nurse.ico"),
    }

    icon = icon_map.get(role)
    if not icon:
        return

    shortcut = _find_our_shortcut()
    if not shortcut:
        return

    try:
        from win32com.client import Dispatch  # type: ignore
    except Exception:
        return

    try:
        shell = Dispatch("WScript.Shell")
        sc = shell.CreateShortcut(shortcut)
        sc.IconLocation = icon
        sc.Save()
    except Exception as exc:
        logger.warning("Failed to update desktop shortcut icon (%s, role=%s): %s", shortcut, role, exc)
        return

    try:
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x0000, None, None)
    except Exception:
        pass
    settings.setValue("shortcut_icon_role", role)


class MainWindow(QMainWindow):
    maintenance_finished = Signal(str)

    def __init__(self, container, role=None, role_session_lock=None, role_key=None):
        super().__init__()
        self.container = container
        self._default_container = container
        self._operblock_container = None
        self._initial_role = role
        self._role_lock = role_session_lock
        self._role_lock_key = role_key if role_session_lock else None
        self._role_lock_owner_id = f"{socket.gethostname()}:{os.getpid()}:rem_card_ui"
        self._initial_role_ui_ready = False
        self._initial_role_auto_refresh_started = False
        self._runtime_outage_handling = False
        self._restore_probe_dialog_active = False
        self._restore_probe_conflict_notified = False
        self._restore_probe_notice_deferred_until = 0.0
        self._pending_emergency_discard = None
        self._exit_role_key = None
        self._last_active_role_key = role if role in ROLE_KEYS else None
        self.maintenance_finished.connect(self._on_maintenance_finished)
        
        self.setup_base_ui()
        self._connect_runtime_outage_signal()
        self._connect_restore_probe_signal()

        if role:
            self.init_with_role(role)
        else:
            self.init_ui()

        self._schedule_maintenance()

        is_max_saved = self.settings.value("is_maximized", False, type=bool)
        if is_max_saved:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, self._apply_restored_maximization)

    def setup_base_ui(self):
        self.settings = QSettings("MyHospital", "RemCard")
        self.settings.sync()
        self.setWindowTitle(APP_DISPLAY_TITLE)
        
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowMinMaxButtonsHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_Hover)
        
        self._resize_margin = 10
        self._is_resizing = False
        self._resize_edge = None
        self._is_custom_maximized = False

        self.main_container = QFrame()
        self.main_container.setObjectName("MainFrame")
        self.main_container.setStyleSheet(f"""
            QFrame#MainFrame {{ 
                background-color: {BG_MAIN} !important; 
                border: 1px solid #bdc3c7; 
                border-radius: {CUSTOM_DIALOG_RADIUS}; 
            }}
        """)
        
        self.main_layout = QVBoxLayout(self.main_container)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)
        
        self.title_bar = CustomTitleBar(self)
        self.main_layout.addWidget(self.title_bar)

        self._emergency_banner = None
        runtime_context = getattr(self.container, "runtime_context", None)
        runtime_mode = getattr(runtime_context, "mode", "")
        if runtime_mode in {"emergency", "opblock_offline"}:
            if runtime_mode == "opblock_offline":
                banner_text = (
                    "Локальный режим оперблока: работа ведется на этом ПК. "
                    "Завершённые случаи будут перенесены после восстановления связи."
                )
            else:
                banner_text = (
                    "Аварийный режим: работа ведется на локальной базе этого ПК. "
                    "До восстановления сети работайте только здесь."
                )
            self._emergency_banner = QLabel(
                banner_text
            )
            self._emergency_banner.setObjectName("EmergencyModeBanner")
            self._emergency_banner.setWordWrap(True)
            self._emergency_banner.setStyleSheet(
                """
                QLabel#EmergencyModeBanner {
                    background-color: #fff3cd;
                    color: #5f4300;
                    border-bottom: 1px solid #e0b849;
                    padding: 8px 14px;
                    font-weight: 700;
                }
                """
            )
            self.main_layout.addWidget(self._emergency_banner)

            if runtime_mode == "emergency":
                self._restore_probe_status_label = QLabel("Ожидание восстановления сетевой базы")
                self._restore_probe_status_label.setObjectName("EmergencyRestoreProbeStatus")
                self._restore_probe_status_label.setStyleSheet(
                    """
                    QLabel#EmergencyRestoreProbeStatus {
                        background-color: #e8f2ff;
                        color: #174264;
                        border-bottom: 1px solid #9fc4e8;
                        padding: 5px 14px;
                        font-weight: 600;
                    }
                    """
                )
                self.main_layout.addWidget(self._restore_probe_status_label)
            else:
                self._restore_probe_status_label = None
        else:
            self._restore_probe_status_label = None
        
        self.stack = QStackedWidget()
        self.main_layout.addWidget(self.stack)
        
        self.setCentralWidget(self.main_container)

        geom = self.settings.value("geometry")
        if geom: self.restoreGeometry(geom)
        state = self.settings.value("windowState")
        if state: self.restoreState(state)
        if not geom: self.resize(1280, 900)

        norm_geom = self.settings.value("normalGeometry")
        if norm_geom: self.setProperty("normalGeometry", norm_geom)

        self.doctor_main = None
        self.nurse_main = None
        self.operblock_main = None
        self._operblock_widgets = {}
        self._active_operblock_role_key = None
        self.admin_main = None
        self.welcome = None
        self._maintenance_scheduled = False
        self._maintenance_timer = None
        self._loading_overlay = None
        self._loading_tokens = set()
        self._loading_messages = {}
        self._loading_generations = {}
        self._loading_order = []
        self._is_closing = False
        self._event_loop_watchdog_timer = None
        self._event_loop_watchdog_last_ts = 0.0
        self._event_loop_watchdog_last_log_ts = 0.0
        self._focus_refresh_pending = False
        self._focus_refresh_in_progress = False
        self._last_focus_refresh_ts = 0.0
        self._focus_refresh_timer = QTimer(self)
        self._focus_refresh_timer.setSingleShot(True)
        self._focus_refresh_timer.setInterval(0)
        self._focus_refresh_timer.timeout.connect(self._run_focus_refresh)
        self._install_decor_overlay()
        self._start_event_loop_watchdog()

    def _ensure_loading_overlay(self):
        overlay = getattr(self, "_loading_overlay", None)
        if overlay is None:
            from rem_card.ui.shared.loading_overlay import LoadingOverlay

            overlay = LoadingOverlay(
                self.main_container,
                gif_path=os.path.join(get_icon_dir(), "loading.gif"),
            )
            self._loading_overlay = overlay
        self._position_loading_overlay()
        return overlay

    def _position_loading_overlay(self):
        overlay = getattr(self, "_loading_overlay", None)
        if overlay is None:
            return
        try:
            overlay.setGeometry(self.main_container.rect())
            overlay.raise_()
        except RuntimeError:
            pass

    def show_loading_indicator(
        self,
        message: str = "Загрузка...",
        *,
        key: str | None = None,
        auto_hide_ms: int | None = None,
        process_events: bool = False,
    ) -> str:
        if self._is_closing:
            return str(key or "")
        token = str(key or f"loading:{time.monotonic_ns()}")
        generation = int(self._loading_generations.get(token, 0) or 0) + 1
        self._loading_generations[token] = generation
        self._loading_tokens.add(token)
        self._loading_messages[token] = str(message or "Загрузка...")
        self._loading_order.append(token)

        overlay = self._ensure_loading_overlay()
        overlay.show_loading(self._loading_messages[token])
        if auto_hide_ms is not None and int(auto_hide_ms) > 0:
            QTimer.singleShot(
                int(auto_hide_ms),
                lambda token=token, generation=generation: self._hide_loading_if_generation(token, generation),
            )
        if process_events:
            app = QApplication.instance()
            if app is not None:
                app.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        return token

    def _hide_loading_if_generation(self, token: str, generation: int):
        if int(self._loading_generations.get(token, 0) or 0) != int(generation):
            return
        self.hide_loading_indicator(token)

    def hide_loading_indicator(self, key: str | None = None, *, delay_ms: int = 0):
        if delay_ms and int(delay_ms) > 0:
            if key is None:
                generations = dict(self._loading_generations)
                QTimer.singleShot(int(delay_ms), lambda: self._hide_all_loading_if_generations(generations))
            else:
                generation = int(self._loading_generations.get(str(key), 0) or 0)
                QTimer.singleShot(
                    int(delay_ms),
                    lambda token=str(key), generation=generation: self._hide_loading_if_generation(token, generation),
                )
            return

        if key is None:
            self._loading_tokens.clear()
            self._loading_messages.clear()
            self._loading_order.clear()
        else:
            token = str(key)
            self._loading_tokens.discard(token)
            self._loading_messages.pop(token, None)
            self._loading_order = [item for item in self._loading_order if item != token]

        overlay = getattr(self, "_loading_overlay", None)
        if overlay is None:
            return
        if not self._loading_tokens:
            overlay.hide_loading()
            return

        while self._loading_order and self._loading_order[-1] not in self._loading_tokens:
            self._loading_order.pop()
        active_token = self._loading_order[-1] if self._loading_order else next(iter(self._loading_tokens))
        overlay.show_loading(self._loading_messages.get(active_token, "Загрузка..."))

    def _hide_all_loading_if_generations(self, generations: dict):
        if set(self._loading_tokens) - set(generations):
            return
        for token, generation in generations.items():
            if int(self._loading_generations.get(token, 0) or 0) != int(generation or 0):
                return
        self.hide_loading_indicator()

    def _current_role_key(self) -> str:
        current_widget = self.stack.currentWidget()
        if current_widget == self.doctor_main:
            return ROLE_DOCTOR
        if current_widget == self.nurse_main:
            return ROLE_NURSE
        if current_widget == self.operblock_main or current_widget in (self._operblock_widgets or {}).values():
            return self._active_operblock_role_key or ROLE_OPERBLOCK
        if self.stack.currentWidget() == self.admin_main:
            return "admin"
        return str(self._role_lock_key or self._initial_role or "unknown")

    def _current_visible_remcard_context(self) -> dict:
        current_widget = self.stack.currentWidget() if hasattr(self, "stack") else None
        layout = None
        if current_widget == getattr(self, "doctor_main", None):
            remcard_widget = getattr(self.doctor_main, "remcard_widget", None)
            layout = getattr(remcard_widget, "layout_manager", None)
        elif current_widget == getattr(self, "nurse_main", None):
            layout = getattr(self.nurse_main, "layout_manager", None)
        elif current_widget == getattr(self, "operblock_main", None) or current_widget in (self._operblock_widgets or {}).values():
            snapshot_provider = getattr(current_widget, "diagnostic_snapshot", None)
            snapshot = snapshot_provider() if callable(snapshot_provider) else {}
            return {
                "admission_id": snapshot.get("current_admission_id"),
                "tab_name": "operblock",
                "mode": str(snapshot.get("current_table_code") or ""),
                "operation_case_id": snapshot.get("current_operation_case_id"),
            }
        if layout is None:
            return {}

        tab_name = ""
        sector_2b = getattr(layout, "sector_2b", None)
        current_tab_name = getattr(sector_2b, "current_tab_name", None)
        if callable(current_tab_name):
            try:
                tab_name = str(current_tab_name() or "")
            except Exception:
                tab_name = ""
        return {
            "admission_id": getattr(layout, "current_admission_id", None),
            "tab_name": tab_name,
            "mode": str(getattr(layout, "current_mode", "") or ""),
        }

    @staticmethod
    def _watchdog_snapshot_names(snapshot: dict, key: str) -> str:
        items = snapshot.get(key) if isinstance(snapshot, dict) else None
        if not isinstance(items, list):
            return ""
        names = [
            str(item.get("task_type") or item.get("operation_name") or item.get("name") or "")
            for item in items
            if isinstance(item, dict) and str(item.get("task_type") or item.get("operation_name") or item.get("name") or "")
        ]
        return ",".join(names[:4])

    @staticmethod
    def _watchdog_first_snapshot_item(snapshot: dict, key: str) -> dict:
        items = snapshot.get(key) if isinstance(snapshot, dict) else None
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return dict(items[0])
        return {}

    def _current_operblock_diagnostics_snapshot(self) -> dict:
        current_widget = self.stack.currentWidget() if hasattr(self, "stack") else None
        if not (current_widget == getattr(self, "operblock_main", None) or current_widget in (self._operblock_widgets or {}).values()):
            return {}
        provider = getattr(current_widget, "diagnostic_snapshot", None)
        if not callable(provider):
            return {}
        try:
            return dict(provider() or {})
        except Exception:
            return {}

    def _install_decor_overlay(self) -> None:
        try:
            from rem_card.ui.shared.decor_overlay import DecorOverlayWidget

            self.decor_overlay = DecorOverlayWidget(
                self.stack,
                context_provider=self._decor_context,
                target_provider=self._decor_target_widget,
            )
            self.decor_overlay.setGeometry(self.stack.rect())
            self.decor_overlay.raise_()
            self.stack.currentChanged.connect(self._on_decor_stack_changed)
        except Exception as exc:
            self.decor_overlay = None
            logger.warning("Failed to initialize decor overlay: %s", exc, exc_info=True)

    def _on_decor_stack_changed(self, *_args) -> None:
        overlay = getattr(self, "decor_overlay", None)
        if overlay is None:
            return
        try:
            overlay.setGeometry(self.stack.rect())
            overlay.raise_()
            overlay.reload_settings()
        except Exception as exc:
            logger.warning("Failed to refresh decor overlay after stack change: %s", exc)

    def _decor_context(self) -> dict:
        try:
            role = self._current_role_key()
        except Exception:
            role = "unknown"
        current_widget = self.stack.currentWidget() if hasattr(self, "stack") else None
        mode = ""
        if current_widget == getattr(self, "doctor_main", None):
            remcard_widget = getattr(self.doctor_main, "remcard_widget", None)
            layout = getattr(remcard_widget, "layout_manager", None)
            mode = str(getattr(layout, "current_mode", "") or "")
        elif current_widget == getattr(self, "nurse_main", None):
            layout = getattr(self.nurse_main, "layout_manager", None)
            mode = str(getattr(layout, "current_mode", "") or getattr(self.nurse_main, "_selection_mode", "") or "")
        elif current_widget == getattr(self, "admin_main", None):
            mode = "admin"
        elif current_widget == getattr(self, "welcome", None):
            mode = "welcome"
            role = "welcome"
        elif role.startswith("operblock"):
            mode = "operblock"
        return {"role": role, "mode": mode}

    def _decor_target_widget(self, event: dict) -> QWidget | None:
        zone = str((event or {}).get("zone") or "all")
        current_widget = self.stack.currentWidget() if hasattr(self, "stack") else None
        if zone == "w1":
            if current_widget == getattr(self, "doctor_main", None):
                remcard_widget = getattr(self.doctor_main, "remcard_widget", None)
                layout = getattr(remcard_widget, "layout_manager", None)
                return getattr(layout, "beds_selection_widget", None)
            if current_widget == getattr(self, "nurse_main", None):
                layout = getattr(self.nurse_main, "layout_manager", None)
                return getattr(layout, "beds_selection_widget", None)
        return current_widget

    def _connect_runtime_outage_signal(self):
        data_service = getattr(getattr(self.container, "data_service", None), "network_outage_detected", None)
        if data_service is None:
            return
        try:
            data_service.connect(self._handle_runtime_network_outage, Qt.QueuedConnection)
        except Exception as exc:
            logger.warning("Failed to connect runtime outage signal: %s", exc)

    def _connect_restore_probe_signal(self):
        data_service = getattr(getattr(self.container, "data_service", None), "restore_probe_status", None)
        if data_service is None:
            return
        try:
            data_service.connect(self._handle_restore_probe_status, Qt.QueuedConnection)
        except Exception as exc:
            logger.warning("Failed to connect restore probe signal: %s", exc)

    def _is_emergency_runtime(self) -> bool:
        runtime_context = getattr(self.container, "runtime_context", None)
        return getattr(runtime_context, "mode", "") == "emergency"

    def _runtime_outage_change_ids(self) -> tuple[int, int]:
        data_service = getattr(self.container, "data_service", None)
        observed = 0
        if data_service is not None:
            try:
                state = data_service.get_observed_change_state() or {}
                observed = int(state.get("change_id") or 0)
            except Exception:
                observed = 0
        standby = 0
        scheduler = getattr(data_service, "_emergency_standby_scheduler", None) if data_service is not None else None
        manager = getattr(scheduler, "manager", None)
        store = getattr(manager, "store", None)
        if store is not None:
            try:
                metadata = store.get_latest_valid_standby()
                standby = int(getattr(metadata, "remote_last_change_id", 0) or 0)
            except Exception:
                standby = 0
        return observed, standby

    @Slot(dict)
    def _handle_runtime_network_outage(self, payload: dict):
        if self._runtime_outage_handling or self._is_closing:
            return
        self._runtime_outage_handling = True
        data_service = getattr(self.container, "data_service", None)
        role = str((payload or {}).get("role") or self._initial_role or self._current_role_key() or "").lower()
        if is_operblock_role(role):
            self._handle_operblock_runtime_network_outage(payload, data_service=data_service, role=role)
            return
        try:
            self.stack.setEnabled(False)
        except Exception:
            pass

        if role != "nurse":
            from rem_card.app.runtime_outage import build_doctor_runtime_outage_message
            from rem_card.ui.shared.custom_message_box import CustomMessageBox

            CustomMessageBox.warning(None, "База данных недоступна", build_doctor_runtime_outage_message())
            if data_service is not None:
                data_service.prepare_runtime_outage_shutdown(timeout=5.0)
            self.close()
            return

        from rem_card.app.runtime_outage import (
            build_runtime_outage_dialog_message,
            launch_emergency_restart,
            write_runtime_outage_startup_request,
        )
        from rem_card.services.settings.settings_service import reset_settings_service
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        shutdown_ok = False
        if data_service is not None:
            shutdown_ok = bool(data_service.prepare_runtime_outage_shutdown(timeout=5.0))
        queue_state = data_service.get_write_queue_state() if data_service is not None else {}
        observed_change_id, standby_change_id = self._runtime_outage_change_ids()
        unconfirmed_count = int(queue_state.get("unconfirmed_write_count") or 0)
        pending_count = int(queue_state.get("pending_count") or 0)
        queue_shutdown_result = str(queue_state.get("queue_shutdown_result") or ("settled" if shutdown_ok else "failed"))
        queue_settled = queue_state.get("queue_settled")
        unconfirmed = unconfirmed_count > 0
        unknown_active = bool(
            queue_state.get("active_write_in_progress")
            or queue_state.get("unknown_active_write")
            or queue_shutdown_result in {"timeout", "failed"}
        )
        stale_standby = int(observed_change_id or 0) > int(standby_change_id or 0)
        marker_path, marker_payload = write_runtime_outage_startup_request(
            source_role="nurse",
            last_observed_remote_change_id=observed_change_id,
            standby_last_change_id=standby_change_id,
            unconfirmed_writes=bool(unconfirmed or unknown_active),
            pending_write_count=pending_count,
            unconfirmed_write_count=unconfirmed_count,
            unknown_active_write=bool(unknown_active),
            queue_shutdown_result=queue_shutdown_result,
            queue_settled=queue_settled,
        )
        message = build_runtime_outage_dialog_message(
            unconfirmed_writes=bool(unconfirmed),
            unknown_active_write=bool(unknown_active),
            stale_standby=bool(marker_payload.get("stale_gap_detected") or stale_standby),
        )
        result = CustomMessageBox.warning_with_actions(
            None,
            "Сетевая база недоступна",
            message,
            [
                ("Перейти в аварийный режим", 1),
                ("Закрыть RemCard", 0),
            ],
        )
        reset_settings_service()
        if int(result or 0) == 1:
            emergency_session_ready = self._prepare_runtime_outage_emergency_session(marker_payload)
            if not emergency_session_ready:
                self.close()
                return
            launched = launch_emergency_restart(marker_path, role="nurse")
            if not launched:
                CustomMessageBox.warning(
                    None,
                    "Аварийный режим",
                    "RemCard будет закрыта. Запустите RemCard медсестры снова, чтобы открыть аварийный режим.",
                )
        self.close()

    def _handle_operblock_runtime_network_outage(self, payload: dict, *, data_service=None, role: str = ""):
        from rem_card.app.operblock_offline_store import (
            OPERBLOCK_RUNTIME_DROP_WARNING,
            mirror_active_operblock_cases_from_network_db,
            start_or_resume_operblock_offline_session,
        )
        from rem_card.services.settings.settings_service import reset_settings_service
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        try:
            self.stack.setEnabled(False)
        except Exception:
            pass
        try:
            if data_service is not None:
                data_service.prepare_runtime_outage_shutdown(timeout=5.0)
        except Exception as exc:
            logger.warning("Operblock runtime outage shutdown of network data service failed: %s", exc, exc_info=True)
        try:
            db_manager = getattr(self.container, "db_manager", None)
            if db_manager is not None:
                mirror_active_operblock_cases_from_network_db(db_manager, reason="runtime_drop_after_queue_shutdown")
        except Exception as exc:
            logger.warning("Operblock runtime outage active-case mirror failed: %s", exc, exc_info=True)

        network_db_path = str(getattr(getattr(self.container, "db_manager", None), "db_path", "") or "")
        try:
            session = start_or_resume_operblock_offline_session(
                reason=str((payload or {}).get("reason") or "runtime_drop"),
                network_db_path=network_db_path,
            )
            from rem_card.app.bootstrap import bootstrap

            reset_settings_service()
            local_container = bootstrap(role=role or ROLE_OPERBLOCK, runtime_context=session.runtime_context)
            self._replace_operblock_container_after_runtime_drop(local_container, role or ROLE_OPERBLOCK)
            CustomMessageBox.warning(self, "Оперблок: локальный режим", OPERBLOCK_RUNTIME_DROP_WARNING)
        except Exception as exc:
            logger.warning("Operblock runtime offline switch failed: %s", exc, exc_info=True)
            CustomMessageBox.warning(
                self,
                "Оперблок: локальный режим",
                "Не удалось переключить оперблок на локальную базу. Локальные данные, если они были созданы, сохранены.",
            )
            self.close()
        finally:
            try:
                self.stack.setEnabled(True)
            except Exception:
                pass
            self._runtime_outage_handling = False

    def _replace_operblock_container_after_runtime_drop(self, local_container, role_key: str) -> None:
        from .operblock_view.operblock_main_widget import OperBlockMainWidget

        self._ensure_operblock_offline_banner()
        clean_role = role_key if is_operblock_role(role_key) else ROLE_OPERBLOCK
        old_widgets = list(dict.fromkeys((self._operblock_widgets or {}).values()))
        if self.operblock_main is not None and self.operblock_main not in old_widgets:
            old_widgets.append(self.operblock_main)
        for widget in old_widgets:
            try:
                if hasattr(widget, "shutdown"):
                    widget.shutdown()
                elif hasattr(widget, "stop_auto_refresh"):
                    widget.stop_auto_refresh()
            except Exception as exc:
                logger.warning("Failed to shutdown old operblock widget during offline switch: %s", exc)
            try:
                self.stack.removeWidget(widget)
                widget.setParent(None)
            except Exception:
                pass

        self._default_container = local_container
        self._operblock_container = local_container
        self._activate_container(local_container)
        self._operblock_widgets = {}
        self.operblock_main = OperBlockMainWidget(
            self.container.patient_service,
            self.container.remcard_service,
            self.container.operblock_service,
            table_code=operblock_table_code_for_role(clean_role),
            parent=self.stack,
        )
        self._operblock_widgets[clean_role] = self.operblock_main
        self._active_operblock_role_key = clean_role
        self.stack.addWidget(self.operblock_main)
        self.stack.setCurrentWidget(self.operblock_main)
        self._initial_role = clean_role
        self._initial_role_ui_ready = True
        self._initial_role_auto_refresh_started = False
        QTimer.singleShot(0, self.operblock_main.start_auto_refresh)

    def _ensure_operblock_offline_banner(self) -> None:
        if getattr(self, "_emergency_banner", None) is not None:
            self._emergency_banner.setText(
                "Локальный режим оперблока: работа ведется на этом ПК. "
                "Завершённые случаи будут перенесены после восстановления связи."
            )
            return
        self._emergency_banner = QLabel(
            "Локальный режим оперблока: работа ведется на этом ПК. "
            "Завершённые случаи будут перенесены после восстановления связи."
        )
        self._emergency_banner.setObjectName("EmergencyModeBanner")
        self._emergency_banner.setWordWrap(True)
        self._emergency_banner.setStyleSheet(
            """
            QLabel#EmergencyModeBanner {
                background-color: #fff3cd;
                color: #5f4300;
                border-bottom: 1px solid #e0b849;
                padding: 8px 14px;
                font-weight: 700;
            }
            """
        )
        self.main_layout.insertWidget(1, self._emergency_banner)

    def _show_operblock_migration_dialog(self):
        from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout

        dialog = QDialog(self)
        dialog.setWindowTitle("Перенос данных оперблока")
        dialog.setModal(True)
        dialog.setWindowFlags(
            (dialog.windowFlags() | Qt.CustomizeWindowHint | Qt.WindowTitleHint)
            & ~Qt.WindowCloseButtonHint
        )
        layout = QVBoxLayout(dialog)
        label = QLabel("Не выключайте ПК. Идёт перенос данных оперблока.")
        label.setWordWrap(True)
        label.setMinimumWidth(360)
        layout.addWidget(label)
        dialog.show()
        QApplication.processEvents()
        return dialog

    def _close_operblock_migration_dialog(self, dialog) -> None:
        if dialog is None:
            return
        try:
            dialog.close()
            QApplication.processEvents()
        except Exception:
            pass

    def _maybe_migrate_operblock_offline_after_release(self) -> None:
        runtime_context = getattr(getattr(self.container, "db_manager", None), "runtime_context", None)
        if getattr(runtime_context, "mode", "") != "opblock_offline":
            return
        try:
            from rem_card.app.operblock_offline_migration import run_pending_operblock_offline_migration
            from rem_card.app.operblock_offline_store import (
                has_active_local_operblock_case,
                pending_completed_local_cases_count,
            )

            if has_active_local_operblock_case() or pending_completed_local_cases_count() <= 0:
                return
            from rem_card.app.db_runtime_context import build_network_runtime_context
            from rem_card.app.operblock_schema import ensure_operblock_schema
            from rem_card.data.dao.db_manager import DatabaseManager
        except Exception as exc:
            logger.warning("Operblock offline post-release migration precheck failed: %s", exc, exc_info=True)
            return

        dialog = None
        network_manager = None
        try:
            dialog = self._show_operblock_migration_dialog()
            network_context = build_network_runtime_context()
            network_manager = DatabaseManager(
                network_context.medical_db_path,
                network_context.medical_db_path,
                runtime_context=network_context,
            )
            ensure_operblock_schema(network_manager)
            result = run_pending_operblock_offline_migration(network_manager)
            if result.ok:
                from rem_card.app.operblock_offline_store import cleanup_verified_operblock_offline_session

                cleanup_verified_operblock_offline_session(network_manager)
        except Exception as exc:
            logger.info("Operblock offline post-release migration skipped: %s", exc)
            return
        finally:
            if network_manager is not None:
                try:
                    network_manager.close(timeout_sec=1.0)
                except Exception:
                    pass
            self._close_operblock_migration_dialog(dialog)

        if not result.ok:
            from rem_card.ui.shared.custom_message_box import CustomMessageBox

            CustomMessageBox.warning(
                self,
                "Перенос данных оперблока",
                result.user_message or "Перенос не выполнен. Локальные данные сохранены. Повторите позже.",
            )

    def _prepare_runtime_outage_emergency_session(self, marker_payload: dict) -> bool:
        try:
            from rem_card.app.emergency_startup import prepare_emergency_startup, start_or_resume_emergency_session
            from rem_card.ui.shared.custom_message_box import CustomMessageBox

            decision = prepare_emergency_startup("nurse")
            if not decision.allowed:
                CustomMessageBox.warning(None, "Аварийный режим недоступен", decision.user_message)
                return False
            if decision.active_session_metadata is None:
                if not self._confirm_emergency_password_for_transition(str(decision.password_settings_db_path or "")):
                    return False
            start_or_resume_emergency_session(decision, startup_request=marker_payload)
            return True
        except Exception as exc:
            logger.warning("Failed to prepare runtime outage emergency session: %s", exc, exc_info=True)
            try:
                from rem_card.ui.shared.custom_message_box import CustomMessageBox

                CustomMessageBox.warning(
                    None,
                    "Аварийный режим недоступен",
                    f"Не удалось подготовить аварийный режим:\n{exc}",
                )
            except Exception:
                pass
            return False

    def _confirm_emergency_password_for_transition(self, settings_db_path: str) -> bool:
        if os.environ.get("REMCARD_EMERGENCY_PASSWORD_AUTO_ACCEPT") == "1":
            return True

        from rem_card.app.emergency_password import verify_emergency_password_for_offline_startup
        from rem_card.ui.shared.emergency_dialogs import EmergencyPasswordDialog

        return EmergencyPasswordDialog.verify(
            self,
            "Аварийный пароль",
            "Для перехода в аварийный режим медсестра должна ввести аварийный пароль.",
            lambda value: verify_emergency_password_for_offline_startup(
                value,
                settings_db_path=settings_db_path,
            ),
            confirm_text="Перейти в аварийный режим",
            cancel_text="Закрыть RemCard",
            error_text="Пароль неверный. Аварийный режим не будет открыт без подтверждения.",
        )

    @Slot(dict)
    def _handle_restore_probe_status(self, payload: dict):
        if self._is_closing or not self._is_emergency_runtime():
            return
        payload = dict(payload or {})
        status = str(payload.get("status") or "")
        self._update_restore_probe_status_label(status)
        if self._restore_probe_dialog_active:
            return
        if time.monotonic() < float(self._restore_probe_notice_deferred_until or 0.0):
            return
        if status == "merge_ready_mode_a" and payload.get("merge_ready"):
            self._show_restore_probe_merge_ready_dialog(payload)
        elif status == "remote_changed_conflict_pending" and payload.get("network_stable"):
            self._show_restore_probe_merge_ready_dialog(payload)

    def _update_restore_probe_status_label(self, status: str):
        label = getattr(self, "_restore_probe_status_label", None)
        if label is None:
            return
        mapping = {
            "running": "Сетевая база проверяется",
            "network_baza_unavailable": "Ожидание восстановления сетевой базы",
            "network_medical_db_missing": "Ожидание восстановления сетевой базы",
            "network_settings_db_missing": "Ожидание восстановления сетевой базы",
            "network_medical_db_unavailable": "Ожидание восстановления сетевой базы",
            "network_settings_db_unavailable": "Ожидание восстановления сетевой базы",
            "session_lock_active": "Сеть доступна, ожидается закрытие других окон RemCard",
            "db_lock_active": "Сеть доступна, база занята проверкой",
            "emergency_merge_lock_active": "Сеть доступна, выполняется аварийное объединение",
            "probe_file_unavailable": "Сеть доступна, проверяется надежность доступа",
            "merge_ready_mode_a": "Сеть восстановлена, требуется закрытие для объединения",
            "remote_changed_conflict_pending": "Сеть восстановлена, требуется решение по аварийному режиму",
        }
        if status.startswith("round_success_"):
            text = "Сеть восстановлена, проверяется надежность доступа"
        elif status in {"disabled", "shutdown", "stopped"}:
            text = "Ожидание восстановления сетевой базы"
        else:
            text = mapping.get(status, "Ожидание восстановления сетевой базы")
        label.setText(text)

    def _show_restore_probe_merge_ready_dialog(self, payload: dict | None = None):
        from rem_card.app.emergency_restore_probe import MERGE_READY_MODE_A_MESSAGE, REMOTE_CHANGED_CONFLICT_MESSAGE
        from rem_card.ui.shared.emergency_dialogs import EmergencyActionDialog

        payload = dict(payload or {})
        status = str(payload.get("status") or "")
        message = MERGE_READY_MODE_A_MESSAGE
        if status == "remote_changed_conflict_pending":
            message = f"{REMOTE_CHANGED_CONFLICT_MESSAGE}\n\nВыберите дальнейшее действие."

        self._restore_probe_dialog_active = True
        try:
            result = EmergencyActionDialog.ask(
                self,
                "Сетевая база восстановлена",
                message,
                [
                    ("Да, объединить", 1),
                    ("Нет", 0),
                    ("Без объединения", 2),
                ],
                default_code=0,
            )
        finally:
            self._restore_probe_dialog_active = False
        if int(result or 0) == 1:
            self._close_for_emergency_merge()
            return
        if int(result or 0) == 2:
            self._close_for_emergency_discard()
            return
        self._restore_probe_notice_deferred_until = time.monotonic() + 60.0

    def _show_restore_probe_conflict_warning(self):
        if self._restore_probe_conflict_notified:
            return
        from rem_card.app.emergency_restore_probe import REMOTE_CHANGED_CONFLICT_MESSAGE
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        self._restore_probe_dialog_active = True
        try:
            CustomMessageBox.warning(
                self,
                "Сетевая база изменилась",
                REMOTE_CHANGED_CONFLICT_MESSAGE,
            )
        finally:
            self._restore_probe_dialog_active = False
            self._restore_probe_conflict_notified = True
            self._restore_probe_notice_deferred_until = time.monotonic() + 300.0

    def _close_for_emergency_merge(self):
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        if not self._confirm_emergency_password_for_exit(
            "Подтверждение объединения",
            (
                "Для выхода из аварийного режима и объединения с сетевой базой "
                "введите аварийный пароль."
            ),
        ):
            self._restore_probe_notice_deferred_until = time.monotonic() + 60.0
            return

        scheduler = getattr(self.container, "emergency_restore_probe_scheduler", None)
        if scheduler is None:
            CustomMessageBox.warning(self, "Аварийный режим", "Не удалось подготовить закрытие для объединения.")
            return
        try:
            scheduler.mark_merge_ready()
        except Exception as exc:
            logger.warning("Failed to mark emergency merge-ready state: %s", exc, exc_info=True)
            CustomMessageBox.warning(self, "Аварийный режим", f"Не удалось подготовить объединение:\n{exc}")
            return
        self.close()

    def _close_for_emergency_discard(self):
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        if not self._confirm_emergency_password_for_exit(
            "Выход без объединения",
            (
                "Аварийные изменения не будут перенесены в сетевую базу.\n\n"
                "Локальная аварийная сессия будет сохранена в архиве, "
                "после закрытия RemCard следующий запуск перейдет на обычную сетевую базу.\n\n"
                "Введите аварийный пароль, чтобы подтвердить выход без объединения."
            ),
        ):
            self._restore_probe_notice_deferred_until = time.monotonic() + 60.0
            return

        runtime_context = getattr(self.container, "runtime_context", None)
        session_id = str(getattr(runtime_context, "emergency_session_id", "") or "").strip()
        store = self._emergency_store_for_runtime()
        if not session_id or store is None:
            CustomMessageBox.warning(self, "Аварийный режим", "Не удалось определить активную аварийную сессию.")
            return
        try:
            store.mark_session_discarded(
                session_id,
                reason="user_requested_without_merge",
                requested_by_role=self._current_role_key(),
            )
            self._pending_emergency_discard = {
                "store": store,
                "session_id": session_id,
            }
        except Exception as exc:
            logger.warning("Failed to discard emergency session: %s", exc, exc_info=True)
            CustomMessageBox.warning(self, "Аварийный режим", f"Не удалось выйти без объединения:\n{exc}")
            return

        CustomMessageBox.information(
            self,
            "Аварийный режим",
            "Аварийная сессия будет сохранена в архиве без объединения. RemCard будет закрыта.",
        )
        self.close()

    def finalize_pending_emergency_discard(self) -> bool:
        pending = self._pending_emergency_discard
        if not pending:
            return True
        store = pending.get("store")
        session_id = str(pending.get("session_id") or "")
        if store is None or not session_id:
            return False
        try:
            store.archive_discarded_session(session_id)
            self._pending_emergency_discard = None
            return True
        except Exception as exc:
            logger.warning("Failed to archive discarded emergency session: %s", exc, exc_info=True)
            return False

    def _confirm_emergency_password_for_exit(self, title: str, message: str) -> bool:
        if os.environ.get("REMCARD_EMERGENCY_PASSWORD_AUTO_ACCEPT") == "1":
            return True

        from rem_card.app.emergency_password import verify_emergency_password
        from rem_card.ui.shared.emergency_dialogs import EmergencyPasswordDialog

        runtime_context = getattr(self.container, "runtime_context", None)
        settings_db_path = str(getattr(runtime_context, "settings_db_path", "") or "")

        def _verify(password: str) -> bool:
            if settings_db_path:
                return verify_emergency_password(password, settings_db_path=settings_db_path, readonly=True)
            return verify_emergency_password(password, runtime_context=runtime_context, readonly=True)

        return EmergencyPasswordDialog.verify(
            self,
            title,
            message,
            _verify,
            confirm_text="Подтвердить",
            cancel_text="Вернуться в аварийный режим",
        )

    def _emergency_store_for_runtime(self):
        scheduler = getattr(self.container, "emergency_restore_probe_scheduler", None)
        probe = getattr(scheduler, "probe", None)
        store = getattr(probe, "store", None)
        if store is not None:
            return store
        runtime_context = getattr(self.container, "runtime_context", None)
        root = self._emergency_root_from_runtime_context(runtime_context)
        if not root:
            return None
        from rem_card.app.emergency_store import EmergencyLocalStore

        return EmergencyLocalStore(root=root)

    @staticmethod
    def _emergency_root_from_runtime_context(runtime_context) -> str:
        baza_dir = str(getattr(runtime_context, "baza_dir", "") or "")
        if not baza_dir:
            return ""
        active_parent = os.path.dirname(os.path.abspath(os.path.normpath(baza_dir)))
        if os.path.basename(active_parent).lower() != "active":
            return ""
        return os.path.dirname(active_parent)

    def _start_event_loop_watchdog(self):
        if os.environ.get("REMCARD_UI_WATCHDOG_ENABLED", "1") == "0":
            return
        if self._event_loop_watchdog_timer is not None:
            return

        interval_ms = max(100, int(float(os.environ.get("REMCARD_UI_WATCHDOG_INTERVAL_MS", "250"))))
        self._event_loop_watchdog_threshold_ms = max(
            500.0,
            min(1000.0, float(os.environ.get("REMCARD_UI_WATCHDOG_THRESHOLD_MS", "750"))),
        )
        self._event_loop_watchdog_cooldown_sec = max(
            0.0,
            float(os.environ.get("REMCARD_UI_WATCHDOG_LOG_COOLDOWN_SEC", "5")),
        )
        self._event_loop_watchdog_last_ts = time.perf_counter()

        timer = QTimer(self)
        timer.setInterval(interval_ms)
        timer.timeout.connect(lambda expected_interval_ms=interval_ms: self._poll_event_loop_watchdog(expected_interval_ms))
        timer.start()
        self._event_loop_watchdog_timer = timer

    def _poll_event_loop_watchdog(self, expected_interval_ms: int):
        now = time.perf_counter()
        previous = float(self._event_loop_watchdog_last_ts or now)
        self._event_loop_watchdog_last_ts = now
        delta_ms = max(0.0, (now - previous) * 1000.0)
        pause_ms = max(0.0, delta_ms - float(expected_interval_ms or 0))
        if pause_ms < float(getattr(self, "_event_loop_watchdog_threshold_ms", 750.0)):
            return

        cooldown_sec = float(getattr(self, "_event_loop_watchdog_cooldown_sec", 5.0))
        if cooldown_sec > 0 and (now - float(self._event_loop_watchdog_last_log_ts or 0.0)) < cooldown_sec:
            return
        self._event_loop_watchdog_last_log_ts = now
        role = self._current_role_key()
        visible_context = self._current_visible_remcard_context()
        maintenance_snapshot = active_maintenance_snapshot(limit=4)
        foreground_snapshot = foreground_activity_snapshot(limit=4)
        sqlite_snapshot = active_sqlite_operation_snapshot(limit=4)
        opblock_snapshot = self._current_operblock_diagnostics_snapshot()
        active_maintenance = self._watchdog_snapshot_names(maintenance_snapshot, "active")
        foreground_active = self._watchdog_snapshot_names(foreground_snapshot, "active")
        foreground_recent = self._watchdog_snapshot_names(foreground_snapshot, "recent")
        active_sqlite = self._watchdog_snapshot_names(sqlite_snapshot, "active")
        sqlite_item = self._watchdog_first_snapshot_item(sqlite_snapshot, "active")
        maintenance_item = self._watchdog_first_snapshot_item(maintenance_snapshot, "active")
        shadow_snapshot = {}
        current_widget = self.stack.currentWidget() if hasattr(self, "stack") else None
        data_service = getattr(getattr(current_widget, "remcard_service", None), "data_service", None)
        shadow_provider = getattr(data_service, "get_opblock_shadow_mirror_snapshot", None)
        if callable(shadow_provider):
            try:
                shadow_snapshot = dict(shadow_provider() or {})
            except Exception:
                shadow_snapshot = {}
        logger.warning(
            "[UIWatchdog] event_loop_pause_ms=%.1f threshold_ms=%.1f role=%s interval_ms=%s "
            "tab=%s admission_id=%s operation_case_id=%s active_maintenance=%s foreground_active=%s "
            "foreground_recent=%s active_opblock_action=%s active_sqlite_operation=%s "
            "lock_holder=%s/%s/%s shadow_mirror_active=%s ui_pending_action=%s",
            pause_ms,
            float(getattr(self, "_event_loop_watchdog_threshold_ms", 750.0)),
            role,
            int(expected_interval_ms or 0),
            visible_context.get("tab_name", ""),
            visible_context.get("admission_id"),
            visible_context.get("operation_case_id") or opblock_snapshot.get("current_operation_case_id"),
            active_maintenance,
            foreground_active,
            foreground_recent,
            opblock_snapshot.get("active_opblock_action", ""),
            active_sqlite,
            sqlite_item.get("lock_holder_pid"),
            sqlite_item.get("lock_holder_host", ""),
            sqlite_item.get("lock_holder_source", ""),
            int(bool(shadow_snapshot.get("active"))),
            opblock_snapshot.get("ui_pending_action", ""),
        )
        record_metric(
            "event_loop_pause_ms",
            round(pause_ms, 3),
            threshold_ms=round(float(getattr(self, "_event_loop_watchdog_threshold_ms", 750.0)), 3),
            interval_ms=int(expected_interval_ms or 0),
            role=role,
            tab_name=visible_context.get("tab_name", ""),
            admission_id=visible_context.get("admission_id"),
            current_operation_case_id=opblock_snapshot.get("current_operation_case_id"),
            current_admission_id=opblock_snapshot.get("current_admission_id"),
            current_table_code=opblock_snapshot.get("current_table_code", ""),
            active_maintenance_count=int(maintenance_snapshot.get("active_count") or 0),
            active_maintenance=active_maintenance,
            foreground_active_count=int(foreground_snapshot.get("active_count") or 0),
            foreground_active=foreground_active,
            foreground_recent=foreground_recent,
            active_opblock_action=opblock_snapshot.get("active_opblock_action", ""),
            active_sqlite_operation=active_sqlite,
            active_foreground_lease="",
            last_user_action=opblock_snapshot.get("last_user_action", ""),
            idle_before_action_ms=opblock_snapshot.get("idle_before_action_ms"),
            lock_wait_operation=sqlite_item.get("operation_name", ""),
            lock_holder_pid=sqlite_item.get("lock_holder_pid"),
            lock_holder_host=sqlite_item.get("lock_holder_host", ""),
            lock_holder_source=sqlite_item.get("lock_holder_source", ""),
            shadow_mirror_active=int(bool(shadow_snapshot.get("active"))),
            ui_pending_action=opblock_snapshot.get("ui_pending_action", ""),
            ui_pending_since_ms=opblock_snapshot.get("ui_pending_since_ms"),
            source="refresh",
        )
        if active_maintenance and opblock_snapshot.get("active_opblock_action"):
            record_metric(
                "maintenance_overlap_observed",
                round(pause_ms, 3),
                active_maintenance_task=maintenance_item.get("task_type", active_maintenance),
                active_foreground_action=opblock_snapshot.get("active_opblock_action", ""),
                operation_case_id=opblock_snapshot.get("current_operation_case_id"),
                admission_id=opblock_snapshot.get("current_admission_id"),
                duration_ms=round(pause_ms, 3),
                source="ui_watchdog",
            )
        if opblock_snapshot.get("ui_pending_action"):
            record_metric(
                "ui_pending_state_observed",
                1,
                active_opblock_action=opblock_snapshot.get("ui_pending_action", ""),
                request_id=opblock_snapshot.get("active_opblock_request_id", ""),
                pending_since_ms=opblock_snapshot.get("ui_pending_since_ms"),
                widget_alive=opblock_snapshot.get("widget_alive"),
                case_still_current=opblock_snapshot.get("case_still_current"),
                source="ui_watchdog",
            )
        operblock_startup_metrics.record_event_loop_pause(
            pause_ms,
            threshold_ms=round(float(getattr(self, "_event_loop_watchdog_threshold_ms", 750.0)), 3),
            interval_ms=int(expected_interval_ms or 0),
            source="ui_watchdog",
        )

    def init_ui(self):
        self.welcome = WelcomeWidget()
        self.welcome.role_selected.connect(self.on_role_selected)
        self.stack.addWidget(self.welcome)
        self.stack.setCurrentWidget(self.welcome)

    @staticmethod
    def _role_display_name(role_key: str) -> str:
        return role_display_name(role_key)

    @staticmethod
    def _role_key_from_selection(role) -> str:
        role_key = normalize_role_key(role)
        if role_key in ROLE_KEYS:
            return role_key
        text_map = {
            "врач": ROLE_DOCTOR,
            "медсестра": ROLE_NURSE,
            "медицинская сестра": ROLE_NURSE,
            "оперблок": ROLE_OPERBLOCK,
            "операционный блок": ROLE_OPERBLOCK,
            "экстренная операционная": ROLE_OPERBLOCK_EMERGENCY,
            "плановая операционная": ROLE_OPERBLOCK_PLANNED,
        }
        return text_map.get(role_key, role_key)

    def _current_role_lock_path(self, role_key: str) -> str:
        runtime_context = getattr(getattr(self.container, "db_manager", None), "runtime_context", None)
        session_locks_dir = str(getattr(runtime_context, "session_locks_dir", "") or "").strip()
        if session_locks_dir:
            safe_role = str(role_key or "unknown").lower()
            return os.path.join(session_locks_dir, f"{safe_role}.lock")
        return get_role_lock_path(role_key)

    def _build_role_lock(self, role_key: str) -> RoleSessionLock:
        return RoleSessionLock(
            lock_path=self._current_role_lock_path(role_key),
            role=role_key,
            owner_id=self._role_lock_owner_id,
            stale_timeout_sec=60.0,
            heartbeat_sec=8.0,
            logger=logger,
        )

    def _acquire_role_lock(self, role_key: str) -> bool:
        if role_key not in ROLE_KEYS:
            return True
        if self._is_emergency_runtime():
            self._last_active_role_key = role_key
            return True

        if self._role_lock and self._role_lock_key == role_key:
            self._last_active_role_key = role_key
            return True

        new_lock = self._build_role_lock(role_key)
        if not new_lock.acquire():
            holder = new_lock.describe_holder()
            QMessageBox.warning(
                self,
                "Роль занята",
                f"Роль \"{self._role_display_name(role_key)}\" уже открыта на другом ПК.\n\n{holder}",
            )
            return False

        old_lock = self._role_lock
        self._role_lock = new_lock
        self._role_lock_key = role_key
        self._last_active_role_key = role_key

        if old_lock:
            try:
                old_lock.release()
            except Exception as exc:
                logger.warning("Failed to release previous role lock: %s", exc)
        return True

    def release_role_lock(self):
        if self._role_lock:
            try:
                self._role_lock.release()
            except Exception as exc:
                logger.warning("Failed to release role lock: %s", exc)
        self._role_lock = None
        self._role_lock_key = None

    def set_default_container(self, container, *, activate: bool = True) -> None:
        self._default_container = container
        if activate:
            self._activate_container(container)
        else:
            self.container = container

    def _activate_container(self, container) -> None:
        self.container = container
        runtime_context = getattr(getattr(container, "db_manager", None), "runtime_context", None)
        if runtime_context is not None:
            from rem_card.services.settings.settings_service import configure_settings_service

            configure_settings_service(
                runtime_context=runtime_context,
                readonly=bool(getattr(runtime_context, "settings_readonly", False)),
            ).ensure_ready()
        self._connect_runtime_outage_signal()
        self._connect_restore_probe_signal()

    def _ensure_default_container(self):
        if self._default_container is None:
            from rem_card.app.bootstrap import bootstrap

            self._default_container = bootstrap(role=None)
        self._activate_container(self._default_container)
        return self._default_container

    def _ensure_operblock_container(self):
        container = self._ensure_default_container()
        if container is None:
            return None
        try:
            from rem_card.app.operblock_schema import ensure_operblock_schema

            with operblock_startup_metrics.measure("ensure_operblock_schema_ms", source="main_window"):
                ensure_operblock_schema(container.db_manager)
        except Exception as exc:
            logger.exception("Failed to prepare operblock schema in current database: %s", exc)
            QMessageBox.critical(
                self,
                "Оперблок",
                f"Не удалось подготовить схему оперблока в текущей базе.\n\n{exc}",
            )
            return None
        return container

    def _ensure_role_container(self, role_key: str):
        if is_operblock_role(role_key):
            return self._ensure_operblock_container()
        return self._ensure_default_container()

    def iter_runtime_containers(self):
        containers = []
        seen = set()
        for container in (self._default_container, self._operblock_container, self.container):
            if container is None:
                continue
            marker = id(container)
            if marker in seen:
                continue
            seen.add(marker)
            containers.append(container)
        return containers

    def init_with_role(self, role):
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._load_role_ui)

    def _load_role_ui(self):
        """Ленивая догрузка интерфейса роли при старте."""
        from PySide6.QtCore import QTimer

        if self._initial_role_ui_ready:
            return

        if not self.container:
            QTimer.singleShot(50, self._load_role_ui)
            return

        if self._initial_role == 'doctor':
            QTimer.singleShot(10, self._activate_initial_doctor_role)

        elif self._initial_role == 'nurse':
            QTimer.singleShot(10, self._activate_initial_nurse_role)

        elif is_operblock_role(self._initial_role):
            QTimer.singleShot(10, self._activate_initial_operblock_role)

    def _activate_initial_doctor_role(self, *, start_refresh: bool = True):
        from PySide6.QtCore import QTimer
        from .doctor_view.doctor_main_widget import DoctorMainWidget

        if self._initial_role_ui_ready and self.doctor_main is not None:
            if start_refresh:
                QTimer.singleShot(0, self.start_initial_role_refresh)
            return

        if not self.container:
            QTimer.singleShot(50, self._activate_initial_doctor_role)
            return
        if self._ensure_role_container("doctor") is None:
            return
        if not self._acquire_role_lock("doctor"):
            self.close()
            return

        self.doctor_main = DoctorMainWidget(
            self.container.patient_service,
            self.container.remcard_service,
            parent=self.stack,
            operblock_service=self.container.operblock_service,
        )
        self.stack.addWidget(self.doctor_main)
        self.stack.setCurrentWidget(self.doctor_main)

        self.doctor_main.reset_to_beds()

        self._initial_role_ui_ready = True
        QTimer.singleShot(1500, lambda: _apply_role_icon("doctor"))
        self._schedule_maintenance()
        if start_refresh:
            QTimer.singleShot(0, self.start_initial_role_refresh)

    def _activate_initial_operblock_role(self, *, start_refresh: bool = True):
        role_activation_started = operblock_startup_metrics.timer_start()
        try:
            from PySide6.QtCore import QTimer

            widget_import_started = operblock_startup_metrics.timer_start()
            from .operblock_view.operblock_main_widget import OperBlockMainWidget
            operblock_startup_metrics.record_since(
                "operblock_widget_import_ms",
                widget_import_started,
                source="main_window",
            )
            role_key = self._initial_role if is_operblock_role(self._initial_role) else ROLE_OPERBLOCK

            if self._initial_role_ui_ready and self.operblock_main is not None:
                if start_refresh:
                    QTimer.singleShot(0, self.start_initial_role_refresh)
                return

            if not self.container:
                QTimer.singleShot(50, self._activate_initial_operblock_role)
                return
            if self._ensure_role_container(role_key) is None:
                if is_operblock_role(self._initial_role):
                    QTimer.singleShot(0, self.close)
                return
            if not self._acquire_role_lock(role_key):
                self.close()
                return

            widget_create_started = operblock_startup_metrics.timer_start()
            self.operblock_main = OperBlockMainWidget(
                self.container.patient_service,
                self.container.remcard_service,
                self.container.operblock_service,
                table_code=operblock_table_code_for_role(role_key),
                parent=self.stack,
            )
            self._operblock_widgets[role_key] = self.operblock_main
            self._active_operblock_role_key = role_key
            operblock_startup_metrics.record_since(
                "operblock_widget_create_ms",
                widget_create_started,
                source="main_window",
            )
            self.stack.addWidget(self.operblock_main)
            self.stack.setCurrentWidget(self.operblock_main)

            self._initial_role_ui_ready = True
            self._schedule_maintenance()
            if start_refresh:
                QTimer.singleShot(0, self.start_initial_role_refresh)
        finally:
            operblock_startup_metrics.record_since("role_activation_ms", role_activation_started, source="main_window")

    def _activate_initial_nurse_role(self, *, start_refresh: bool = True):
        from PySide6.QtCore import QTimer
        from .nurse_view.nurse_main_widget import NurseMainWidget

        if self._initial_role_ui_ready and self.nurse_main is not None:
            if start_refresh:
                QTimer.singleShot(0, self.start_initial_role_refresh)
            return

        if not self.container:
            QTimer.singleShot(50, self._activate_initial_nurse_role)
            return
        if self._ensure_role_container("nurse") is None:
            return
        if not self._acquire_role_lock("nurse"):
            self.close()
            return

        self.nurse_main = NurseMainWidget(self.container.patient_service, self.container.remcard_service, parent=self.stack)
        self.stack.addWidget(self.nurse_main)
        self.stack.setCurrentWidget(self.nurse_main)

        self.nurse_main.reset_to_beds()

        self._initial_role_ui_ready = True
        QTimer.singleShot(1500, lambda: _apply_role_icon("nurse"))
        self._schedule_maintenance()
        if start_refresh:
            QTimer.singleShot(0, self.start_initial_role_refresh)

    def prepare_initial_role_ui_for_startup(self) -> bool:
        """Синхронно строит стартовую роль до первого показа окна."""
        if self._initial_role_ui_ready:
            return True
        if not self.container or self._initial_role not in ROLE_KEYS:
            return False
        if self._initial_role == "doctor":
            self._activate_initial_doctor_role(start_refresh=False)
        elif self._initial_role == "nurse":
            self._activate_initial_nurse_role(start_refresh=False)
        elif is_operblock_role(self._initial_role):
            self._activate_initial_operblock_role(start_refresh=False)
        return bool(self._initial_role_ui_ready)

    def start_initial_role_refresh(self):
        if self._initial_role_auto_refresh_started:
            return
        role_widget = None
        if self._initial_role == "doctor":
            role_widget = self.doctor_main
        elif self._initial_role == "nurse":
            role_widget = self.nurse_main
        elif is_operblock_role(self._initial_role):
            role_widget = self.operblock_main
        if role_widget is None or not hasattr(role_widget, "start_auto_refresh"):
            return
        self._initial_role_auto_refresh_started = True
        role_widget.start_auto_refresh(wake_monitor=False)

    def wake_initial_role_monitor(self):
        role_widget = None
        if self._initial_role == "doctor":
            role_widget = self.doctor_main
        elif self._initial_role == "nurse":
            role_widget = self.nurse_main
        elif is_operblock_role(self._initial_role):
            role_widget = self.operblock_main
        service = getattr(role_widget, "remcard_service", None)
        data_service = getattr(service, "data_service", None)
        if data_service:
            data_service.request_immediate_refresh(force_emit=False)

    def on_role_selected(self, role):
        from PySide6.QtCore import QTimer

        role_key = self._role_key_from_selection(role)

        if role_key == ROLE_DOCTOR:
            if not self.container:
                QTimer.singleShot(100, lambda: self.on_role_selected(role))
                return
            if self._ensure_role_container(ROLE_DOCTOR) is None:
                return
            if not self._acquire_role_lock(ROLE_DOCTOR):
                return

            if self.doctor_main is None:
                from .doctor_view.doctor_main_widget import DoctorMainWidget
                self.doctor_main = DoctorMainWidget(
                    self.container.patient_service,
                    self.container.remcard_service,
                    parent=self.stack,
                    operblock_service=self.container.operblock_service,
                )
                self.stack.addWidget(self.doctor_main)
            
            self.stack.setCurrentWidget(self.doctor_main)
            self.doctor_main.reset_to_beds()
            
            QTimer.singleShot(1500, lambda: _apply_role_icon("doctor"))
            self._schedule_maintenance()
            if hasattr(self.doctor_main, 'start_auto_refresh'):
                QTimer.singleShot(0, self.doctor_main.start_auto_refresh)
            else:
                QTimer.singleShot(0, self.doctor_main.auto_refresh)

        elif role_key == ROLE_NURSE:
            if not self.container:
                QTimer.singleShot(100, lambda: self.on_role_selected(role))
                return
            if self._ensure_role_container(ROLE_NURSE) is None:
                return
            if not self._acquire_role_lock(ROLE_NURSE):
                return

            if self.nurse_main is None:
                from .nurse_view.nurse_main_widget import NurseMainWidget
                self.nurse_main = NurseMainWidget(self.container.patient_service, self.container.remcard_service, parent=self.stack)
                self.stack.addWidget(self.nurse_main)
            
            self.stack.setCurrentWidget(self.nurse_main)
            self.nurse_main.reset_to_beds()
            
            QTimer.singleShot(1500, lambda: _apply_role_icon("nurse"))
            self._schedule_maintenance()
            if hasattr(self.nurse_main, 'start_auto_refresh'):
                QTimer.singleShot(0, self.nurse_main.start_auto_refresh)
            else:
                QTimer.singleShot(0, self.nurse_main.auto_refresh)

        elif is_operblock_role(role_key):
            if not self.container:
                QTimer.singleShot(100, lambda: self.on_role_selected(role))
                return
            if self._ensure_role_container(role_key) is None:
                return
            if not self._acquire_role_lock(role_key):
                return

            self.operblock_main = self._operblock_widgets.get(role_key)
            if self.operblock_main is None:
                from .operblock_view.operblock_main_widget import OperBlockMainWidget

                self.operblock_main = OperBlockMainWidget(
                    self.container.patient_service,
                    self.container.remcard_service,
                    self.container.operblock_service,
                    table_code=operblock_table_code_for_role(role_key),
                    parent=self.stack,
                )
                self._operblock_widgets[role_key] = self.operblock_main
                self.stack.addWidget(self.operblock_main)
            self._active_operblock_role_key = role_key
            if hasattr(self.operblock_main, "set_role_launcher_mode"):
                self.operblock_main.set_role_launcher_mode(self.welcome is not None)

            self.stack.setCurrentWidget(self.operblock_main)
            self._schedule_maintenance()
            if hasattr(self.operblock_main, 'start_auto_refresh'):
                QTimer.singleShot(0, self.operblock_main.start_auto_refresh)

    def _schedule_maintenance(self):
        from PySide6.QtCore import QTimer

        if self._maintenance_timer is None:
            interval_hours = max(1.0, float(os.environ.get("REMCARD_MAINTENANCE_INTERVAL_HOURS", "6")))
            timer = QTimer(self)
            timer.setInterval(int(interval_hours * 3600 * 1000))
            timer.timeout.connect(self._run_maintenance_async)
            timer.start()
            self._maintenance_timer = timer

        if self._maintenance_scheduled:
            return

        self._maintenance_scheduled = True
        # Не запускаем тяжелое обслуживание БД сразу после старта UI:
        # даем окну прогреться и снизить риск лагов на первых действиях.
        start_delay_sec = max(
            5.0,
            float(os.environ.get("REMCARD_MAINTENANCE_START_DELAY_SEC", "25")),
        )
        QTimer.singleShot(int(start_delay_sec * 1000), self._run_maintenance_async)

    def _defer_ui_maintenance_if_foreground(self) -> bool:
        if getattr(self, "_is_closing", False):
            return True
        idle_window_sec = max(
            30.0,
            float(os.environ.get("REMCARD_UI_MAINTENANCE_FOREGROUND_IDLE_SEC", "120")),
        )
        retry_sec = max(
            10.0,
            float(os.environ.get("REMCARD_UI_MAINTENANCE_DEFER_RETRY_SEC", "60")),
        )
        should_defer, reason, age_sec = should_defer_background_io(
            idle_window_sec=idle_window_sec,
            names=None,
        )
        if not should_defer:
            return False
        logger.info(
            "UI maintenance deferred: foreground activity is active/recent (reason=%s age_sec=%s retry_sec=%.1f)",
            reason,
            None if age_sec is None else round(age_sec, 3),
            retry_sec,
        )
        record_metric(
            "maintenance_task_deferred",
            1,
            task_type="daily_backup_cleanup",
            source="main_window",
            reason=f"foreground_activity:{reason}",
            age_sec=None if age_sec is None else round(age_sec, 3),
            idle_window_sec=round(idle_window_sec, 3),
            retry_sec=round(retry_sec, 3),
        )
        QTimer.singleShot(int(retry_sec * 1000), self._run_maintenance_async)
        return True

    def _run_maintenance_async(self):
        if self._defer_ui_maintenance_if_foreground():
            return
        token = self.show_loading_indicator(
            "Обслуживание базы: проверка и резервное копирование...",
            key="maintenance",
            auto_hide_ms=300000,
        )

        def _worker():
            try:
                from rem_card.app.backup_and_cleanup import perform_daily_backup_and_cleanup

                with maintenance_task("daily_backup_cleanup", source="main_window"):
                    perform_daily_backup_and_cleanup()
            except Exception:
                pass
            finally:
                try:
                    self.maintenance_finished.emit(token)
                except Exception:
                    pass

        thread = threading.Thread(target=_worker, name="RemCardDailyMaintenance", daemon=True)
        thread.start()

    @Slot(str)
    def _on_maintenance_finished(self, token: str):
        self.hide_loading_indicator(token or "maintenance", delay_ms=350)

    def show_roles(self):
        self.release_role_lock()
        if self._default_container is not None:
            self._activate_container(self._default_container)
        if self.admin_main:
            self.stack.setCurrentWidget(self.welcome)
        else:
            self.stack.setCurrentIndex(0)

    def _ensure_admin_main(self):
        if self.admin_main is None:
            from .admin_view.admin_main_widget import AdminMainWidget
            self.admin_main = AdminMainWidget(service=self.container.remcard_service, role="admin")
            self.admin_main.btn_back_to_roles.clicked.connect(self.show_roles)
            self.stack.addWidget(self.admin_main)
        return self.admin_main

    def on_settings_clicked(self):
        admin = self._ensure_admin_main()
        self.stack.setCurrentWidget(admin)

    def changeEvent(self, event):
        if event.type() == QEvent.WindowStateChange:
            pass
        super().changeEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            edge = self._get_resize_edge(event.pos())
            if edge:
                self._is_resizing = True
                self._resize_edge = edge
                event.accept()
                return

        if event.button() == Qt.BackButton or event.button() == Qt.XButton1:
            current_widget = self.stack.currentWidget()
            if current_widget == self.doctor_main:
                if hasattr(self.doctor_main, 'remcard_widget') and hasattr(self.doctor_main.remcard_widget, 'on_back_clicked'):
                    self.doctor_main.remcard_widget.on_back_clicked()
            elif current_widget == self.nurse_main:
                if hasattr(self.nurse_main, 'on_back_clicked'):
                    self.nurse_main.on_back_clicked()
            elif current_widget == self.operblock_main or current_widget in (self._operblock_widgets or {}).values():
                if hasattr(current_widget, 'on_back_clicked'):
                    current_widget.on_back_clicked()
            elif current_widget == self.admin_main:
                self.show_roles()
        
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._is_resizing:
            self._handle_resize(event.globalPosition().toPoint())
            event.accept()
        else:
            edge = self._get_resize_edge(event.pos())
            if edge == "left" or edge == "right":
                self.setCursor(Qt.SizeHorCursor)
            elif edge == "top" or edge == "bottom":
                self.setCursor(Qt.SizeVerCursor)
            elif edge in ["topleft", "bottomright"]:
                self.setCursor(Qt.SizeFDiagCursor)
            elif edge in ["topright", "bottomleft"]:
                self.setCursor(Qt.SizeBDiagCursor)
            else:
                self.setCursor(Qt.ArrowCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._is_resizing = False
        self._resize_edge = None
        self.setCursor(Qt.ArrowCursor)
        super().mouseReleaseEvent(event)

    def event(self, event):
        if event.type() == QEvent.WindowActivate:
            self._schedule_focus_refresh()
        return super().event(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_loading_overlay()

    def _schedule_focus_refresh(self):
        if self._is_closing or self._focus_refresh_pending:
            return
        self._focus_refresh_pending = True
        self._focus_refresh_timer.start()

    @staticmethod
    def _is_qobject_alive(obj) -> bool:
        if obj is None:
            return False
        try:
            import shiboken6  # type: ignore
            is_valid = getattr(shiboken6, "isValid", None)
            return bool(is_valid(obj)) if callable(is_valid) else True
        except Exception:
            return True

    def _run_focus_refresh(self):
        self._focus_refresh_pending = False
        if self._is_closing or self._focus_refresh_in_progress:
            return
        if not self.isVisible() or not self.isActiveWindow():
            return

        now = time.monotonic()
        if (now - self._last_focus_refresh_ts) < 0.5:
            return
        self._last_focus_refresh_ts = now

        self._focus_refresh_in_progress = True
        try:
            self._trigger_refresh_on_focus()
        finally:
            self._focus_refresh_in_progress = False

    def _trigger_refresh_on_focus(self):
        if self._is_closing or not self.container:
            return
        data_service = getattr(self.container, "data_service", None)
        if data_service:
            try:
                data_service.request_immediate_refresh(force_emit=True)
            except Exception:
                logger.exception("Focus refresh monitor wake failed")

    def _get_resize_edge(self, pos):
        if self.isMaximized():
            return None
            
        w, h = self.width(), self.height()
        margin = self._resize_margin
        
        x, y = pos.x(), pos.y()
        
        left = x < margin
        right = x > w - margin
        top = y < margin
        bottom = y > h - margin
        
        if left and top: return "topleft"
        if right and top: return "topright"
        if left and bottom: return "bottomleft"
        if right and bottom: return "bottomright"
        if left: return "left"
        if right: return "right"
        if top: return "top"
        if bottom: return "bottom"
        
        return None

    def _handle_resize(self, global_pos):
        geom = self.geometry()
        edge = self._resize_edge
        
        if "left" in edge:
            new_width = geom.right() - global_pos.x()
            if new_width > self.minimumWidth():
                geom.setLeft(global_pos.x())
        elif "right" in edge:
            geom.setRight(global_pos.x())
            
        if "top" in edge:
            new_height = geom.bottom() - global_pos.y()
            if new_height > self.minimumHeight():
                geom.setTop(global_pos.y())
        elif "bottom" in edge:
            geom.setBottom(global_pos.y())
            
        self.setGeometry(geom)

    def _apply_restored_maximization(self):
        if hasattr(self, 'title_bar'):
            if not self.property("normalGeometry"):
                self.setProperty("normalGeometry", QPoint(100, 100))
            
            self._is_custom_maximized = False
            self.title_bar.on_maximize_restore()

    def closeEvent(self, event):
        self._is_closing = True
        exit_role = self._current_role_key()
        if exit_role not in ("doctor", "nurse"):
            exit_role = self._last_active_role_key or exit_role
        self._exit_role_key = exit_role
        try:
            logger.info("MainWindow closeEvent started")
            for container in self.iter_runtime_containers():
                if (
                    container
                    and hasattr(container, "data_service")
                    and container.data_service
                    and hasattr(container.data_service, "set_shutting_down")
                ):
                    container.data_service.set_shutting_down()
            self.release_role_lock()

            if hasattr(self, 'doctor_main'):
                if hasattr(self.doctor_main, 'shutdown'):
                    self.doctor_main.shutdown()
                elif hasattr(self.doctor_main, 'stop_auto_refresh'):
                    self.doctor_main.stop_auto_refresh()
            if hasattr(self, 'nurse_main'):
                if hasattr(self.nurse_main, 'shutdown'):
                    self.nurse_main.shutdown()
                elif hasattr(self.nurse_main, 'stop_auto_refresh'):
                    self.nurse_main.stop_auto_refresh()
            operblock_widgets = list(dict.fromkeys((self._operblock_widgets or {}).values()))
            if self.operblock_main is not None and self.operblock_main not in operblock_widgets:
                operblock_widgets.append(self.operblock_main)
            for operblock_widget in operblock_widgets:
                if hasattr(operblock_widget, 'shutdown'):
                    operblock_widget.shutdown()
                elif hasattr(operblock_widget, 'stop_auto_refresh'):
                    operblock_widget.stop_auto_refresh()

            is_max = getattr(self, '_is_custom_maximized', False) or self.isMaximized()
            self.settings.setValue("is_maximized", is_max)
            
            norm_geom = self.property("normalGeometry")
            if norm_geom:
                self.settings.setValue("normalGeometry", norm_geom)
            
            self.settings.setValue("geometry", self.saveGeometry())
            self.settings.setValue("windowState", self.saveState())
            self.settings.sync()
            
            from PySide6.QtCore import QThread, QTimer
            from rem_card.ui.shared.async_call import AsyncCallThread

            if not AsyncCallThread.shutdown_all(timeout_ms=2500):
                logger.warning("Some async UI workers did not stop before application shutdown")

            for child in self.findChildren(QThread):
                if child.isRunning():
                    child.quit()
                    child.wait(500)
                    if child.isRunning():
                        child.terminate()
            
            for child in self.findChildren(QTimer):
                child.stop()

            logger.info("MainWindow UI shutdown finished; data resources will close after Qt loop exits")

        except Exception as e:
            print(f"Error during closeEvent: {e}")
        finally:
            self.release_role_lock()
            QApplication.quit()
            super().closeEvent(event)
