"""Prepare empty doctor/nurse entry widgets without a clinical session or I/O."""
import time
from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid
from rem_card.app.local_metrics import record_metric


class RoleEntryPreload(QObject):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.items = {}

    def prepare(self, role):
        if role not in ('doctor', 'nurse') or role in self.items:
            return
        started = time.perf_counter()
        # Imports are also paid before the chooser. No bootstrap/container here.
        if role == 'doctor':
            from rem_card.ui.doctor_view import doctor_main_widget  # noqa: F401
            from rem_card.ui.doctor_view.components.sector8_panel import Sector8Panel as Panel
        else:
            from rem_card.ui.nurse_view import nurse_main_widget  # noqa: F401
            from rem_card.ui.nurse_view.components.nurse_sector8_panel import NurseSector8Panel as Panel
        from rem_card.ui.shared.lightweight_w1_shell import LightweightW1Shell
        shell = LightweightW1Shell(role=role, patient_service=None, parent=self.owner, preparing=True)
        shell.hide()
        try:
            panel = Panel(parent=shell, preparing=True)
            shell.sector_8.set_content(panel)
            shell.ensurePolished()
            for child in shell.findChildren(type(shell.sector_8)):
                child.ensurePolished()
            self.items[role] = (shell, panel)
        except Exception:
            shell.deleteLater()
            raise
        record_metric('role_entry_preload_ms', round((time.perf_counter()-started)*1000, 2), role=role)

    def take(self, role, *, patient_service, remcard_service, operblock_service, parent):
        pair = self.items.pop(role, None)
        if pair is None:
            return None
        shell, panel = pair
        shell.setParent(parent)
        shell._preparing = False
        shell.patient_service = patient_service
        shell.remcard_service = remcard_service
        shell.operblock_service = operblock_service
        shell.patient_status_service = getattr(remcard_service, 'status_service', None)
        beds = shell.beds_selection_widget
        beds.patient_service = patient_service
        beds.remcard_service = remcard_service
        shell.sector_w1a.service = remcard_service
        for sector in (shell.sector_w1a, shell.sector_w1b, shell.sector_w1b_nurse):
            if sector is not None:
                sector._preparing = False
        shell.apply_display_settings()
        timer = getattr(beds, '_shift_boundary_timer', None)
        if timer is not None:
            beds._last_shift_start = beds._current_shift_start()
            beds._last_plan_card_window_active = beds._current_plan_card_window_active()
            timer.start(30000)
        panel._preparing = False
        panel.apply_display_settings()
        panel._reports_count_timer.start(60000)
        QTimer.singleShot(0, panel, panel.refresh_user_reports_count)
        record_metric('role_entry_preload_reused', 1, role=role)
        return pair


def take_prepared_entry(role, **kwargs):
    app = QApplication.instance()
    pool = getattr(app, '_role_entry_preload', None)
    return pool.take(role, **kwargs) if pool is not None and isValid(pool) else None
