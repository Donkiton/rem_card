import os
from datetime import datetime, timedelta

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from rem_card.services.analytics.operblock_statistics_service import (
    OPERBLOCK_SECTION_GROUPS,
    OPERBLOCK_TOP_INDICATORS,
    build_operblock_statistics_report_html,
)
from rem_card.ui.shared.analytics_worker import AnalyticsWorker
from rem_card.ui.shared.html_pdf_worker import HtmlPdfWorker
from rem_card.ui.shared.window_state import SavedFramelessDialogMixin
from rem_card.ui.styles.theme import (
    STYLE_ANALYTICS_CHECKBOX,
    STYLE_ANALYTICS_CHECKBOX_CONTAINER,
    STYLE_ANALYTICS_CONTROL_FRAME,
    STYLE_ANALYTICS_DIALOG_CONTAINER,
    STYLE_ANALYTICS_GROUP_LABEL,
    STYLE_ANALYTICS_OPTION_BUTTON,
    STYLE_ANALYTICS_PERIOD,
    STYLE_ANALYTICS_PREVIEW_BUTTON,
    STYLE_ANALYTICS_PRIMARY_BUTTON,
    STYLE_ANALYTICS_SCROLL_AREA,
    STYLE_ANALYTICS_TEXT_BROWSER,
    STYLE_ANALYTICS_TITLE,
    STYLE_DIALOG_CLOSE_BUTTON,
    STYLE_TRANSPARENT_WIDGET,
)


