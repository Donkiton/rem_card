import os

from PySide6.QtWidgets import (QWidget, QHBoxLayout, QStackedWidget)
from PySide6.QtCore import Qt, QCoreApplication, QTimer

try:
    import shiboken6  # type: ignore
except Exception:
    shiboken6 = None

DOCTOR_BEDS_POLL_INTERVAL_MS = 7000
W1A_STARTUP_IDLE_DELAY_MS = max(0, int(os.environ.get("REMCARD_W1A_STARTUP_IDLE_DELAY_MS", "500")))
W1A_PANEL_REFRESH_ENTITIES = {
    "orders",
    "administrations",
    "lab_orders",
    "patients",
    "admissions",
    "beds",
    "patient_status_events",
}
W1_BEDS_REFRESH_ENTITIES = {
    "patients",
    "admissions",
    "beds",
    "operations",
    "vitals",
    "vital_settings",
    "patient_status_events",
    "fluids",
    "orders",
    "diet_plan",
    "oral_intake_events",
}
W1_REFRESH_ENTITIES = W1_BEDS_REFRESH_ENTITIES | W1A_PANEL_REFRESH_ENTITIES | {"diet_templates"}
W1_BEDS_REFRESH_SOURCE_PREFIXES = (
    "patient_bed",
    "archive_",
    "status_",
)


def _qt_is_valid(obj) -> bool:
    if obj is None:
        return False
    if shiboken6 is None:
        return True
    try:
        return bool(shiboken6.isValid(obj))
    except Exception:
        return True


def _app_is_closing() -> bool:
    app = QCoreApplication.instance()
    if app is None:
        return True
    try:
        return bool(QCoreApplication.closingDown())
    except Exception:
        return False


