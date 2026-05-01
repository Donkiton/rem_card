from PySide6.QtWidgets import (QWidget, QHBoxLayout, QStackedWidget)
from PySide6.QtCore import Qt

DOCTOR_BEDS_POLL_INTERVAL_MS = 7000
W1_REFRESH_ENTITIES = {
    "patients",
    "admissions",
    "beds",
    "operations",
    "vitals",
    "vital_settings",
    "patient_status_events",
    "fluids",
    "orders",
    "administrations",
    "diet_templates",
    "diet_plan",
    "oral_intake_events",
}

class DoctorMainWidget(QWidget):
    """Главный виджет врача. Теперь является оберткой над DoctorRemCardWidget."""
    def __init__(self, patient_service, remcard_service, parent=None):
        super().__init__(parent)
        self.patient_service = patient_service
        self.remcard_service = remcard_service
        self._last_global_change_id = 0
        self._monitor_connected = False
        
        self.init_ui()

    def start_auto_refresh(self):
        data_service = self._get_data_service()
        if data_service and not self._monitor_connected:
            data_service.changes_detected.connect(self._on_data_changes, Qt.QueuedConnection)
            self._monitor_connected = True
        if hasattr(self.remcard_widget.layout_manager, 'beds_selection_widget'):
            self.remcard_widget.layout_manager.beds_selection_widget.refresh(queue_if_running=False)
        if data_service:
            data_service.request_immediate_refresh(force_emit=False)

    def auto_refresh(self, force: bool = False):
        data_service = self._get_data_service()
        if force and hasattr(self.remcard_widget, "force_refresh_everywhere"):
            self.remcard_widget.force_refresh_everywhere()
        elif self.remcard_widget.admission_id is None and hasattr(self.remcard_widget.layout_manager, 'beds_selection_widget'):
            self.remcard_widget.layout_manager.beds_selection_widget.refresh()
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
        self.stop_auto_refresh()
        if hasattr(self, "remcard_widget") and hasattr(self.remcard_widget, "shutdown"):
            self.remcard_widget.shutdown()

    def _get_data_service(self):
        return getattr(self.remcard_service, "data_service", None)

    def _on_data_changes(self, payload: dict):
        if self.remcard_widget.admission_id is not None:
            return
        layout = getattr(self.remcard_widget, "layout_manager", None)
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
        if not payload.get("forced") and not changed_entities.intersection(W1_REFRESH_ENTITIES):
            return
        if layout is not None and hasattr(layout, 'beds_selection_widget'):
            layout.beds_selection_widget.refresh()

    def init_ui(self):
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        
        self.main_stack = QStackedWidget()
        
        # Режим карты (DoctorRemCardWidget)
        from .doctor_remcard_widget import DoctorRemCardWidget
        self.remcard_widget = DoctorRemCardWidget(self.remcard_service, None, self.patient_service)
        # ВАЖНО: передаем remcard_service в LayoutManager
        if hasattr(self.remcard_widget, 'layout_manager'):
            self.remcard_widget.layout_manager.remcard_service = self.remcard_service
        self.remcard_widget.back_to_roles_requested.connect(self.back_to_roles)
        self.remcard_widget.refresh_requested.connect(lambda: self.auto_refresh(force=True))
        self.main_stack.addWidget(self.remcard_widget)
        
        main_layout.addWidget(self.main_stack)

    def back_to_roles(self):
        """Возврат к выбору ролей."""
        if self.parent() and hasattr(self.parent(), 'setCurrentIndex'):
            self.parent().setCurrentIndex(0)