class OperBlockStatisticsDialog(SavedFramelessDialogMixin, QDialog):
    def __init__(
        self,
        db_manager,
        start_date_str: str,
        end_date_str: str,
        parent=None,
        *,
        db_paths=None,
    ):
        super().__init__(parent)
        self.db_manager = db_manager
        self.db_paths = list(db_paths or [])
        self._start_dt = self._parse_datetime(start_date_str) or (datetime.now() - timedelta(days=30))
        self._end_dt = self._parse_datetime(end_date_str) or datetime.now()
        if self._end_dt < self._start_dt:
            self._start_dt, self._end_dt = self._end_dt, self._start_dt
        self.start_date_str = self._start_dt.strftime("%Y-%m-%d 00:00:00")
        self.end_date_str = self._end_dt.strftime("%Y-%m-%d 23:59:59")
        self._stats_worker = None
        self._stats_pdf_worker = None
        self._closing = False
        self._latest_html = ""
        self.section_groups = OPERBLOCK_SECTION_GROUPS
        self.top_sections = OPERBLOCK_TOP_INDICATORS
        self.checkboxes = {}

        self.setWindowTitle("Статистика оперблока")
        self.setMinimumSize(820, 560)
        self.resize(1180, 860)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        self.setMouseTracking(True)
        self._init_saved_frameless_dialog("analytics/operblock_statistics_dialog_geometry", drag_area_height=86)

        self._init_ui()
        self._restore_saved_geometry()

    @staticmethod
    def _parse_datetime(value):
        if isinstance(value, datetime):
            return value
        text = str(value or "").strip().replace("T", " ")
        if not text:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y"):
            try:
                parsed = datetime.strptime(text, fmt)
                if fmt in ("%Y-%m-%d", "%d.%m.%Y"):
                    return parsed.replace(hour=0, minute=0, second=0, microsecond=0)
                return parsed
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None

    def _init_ui(self):
        self.bg_container = QWidget(self)
        self.bg_container.setMouseTracking(True)
        self.bg_container.setObjectName("bg_container")
        self.bg_container.setStyleSheet(STYLE_ANALYTICS_DIALOG_CONTAINER)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(30)
        shadow.setColor(QColor(0, 0, 0, 40))
        shadow.setOffset(0, 5)
        self.bg_container.setGraphicsEffect(shadow)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.addWidget(self.bg_container)

        self.main_layout = QVBoxLayout(self.bg_container)
        self.main_layout.setContentsMargins(20, 12, 20, 20)
        self.main_layout.setSpacing(14)

        self._build_header()
        self._build_content()

    def _build_header(self):
        header = QWidget()
        header.setFixedHeight(60)
        header.setStyleSheet(STYLE_TRANSPARENT_WIDGET)
        h_layout = QHBoxLayout(header)
        h_layout.setContentsMargins(8, 0, 0, 0)

        title_block = QVBoxLayout()
        title_block.setSpacing(2)

        title = QLabel("СТАТИСТИКА ОПЕРБЛОКА")
        title.setStyleSheet(STYLE_ANALYTICS_TITLE)
        title_block.addWidget(title)

        period_label = QLabel(
            f"Период: {self._start_dt.strftime('%d.%m.%Y')} - {self._end_dt.strftime('%d.%m.%Y')}"
        )
        period_label.setStyleSheet(STYLE_ANALYTICS_PERIOD)
        title_block.addWidget(period_label)

        h_layout.addLayout(title_block)
        h_layout.addStretch()

        close_button = QPushButton("×")
        close_button.setFixedSize(30, 30)
        close_button.setCursor(Qt.PointingHandCursor)
        close_button.setStyleSheet(STYLE_DIALOG_CLOSE_BUTTON)
        close_button.clicked.connect(self.reject)
        h_layout.addWidget(close_button)

        self.main_layout.addWidget(header)

    def _build_content(self):
        content = QHBoxLayout()
        self.main_layout.addLayout(content, 1)

        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)

        ctrl_frame = QFrame()
        ctrl_frame.setStyleSheet(STYLE_ANALYTICS_CONTROL_FRAME)
        ctrl_layout = QVBoxLayout(ctrl_frame)
        ctrl_layout.setSpacing(5)

        self.btn_select_all = QPushButton("Отметить все")
        self.btn_deselect_all = QPushButton("Снять отметки")
        self.btn_select_top = QPushButton("Ключевые показатели")
        for button in (self.btn_select_all, self.btn_deselect_all, self.btn_select_top):
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet(STYLE_ANALYTICS_OPTION_BUTTON)
            ctrl_layout.addWidget(button)

        self.btn_select_all.clicked.connect(self._select_all)
        self.btn_deselect_all.clicked.connect(self._deselect_all)
        self.btn_select_top.clicked.connect(self._select_top)
        left_layout.addWidget(ctrl_frame)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(STYLE_ANALYTICS_SCROLL_AREA)

        cb_container = QWidget()
        cb_container.setStyleSheet(STYLE_ANALYTICS_CHECKBOX_CONTAINER)
        cb_layout = QVBoxLayout(cb_container)
        cb_layout.setSpacing(7)

        for group_name, items in self.section_groups.items():
            group_lbl = QLabel(group_name.upper())
            group_lbl.setStyleSheet(STYLE_ANALYTICS_GROUP_LABEL)
            cb_layout.addWidget(group_lbl)

            for key, caption in items.items():
                cb = QCheckBox(caption)
                cb.setChecked(False)
                cb.setStyleSheet(STYLE_ANALYTICS_CHECKBOX)
                cb_layout.addWidget(cb)
                self.checkboxes[key] = cb

        cb_layout.addStretch()
        scroll.setWidget(cb_container)
        left_layout.addWidget(scroll, 1)

        self.preview_btn = QPushButton("ПРЕДПРОСМОТР ОТЧЕТА")
        self.preview_btn.setCursor(Qt.PointingHandCursor)
        self.preview_btn.setFixedHeight(40)
        self.preview_btn.setStyleSheet(STYLE_ANALYTICS_PREVIEW_BUTTON)
        self.preview_btn.clicked.connect(self._on_preview_clicked)
        left_layout.addWidget(self.preview_btn)

        self.save_pdf_btn = QPushButton("СОХРАНИТЬ ОТЧЕТ В PDF")
        self.save_pdf_btn.setCursor(Qt.PointingHandCursor)
        self.save_pdf_btn.setFixedHeight(45)
        self.save_pdf_btn.setStyleSheet(STYLE_ANALYTICS_PRIMARY_BUTTON)
        self.save_pdf_btn.clicked.connect(self._on_save_pdf_clicked)
        left_layout.addWidget(self.save_pdf_btn)

        content.addWidget(left_widget, 1)

        self.report_text = QTextBrowser()
        self.report_text.setStyleSheet(STYLE_ANALYTICS_TEXT_BROWSER)
        content.addWidget(self.report_text, 2)

    def _select_all(self):
        for cb in self.checkboxes.values():
            cb.setChecked(True)

    def _deselect_all(self):
        for cb in self.checkboxes.values():
            cb.setChecked(False)

    def _select_top(self):
        self._deselect_all()
        for key in self.top_sections:
            if key in self.checkboxes:
                self.checkboxes[key].setChecked(True)

    def _selected_indicators(self):
        return [key for key, cb in self.checkboxes.items() if cb.isChecked()]

    def _on_preview_clicked(self):
        self._start_statistics_build(save_pdf=False)

    def _on_save_pdf_clicked(self):
        self._start_statistics_build(save_pdf=True)

    def _start_statistics_build(self, save_pdf=False):
        if self._closing:
            return
        if self._stats_worker is not None and self._stats_worker.isRunning():
            return

        selected = self._selected_indicators()
        if not selected:
            from rem_card.ui.shared.custom_message_box import CustomMessageBox

            CustomMessageBox.information(self, "Внимание", "Выберите хотя бы один показатель статистики.")
            return

        self._set_statistics_busy(True, "Формирование статистики оперблока...")
        self._stats_worker = AnalyticsWorker(
            lambda: build_operblock_statistics_report_html(
                self.db_manager,
                self.start_date_str,
                self.end_date_str,
                selected,
                db_paths=self.db_paths,
            ),
            parent=self,
        )
        self._stats_worker.completed.connect(lambda html: self._on_statistics_ready(html, save_pdf))
        self._stats_worker.failed.connect(self._on_statistics_failed)
        self._stats_worker.cancelled.connect(self._on_statistics_cancelled)
        self._stats_worker.finished.connect(self._clear_statistics_worker)
        self._stats_worker.start()

    def _on_statistics_ready(self, html: str, save_pdf: bool):
        if self._closing:
            return
        self._latest_html = str(html or "")
        self.report_text.setHtml(self._latest_html)
        if save_pdf:
            self._start_statistics_pdf_worker(self._latest_html)
            return
        self._set_statistics_busy(False)

    def _start_statistics_pdf_worker(self, html: str):
        if self._closing:
            return
        if self._stats_pdf_worker is not None and self._stats_pdf_worker.isRunning():
            return
        from rem_card.app.paths import REPORT_DIR

        os.makedirs(REPORT_DIR, exist_ok=True)
        filename = f"operblock_statistics_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
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

        self._set_statistics_busy(False)
        CustomMessageBox.information(
            self,
            "Успех",
            f"Статистика оперблока успешно сохранена:\n{os.path.basename(pdf_path)}",
        )

    def _on_statistics_failed(self, message: str):
        if self._closing:
            return
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        self._set_statistics_busy(False)
        CustomMessageBox.information(self, "Ошибка", f"Не удалось сформировать статистику оперблока:\n{message}")

    def _on_statistics_cancelled(self):
        if self._closing:
            return
        self._set_statistics_busy(False)

    def _set_statistics_busy(self, busy: bool, text: str = ""):
        self.preview_btn.setEnabled(not busy)
        self.save_pdf_btn.setEnabled(not busy)
        if text:
            self.report_text.setHtml(f"<p>{text}</p>")

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
