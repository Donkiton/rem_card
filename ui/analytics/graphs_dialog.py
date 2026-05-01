import os
from datetime import datetime
try:
    import pandas as pd
    import matplotlib.pyplot as plt
    try:
        import seaborn as sns
    except ImportError:
        sns = None
except ImportError:
    pd = None
    plt = None
    sns = None
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QWidget, QGraphicsDropShadowEffect, QScrollArea, QCheckBox, QTextBrowser, QFrame)
from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QColor

from rem_card.ui.analytics.graphs_generators_1 import generate_g1_g5, generate_g6_g13, generate_g14_g18, generate_g19_g22
from rem_card.ui.analytics.graphs_generators_2 import generate_g23_g30, generate_g31_g35, generate_g36_g40, generate_g41_g45
from rem_card.ui.analytics.graphs_generators_3 import generate_g46_g50, generate_g51_g55, generate_g56_g60, generate_g61_g65


class GraphsDialog(QDialog):
    def __init__(self, db_manager, start_date_str, end_date_str, parent=None):
        super().__init__(parent)
        self.db_manager = db_manager
        self.start_date_str = start_date_str
        self.end_date_str = end_date_str

        self.setWindowTitle("Формирование графиков")
        self.resize(1100, 850)

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

        # Настраиваем стиль графиков: seaborn (если доступен) или fallback.
        if plt:
            if sns:
                sns.set_theme(
                    style="whitegrid",
                    context="talk",
                    font_scale=0.72,
                    rc={
                        "figure.facecolor": "#ffffff",
                        "axes.facecolor": "#fcfcfa",
                        "axes.edgecolor": "#d4d4c4",
                        "grid.color": "#e6e6dc",
                        "grid.linestyle": "-",
                        "grid.linewidth": 0.8,
                        "axes.titleweight": "bold",
                        "axes.labelcolor": "#2d2d24",
                        "xtick.color": "#3c3c34",
                        "ytick.color": "#3c3c34",
                    },
                )
                self.chart_colors = sns.color_palette("Set2", 10).as_hex()
            else:
                plt.style.use('ggplot')
                self.chart_colors = ['#8a8a68', '#d97706', '#c0504d', '#5b9bd5', '#71a95a', '#705470', '#eeb211', '#4b5563', '#10b981', '#f43f5e']
        else:
            self.chart_colors = ['#8a8a68', '#d97706', '#c0504d', '#5b9bd5', '#71a95a', '#705470', '#eeb211', '#4b5563', '#10b981', '#f43f5e']

        self._init_ui()

    def _init_ui(self):
        self.bg_container = QWidget(self)
        self.bg_container.setMouseTracking(True)
        self.bg_container.setStyleSheet(f"""
            QWidget#bg_container {{
                background-color: {self.bg_color};
                border: 2px solid {self.border_color};
                border-radius: 15px;
            }}
        """)
        self.bg_container.setObjectName("bg_container")

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
        self.header_panel.setStyleSheet("background: transparent;")
        self.header_panel_layout = QHBoxLayout(self.header_panel)
        self.header_panel_layout.setContentsMargins(10, 0, 0, 0)

        self.title_label = QLabel("ФОРМИРОВАНИЕ ГРАФИКОВ")
        self.title_label.setStyleSheet("color: #4a4a3a; font-size: 13px; font-weight: 800; letter-spacing: 1px; background: transparent;")
        self.header_panel_layout.addWidget(self.title_label)
        self.header_panel_layout.addStretch()

        self.close_button = QPushButton("×")
        self.close_button.setFixedSize(30, 30)
        self.close_button.setCursor(Qt.PointingHandCursor)
        self.close_button.setStyleSheet("""
            QPushButton { background: transparent; color: #7a7a6a; font-size: 22px; border: none; }
            QPushButton:hover { background: #ef4444; color: white; border-radius: 5px; }
        """)
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
        ctrl_frame.setStyleSheet(f"QFrame {{ background: #fdfdfa; border: 1px solid {self.border_color}; border-radius: 8px; }}")
        ctrl_layout = QVBoxLayout(ctrl_frame)
        ctrl_layout.setSpacing(5)

        self.btn_select_all = QPushButton("Отметить все")
        self.btn_deselect_all = QPushButton("Снять отметки")
        self.btn_select_top = QPushButton("Наиболее информативные")

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
        for b in [self.btn_select_all, self.btn_deselect_all, self.btn_select_top]:
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(btn_style)
            ctrl_layout.addWidget(b)

        self.btn_select_all.clicked.connect(self._select_all)
        self.btn_deselect_all.clicked.connect(self._deselect_all)
        self.btn_select_top.clicked.connect(self._select_top)

        self.options_layout.addWidget(ctrl_frame)

        # Checkboxes for graphs
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet(f"""
            QScrollArea {{ background: transparent; border: 1px solid {self.border_color}; border-radius: 8px; }}
            QScrollBar:vertical {{ border: none; background: #fdfdfa; width: 10px; margin: 0px 0px 0px 0px; }}
            QScrollBar::handle:vertical {{ background: #c9c9b4; min-height: 20px; border-radius: 5px; }}
        """)

        self.checkboxes_container = QWidget()
        self.checkboxes_container.setStyleSheet("background: #fdfdfa;")
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
            group_lbl.setStyleSheet(f"color: {self.accent_color}; font-weight: 800; font-size: 10px; margin-top: 10px; border-bottom: 1px solid {self.border_color};")
            self.checkboxes_layout.addWidget(group_lbl)

            for key, name in graphs.items():
                cb = QCheckBox(name)
                cb.setChecked(False)
                cb.setStyleSheet(f"QCheckBox {{ color: {self.text_color}; font-weight: 500; font-size: 11px; background: transparent; }}")
                self.checkboxes_layout.addWidget(cb)
                self.checkboxes[key] = cb

        self.checkboxes_layout.addStretch()
        self.scroll_area.setWidget(self.checkboxes_container)
        self.options_layout.addWidget(self.scroll_area, 1)

        self.preview_btn = QPushButton("ПРЕДПРОСМОТР ГРАФИКОВ")
        self.preview_btn.setCursor(Qt.PointingHandCursor)
        self.preview_btn.setFixedHeight(40)
        self.preview_btn.setStyleSheet(f"""
            QPushButton {{ background-color: #fdfdfa; color: {self.accent_color}; border: 2px solid {self.accent_color}; border-radius: 6px; font-weight: 800; font-size: 11px; }}
            QPushButton:hover {{ background-color: #e8e4d5; }}
        """)
        self.preview_btn.clicked.connect(self._on_preview_clicked)
        self.options_layout.addWidget(self.preview_btn)

        self.save_pdf_btn = QPushButton("СОХРАНИТЬ ОТДЕЛЬНО В PDF")
        self.save_pdf_btn.setCursor(Qt.PointingHandCursor)
        self.save_pdf_btn.setFixedHeight(45)
        self.save_pdf_btn.setStyleSheet(f"""
            QPushButton {{ background-color: {self.accent_color}; color: white; border: none; border-radius: 6px; font-weight: 800; font-size: 12px; }}
            QPushButton:hover {{ background-color: #707054; }}
        """)
        self.save_pdf_btn.clicked.connect(self._on_save_pdf_clicked)
        self.options_layout.addWidget(self.save_pdf_btn)

        self.content_layout.addWidget(self.options_widget, 1)

        # Right Side - Preview (TextBrowser to show generated images)
        self.report_text = QTextBrowser()
        self.report_text.setStyleSheet(f"""
            QTextBrowser {{ background-color: #fdfdfa; border: 1px solid {self.border_color}; border-radius: 8px; padding: 10px; }}
        """)
        self.content_layout.addWidget(self.report_text, 2)

    def _get_resize_edges(self, pos):
        edges = Qt.Edge(0)
        if pos.x() < self._margin: edges |= Qt.LeftEdge
        if pos.x() > self.width() - self._margin: edges |= Qt.RightEdge
        if pos.y() < self._margin: edges |= Qt.TopEdge
        if pos.y() > self.height() - self._margin: edges |= Qt.BottomEdge
        return edges

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            edges = self._get_resize_edges(event.pos())
            if edges:
                self._resizing = True
                self.windowHandle().startSystemResize(edges)
            elif self.childAt(event.pos()) in [self.bg_container, self.header_panel, self.title_label, None]:
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
        if edges == (Qt.LeftEdge | Qt.TopEdge) or edges == (Qt.RightEdge | Qt.BottomEdge): self.setCursor(Qt.SizeFDiagCursor)
        elif edges == (Qt.RightEdge | Qt.TopEdge) or edges == (Qt.LeftEdge | Qt.BottomEdge): self.setCursor(Qt.SizeBDiagCursor)
        elif edges & (Qt.LeftEdge | Qt.RightEdge): self.setCursor(Qt.SizeHorCursor)
        elif edges & (Qt.TopEdge | Qt.BottomEdge): self.setCursor(Qt.SizeVerCursor)
        else: self.setCursor(Qt.ArrowCursor)

        if event.buttons() & Qt.LeftButton and getattr(self, '_dragging', False) and not self._resizing:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(event)

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
        html = self._generate_graphs(save_pdf=False)
        self.report_text.setHtml(html)

    def _on_save_pdf_clicked(self):
        html = self._generate_graphs(save_pdf=True)
        if html:
            self.report_text.setHtml(html)

    def _generate_graphs(self, save_pdf=False):
        if not pd or not plt:
            from rem_card.ui.shared.custom_message_box import CustomMessageBox
            CustomMessageBox.information(self, "Ошибка", "Библиотеки pandas или matplotlib не установлены.")
            return ""

        # Если ничего не выбрано
        selected = [k for k, cb in self.checkboxes.items() if cb.isChecked()]
        if not selected:
            from rem_card.ui.shared.custom_message_box import CustomMessageBox
            CustomMessageBox.information(self, "Внимание", "Выберите хотя бы один график для формирования.")
            return ""

        conn = self.db_manager.get_connection()
        params = (self.start_date_str, self.end_date_str)

        img_paths = []
        html_content = f"<h2>Графический отчет ОАР №3</h2><p>Период: {self.start_date_str.split(' ')[0]} - {self.end_date_str.split(' ')[0]}</p>"

        try:
            # Пре-загрузка данных для сложных расчетов
            cursor = conn.cursor()
            cursor.execute("SELECT id, patient_id, admission_datetime, transfer_datetime, death_datetime, outcome, patient_age, patient_age_unit, patient_gender, source_department, diagnosis_code, diagnosis_text FROM admissions WHERE admission_datetime BETWEEN ? AND ?", params)
            adms = [dict(zip([column[0] for column in cursor.description], row)) for row in cursor.fetchall()]

            # Вызов функций генерации из модулей
            # Модуль 1
            html_content = generate_g1_g5(selected, conn, params, self.chart_colors, img_paths, html_content)
            html_content = generate_g6_g13(selected, conn, params, self.chart_colors, img_paths, adms, self.start_date_str, self.end_date_str, html_content)
            html_content = generate_g14_g18(selected, conn, params, self.chart_colors, img_paths, adms, self.start_date_str, self.end_date_str, html_content)
            html_content = generate_g19_g22(selected, conn, params, self.chart_colors, img_paths, adms, html_content)

            # Модуль 2
            html_content = generate_g23_g30(selected, conn, params, self.chart_colors, img_paths, html_content)
            html_content = generate_g31_g35(selected, conn, params, self.chart_colors, img_paths, html_content)
            html_content = generate_g36_g40(selected, conn, params, self.chart_colors, img_paths, adms, html_content)
            html_content = generate_g41_g45(selected, conn, params, self.chart_colors, img_paths, html_content)

            # Модуль 3
            html_content = generate_g46_g50(selected, conn, params, self.chart_colors, img_paths, adms, html_content)
            html_content = generate_g51_g55(selected, conn, params, self.chart_colors, img_paths, adms, self.start_date_str, self.end_date_str, html_content)
            html_content = generate_g56_g60(selected, conn, params, self.chart_colors, img_paths, html_content)
            html_content = generate_g61_g65(selected, conn, params, self.chart_colors, img_paths, html_content)


            if save_pdf:
                from rem_card.app.paths import REPORT_DIR
                reports_dir = REPORT_DIR
                if not os.path.exists(reports_dir):
                    os.makedirs(reports_dir)

                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"graphs_{timestamp}.pdf"
                pdf_path = os.path.join(reports_dir, filename)

                from PySide6.QtGui import QTextDocument, QPdfWriter, QPageLayout, QPageSize
                from PySide6.QtCore import QMarginsF

                styled_html = f"""
                <html>
                <head>
                    <meta charset="UTF-8">
                    <style>
                        body {{
                            font-family: 'Arial', sans-serif;
                            color: #222222;
                            background: #ffffff;
                            margin: 0;
                            padding: 16px 18px;
                            text-align: center;
                        }}
                        h2 {{ color: #333333; margin-bottom: 4px; }}
                        h3 {{ color: #50503f; margin-top: 26px; margin-bottom: 8px; font-size: 14px; }}
                        div {{ page-break-inside: avoid; }}
                        img {{
                            max-width: 100%;
                            height: auto;
                            border: 1px solid #e3e3d7;
                            border-radius: 4px;
                            padding: 3px;
                            background: #ffffff;
                        }}
                    </style>
                </head>
                <body>
                    {html_content}
                </body>
                </html>
                """
                doc = QTextDocument()
                doc.setHtml(styled_html)

                writer = QPdfWriter(pdf_path)
                writer.setPageLayout(QPageLayout(QPageSize(QPageSize.A4), QPageLayout.Portrait, QMarginsF(15, 15, 15, 15)))

                if hasattr(doc, 'print_'): doc.print_(writer)
                else: doc.print(writer)

                from rem_card.ui.shared.custom_message_box import CustomMessageBox
                CustomMessageBox.information(self, "Успех", f"Графики успешно сохранены:\n{filename}")
                return html_content

        except Exception as e:
            from rem_card.ui.shared.custom_message_box import CustomMessageBox
            CustomMessageBox.information(self, "Ошибка", f"Ошибка при формировании графиков:\n{str(e)}")
            import traceback
            traceback.print_exc()
            return ""

        return html_content
