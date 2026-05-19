from PySide6.QtWidgets import QMainWindow, QStackedWidget, QApplication, QVBoxLayout, QFrame, QMessageBox
from PySide6.QtCore import QSettings, Qt, QPoint, QEvent, QTimer

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
from rem_card.app.paths import get_icon_dir, get_role_lock_path
from rem_card.app.role_session_lock import RoleSessionLock
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
    def __init__(self, container, role=None, role_session_lock=None, role_key=None):
        super().__init__()
        self.container = container
        self._initial_role = role
        self._role_lock = role_session_lock
        self._role_lock_key = role_key if role_session_lock else None
        self._role_lock_owner_id = f"{socket.gethostname()}:{os.getpid()}:rem_card_ui"
        self._initial_role_ui_ready = False
        self._initial_role_auto_refresh_started = False
        
        self.setup_base_ui()

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
        self.admin_main = None
        self.welcome = None
        self._maintenance_scheduled = False
        self._maintenance_timer = None
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
        self._start_event_loop_watchdog()

    def _current_role_key(self) -> str:
        if self.stack.currentWidget() == self.doctor_main:
            return "doctor"
        if self.stack.currentWidget() == self.nurse_main:
            return "nurse"
        if self.stack.currentWidget() == self.admin_main:
            return "admin"
        return str(self._role_lock_key or self._initial_role or "unknown")

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
        logger.warning(
            "[UIWatchdog] event_loop_pause_ms=%.1f threshold_ms=%.1f role=%s interval_ms=%s",
            pause_ms,
            float(getattr(self, "_event_loop_watchdog_threshold_ms", 750.0)),
            role,
            int(expected_interval_ms or 0),
        )
        record_metric(
            "event_loop_pause_ms",
            round(pause_ms, 3),
            threshold_ms=round(float(getattr(self, "_event_loop_watchdog_threshold_ms", 750.0)), 3),
            interval_ms=int(expected_interval_ms or 0),
            role=role,
            source="refresh",
        )

    def init_ui(self):
        self.welcome = WelcomeWidget()
        self.welcome.role_selected.connect(self.on_role_selected)
        self.stack.addWidget(self.welcome)
        self.stack.setCurrentWidget(self.welcome)

    @staticmethod
    def _role_display_name(role_key: str) -> str:
        if role_key == "doctor":
            return "Врач"
        if role_key == "nurse":
            return "Медсестра"
        return str(role_key)

    def _build_role_lock(self, role_key: str) -> RoleSessionLock:
        return RoleSessionLock(
            lock_path=get_role_lock_path(role_key),
            role=role_key,
            owner_id=self._role_lock_owner_id,
            stale_timeout_sec=60.0,
            heartbeat_sec=8.0,
            logger=logger,
        )

    def _acquire_role_lock(self, role_key: str) -> bool:
        if role_key not in ("doctor", "nurse"):
            return True

        if self._role_lock and self._role_lock_key == role_key:
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
        if not self._acquire_role_lock("doctor"):
            self.close()
            return

        self.doctor_main = DoctorMainWidget(self.container.patient_service, self.container.remcard_service, parent=self.stack)
        self.stack.addWidget(self.doctor_main)
        self.stack.setCurrentWidget(self.doctor_main)

        self.doctor_main.remcard_widget.admission_id = None
        self.doctor_main.remcard_widget.layout_manager.set_patient_selection_mode("beds")

        self._initial_role_ui_ready = True
        QTimer.singleShot(1500, lambda: _apply_role_icon("doctor"))
        self._schedule_maintenance()
        if start_refresh:
            QTimer.singleShot(0, self.start_initial_role_refresh)

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
        if not self._acquire_role_lock("nurse"):
            self.close()
            return

        self.nurse_main = NurseMainWidget(self.container.patient_service, self.container.remcard_service, parent=self.stack)
        self.stack.addWidget(self.nurse_main)
        self.stack.setCurrentWidget(self.nurse_main)

        if hasattr(self.nurse_main, 'layout_manager'):
            self.nurse_main.layout_manager.current_admission_id = None
            self.nurse_main.layout_manager.set_patient_selection_mode("beds")

        self._initial_role_ui_ready = True
        QTimer.singleShot(1500, lambda: _apply_role_icon("nurse"))
        self._schedule_maintenance()
        if start_refresh:
            QTimer.singleShot(0, self.start_initial_role_refresh)

    def prepare_initial_role_ui_for_startup(self) -> bool:
        """Синхронно строит стартовую роль до первого показа окна."""
        if self._initial_role_ui_ready:
            return True
        if not self.container or self._initial_role not in ("doctor", "nurse"):
            return False
        if self._initial_role == "doctor":
            self._activate_initial_doctor_role(start_refresh=False)
        elif self._initial_role == "nurse":
            self._activate_initial_nurse_role(start_refresh=False)
        return bool(self._initial_role_ui_ready)

    def start_initial_role_refresh(self):
        if self._initial_role_auto_refresh_started:
            return
        role_widget = None
        if self._initial_role == "doctor":
            role_widget = self.doctor_main
        elif self._initial_role == "nurse":
            role_widget = self.nurse_main
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
        service = getattr(role_widget, "remcard_service", None)
        data_service = getattr(service, "data_service", None)
        if data_service:
            data_service.request_immediate_refresh(force_emit=False)

    def on_role_selected(self, role):
        from PySide6.QtCore import QTimer
        
        if role == "Врач":
            if not self.container:
                QTimer.singleShot(100, lambda: self.on_role_selected(role))
                return
            if not self._acquire_role_lock("doctor"):
                return

            if self.doctor_main is None:
                from .doctor_view.doctor_main_widget import DoctorMainWidget
                self.doctor_main = DoctorMainWidget(self.container.patient_service, self.container.remcard_service, parent=self.stack)
                self.stack.addWidget(self.doctor_main)
            
            self.stack.setCurrentWidget(self.doctor_main)
            self.doctor_main.remcard_widget.admission_id = None
            self.doctor_main.remcard_widget.layout_manager.set_patient_selection_mode("beds")
            
            QTimer.singleShot(1500, lambda: _apply_role_icon("doctor"))
            self._schedule_maintenance()
            if hasattr(self.doctor_main, 'start_auto_refresh'):
                QTimer.singleShot(0, self.doctor_main.start_auto_refresh)
            else:
                QTimer.singleShot(0, self.doctor_main.auto_refresh)

        elif role == "Медсестра":
            if not self.container:
                QTimer.singleShot(100, lambda: self.on_role_selected(role))
                return
            if not self._acquire_role_lock("nurse"):
                return

            if self.nurse_main is None:
                from .nurse_view.nurse_main_widget import NurseMainWidget
                self.nurse_main = NurseMainWidget(self.container.patient_service, self.container.remcard_service, parent=self.stack)
                self.stack.addWidget(self.nurse_main)
            
            self.stack.setCurrentWidget(self.nurse_main)
            if hasattr(self.nurse_main, 'layout_manager'):
                self.nurse_main.layout_manager.current_admission_id = None
                self.nurse_main.layout_manager.set_patient_selection_mode("beds")
            
            QTimer.singleShot(1500, lambda: _apply_role_icon("nurse"))
            self._schedule_maintenance()
            if hasattr(self.nurse_main, 'start_auto_refresh'):
                QTimer.singleShot(0, self.nurse_main.start_auto_refresh)
            else:
                QTimer.singleShot(0, self.nurse_main.auto_refresh)

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

    def _run_maintenance_async(self):
        def _worker():
            try:
                from rem_card.app.backup_and_cleanup import perform_daily_backup_and_cleanup

                perform_daily_backup_and_cleanup()
            except Exception:
                pass

        thread = threading.Thread(target=_worker, name="RemCardDailyMaintenance", daemon=True)
        thread.start()

    def show_roles(self):
        self.release_role_lock()
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
        try:
            logger.info("MainWindow closeEvent started")
            if (
                self.container
                and hasattr(self.container, "data_service")
                and self.container.data_service
                and hasattr(self.container.data_service, "set_shutting_down")
            ):
                self.container.data_service.set_shutting_down()
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
