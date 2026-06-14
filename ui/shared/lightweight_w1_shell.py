from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QStackedWidget, QVBoxLayout, QWidget

from rem_card.app.logger import logger
from rem_card.ui.shared.display_settings_storage import (
    DisplaySettingsStorage,
    w1a_upcoming_orders_enabled,
    w1b_lower_sector_enabled,
)
from rem_card.ui.shared.layout_components import CurrentPageStack, SplitterManager


PATIENT_BED_MANAGEMENT_MODE = "patient_bed_management"


class LightweightW1Shell(QWidget):
    """Стартовый W1 без скрытой полной карты пациента."""

    selection_mode_changed = Signal(str)

    def __init__(
        self,
        *,
        role: str,
        patient_service,
        remcard_service=None,
        parent=None,
        operblock_service=None,
    ):
        super().__init__(parent)
        self.role = str(role or "doctor").lower()
        self.patient_service = patient_service
        self.remcard_service = remcard_service
        self.operblock_service = operblock_service
        self.patient_status_service = getattr(remcard_service, "status_service", None)
        self.current_admission_id = None
        self.current_date = None
        self.current_mode = "beds"
        self._is_closing = False
        self._archive_last_change_id = -1

        self.archive_widget = None
        self.admin_widget = None
        self.journal_widget = None
        self.sector_w1c = None

        self._init_ui()

    def _init_ui(self):
        from rem_card.ui.rem_card_sectors.sector_8 import Sector8
        from rem_card.ui.rem_card_sectors.sector_w1a import SectorW1a

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 2, 0, 0)
        main_layout.setSpacing(0)

        self.sector_8 = Sector8()
        self.sector_8.setFixedHeight(38)

        self.lower_area = SplitterManager.create_splitter(Qt.Horizontal)
        self.left_column = QWidget()
        self.left_column.setFixedWidth(250)
        self.l_layout = QVBoxLayout(self.left_column)
        self.l_layout.setContentsMargins(0, 0, 0, 0)
        self.l_layout.setSpacing(0)

        self.sector_1a_stack = CurrentPageStack()
        self.sector_w1a = SectorW1a(self.remcard_service, role=self.role, auto_initial_refresh=False)
        self.sector_1a_stack.addWidget(self.sector_w1a)
        self.l_layout.addWidget(self.sector_1a_stack, 1)

        self.sector_1b_stack = CurrentPageStack()
        if self.role == "nurse":
            from rem_card.ui.rem_card_sectors.sector_w1b_nurse import SectorW1bNurse

            self.sector_w1b = None
            self.sector_w1b_nurse = SectorW1bNurse(role="nurse")
            self.sector_1b_stack.addWidget(self.sector_w1b_nurse)
        else:
            from rem_card.ui.rem_card_sectors.sector_w1b import SectorW1b

            self.sector_w1b = SectorW1b(role="doctor")
            self.sector_w1b_nurse = None
            self.sector_1b_stack.addWidget(self.sector_w1b)
        self.l_layout.addWidget(self.sector_1b_stack, 0)

        self.selection_stack = QStackedWidget()
        self.right_area = QWidget()
        self.selection_stack.addWidget(self.right_area)

        self.beds_view = QWidget()
        beds_layout = QVBoxLayout(self.beds_view)
        beds_layout.setContentsMargins(0, 0, 0, 0)
        if self.role == "nurse":
            from rem_card.ui.nurse_view.components.nurse_beds_selection_widget import NurseBedsSelectionWidget

            self.beds_selection_widget = NurseBedsSelectionWidget(
                self.patient_service,
                self.remcard_service,
                auto_initial_refresh=False,
            )
        else:
            from rem_card.ui.doctor_view.components.beds_selection_widget import BedsSelectionWidget

            self.beds_selection_widget = BedsSelectionWidget(
                self.patient_service,
                self.remcard_service,
                auto_initial_refresh=False,
            )
        beds_layout.addWidget(self.beds_selection_widget)
        self.selection_stack.addWidget(self.beds_view)

        self.archive_view = QWidget()
        self._archive_layout = QVBoxLayout(self.archive_view)
        self._archive_layout.setContentsMargins(0, 0, 0, 0)
        self.selection_stack.addWidget(self.archive_view)

        self.admin_view = QWidget()
        self._admin_layout = QVBoxLayout(self.admin_view)
        self._admin_layout.setContentsMargins(0, 0, 0, 0)
        self.selection_stack.addWidget(self.admin_view)

        self.journal_view = QWidget()
        self._journal_layout = QVBoxLayout(self.journal_view)
        self._journal_layout.setContentsMargins(0, 0, 0, 0)
        self.selection_stack.addWidget(self.journal_view)

        self.bottom_row = QWidget()
        self.bottom_row.hide()

        self.lower_area.addWidget(self.left_column)
        self.lower_area.addWidget(self.selection_stack)

        self.v_splitter_top = SplitterManager.create_splitter(Qt.Vertical)
        self.v_splitter_top.addWidget(self.sector_8)
        self.v_splitter_top.addWidget(self.lower_area)
        main_layout.addWidget(self.v_splitter_top)

        self.selection_stack.setCurrentIndex(1)
        self._apply_w1_beds_sector_visibility(refresh_w1a=False)

    def _w1_display_flags(self) -> tuple[bool, bool]:
        try:
            payload = DisplaySettingsStorage().load()
            return (
                w1a_upcoming_orders_enabled(payload, self.role),
                w1b_lower_sector_enabled(payload, self.role),
            )
        except Exception:
            return True, True

    def _ensure_sector_w1c(self):
        if self.sector_w1c is None:
            from rem_card.ui.rem_card_sectors.sector_w1c import SectorW1c

            self.sector_w1c = SectorW1c()
            self.sector_1a_stack.addWidget(self.sector_w1c)
        return self.sector_w1c

    def _apply_w1_beds_sector_visibility(self, *, refresh_w1a: bool = True):
        w1a_enabled, w1b_enabled = self._w1_display_flags()
        use_w1c = not w1a_enabled and not w1b_enabled

        if hasattr(self.sector_w1a, "apply_display_settings"):
            self.sector_w1a.apply_display_settings()
        w1b = self.sector_w1b_nurse if self.role == "nurse" else self.sector_w1b
        if w1b is not None and hasattr(w1b, "apply_display_settings"):
            w1b.apply_display_settings()

        if use_w1c:
            self.sector_1a_stack.setCurrentWidget(self._ensure_sector_w1c())
        else:
            self.sector_1a_stack.setCurrentWidget(self.sector_w1a)
            if refresh_w1a and w1a_enabled and hasattr(self.sector_w1a, "refresh_data"):
                self.sector_w1a.refresh_data()

        self.left_column.updateGeometry()
        self.left_column.update()

    def apply_display_settings(self):
        if self.current_mode in ("beds", PATIENT_BED_MANAGEMENT_MODE):
            self._apply_w1_beds_sector_visibility(refresh_w1a=False)

    def set_patient_selection_mode(self, mode: str):
        if self._is_closing:
            return
        normalized = "patient_bed_management" if mode == "journal" else str(mode or "beds")
        if normalized == "beds":
            already_beds = self.current_mode == "beds" and self.selection_stack.currentIndex() == 1
            self.selection_stack.setCurrentIndex(1)
            self._apply_w1_beds_sector_visibility(refresh_w1a=not already_beds)
            if not already_beds and hasattr(self.beds_selection_widget, "refresh"):
                QTimer.singleShot(0, self.refresh_beds)
            self.current_mode = "beds"
            self.selection_mode_changed.emit("beds")
            return
        if normalized == "archive":
            if self._ensure_archive_widget() is None:
                return
            self.selection_stack.setCurrentIndex(2)
            self._refresh_archive_if_needed(force=self._archive_last_change_id < 0)
            self.current_mode = "archive"
            self.selection_mode_changed.emit("archive")
            return
        if normalized == "admin":
            if self._ensure_admin_widget() is None:
                return
            self.selection_stack.setCurrentIndex(3)
            self.current_mode = "admin"
            self.selection_mode_changed.emit("admin")
            return
        if normalized == PATIENT_BED_MANAGEMENT_MODE:
            if self._ensure_journal_widget() is None:
                return
            self.selection_stack.setCurrentIndex(4)
            self._apply_w1_beds_sector_visibility()
            if hasattr(self.journal_widget, "refresh_bed_statuses"):
                QTimer.singleShot(0, self.journal_widget.refresh_bed_statuses)
            self.current_mode = PATIENT_BED_MANAGEMENT_MODE
            self.selection_mode_changed.emit(PATIENT_BED_MANAGEMENT_MODE)
            return
        logger.debug("LightweightW1Shell ignores mode %s before full card layout exists", mode)

    def refresh_beds(self, *, queue_if_running: bool = True):
        if self._is_closing:
            return
        if hasattr(self.beds_selection_widget, "refresh"):
            self.beds_selection_widget.refresh(queue_if_running=queue_if_running)

    def refresh_w1a(self, payload: dict | None = None):
        if self._is_closing:
            return
        if hasattr(self.sector_w1a, "set_service"):
            self.sector_w1a.set_service(self.remcard_service)
        if payload is not None and hasattr(self.sector_w1a, "handle_data_changes"):
            self.sector_w1a.handle_data_changes(payload)
        elif hasattr(self.sector_w1a, "refresh_data"):
            self.sector_w1a.refresh_data()

    def _ensure_archive_widget(self):
        if self.archive_widget is not None:
            return self.archive_widget
        if self.patient_service is None:
            return None
        from rem_card.ui.doctor_view.archive_widget import ArchiveWidget

        allow_archive_edit = self.role == "doctor"
        self.archive_widget = ArchiveWidget(
            self.patient_service,
            remcard_service=self.remcard_service,
            allow_edit=allow_archive_edit,
            operblock_service=self.operblock_service if allow_archive_edit else None,
        )
        self._archive_layout.addWidget(self.archive_widget)
        return self.archive_widget

    def _refresh_archive_if_needed(self, force: bool = False):
        if self.archive_widget is None:
            return
        if force or not getattr(self.archive_widget, "all_archived_patients", None):
            self.archive_widget.load_data()
            self._archive_last_change_id = max(self._archive_last_change_id, 0)

    def _ensure_admin_widget(self):
        if self.admin_widget is not None:
            return self.admin_widget
        if self.remcard_service is None:
            return None
        from rem_card.ui.admin_view.admin_main_widget import AdminMainWidget

        self.admin_widget = AdminMainWidget(service=self.remcard_service, role=self.role)
        self._admin_layout.addWidget(self.admin_widget)
        return self.admin_widget

    def _ensure_journal_widget(self):
        if self.journal_widget is not None:
            return self.journal_widget
        if self.remcard_service is None:
            return None
        from rem_card.ui.patient_bed_management.management_widget import PatientBedManagementWidget

        db_manager = self.remcard_service.orders_dao.db
        self.journal_widget = PatientBedManagementWidget(
            db_manager,
            data_service=getattr(self.remcard_service, "data_service", None),
            parent=self.journal_view,
        )
        self._journal_layout.addWidget(self.journal_widget)
        return self.journal_widget

    def prewarm_journal_widget(self):
        return self._ensure_journal_widget()

    def shutdown(self):
        self._is_closing = True
        widgets = (
            self.beds_selection_widget,
            self.sector_w1a,
            self.archive_widget,
            self.admin_widget,
            self.journal_widget,
        )
        for widget in widgets:
            if widget is not None and hasattr(widget, "shutdown"):
                try:
                    widget.shutdown()
                except Exception:
                    logger.debug("Lightweight W1 child shutdown failed", exc_info=True)