class DoctorMainWidget(QWidget):
    """Главный виджет врача. Теперь является оберткой над DoctorRemCardWidget."""
    def __init__(self, patient_service, remcard_service, parent=None, operblock_service=None):
        super().__init__(parent)
        self.patient_service = patient_service
        self.remcard_service = remcard_service
        self.operblock_service = operblock_service
        self._last_global_change_id = 0
        self._monitor_connected = False
        self._is_closing = False
        self._initial_beds_refresh_requested = False
        self._initial_w1a_refresh_requested = False
        
        self.init_ui()

    def start_auto_refresh(self, *, wake_monitor: bool = True):
        if self._is_closing or _app_is_closing():
            return
        data_service = self._get_data_service()
        if data_service and hasattr(data_service, "set_change_monitor_enabled"):
            data_service.set_change_monitor_enabled(True)
        if data_service and not self._monitor_connected:
            data_service.changes_detected.connect(self._on_data_changes, Qt.QueuedConnection)
            self._monitor_connected = True
        if not self._initial_beds_refresh_requested:
            self._initial_beds_refresh_requested = True
            self._refresh_beds_if_available(queue_if_running=False, allow_hidden=True)
        self._schedule_initial_w1a_refresh()
        if data_service and wake_monitor:
            data_service.request_immediate_refresh(force_emit=False)

    def auto_refresh(self, force: bool = False):
        if self._is_closing or _app_is_closing():
            return
        data_service = self._get_data_service()
        if force and hasattr(self.remcard_widget, "force_refresh_everywhere"):
            self.remcard_widget.force_refresh_everywhere()
        elif getattr(self.remcard_widget, "admission_id", None) is None:
            self._refresh_beds_if_available()
            self._refresh_w1a()
        if data_service:
            data_service.request_immediate_refresh(force_emit=force)

    def stop_auto_refresh(self):
        data_service = self._get_data_service()
        if data_service and self._monitor_connected:
            try:
                data_service.changes_detected.disconnect(self._on_data_changes)
            except Exception:
                pass
        self._monitor_connected = False

    def shutdown(self):
        self._is_closing = True
        self.stop_auto_refresh()
        if hasattr(self, "remcard_widget") and hasattr(self.remcard_widget, "shutdown"):
            self.remcard_widget.shutdown()

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)

    def _get_data_service(self):
        return getattr(self.remcard_service, "data_service", None)

    def _refresh_w1a(self, payload: dict | None = None):
        if self._is_closing or _app_is_closing():
            return
        layout = getattr(self.remcard_widget, "layout_manager", None)
        sector = getattr(layout, "sector_w1a", None)
        if sector is None or not _qt_is_valid(sector):
            return
        if hasattr(sector, "set_service"):
            sector.set_service(self.remcard_service)
        if payload is not None and hasattr(sector, "handle_data_changes"):
            sector.handle_data_changes(payload)
        elif hasattr(sector, "refresh_data"):
            sector.refresh_data()

    def _schedule_initial_w1a_refresh(self):
        if self._initial_w1a_refresh_requested:
            return
        self._initial_w1a_refresh_requested = True
        QTimer.singleShot(W1A_STARTUP_IDLE_DELAY_MS, self._run_initial_w1a_refresh)

    def _run_initial_w1a_refresh(self):
        if self._is_closing or _app_is_closing():
            return
        remcard_widget = getattr(self, "remcard_widget", None)
        layout = getattr(remcard_widget, "layout_manager", None) if _qt_is_valid(remcard_widget) else None
        if layout is not None and getattr(layout, "current_mode", "beds") != "beds":
            return
        self._refresh_w1a()

    def _refresh_beds_if_available(self, *, queue_if_running: bool = True, allow_hidden: bool = False):
        if self._is_closing or _app_is_closing() or (not allow_hidden and not self.isVisible()):
            return
        remcard_widget = getattr(self, "remcard_widget", None)
        if not _qt_is_valid(remcard_widget):
            return
        layout = getattr(remcard_widget, "layout_manager", None)
        if layout is None or not _qt_is_valid(layout):
            return
        beds_widget = getattr(layout, "beds_selection_widget", None)
        if beds_widget is None or not _qt_is_valid(beds_widget) or not hasattr(beds_widget, "refresh"):
            return
        beds_widget.refresh(queue_if_running=queue_if_running)

    def _on_data_changes(self, payload: dict):
        if self._is_closing or _app_is_closing() or not self.isVisible():
            return
        payload = payload or {}
        remcard_widget = getattr(self, "remcard_widget", None)
        if not _qt_is_valid(remcard_widget):
            return
        if getattr(remcard_widget, "admission_id", None) is not None:
            return
        layout = getattr(remcard_widget, "layout_manager", None)
        if layout is not None and not _qt_is_valid(layout):
            return
        if layout is not None and getattr(layout, "current_mode", "beds") != "beds":
            return
        changed_entities = {
            str(entity)
            for entity in (payload.get("changed_entities") or [])
            if entity is not None
        }
        if not changed_entities:
            changed_entities = {
                str(change.get("entity_name") or "")
                for change in (payload.get("changes") or [])
                if change.get("entity_name")
        }
        force_sources = list(payload.get("force_sources") or [])
        if payload.get("force_source"):
            force_sources.append(str(payload.get("force_source")))
        forced_refresh = bool(payload.get("gap_detected") or (payload.get("forced") and not force_sources))
        has_w1_changes = bool(changed_entities.intersection(W1_REFRESH_ENTITIES))
        forced_event = bool(payload.get("forced") or payload.get("gap_detected"))
        if not forced_event and not has_w1_changes:
            return
        if (
            forced_refresh
            or changed_entities.intersection(W1_BEDS_REFRESH_ENTITIES)
            or any(
                source.startswith(prefix)
                for source in force_sources
                for prefix in W1_BEDS_REFRESH_SOURCE_PREFIXES
            )
        ):
            self._refresh_beds_if_available()
        if forced_event or changed_entities.intersection(W1A_PANEL_REFRESH_ENTITIES):
            self._refresh_w1a(payload)

    def init_ui(self):
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        
        self.main_stack = QStackedWidget()
        
        # Режим карты (DoctorRemCardWidget)
        from .doctor_remcard_widget import DoctorRemCardWidget
        self.remcard_widget = DoctorRemCardWidget(
            self.remcard_service,
            None,
            self.patient_service,
            parent=self.main_stack,
            operblock_service=self.operblock_service,
        )
        # ВАЖНО: передаем remcard_service в LayoutManager
        if hasattr(self.remcard_widget, 'layout_manager'):
            self.remcard_widget.layout_manager.remcard_service = self.remcard_service
            self.remcard_widget.layout_manager.operblock_service = self.operblock_service
            if hasattr(self.remcard_widget.layout_manager, "sector_w1a"):
                self.remcard_widget.layout_manager.sector_w1a.set_service(self.remcard_service)
        self.remcard_widget.back_to_roles_requested.connect(self.back_to_roles)
        self.remcard_widget.refresh_requested.connect(lambda: self.auto_refresh(force=True))
        self.main_stack.addWidget(self.remcard_widget)
        
        main_layout.addWidget(self.main_stack)

    def back_to_roles(self):
        """Возврат к выбору ролей."""
        if self.parent() and hasattr(self.parent(), 'setCurrentIndex'):
            self.parent().setCurrentIndex(0)
