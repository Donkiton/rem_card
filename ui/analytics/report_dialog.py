import os
from datetime import datetime

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import QDateEdit, QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from rem_card.services.analytics.statistics_service import build_statistical_report_html
from rem_card.ui.shared.analytics_worker import AnalyticsWorker
from rem_card.ui.shared.html_pdf_worker import HtmlPdfWorker
from rem_card.ui.shared.window_state import SavedFramelessDialogMixin
from rem_card.ui.styles.theme import (
    STYLE_ANALYTICS_DATE_EDIT,
    STYLE_ANALYTICS_DATE_FRAME,
    STYLE_ANALYTICS_DATE_LABEL,
    STYLE_ANALYTICS_DIALOG_CONTAINER,
    STYLE_ANALYTICS_PREVIEW_BUTTON,
    STYLE_ANALYTICS_PRIMARY_BUTTON,
    STYLE_ANALYTICS_TITLE,
    STYLE_DIALOG_CLOSE_BUTTON,
)

class ReportDialog(SavedFramelessDialogMixin, QDialog):
    def __init__(
        self,
        db_manager,
        parent=None,
        start_dt: str | None = None,
        end_dt: str | None = None,
        show_graph_button: bool = True,
    ):
        super().__init__(parent)
        self.db_manager = db_manager
        self._prefill_start_dt = start_dt
        self._prefill_end_dt = end_dt
        self._show_graph_button = bool(show_graph_button)
        self._stats_worker = None
        self._stats_pdf_worker = None
        self._closing = False

        self.setWindowTitle("Отчеты и статистика")
        self.setMinimumSize(440, 340)
        self.resize(500, 400)

        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self._init_saved_frameless_dialog("analytics/report_dialog_geometry", drag_area_height=78)

        self._init_ui()
        self._restore_saved_geometry()

    @staticmethod
    def _parse_period_qdate(value: str | None) -> QDate | None:
        if not value:
            return None
        text = str(value).strip()
        if not text:
            return None
        date_part = text.split(" ")[0]

        parsed = QDate.fromString(date_part, "yyyy-MM-dd")
        if parsed.isValid():
            return parsed

        parsed = QDate.fromString(date_part, "dd.MM.yyyy")
        if parsed.isValid():
            return parsed

        return None

    def _init_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(10, 10, 10, 10)

        self.container = QWidget()
        self.container.setObjectName("container")
        self.container.setStyleSheet(STYLE_ANALYTICS_DIALOG_CONTAINER)
        self.container_layout = QVBoxLayout(self.container)
        self.container_layout.setContentsMargins(30, 20, 30, 30)
        self.container_layout.setSpacing(20)

        self.main_layout.addWidget(self.container)

        # Header
        header_layout = QHBoxLayout()
        title_label = QLabel("ОТЧЕТНЫЙ ПЕРИОД")
        title_label.setStyleSheet(STYLE_ANALYTICS_TITLE)
        header_layout.addWidget(title_label)
        header_layout.addStretch()

        self.close_btn = QPushButton("×")
        self.close_btn.setFixedSize(30, 30)
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.setStyleSheet(STYLE_DIALOG_CLOSE_BUTTON)
        self.close_btn.clicked.connect(self.reject)
        header_layout.addWidget(self.close_btn)
        self.container_layout.addLayout(header_layout)

        # Date Selection
        dates_frame = QFrame()
        dates_frame.setStyleSheet(STYLE_ANALYTICS_DATE_FRAME)
        dates_layout = QVBoxLayout(dates_frame)
        dates_layout.setContentsMargins(20, 20, 20, 20)
        dates_layout.setSpacing(15)

        # Start Date
        start_layout = QHBoxLayout()
        start_label = QLabel("Начало периода:")
        start_label.setStyleSheet(STYLE_ANALYTICS_DATE_LABEL)
        self.start_date = QDateEdit()
        self.start_date.setCalendarPopup(True)
        self.start_date.setDate(
            self._parse_period_qdate(self._prefill_start_dt) or QDate.currentDate().addMonths(-1)
        )
        self.start_date.setStyleSheet(STYLE_ANALYTICS_DATE_EDIT)
        start_layout.addWidget(start_label)
        start_layout.addWidget(self.start_date)
        dates_layout.addLayout(start_layout)

        # End Date
        end_layout = QHBoxLayout()
        end_label = QLabel("Конец периода:")
        end_label.setStyleSheet(STYLE_ANALYTICS_DATE_LABEL)
        self.end_date = QDateEdit()
        self.end_date.setCalendarPopup(True)
        self.end_date.setDate(
            self._parse_period_qdate(self._prefill_end_dt) or QDate.currentDate()
        )
        self.end_date.setStyleSheet(STYLE_ANALYTICS_DATE_EDIT)
        end_layout.addWidget(end_label)
        end_layout.addWidget(self.end_date)
        dates_layout.addLayout(end_layout)

        self.container_layout.addWidget(dates_frame)

        # Actions
        self.excel_btn = QPushButton("СФОРМИРОВАТЬ ГРАФИКИ")
        self.excel_btn.setFixedHeight(50)
        self.excel_btn.setCursor(Qt.PointingHandCursor)
        self.excel_btn.setStyleSheet(STYLE_ANALYTICS_PRIMARY_BUTTON)
        self.excel_btn.clicked.connect(self._open_graphs_dialog)
        self.container_layout.addWidget(self.excel_btn)
        self.excel_btn.setVisible(self._show_graph_button)

        self.pdf_btn = QPushButton("СОХРАНИТЬ ПОЛНЫЙ ОТЧЕТ (PDF)")
        self.pdf_btn.setFixedHeight(50)
        self.pdf_btn.setCursor(Qt.PointingHandCursor)
        self.pdf_btn.setStyleSheet(STYLE_ANALYTICS_PREVIEW_BUTTON)
        self.pdf_btn.clicked.connect(self._generate_pdf_report)
        self.container_layout.addWidget(self.pdf_btn)

    def _open_graphs_dialog(self):
        if self._closing:
            return
        start_dt = self.start_date.date().toString("yyyy-MM-dd 00:00:00")
        end_dt = self.end_date.date().toString("yyyy-MM-dd 23:59:59")

        from rem_card.ui.analytics.graphs_dialog import GraphsDialog
        dialog = GraphsDialog(self.db_manager, start_dt, end_dt, self)
        dialog.exec()

    def _generate_pdf_report(self):
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        if self._closing:
            return
        if self.start_date.date() > self.end_date.date():
            CustomMessageBox.information(self, "Внимание", "Дата начала периода не может быть позже даты окончания.")
            return
        if getattr(self, "_stats_worker", None) is not None and self._stats_worker.isRunning():
            return

        start_dt = self.start_date.date().toString("yyyy-MM-dd 00:00:00")
        end_dt = self.end_date.date().toString("yyyy-MM-dd 23:59:59")
        self._set_pdf_busy(True)
        self._stats_worker = AnalyticsWorker(
            lambda: build_statistical_report_html(self.db_manager, start_dt, end_dt),
            parent=self,
        )
        self._stats_worker.completed.connect(self._on_statistics_html_ready)
        self._stats_worker.failed.connect(self._on_statistics_failed)
        self._stats_worker.finished.connect(self._clear_statistics_worker)
        self._stats_worker.start()

    def _on_statistics_html_ready(self, html: str):
        if self._closing:
            return
        from rem_card.app.paths import REPORT_DIR

        os.makedirs(REPORT_DIR, exist_ok=True)
        filename = f"statistics_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        pdf_path = os.path.join(REPORT_DIR, filename)
        self._stats_pdf_worker = HtmlPdfWorker(html, pdf_path, parent=self)
        self._stats_pdf_worker.completed.connect(self._on_statistics_pdf_ready)
        self._stats_pdf_worker.failed.connect(self._on_statistics_failed)
        self._stats_pdf_worker.finished.connect(self._clear_statistics_pdf_worker)
        self._stats_pdf_worker.start()

    def _on_statistics_pdf_ready(self, pdf_path: str):
        if self._closing:
            return
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        self._set_pdf_busy(False)
        CustomMessageBox.information(self, "Успех", f"Статистический отчет успешно сохранен:\n{os.path.basename(pdf_path)}")

    def _on_statistics_failed(self, message: str):
        if self._closing:
            return
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        self._set_pdf_busy(False)
        CustomMessageBox.information(self, "Ошибка PDF", f"Не удалось сохранить PDF:\n{message}")

    def _set_pdf_busy(self, busy: bool):
        self.pdf_btn.setEnabled(not busy)
        self.excel_btn.setEnabled(not busy and self._show_graph_button)
        self.pdf_btn.setText("ФОРМИРУЕТСЯ..." if busy else "СОХРАНИТЬ ПОЛНЫЙ ОТЧЕТ (PDF)")

    def _clear_statistics_worker(self):
        self._stats_worker = None

    def _clear_statistics_pdf_worker(self):
        self._stats_pdf_worker = None

    def _cancel_workers(self):
        self._closing = True
        for worker in (self._stats_worker, self._stats_pdf_worker):
            if worker is not None and hasattr(worker, "cancel"):
                try:
                    worker.cancel()
                except Exception:
                    pass

    def reject(self):
        self._cancel_workers()
        super().reject()

    def closeEvent(self, event):
        self._cancel_workers()
        super().closeEvent(event)
