import os
from datetime import datetime
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QWidget, QGraphicsDropShadowEffect, QScrollArea, QCheckBox, QTextBrowser, QFrame)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

from rem_card.services.analytics.graphs_service import build_graphs_html, wrap_graphs_pdf_html
from rem_card.ui.shared.analytics_worker import AnalyticsWorker
from rem_card.ui.shared.html_pdf_worker import HtmlPdfWorker
from rem_card.ui.shared.window_state import SavedFramelessDialogMixin
from rem_card.ui.styles.theme import (
    ANALYTICS_CHART_COLORS,
    STYLE_ANALYTICS_CHECKBOX,
    STYLE_ANALYTICS_CHECKBOX_CONTAINER,
    STYLE_ANALYTICS_CONTROL_FRAME,
    STYLE_ANALYTICS_DIALOG_CONTAINER,
    STYLE_ANALYTICS_GROUP_LABEL,
    STYLE_ANALYTICS_OPTION_BUTTON,
    STYLE_ANALYTICS_PREVIEW_BUTTON,
    STYLE_ANALYTICS_PRIMARY_BUTTON,
    STYLE_ANALYTICS_SCROLL_AREA,
    STYLE_ANALYTICS_TEXT_BROWSER,
    STYLE_ANALYTICS_TITLE,
    STYLE_DIALOG_CLOSE_BUTTON,
    STYLE_TRANSPARENT_WIDGET,
)


