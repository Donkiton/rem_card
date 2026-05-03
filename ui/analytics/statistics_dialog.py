import os
from datetime import datetime, timedelta

from PySide6.QtCore import QPoint, Qt
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

from rem_card.services.analytics.detailed_statistics_service import (
    SECTION_GROUPS,
    TOP_SECTIONS,
    build_detailed_statistics_report_html,
    parse_statistics_datetime,
)
from rem_card.ui.shared.analytics_worker import AnalyticsWorker
from rem_card.ui.shared.html_pdf_worker import HtmlPdfWorker


class StatisticsDialog(QDialog):
    def __init__(self, db_manager, start_date_str: str, end_date_str: str, parent=None):
        super().__init__(parent)
        self.db_manager = db_manager

        self._start_dt = parse_statistics_datetime(start_date_str) or (datetime.now() - timedelta(days=30))
        self._end_dt = parse_statistics_datetime(end_date_str) or datetime.now()
        if self._end_dt < self._start_dt:
            self._start_dt, self._end_dt = self._end_dt, self._start_dt

        self.start_date_str = self._start_dt.strftime("%Y-%m-%d 00:00:00")
        self.end_date_str = self._end_dt.strftime("%Y-%m-%d 23:59:59")

        self.setWindowTitle("Статистический отчет")
        self.resize(1180, 860)

        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        self.setMouseTracking(True)
        self._drag_pos = QPoint()
        self._margin = 10
        self._resizing = False

        self.bg_color = "#f5f2e9"
        self.border_color = "#d1d1bc"
        self.accent_color = "#8a8a68"
        self.text_color = "#2d2d24"

        self.section_groups = SECTION_GROUPS
        self.top_sections = TOP_SECTIONS
        self.checkboxes = {}
        self._stats_worker = None
        self._stats_pdf_worker = None
        self._closing = False

        self._init_ui()

    def _init_ui(self):
        self.bg_container = QWidget(self)
        self.bg_container.setMouseTracking(True)
        self.bg_container.setStyleSheet(
            f"""
            QWidget#bg_container {{
                background-color: {self.bg_color};
                border: 2px solid {self.border_color};
                border-radius: 15px;
            }}
            """
        )
        self.bg_container.setObjectName("bg_container")

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
        header.setStyleSheet("background: transparent;")
        h_layout = QHBoxLayout(header)
        h_layout.setContentsMargins(8, 0, 0, 0)

        title_block = QVBoxLayout()
        title_block.setSpacing(2)

        title = QLabel("СТАТИСТИЧЕСКИЙ ОТЧЕТ")
        title.setStyleSheet(
            "color: #4a4a3a; font-size: 13px; font-weight: 800; letter-spacing: 1px; background: transparent;"
        )
        title_block.addWidget(title)

        period_label = QLabel(
            f"Период (из архива): {self._start_dt.strftime('%d.%m.%Y')} - {self._end_dt.strftime('%d.%m.%Y')}"
        )
        period_label.setStyleSheet("color: #6f6f57; font-size: 11px; font-weight: 600;")
        title_block.addWidget(period_label)

        h_layout.addLayout(title_block)
        h_layout.addStretch()

        close_button = QPushButton("×")
        close_button.setFixedSize(30, 30)
        close_button.setCursor(Qt.PointingHandCursor)
        close_button.setStyleSheet(
            """
            QPushButton { background: transparent; color: #7a7a6a; font-size: 22px; border: none; }
            QPushButton:hover { background: #ef4444; color: white; border-radius: 5px; }
            """
        )
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
        ctrl_frame.setStyleSheet(
            f"QFrame {{ background: #fdfdfa; border: 1px solid {self.border_color}; border-radius: 8px; }}"
        )
        ctrl_layout = QVBoxLayout(ctrl_frame)
        ctrl_layout.setSpacing(5)

        self.btn_select_all = QPushButton("Отметить все")
        self.btn_deselect_all = QPushButton("Снять отметки")
        self.btn_select_top = QPushButton("Ключевые показатели")
        ctrl_buttons = [self.btn_select_all, self.btn_deselect_all, self.btn_select_top]

        btn_style = f"""
            QPushButton {{
                color: white;
                background: {self.accent_color};
                border: none;
                border-radius: 4px;
                font-weight: 600;
                font-size: 12px;
                text-align: center;
                padding: 6px 10px;
            }}
            QPushButton:hover {{
                background: #707054;
            }}
        """
        for button in ctrl_buttons:
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet(btn_style)
            ctrl_layout.addWidget(button)

        self.btn_select_all.clicked.connect(self._select_all)
        self.btn_deselect_all.clicked.connect(self._deselect_all)
        self.btn_select_top.clicked.connect(self._select_top)
        left_layout.addWidget(ctrl_frame)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            f"""
            QScrollArea {{ background: transparent; border: 1px solid {self.border_color}; border-radius: 8px; }}
            QScrollBar:vertical {{ border: none; background: #fdfdfa; width: 10px; margin: 0px; }}
            QScrollBar::handle:vertical {{ background: #c9c9b4; min-height: 20px; border-radius: 5px; }}
            """
        )

        cb_container = QWidget()
        cb_container.setStyleSheet("background: #fdfdfa;")
        cb_layout = QVBoxLayout(cb_container)
        cb_layout.setSpacing(7)

        for group_name, items in self.section_groups.items():
            group_lbl = QLabel(group_name.upper())
            group_lbl.setStyleSheet(
                f"color: {self.accent_color}; font-weight: 800; font-size: 10px; margin-top: 10px; border-bottom: 1px solid {self.border_color};"
            )
            cb_layout.addWidget(group_lbl)

            for key, caption in items.items():
                cb = QCheckBox(caption)
                cb.setChecked(False)
                cb.setStyleSheet(
                    f"QCheckBox {{ color: {self.text_color}; font-weight: 500; font-size: 11px; background: transparent; }}"
                )
                cb_layout.addWidget(cb)
                self.checkboxes[key] = cb

        cb_layout.addStretch()
        scroll.setWidget(cb_container)
        left_layout.addWidget(scroll, 1)

        self.preview_btn = QPushButton("ПРЕДПРОСМОТР ОТЧЕТА")
        self.preview_btn.setCursor(Qt.PointingHandCursor)
        self.preview_btn.setFixedHeight(40)
        self.preview_btn.setStyleSheet(
            f"""
            QPushButton {{ background-color: #fdfdfa; color: {self.accent_color}; border: 2px solid {self.accent_color}; border-radius: 6px; font-weight: 800; font-size: 11px; }}
            QPushButton:hover {{ background-color: #e8e4d5; }}
            """
        )
        self.preview_btn.clicked.connect(self._on_preview_clicked)
        left_layout.addWidget(self.preview_btn)

        self.save_pdf_btn = QPushButton("СОХРАНИТЬ ОТЧЕТ В PDF")
        self.save_pdf_btn.setCursor(Qt.PointingHandCursor)
        self.save_pdf_btn.setFixedHeight(45)
        self.save_pdf_btn.setStyleSheet(
            f"""
            QPushButton {{ background-color: {self.accent_color}; color: white; border: none; border-radius: 6px; font-weight: 800; font-size: 12px; }}
            QPushButton:hover {{ background-color: #707054; }}
            """
        )
        self.save_pdf_btn.clicked.connect(self._on_save_pdf_clicked)
        left_layout.addWidget(self.save_pdf_btn)

        content.addWidget(left_widget, 1)

        self.report_text = QTextBrowser()
        self.report_text.setStyleSheet(
            f"""
            QTextBrowser {{ background-color: #fdfdfa; border: 1px solid {self.border_color}; border-radius: 8px; padding: 10px; }}
            """
        )
        content.addWidget(self.report_text, 2)

    def _get_resize_edges(self, pos):
        edges = Qt.Edge(0)
        if pos.x() < self._margin:
            edges |= Qt.LeftEdge
        if pos.x() > self.width() - self._margin:
            edges |= Qt.RightEdge
        if pos.y() < self._margin:
            edges |= Qt.TopEdge
        if pos.y() > self.height() - self._margin:
            edges |= Qt.BottomEdge
        return edges

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            edges = self._get_resize_edges(event.pos())
            if edges:
                self._resizing = True
                self.windowHandle().startSystemResize(edges)
            elif self.childAt(event.pos()) in [self.bg_container, None]:
                self._drag_pos = event.globalPosition().toPoint() - self.pos()
                self._dragging = True
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        self._resizing = False
        if event.button() == Qt.LeftButton:
            self._dragging = False
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):
        edges = self._get_resize_edges(event.pos())
        if edges == (Qt.LeftEdge | Qt.TopEdge) or edges == (Qt.RightEdge | Qt.BottomEdge):
            self.setCursor(Qt.SizeFDiagCursor)
        elif edges == (Qt.RightEdge | Qt.TopEdge) or edges == (Qt.LeftEdge | Qt.BottomEdge):
            self.setCursor(Qt.SizeBDiagCursor)
        elif edges & (Qt.LeftEdge | Qt.RightEdge):
            self.setCursor(Qt.SizeHorCursor)
        elif edges & (Qt.TopEdge | Qt.BottomEdge):
            self.setCursor(Qt.SizeVerCursor)
        else:
            self.setCursor(Qt.ArrowCursor)

        if event.buttons() & Qt.LeftButton and getattr(self, "_dragging", False) and not self._resizing:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(event)

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

    def _selected_sections(self):
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

        selected = self._selected_sections()
        if not selected:
            from rem_card.ui.shared.custom_message_box import CustomMessageBox

            CustomMessageBox.information(self, "Внимание", "Выберите хотя бы один раздел статистики.")
            return

        self._set_statistics_busy(True, "Формирование статистического отчета...")
        self._stats_worker = AnalyticsWorker(
            lambda: build_detailed_statistics_report_html(
                self.db_manager,
                self.start_date_str,
                self.end_date_str,
                selected,
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
        self.report_text.setHtml(html)
        if save_pdf:
            self._start_statistics_pdf_worker(html)
            return
        self._set_statistics_busy(False)

    def _start_statistics_pdf_worker(self, html: str):
        if self._closing:
            return
        if self._stats_pdf_worker is not None and self._stats_pdf_worker.isRunning():
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

        self._set_statistics_busy(False)
        CustomMessageBox.information(
            self,
            "Успех",
            f"Статистический отчет успешно сохранен:\n{os.path.basename(pdf_path)}",
        )

    def _on_statistics_failed(self, message: str):
        if self._closing:
            return
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        self._set_statistics_busy(False)
        CustomMessageBox.information(self, "Ошибка", f"Не удалось сформировать статистический отчет:\n{message}")

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
