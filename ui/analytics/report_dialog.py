import os
from datetime import datetime
from html import escape

from PySide6.QtCore import QDate, QPoint, Qt, QMarginsF
from PySide6.QtGui import QPageLayout, QPageSize, QPdfWriter, QTextDocument
from PySide6.QtWidgets import QDateEdit, QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

class ReportDialog(QDialog):
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

        self.setWindowTitle("Отчеты и статистика")
        self.resize(500, 400)

        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)

        self.bg_color = "#f5f2e9"
        self.border_color = "#d1d1bc"
        self.accent_color = "#8a8a68"

        self._drag_pos = QPoint()

        self._init_ui()

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
        self.container.setStyleSheet(f"""
            QWidget#container {{
                background-color: {self.bg_color};
                border: 2px solid {self.border_color};
                border-radius: 15px;
            }}
        """)
        self.container_layout = QVBoxLayout(self.container)
        self.container_layout.setContentsMargins(30, 20, 30, 30)
        self.container_layout.setSpacing(20)

        self.main_layout.addWidget(self.container)

        # Header
        header_layout = QHBoxLayout()
        title_label = QLabel("ОТЧЕТНЫЙ ПЕРИОД")
        title_label.setStyleSheet("color: #4a4a3a; font-weight: 800; font-size: 13px; letter-spacing: 1px;")
        header_layout.addWidget(title_label)
        header_layout.addStretch()

        self.close_btn = QPushButton("×")
        self.close_btn.setFixedSize(30, 30)
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.setStyleSheet("""
            QPushButton { background: transparent; color: #7a7a6a; font-size: 22px; border: none; }
            QPushButton:hover { background: #ef4444; color: white; border-radius: 5px; }
        """)
        self.close_btn.clicked.connect(self.reject)
        header_layout.addWidget(self.close_btn)
        self.container_layout.addLayout(header_layout)

        # Date Selection
        dates_frame = QFrame()
        dates_frame.setStyleSheet(f"background: #fdfdfa; border: 1px solid {self.border_color}; border-radius: 10px;")
        dates_layout = QVBoxLayout(dates_frame)
        dates_layout.setContentsMargins(20, 20, 20, 20)
        dates_layout.setSpacing(15)

        # Start Date
        start_layout = QHBoxLayout()
        start_label = QLabel("Начало периода:")
        start_label.setStyleSheet("border: none; font-weight: 600; color: #5d5d4a;")
        self.start_date = QDateEdit()
        self.start_date.setCalendarPopup(True)
        self.start_date.setDate(
            self._parse_period_qdate(self._prefill_start_dt) or QDate.currentDate().addMonths(-1)
        )
        self.start_date.setStyleSheet("padding: 5px; border: 1px solid #c9c9b4; border-radius: 4px;")
        start_layout.addWidget(start_label)
        start_layout.addWidget(self.start_date)
        dates_layout.addLayout(start_layout)

        # End Date
        end_layout = QHBoxLayout()
        end_label = QLabel("Конец периода:")
        end_label.setStyleSheet("border: none; font-weight: 600; color: #5d5d4a;")
        self.end_date = QDateEdit()
        self.end_date.setCalendarPopup(True)
        self.end_date.setDate(
            self._parse_period_qdate(self._prefill_end_dt) or QDate.currentDate()
        )
        self.end_date.setStyleSheet("padding: 5px; border: 1px solid #c9c9b4; border-radius: 4px;")
        end_layout.addWidget(end_label)
        end_layout.addWidget(self.end_date)
        dates_layout.addLayout(end_layout)

        self.container_layout.addWidget(dates_frame)

        # Actions
        self.excel_btn = QPushButton("СФОРМИРОВАТЬ ГРАФИКИ")
        self.excel_btn.setFixedHeight(50)
        self.excel_btn.setCursor(Qt.PointingHandCursor)
        self.excel_btn.setStyleSheet(f"""
            QPushButton {{ background: {self.accent_color}; color: white; font-weight: 800; font-size: 12px; border-radius: 8px; }}
            QPushButton:hover {{ background: #707054; }}
        """)
        self.excel_btn.clicked.connect(self._open_graphs_dialog)
        self.container_layout.addWidget(self.excel_btn)
        self.excel_btn.setVisible(self._show_graph_button)

        self.pdf_btn = QPushButton("СОХРАНИТЬ ПОЛНЫЙ ОТЧЕТ (PDF)")
        self.pdf_btn.setFixedHeight(50)
        self.pdf_btn.setCursor(Qt.PointingHandCursor)
        self.pdf_btn.setStyleSheet(f"""
            QPushButton {{ background: white; color: {self.accent_color}; border: 2px solid {self.accent_color}; font-weight: 800; font-size: 12px; border-radius: 8px; }}
            QPushButton:hover {{ background: #f0f0e0; }}
        """)
        self.pdf_btn.clicked.connect(self._generate_pdf_report)
        self.container_layout.addWidget(self.pdf_btn)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint()

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            delta = event.globalPosition().toPoint() - self._drag_pos
            self.move(self.x() + delta.x(), self.y() + delta.y())
            self._drag_pos = event.globalPosition().toPoint()

    def _open_graphs_dialog(self):
        start_dt = self.start_date.date().toString("yyyy-MM-dd 00:00:00")
        end_dt = self.end_date.date().toString("yyyy-MM-dd 23:59:59")

        from rem_card.ui.analytics.graphs_dialog import GraphsDialog
        dialog = GraphsDialog(self.db_manager, start_dt, end_dt, self)
        dialog.exec()

    def _generate_pdf_report(self):
        try:
            from rem_card.ui.shared.custom_message_box import CustomMessageBox

            if self.start_date.date() > self.end_date.date():
                CustomMessageBox.information(self, "Внимание", "Дата начала периода не может быть позже даты окончания.")
                return

            start_dt = self.start_date.date().toString("yyyy-MM-dd 00:00:00")
            end_dt = self.end_date.date().toString("yyyy-MM-dd 23:59:59")

            html = self._build_statistical_report_html(start_dt, end_dt)
            pdf_path = self._save_html_report_to_pdf(html)
            filename = os.path.basename(pdf_path)
            CustomMessageBox.information(self, "Успех", f"Статистический отчет успешно сохранен:\n{filename}")
        except Exception as e:
            from rem_card.ui.shared.custom_message_box import CustomMessageBox
            CustomMessageBox.information(self, "Ошибка PDF", f"Не удалось сохранить PDF:\n{str(e)}")

    def _build_statistical_report_html(self, start_dt: str, end_dt: str) -> str:
        conn = self.db_manager.get_connection()
        cursor = conn.cursor()
        period_params = (start_dt, end_dt)

        def _scalar(query: str, params: tuple = period_params):
            cursor.execute(query, params)
            row = cursor.fetchone()
            if not row or row[0] is None:
                return 0
            return row[0]

        total_admissions = int(
            _scalar("SELECT COUNT(*) FROM admissions WHERE admission_datetime BETWEEN ? AND ?", period_params)
        )
        in_department = int(
            _scalar(
                """
                SELECT COUNT(*)
                FROM admissions
                WHERE admission_datetime BETWEEN ? AND ?
                  AND (outcome IS NULL OR TRIM(outcome) = '')
                """,
                period_params,
            )
        )
        transferred = int(
            _scalar(
                """
                SELECT COUNT(*)
                FROM admissions
                WHERE admission_datetime BETWEEN ? AND ?
                  AND lower(TRIM(COALESCE(outcome, ''))) = 'переведен'
                """,
                period_params,
            )
        )
        deaths = int(
            _scalar(
                """
                SELECT COUNT(*)
                FROM admissions
                WHERE admission_datetime BETWEEN ? AND ?
                  AND lower(TRIM(COALESCE(outcome, ''))) = 'умер'
                """,
                period_params,
            )
        )

        bed_days = float(
            _scalar(
                """
                SELECT COALESCE(
                    SUM(
                        MAX(
                            0,
                            julianday(
                                CASE
                                    WHEN death_datetime IS NOT NULL AND death_datetime < ? THEN death_datetime
                                    WHEN transfer_datetime IS NOT NULL AND transfer_datetime < ? THEN transfer_datetime
                                    ELSE ?
                                END
                            ) - julianday(admission_datetime)
                        )
                    ),
                    0
                )
                FROM admissions
                WHERE admission_datetime BETWEEN ? AND ?
                """,
                (end_dt, end_dt, end_dt, start_dt, end_dt),
            )
        )
        avg_stay = float(
            _scalar(
                """
                SELECT COALESCE(
                    AVG(
                        MAX(
                            0,
                            julianday(
                                CASE
                                    WHEN death_datetime IS NOT NULL AND death_datetime < ? THEN death_datetime
                                    WHEN transfer_datetime IS NOT NULL AND transfer_datetime < ? THEN transfer_datetime
                                    ELSE ?
                                END
                            ) - julianday(admission_datetime)
                        )
                    ),
                    0
                )
                FROM admissions
                WHERE admission_datetime BETWEEN ? AND ?
                """,
                (end_dt, end_dt, end_dt, start_dt, end_dt),
            )
        )

        operations_count = int(_scalar("SELECT COUNT(*) FROM operations WHERE operation_datetime BETWEEN ? AND ?", period_params))
        cursor.execute(
            "SELECT COUNT(*), COALESCE(SUM(volume_ml), 0) FROM transfusions WHERE datetime BETWEEN ? AND ?",
            period_params,
        )
        transfusions_row = cursor.fetchone() or (0, 0)
        transfusions_count = int(transfusions_row[0] or 0)
        transfusions_volume_ml = int(transfusions_row[1] or 0)

        cursor.execute(
            """
            SELECT
                COUNT(*) AS ivl_count,
                COALESCE(
                    SUM(
                        MAX(
                            0,
                            (julianday(CASE WHEN end_time IS NOT NULL AND end_time < ? THEN end_time ELSE ? END) - julianday(start_time)) * 24.0
                        )
                    ),
                    0
                ) AS ivl_hours
            FROM ivl_episodes
            WHERE start_time BETWEEN ? AND ?
            """,
            (end_dt, end_dt, start_dt, end_dt),
        )
        ivl_row = cursor.fetchone() or (0, 0)
        ivl_count = int(ivl_row[0] or 0)
        ivl_hours = float(ivl_row[1] or 0.0)

        cursor.execute(
            """
            SELECT COALESCE(NULLIF(TRIM(patient_gender), ''), 'Не указано') AS gender, COUNT(*) AS count
            FROM admissions
            WHERE admission_datetime BETWEEN ? AND ?
            GROUP BY COALESCE(NULLIF(TRIM(patient_gender), ''), 'Не указано')
            ORDER BY count DESC, gender
            """,
            period_params,
        )
        gender_rows = cursor.fetchall()

        cursor.execute(
            """
            SELECT COALESCE(NULLIF(TRIM(source_department), ''), 'Не указано') AS source, COUNT(*) AS count
            FROM admissions
            WHERE admission_datetime BETWEEN ? AND ?
            GROUP BY COALESCE(NULLIF(TRIM(source_department), ''), 'Не указано')
            ORDER BY count DESC, source
            """,
            period_params,
        )
        source_rows = cursor.fetchall()

        cursor.execute(
            """
            SELECT
                COALESCE(NULLIF(TRIM(diagnosis_code), ''), '—') AS code,
                COALESCE(NULLIF(TRIM(diagnosis_text), ''), 'Без уточнения') AS diagnosis,
                COUNT(*) AS count
            FROM admissions
            WHERE admission_datetime BETWEEN ? AND ?
            GROUP BY
                COALESCE(NULLIF(TRIM(diagnosis_code), ''), '—'),
                COALESCE(NULLIF(TRIM(diagnosis_text), ''), 'Без уточнения')
            ORDER BY count DESC, code
            LIMIT 12
            """,
            period_params,
        )
        diagnosis_rows = cursor.fetchall()

        period_days = max(1, self.start_date.date().daysTo(self.end_date.date()) + 1)
        NUM_BEDS = int(os.environ.get("REMCARD_NUM_BEDS", "12"))

        bed_capacity_days = NUM_BEDS * period_days
        occupancy = (bed_days / bed_capacity_days * 100.0) if bed_capacity_days else 0.0
        mortality = (deaths / total_admissions * 100.0) if total_admissions else 0.0

        def _distribution_rows(rows):
            if not rows:
                return "<tr><td colspan='2'>Нет данных</td></tr>"
            return "".join(
                f"<tr><td>{escape(str(r[0]))}</td><td class='num'>{int(r[1] or 0)}</td></tr>"
                for r in rows
            )

        if diagnosis_rows:
            diagnosis_html = "".join(
                f"<tr><td>{escape(str(r[0]))}</td><td>{escape(str(r[1]))}</td><td class='num'>{int(r[2] or 0)}</td></tr>"
                for r in diagnosis_rows
            )
        else:
            diagnosis_html = "<tr><td colspan='3'>Нет данных</td></tr>"

        start_label = self.start_date.date().toString("dd.MM.yyyy")
        end_label = self.end_date.date().toString("dd.MM.yyyy")

        return f"""
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: 'Arial', sans-serif; color: #2d2d24; margin: 0; padding: 0; }}
                .page {{ padding: 24px 28px; }}
                h1 {{ margin: 0 0 6px 0; font-size: 20px; color: #4a4a3a; }}
                h2 {{ margin: 18px 0 8px 0; font-size: 14px; color: #6b6b52; text-transform: uppercase; }}
                .period {{ margin: 0 0 10px 0; color: #5d5d4a; }}
                table {{ width: 100%; border-collapse: collapse; margin-bottom: 12px; }}
                th, td {{ border: 1px solid #d9d9c8; padding: 6px 8px; text-align: left; font-size: 12px; }}
                th {{ background: #f0f0e0; color: #4a4a3a; font-weight: 700; }}
                .num {{ text-align: right; }}
                .footnote {{ margin-top: 12px; color: #6d6d58; font-size: 11px; }}
            </style>
        </head>
        <body>
            <div class="page">
                <h1>Статистический отчет ОАР №3</h1>
                <p class="period">Период: {start_label} - {end_label}</p>

                <h2>Ключевые показатели</h2>
                <table>
                    <tr><th>Показатель</th><th class="num">Значение</th></tr>
                    <tr><td>Поступило пациентов</td><td class="num">{total_admissions}</td></tr>
                    <tr><td>Находятся в отделении</td><td class="num">{in_department}</td></tr>
                    <tr><td>Переведено</td><td class="num">{transferred}</td></tr>
                    <tr><td>Умерло</td><td class="num">{deaths}</td></tr>
                    <tr><td>Летальность, %</td><td class="num">{mortality:.1f}</td></tr>
                    <tr><td>Койко-дни</td><td class="num">{bed_days:.1f}</td></tr>
                    <tr><td>Средняя длительность лечения, дней</td><td class="num">{avg_stay:.2f}</td></tr>
                    <tr><td>Занятость коечного фонда, %</td><td class="num">{occupancy:.1f}</td></tr>
                    <tr><td>Операций выполнено</td><td class="num">{operations_count}</td></tr>
                    <tr><td>Трансфузий выполнено</td><td class="num">{transfusions_count}</td></tr>
                    <tr><td>Перелито компонентов крови, мл</td><td class="num">{transfusions_volume_ml}</td></tr>
                    <tr><td>Эпизодов ИВЛ</td><td class="num">{ivl_count}</td></tr>
                    <tr><td>Суммарная длительность ИВЛ, часов</td><td class="num">{ivl_hours:.1f}</td></tr>
                </table>

                <h2>Распределение по полу</h2>
                <table>
                    <tr><th>Пол</th><th class="num">Количество</th></tr>
                    {_distribution_rows(gender_rows)}
                </table>

                <h2>Источники поступления</h2>
                <table>
                    <tr><th>Источник</th><th class="num">Количество</th></tr>
                    {_distribution_rows(source_rows)}
                </table>

                <h2>Топ диагнозов</h2>
                <table>
                    <tr><th>Код МКБ</th><th>Диагноз</th><th class="num">Случаев</th></tr>
                    {diagnosis_html}
                </table>

                <p class="footnote">Отчет сформирован автоматически: {datetime.now().strftime("%d.%m.%Y %H:%M:%S")}</p>
            </div>
        </body>
        </html>
        """

    def _save_html_report_to_pdf(self, html: str) -> str:
        from rem_card.app.paths import REPORT_DIR

        os.makedirs(REPORT_DIR, exist_ok=True)
        filename = f"statistics_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        pdf_path = os.path.join(REPORT_DIR, filename)

        document = QTextDocument()
        document.setHtml(html)

        writer = QPdfWriter(pdf_path)
        writer.setPageLayout(QPageLayout(QPageSize(QPageSize.A4), QPageLayout.Portrait, QMarginsF(15, 15, 15, 15)))

        if hasattr(document, "print_"):
            document.print_(writer)
        else:
            document.print(writer)

        return pdf_path