class GraphsDialog(SavedFramelessDialogMixin, QDialog):
    def __init__(self, db_manager, start_date_str, end_date_str, parent=None):
        super().__init__(parent)
        self.db_manager = db_manager
        self.start_date_str = start_date_str
        self.end_date_str = end_date_str

        self.setWindowTitle("Формирование графиков")
        self.setMinimumSize(760, 560)
        self.resize(1100, 850)

        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        self.setMouseTracking(True)
        self._init_saved_frameless_dialog("analytics/graphs_dialog_geometry", drag_area_height=80)

        self.chart_colors = list(ANALYTICS_CHART_COLORS)
        self._graphs_worker = None
        self._graphs_pdf_worker = None
        self._closing = False

        self._init_ui()
        self._restore_saved_geometry()

    def _init_ui(self):
        self.bg_container = QWidget(self)
        self.bg_container.setMouseTracking(True)
        self.bg_container.setObjectName("bg_container")
        self.bg_container.setStyleSheet(STYLE_ANALYTICS_DIALOG_CONTAINER)

        self.shadow = QGraphicsDropShadowEffect(self)
        self.shadow.setBlurRadius(30)
        self.shadow.setColor(QColor(0, 0, 0, 40))
        self.shadow.setOffset(0, 5)
        self.bg_container.setGraphicsEffect(self.shadow)

        self.layout_container = QVBoxLayout(self)
        self.layout_container.setContentsMargins(10, 10, 10, 10)
        self.layout_container.addWidget(self.bg_container)

        self.main_layout = QVBoxLayout(self.bg_container)
        self.main_layout.setContentsMargins(20, 10, 20, 20)
        self.main_layout.setSpacing(15)

        # Header panel
        self.header_panel = QWidget()
        self.header_panel.setFixedHeight(40)
        self.header_panel.setStyleSheet(STYLE_TRANSPARENT_WIDGET)
        self.header_panel_layout = QHBoxLayout(self.header_panel)
        self.header_panel_layout.setContentsMargins(10, 0, 0, 0)

        self.title_label = QLabel("ФОРМИРОВАНИЕ ГРАФИКОВ")
        self.title_label.setStyleSheet(STYLE_ANALYTICS_TITLE)
        self.header_panel_layout.addWidget(self.title_label)
        self.header_panel_layout.addStretch()

        self.close_button = QPushButton("×")
        self.close_button.setFixedSize(30, 30)
        self.close_button.setCursor(Qt.PointingHandCursor)
        self.close_button.setStyleSheet(STYLE_DIALOG_CLOSE_BUTTON)
        self.close_button.clicked.connect(self.reject)
        self.header_panel_layout.addWidget(self.close_button)
        self.main_layout.addWidget(self.header_panel)

        # Content Layout
        self.content_layout = QHBoxLayout()
        self.main_layout.addLayout(self.content_layout, 1)

        # Left Side - Options
        self.options_widget = QWidget()
        self.options_layout = QVBoxLayout(self.options_widget)
        self.options_layout.setContentsMargins(0, 0, 0, 0)
        self.options_layout.setSpacing(10)

        # Buttons to check/uncheck - more neat
        ctrl_frame = QFrame()
        ctrl_frame.setStyleSheet(STYLE_ANALYTICS_CONTROL_FRAME)
        ctrl_layout = QVBoxLayout(ctrl_frame)
        ctrl_layout.setSpacing(5)

        self.btn_select_all = QPushButton("Отметить все")
        self.btn_deselect_all = QPushButton("Снять отметки")
        self.btn_select_top = QPushButton("Наиболее информативные")

        for b in [self.btn_select_all, self.btn_deselect_all, self.btn_select_top]:
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(STYLE_ANALYTICS_OPTION_BUTTON)
            ctrl_layout.addWidget(b)

        self.btn_select_all.clicked.connect(self._select_all)
        self.btn_deselect_all.clicked.connect(self._deselect_all)
        self.btn_select_top.clicked.connect(self._select_top)

        self.options_layout.addWidget(ctrl_frame)

        # Checkboxes for graphs
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet(STYLE_ANALYTICS_SCROLL_AREA)

        self.checkboxes_container = QWidget()
        self.checkboxes_container.setStyleSheet(STYLE_ANALYTICS_CHECKBOX_CONTAINER)
        self.checkboxes_layout = QVBoxLayout(self.checkboxes_container)
        self.checkboxes_layout.setSpacing(8)

        self.graph_groups = {
            "Поток пациентов": {
                "g1": "1. Поступления пациентов по месяцам",
                "g2": "2. Поступления пациентов по дням недели",
                "g3": "3. Поступления пациентов по датам (динамика)",
                "g4": "4. Источники поступления пациентов (тип)",
                "g5": "5. Распределение по отделениям-источникам"
            },
            "Использование коечного фонда": {
                "g6": "6. Койко-дни по месяцам",
                "g7": "7. Загрузка коек по месяцам (%)",
                "g8": "8. Использование по номерам коек",
                "g9": "9. Оборот койки",
                "g10": "10. Среднесуточная занятость коек (чел.)",
                "g11": "11. Занятость коек по дням (столбчатый)",
                "g12": "12. Индекс интенсивности исп. к.ф.",
                "g13": "13. Индекс интенсивности по месяцам (%)"
            },
            "Пиковая нагрузка": {
                "g14": "14. Периоды повышенной загрузки (≥4)",
                "g15": "15. Длительность периодов пиковой загрузки",
                "g16": "16. Макс. число пациентов одновременно",
                "g17": "17. Доля времени повыш. загрузки",
                "g18": "18. Динамика одновременно находящихся"
            },
            "Демографическая структура": {
                "g19": "19. Возрастная структура пациентов",
                "g20": "20. Распределение пациентов по полу",
                "g21": "21. Возрастная структура умерших",
                "g22": "22. Возрастные группы"
            },
            "Диагностическая структура": {
                "g23": "23. Топ-10 диагнозов",
                "g24": "24. Структура диагнозов по классам МКБ-10",
                "g25": "25. Частота отдельных диагнозов",
                "g26": "26. Диагнозы у умерших пациентов",
                "g27": "27. Летальность по диагнозам"
            },
            "Исходы лечения": {
                "g28": "28. Распределение исходов лечения",
                "g29": "29. Летальность по месяцам",
                "g30": "30. Летальность по полу",
                "g31": "31. Летальность по возр. группам",
                "g32": "32. Летальность по источнику поступл."
            },
            "Длительность лечения": {
                "g33": "33. Распределение длительности пребывания",
                "g34": "34. Длительность пребывания по месяцам",
                "g35": "35. Доля пациентов LOS > 7 сут.",
                "g36": "36. Доля пациентов LOS > 14 сут."
            },
            "Анализ смертности": {
                "g37": "37. Время до смерти пациентов",
                "g38": "38. Структура летальности по срокам",
                "g39": "39. Доля ранней летальности",
                "g40": "40. Индекс тяжести поступающего потока",
                "g41": "41. Кривая выживаемости (Kaplan–Meier)"
            },
            "ИВЛ": {
                "g42": "42. Доля пациентов на ИВЛ",
                "g43": "43. Число эпизодов ИВЛ",
                "g44": "44. Длительность ИВЛ",
                "g45": "45. ИВЛ-дни по месяцам"
            },
            "Интенсивность": {
                 "g46": "46. Средняя интенсивность по месяцам (%)",
                 "g47": "47. Средняя интенсивность по дням недели",
                 "g48": "48. Максимальная одномоментная интенсивность",
                 "g49": "49. Средняя длительность (умершие vs выписанные)",
                 "g50": "50. Топ-5 диагнозов по длительности лечения"
            },
            "Использование коечного фонда 2": {
                 "g51": "51. Средняя загрузка коек по дням недели (%)",
                 "g52": "52. Пациенты по номерам коек (доп)",
                 "g53": "53. Динамика занятости коек (доп)",
                 "g54": "54. Средняя длительность (краткосрочные)",
                 "g55": "55. Средняя длительность (долгосрочные)"
            },
            "Операции и переливания": {
                "g56": "56. Количество операций по месяцам",
                "g57": "57. Типы проведенных операций",
                "g58": "58. Количество переливаний по месяцам",
                "g59": "59. Типы проведенных переливаний",
                "g60": "60. Длительность пребывания после операций"
            },
            "Другое": {
                "g61": "61. Распределение пациентов по отделениям",
                "g62": "62. Средняя длительность пребывания по отделениям",
                "g63": "63. Распределение длительности по отделениям",
                "g65": "65. Пациенты по времени суток поступления"
            }
        }

        # Наиболее информативные по ТЗ (можно скорректировать по необходимости)
        self.top_graphs = ["g1", "g4", "g7", "g10", "g8", "g18", "g14", "g19", "g20", "g23", "g24", "g28", "g29", "g33", "g38", "g42", "g44", "g56", "g58", "g62", "g65"]

        self.checkboxes = {}
        for group_name, graphs in self.graph_groups.items():
            group_lbl = QLabel(group_name.upper())
            group_lbl.setStyleSheet(STYLE_ANALYTICS_GROUP_LABEL)
            self.checkboxes_layout.addWidget(group_lbl)

            for key, name in graphs.items():
                cb = QCheckBox(name)
                cb.setChecked(False)
                cb.setStyleSheet(STYLE_ANALYTICS_CHECKBOX)
                self.checkboxes_layout.addWidget(cb)
                self.checkboxes[key] = cb

        self.checkboxes_layout.addStretch()
        self.scroll_area.setWidget(self.checkboxes_container)
        self.options_layout.addWidget(self.scroll_area, 1)

        self.preview_btn = QPushButton("ПРЕДПРОСМОТР ГРАФИКОВ")
        self.preview_btn.setCursor(Qt.PointingHandCursor)
        self.preview_btn.setFixedHeight(40)
        self.preview_btn.setStyleSheet(STYLE_ANALYTICS_PREVIEW_BUTTON)
        self.preview_btn.clicked.connect(self._on_preview_clicked)
        self.options_layout.addWidget(self.preview_btn)

        self.save_pdf_btn = QPushButton("СОХРАНИТЬ ОТДЕЛЬНО В PDF")
        self.save_pdf_btn.setCursor(Qt.PointingHandCursor)
        self.save_pdf_btn.setFixedHeight(45)
        self.save_pdf_btn.setStyleSheet(STYLE_ANALYTICS_PRIMARY_BUTTON)
        self.save_pdf_btn.clicked.connect(self._on_save_pdf_clicked)
        self.options_layout.addWidget(self.save_pdf_btn)

        self.content_layout.addWidget(self.options_widget, 1)

        # Right Side - Preview (TextBrowser to show generated images)
        self.report_text = QTextBrowser()
        self.report_text.setStyleSheet(STYLE_ANALYTICS_TEXT_BROWSER)
        self.content_layout.addWidget(self.report_text, 2)

    def _select_all(self):
        for cb in self.checkboxes.values(): cb.setChecked(True)

    def _deselect_all(self):
        for cb in self.checkboxes.values(): cb.setChecked(False)

    def _select_top(self):
        self._deselect_all()
        for key in self.top_graphs:
            if key in self.checkboxes:
                self.checkboxes[key].setChecked(True)

    def _on_preview_clicked(self):
        self._start_graphs_build(save_pdf=False)

    def _on_save_pdf_clicked(self):
        self._start_graphs_build(save_pdf=True)

    def _start_graphs_build(self, save_pdf=False):
        if self._graphs_worker is not None and self._graphs_worker.isRunning():
            return

        selected = [k for k, cb in self.checkboxes.items() if cb.isChecked()]
        if not selected:
            from rem_card.ui.shared.custom_message_box import CustomMessageBox
            CustomMessageBox.information(self, "Внимание", "Выберите хотя бы один график для формирования.")
            return

        self._set_graphs_busy(True, "Формирование графиков...")
        self._graphs_worker = AnalyticsWorker(
            lambda: build_graphs_html(
                self.db_manager,
                self.start_date_str,
                self.end_date_str,
                selected,
                self.chart_colors,
            ),
            parent=self,
        )
        self._graphs_worker.completed.connect(lambda result: self._on_graphs_ready(result, save_pdf))
        self._graphs_worker.failed.connect(self._on_graphs_failed)
        self._graphs_worker.finished.connect(self._clear_graphs_worker)
        self._graphs_worker.start()

    def _on_graphs_ready(self, result, save_pdf: bool):
        if self._closing:
            return
        html = getattr(result, "html", "")
        self.report_text.setHtml(html)
        if save_pdf:
            self._start_graphs_pdf_worker(html)
            return
        self._set_graphs_busy(False)

    def _start_graphs_pdf_worker(self, html: str):
        if self._closing:
            return
        if self._graphs_pdf_worker is not None and self._graphs_pdf_worker.isRunning():
            return
        from rem_card.app.paths import REPORT_DIR

        os.makedirs(REPORT_DIR, exist_ok=True)
        filename = f"graphs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        pdf_path = os.path.join(REPORT_DIR, filename)
        self.report_text.setHtml(html)
        self._graphs_pdf_worker = HtmlPdfWorker(wrap_graphs_pdf_html(html), pdf_path, parent=self)
        self._graphs_pdf_worker.completed.connect(self._on_graphs_pdf_ready)
        self._graphs_pdf_worker.failed.connect(self._on_graphs_pdf_failed)
        self._graphs_pdf_worker.finished.connect(self._clear_graphs_pdf_worker)
        self._graphs_pdf_worker.start()

    def _on_graphs_pdf_ready(self, pdf_path: str):
        if self._closing:
            return
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        self._set_graphs_busy(False)
        CustomMessageBox.information(self, "Успех", f"Графики успешно сохранены:\n{os.path.basename(pdf_path)}")

    def _on_graphs_pdf_failed(self, message: str):
        if self._closing:
            return
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        self._set_graphs_busy(False)
        CustomMessageBox.information(self, "Ошибка", f"Ошибка при сохранении PDF:\n{message}")

    def _on_graphs_failed(self, message: str):
        if self._closing:
            return
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        self._set_graphs_busy(False)
        CustomMessageBox.information(self, "Ошибка", f"Ошибка при формировании графиков:\n{message}")

    def _set_graphs_busy(self, busy: bool, text: str = ""):
        self.preview_btn.setEnabled(not busy)
        self.save_pdf_btn.setEnabled(not busy)
        if text:
            self.report_text.setHtml(f"<p>{text}</p>")

    def _clear_graphs_worker(self):
        self._graphs_worker = None

    def _clear_graphs_pdf_worker(self):
        self._graphs_pdf_worker = None

    def _cancel_workers(self):
        self._closing = True
        for worker in (self._graphs_worker, self._graphs_pdf_worker):
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
