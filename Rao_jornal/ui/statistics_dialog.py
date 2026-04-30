import os
from datetime import datetime, timedelta
from html import escape
from statistics import median

from PySide6.QtCore import QMarginsF, QPoint, Qt
from PySide6.QtGui import QColor, QPageLayout, QPageSize, QPdfWriter, QTextDocument
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


class StatisticsDialog(QDialog):
    def __init__(self, db_manager, start_date_str: str, end_date_str: str, parent=None):
        super().__init__(parent)
        self.db_manager = db_manager

        self._start_dt = self._parse_datetime(start_date_str) or (datetime.now() - timedelta(days=30))
        self._end_dt = self._parse_datetime(end_date_str) or datetime.now()
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

        self.section_groups = {
            "Основная деятельность": {
                "s1": "1. Общая деятельность отделения",
                "s2": "2. Использование коечного фонда",
                "s3": "3. Демография",
                "s4": "4. Поток пациентов",
                "s5": "5. Диагностическая структура",
                "s6": "6. Исходы лечения",
                "s7": "7. Время до смерти",
                "s8": "8. Летальность по группам",
            },
            "Интенсивная терапия и вмешательства": {
                "s9": "9. ИВЛ",
                "s10": "10. Операции",
                "s11": "11. Переливания",
                "s16": "16. Индексы интенсивности",
                "s17": "17. Индексы нагрузки",
                "s18": "18. Специальные показатели",
                "s19": "19. Нагрузка персонала",
                "sx": "➕ Дополнительные показатели",
            },
        }
        self.top_sections = ["s1", "s2", "s6", "s7", "s9", "s10", "s11", "s18", "s19", "sx"]
        self.checkboxes = {}

        self._init_ui()

    @staticmethod
    def _parse_datetime(value):
        if isinstance(value, datetime):
            return value
        text = str(value or "").strip()
        if not text:
            return None
        text = text.replace("T", " ")
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

    @staticmethod
    def _safe_float(value, default=0.0):
        try:
            if value is None:
                return float(default)
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    @staticmethod
    def _safe_int(value, default=0):
        try:
            if value is None:
                return int(default)
            return int(value)
        except (TypeError, ValueError):
            return int(default)

    @staticmethod
    def _safe_div(num: float, den: float) -> float:
        if not den:
            return 0.0
        return float(num) / float(den)

    @staticmethod
    def _pct(num: float, den: float) -> float:
        if not den:
            return 0.0
        return (float(num) / float(den)) * 100.0

    @staticmethod
    def _fmt_num(value, digits=2):
        if value is None:
            return "н/д"
        if isinstance(value, int):
            return str(value)
        return f"{float(value):.{digits}f}"

    @staticmethod
    def _fmt_pct(num: float, den: float, digits=1):
        return f"{StatisticsDialog._pct(num, den):.{digits}f}%"

    @staticmethod
    def _normalize_text(value, fallback="Не указано"):
        text = str(value or "").strip()
        return text if text else fallback

    @staticmethod
    def _normalize_outcome(raw_outcome: str, transfer_dt, death_dt) -> str:
        text = str(raw_outcome or "").strip().lower()
        if death_dt is not None or text == "умер":
            return "умер"
        if transfer_dt is not None or text == "переведен":
            return "переведен"
        if not text or text == "в отделении":
            return "в отделении"
        return text

    @staticmethod
    def _age_to_years(age_value, age_unit: str):
        if age_value is None:
            return None
        try:
            age = float(age_value)
        except (TypeError, ValueError):
            return None
        unit = str(age_unit or "").strip().lower()
        if "меся" in unit:
            return age / 12.0
        return age

    @staticmethod
    def _age_group(age_years):
        if age_years is None:
            return "Не указан"
        if age_years < 1:
            return "до 1 года"
        if age_years < 18:
            return "1–17"
        if age_years <= 44:
            return "18–44"
        if age_years <= 60:
            return "45–60"
        if age_years <= 75:
            return "61–75"
        return "75+"

    @staticmethod
    def _weekday_name(value: datetime):
        names = [
            "Понедельник",
            "Вторник",
            "Среда",
            "Четверг",
            "Пятница",
            "Суббота",
            "Воскресенье",
        ]
        return names[value.weekday()]

    @staticmethod
    def _mkb_class(code: str):
        text = str(code or "").strip()
        if not text:
            return "Не указан"
        return text[:3].upper()

    @staticmethod
    def _diagnosis_key(code: str, diagnosis_text: str):
        clean_code = str(code or "").strip() or "—"
        clean_text = str(diagnosis_text or "").strip() or "Без уточнения"
        return f"{clean_code} {clean_text}"

    @staticmethod
    def _distribution_lines(counter: dict[str, int], total: int, *, limit=12, forced_order=None):
        if not counter:
            return "н/д"
        if forced_order:
            items = [(name, counter.get(name, 0)) for name in forced_order if counter.get(name, 0) > 0]
            tail = [(k, v) for k, v in counter.items() if k not in forced_order]
            tail.sort(key=lambda x: (-x[1], x[0]))
            items.extend(tail)
        else:
            items = sorted(counter.items(), key=lambda x: (-x[1], x[0]))
        visible = items[:limit]
        lines = [
            f"{escape(str(name))}: {count} ({StatisticsDialog._fmt_pct(count, total)})"
            for name, count in visible
        ]
        hidden_count = len(items) - len(visible)
        if hidden_count > 0:
            lines.append(f"... еще {hidden_count}")
        return "<br/>".join(lines) if lines else "н/д"

    @staticmethod
    def _distribution_mortality_lines(total_counter: dict[str, int], deaths_counter: dict[str, int], *, limit=12):
        if not total_counter:
            return "н/д"
        items = sorted(total_counter.items(), key=lambda x: (-x[1], x[0]))[:limit]
        lines = []
        for group_name, total in items:
            deaths = deaths_counter.get(group_name, 0)
            rate = StatisticsDialog._fmt_pct(deaths, total)
            lines.append(f"{escape(str(group_name))}: {deaths}/{total} ({rate})")
        return "<br/>".join(lines) if lines else "н/д"

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

    def _on_preview_clicked(self):
        html = self._generate_report_html(save_pdf=False)
        if html:
            self.report_text.setHtml(html)

    def _on_save_pdf_clicked(self):
        html = self._generate_report_html(save_pdf=True)
        if html:
            self.report_text.setHtml(html)

    def _fetch_context(self):
        conn = self.db_manager.get_connection()
        cursor = conn.cursor()
        period_params = (self.start_date_str, self.end_date_str)

        cursor.execute(
            """
            SELECT
                id,
                patient_id,
                admission_datetime,
                transfer_datetime,
                death_datetime,
                outcome,
                patient_age,
                patient_age_unit,
                patient_gender,
                source_department,
                diagnosis_code,
                diagnosis_text
            FROM admissions
            WHERE admission_datetime BETWEEN ? AND ?
            """,
            period_params,
        )
        columns = [column[0] for column in cursor.description]
        raw_admissions = [dict(zip(columns, row)) for row in cursor.fetchall()]

        admissions = []
        for row in raw_admissions:
            admission_id = self._safe_int(row.get("id"), default=0)
            patient_id = row.get("patient_id")
            adm_dt = self._parse_datetime(row.get("admission_datetime"))
            if adm_dt is None:
                continue

            transfer_dt = self._parse_datetime(row.get("transfer_datetime"))
            death_dt = self._parse_datetime(row.get("death_datetime"))

            raw_end_candidates = [dt for dt in (death_dt, transfer_dt, self._end_dt) if dt is not None]
            los_end_dt = min(raw_end_candidates) if raw_end_candidates else self._end_dt
            if los_end_dt < adm_dt:
                los_end_dt = adm_dt

            los_days = max(0.0, (los_end_dt - adm_dt).total_seconds() / 86400.0)

            outcome = self._normalize_outcome(row.get("outcome"), transfer_dt, death_dt)
            is_death = outcome == "умер"
            death_time_hours = None
            if is_death and death_dt is not None:
                death_time_hours = max(0.0, (death_dt - adm_dt).total_seconds() / 3600.0)

            age_years = self._age_to_years(row.get("patient_age"), row.get("patient_age_unit"))
            gender = self._normalize_text(row.get("patient_gender"))
            source = self._normalize_text(row.get("source_department"))
            diagnosis_code = str(row.get("diagnosis_code") or "").strip()
            diagnosis_text = str(row.get("diagnosis_text") or "").strip()

            admissions.append(
                {
                    "admission_id": admission_id,
                    "patient_id": patient_id,
                    "admission_dt": adm_dt,
                    "transfer_dt": transfer_dt,
                    "death_dt": death_dt,
                    "outcome": outcome,
                    "los_days": los_days,
                    "age_years": age_years,
                    "age_group": self._age_group(age_years),
                    "gender": gender,
                    "source": source,
                    "diagnosis_code": diagnosis_code,
                    "diagnosis_text": diagnosis_text,
                    "diagnosis_key": self._diagnosis_key(diagnosis_code, diagnosis_text),
                    "mkb_class": self._mkb_class(diagnosis_code),
                    "weekday_name": self._weekday_name(adm_dt),
                    "month_label": adm_dt.strftime("%Y-%m"),
                    "is_death": is_death,
                    "death_time_hours": death_time_hours,
                }
            )

        admission_ids = {row["admission_id"] for row in admissions if row["admission_id"]}

        cursor.execute(
            """
            SELECT admission_id
            FROM operations
            WHERE operation_datetime BETWEEN ? AND ?
            """,
            period_params,
        )
        raw_ops = [self._safe_int(r[0], default=0) for r in cursor.fetchall()]
        operations_adm_ids = [aid for aid in raw_ops if aid and aid in admission_ids]

        cursor.execute(
            """
            SELECT admission_id, type, volume_ml
            FROM transfusions
            WHERE datetime BETWEEN ? AND ?
            """,
            period_params,
        )
        transfusions = []
        for admission_id, transf_type, volume_ml in cursor.fetchall():
            aid = self._safe_int(admission_id, default=0)
            if not aid or aid not in admission_ids:
                continue
            transfusions.append(
                {
                    "admission_id": aid,
                    "type": self._normalize_text(transf_type),
                    "volume_ml": self._safe_float(volume_ml, default=0.0),
                }
            )

        cursor.execute(
            """
            SELECT admission_id, start_time, end_time
            FROM ivl_episodes
            WHERE start_time BETWEEN ? AND ?
            """,
            period_params,
        )
        ivl_episodes = []
        for admission_id, start_time, end_time in cursor.fetchall():
            aid = self._safe_int(admission_id, default=0)
            if not aid or aid not in admission_ids:
                continue
            start_dt = self._parse_datetime(start_time)
            if start_dt is None:
                continue
            end_dt = self._parse_datetime(end_time) or self._end_dt
            if end_dt < start_dt:
                end_dt = start_dt
            end_dt = min(end_dt, self._end_dt)
            duration_hours = max(0.0, (end_dt - start_dt).total_seconds() / 3600.0)
            ivl_episodes.append(
                {
                    "admission_id": aid,
                    "duration_hours": duration_hours,
                }
            )

        return {
            "admissions": admissions,
            "operations_adm_ids": operations_adm_ids,
            "transfusions": transfusions,
            "ivl_episodes": ivl_episodes,
        }

    def _population_stats(self, admissions):
        return {
            "total_n": len(admissions),
            "unique_patients": len({a["patient_id"] for a in admissions if a["patient_id"] is not None}),
            "death_ids": {a["admission_id"] for a in admissions if a["is_death"] and a["admission_id"]},
        }

    def _los_stats(self, admissions, total_n):
        los_values = [a["los_days"] for a in admissions]
        bed_days = sum(los_values)
        return {
            "bed_days": bed_days,
            "alos": self._safe_div(bed_days, total_n),
            "los_median": median(los_values) if los_values else None,
            "los_min": min(los_values) if los_values else None,
            "los_max": max(los_values) if los_values else None,
            "long_stay_count": sum(1 for x in los_values if x > 7.0),
        }

    def _age_stats(self, admissions):
        ages = [a["age_years"] for a in admissions if a["age_years"] is not None]
        return {
            "mean_age": self._safe_div(sum(ages), len(ages)) if ages else None,
            "median_age": median(ages) if ages else None,
        }

    def _death_timing_stats(self, admissions):
        death_times_hours = [a["death_time_hours"] for a in admissions if a["death_time_hours"] is not None]
        return {
            "mean_time_to_death_days": self._safe_div(sum(death_times_hours), len(death_times_hours)) / 24.0 if death_times_hours else None,
            "median_time_to_death_days": (median(death_times_hours) / 24.0) if death_times_hours else None,
            "early_deaths": sum(1 for h in death_times_hours if h < 24.0),
            "deaths_1_3_days": sum(1 for h in death_times_hours if 24.0 <= h < 72.0),
            "deaths_3_7_days": sum(1 for h in death_times_hours if 72.0 <= h < 168.0),
            "deaths_ge_7_days": sum(1 for h in death_times_hours if h >= 168.0),
        }

    @staticmethod
    def _inc_counter(counter, key):
        counter[key] = counter.get(key, 0) + 1

    def _distribution_stats(self, admissions):
        age_groups = {}
        age_groups_deaths = {}
        genders = {}
        sources = {}
        months = {}
        weekdays = {}
        diagnoses = {}
        mkb_classes = {}
        outcomes = {}

        for admission in admissions:
            self._inc_counter(age_groups, admission["age_group"])
            self._inc_counter(genders, admission["gender"])
            self._inc_counter(sources, admission["source"])
            self._inc_counter(months, admission["month_label"])
            self._inc_counter(weekdays, admission["weekday_name"])
            self._inc_counter(diagnoses, admission["diagnosis_key"])
            self._inc_counter(mkb_classes, admission["mkb_class"])
            outcome_label = admission["outcome"].capitalize()
            self._inc_counter(outcomes, outcome_label)
            if admission["is_death"]:
                self._inc_counter(age_groups_deaths, admission["age_group"])

        return {
            "age_groups": age_groups,
            "age_groups_deaths": age_groups_deaths,
            "genders": genders,
            "sources": sources,
            "months": months,
            "weekdays": weekdays,
            "diagnoses": diagnoses,
            "mkb_classes": mkb_classes,
            "outcomes": outcomes,
        }

    def _operation_stats(self, operations_adm_ids, death_ids):
        surg_adm_ids = set(operations_adm_ids)
        return {
            "operations_count": len(operations_adm_ids),
            "surg_adm_ids": surg_adm_ids,
            "n_surg": len(surg_adm_ids),
            "deaths_surg": len(death_ids.intersection(surg_adm_ids)),
        }

    def _transfusion_stats(self, transfusions, death_ids):
        transf_adm_ids = {t["admission_id"] for t in transfusions}
        transf_by_type = {}
        for t in transfusions:
            self._inc_counter(transf_by_type, t["type"])
        return {
            "transfusion_units": len(transfusions),
            "transf_adm_ids": transf_adm_ids,
            "n_transf": len(transf_adm_ids),
            "volume_total": sum(t["volume_ml"] for t in transfusions),
            "transf_by_type": transf_by_type,
            "deaths_transf": len(death_ids.intersection(transf_adm_ids)),
        }

    def _ivl_stats(self, ivl_episodes, death_ids):
        ivl_adm_ids = {e["admission_id"] for e in ivl_episodes}
        ivl_hours = sum(e["duration_hours"] for e in ivl_episodes)
        return {
            "ivl_adm_ids": ivl_adm_ids,
            "n_ivl": len(ivl_adm_ids),
            "ivl_episodes_count": len(ivl_episodes),
            "ivl_days": ivl_hours / 24.0,
            "deaths_ivl": len(death_ids.intersection(ivl_adm_ids)),
        }

    def _period_bed_stats(self, bed_days, total_n):
        period_days = max(1, (self._end_dt.date() - self._start_dt.date()).days + 1)
        beds = self._statistics_beds()
        bed_fund = beds * period_days
        return {
            "period_days": period_days,
            "beds": beds,
            "bed_fund": bed_fund,
            "occupancy": self._pct(bed_days, bed_fund),
            "bed_utilization_days": self._safe_div(bed_days, beds),
            "turnover": self._safe_div(total_n, beds),
            "bti": self._safe_div((bed_fund - bed_days), total_n),
        }

    def _statistics_beds(self):
        try:
            from rem_card.Rao_jornal.config.settings import NUM_BEDS

            return self._safe_int(NUM_BEDS, default=9)
        except Exception:
            return 9

    def _admission_active_on_day(self, admission, day_start, day_end):
        adm_start = admission["admission_dt"]
        end_candidates = [self._end_dt]
        if admission["transfer_dt"] is not None:
            end_candidates.append(admission["transfer_dt"])
        if admission["death_dt"] is not None:
            end_candidates.append(admission["death_dt"])
        adm_end = min(end_candidates)
        return adm_start < day_end and adm_end > day_start

    def _daily_load_stats(self, admissions, bed_days, period_days, beds):
        mean_patients = self._safe_div(bed_days, period_days)
        daily_counts = []
        for day_idx in range(period_days):
            day_start = datetime.combine(self._start_dt.date(), datetime.min.time()) + timedelta(days=day_idx)
            day_end = day_start + timedelta(days=1)
            count = 0
            for admission in admissions:
                if self._admission_active_on_day(admission, day_start, day_end):
                    count += 1
            daily_counts.append(count)

        max_patients = max(daily_counts) if daily_counts else 0
        threshold = 4
        high_load_periods = sum(1 for c in daily_counts if c >= threshold)
        return {
            "mean_patients": mean_patients,
            "utilization": self._safe_div(mean_patients, beds),
            "max_patients": max_patients,
            "load_threshold": threshold,
            "load_time_pct": self._pct(high_load_periods, period_days),
        }

    def _intensity_indexes(self, *, n_ivl, n_surg, n_transf, total_n, early_deaths, deaths, n_with_interventions, ivl_days, operations_count, transfusion_units, bed_days, period_days, max_patients, beds):
        return {
            "intensity_index": self._safe_div((n_ivl + n_surg + n_transf), total_n),
            "severity_index": self._safe_div(early_deaths, deaths),
            "technology_index": self._pct(n_with_interventions, total_n),
            "resource_use_index": self._safe_div((ivl_days + operations_count + transfusion_units), bed_days),
            "throughput": self._safe_div(total_n, period_days),
            "load_coefficient": self._safe_div(max_patients, beds),
        }

    def _calculate_statistics(self):
        context = self._fetch_context()
        admissions = context["admissions"]
        operations_adm_ids = context["operations_adm_ids"]
        transfusions = context["transfusions"]
        ivl_episodes = context["ivl_episodes"]

        population = self._population_stats(admissions)
        total_n = population["total_n"]
        death_ids = population["death_ids"]
        deaths = len(death_ids)

        los = self._los_stats(admissions, total_n)
        age = self._age_stats(admissions)
        death_timing = self._death_timing_stats(admissions)
        distributions = self._distribution_stats(admissions)
        operations = self._operation_stats(operations_adm_ids, death_ids)
        transfusion = self._transfusion_stats(transfusions, death_ids)
        ivl = self._ivl_stats(ivl_episodes, death_ids)
        bed_period = self._period_bed_stats(los["bed_days"], total_n)
        load = self._daily_load_stats(admissions, los["bed_days"], bed_period["period_days"], bed_period["beds"])

        n_with_interventions = len(
            ivl["ivl_adm_ids"].union(operations["surg_adm_ids"]).union(transfusion["transf_adm_ids"])
        )
        indexes = self._intensity_indexes(
            n_ivl=ivl["n_ivl"],
            n_surg=operations["n_surg"],
            n_transf=transfusion["n_transf"],
            total_n=total_n,
            early_deaths=death_timing["early_deaths"],
            deaths=deaths,
            n_with_interventions=n_with_interventions,
            ivl_days=ivl["ivl_days"],
            operations_count=operations["operations_count"],
            transfusion_units=transfusion["transfusion_units"],
            bed_days=los["bed_days"],
            period_days=bed_period["period_days"],
            max_patients=load["max_patients"],
            beds=bed_period["beds"],
        )

        return {
            "N": total_n,
            "N_unique": population["unique_patients"],
            "bed_days": los["bed_days"],
            "alos": los["alos"],
            "los_median": los["los_median"],
            "los_min": los["los_min"],
            "los_max": los["los_max"],
            "beds": bed_period["beds"],
            "period_days": bed_period["period_days"],
            "bed_fund": bed_period["bed_fund"],
            "occupancy": bed_period["occupancy"],
            "bed_utilization_days": bed_period["bed_utilization_days"],
            "turnover": bed_period["turnover"],
            "bti": bed_period["bti"],
            "mean_age": age["mean_age"],
            "median_age": age["median_age"],
            "age_groups": distributions["age_groups"],
            "genders": distributions["genders"],
            "months": distributions["months"],
            "weekdays": distributions["weekdays"],
            "sources": distributions["sources"],
            "diagnoses": distributions["diagnoses"],
            "mkb_classes": distributions["mkb_classes"],
            "deaths": deaths,
            "mortality_pct": self._pct(deaths, total_n),
            "mortality_per_1000_bed_days": self._safe_div(deaths, los["bed_days"]) * 1000.0,
            "outcomes": distributions["outcomes"],
            "mean_time_to_death_days": death_timing["mean_time_to_death_days"],
            "median_time_to_death_days": death_timing["median_time_to_death_days"],
            "early_deaths": death_timing["early_deaths"],
            "deaths_1_3_days": death_timing["deaths_1_3_days"],
            "deaths_3_7_days": death_timing["deaths_3_7_days"],
            "deaths_ge_7_days": death_timing["deaths_ge_7_days"],
            "age_groups_deaths": distributions["age_groups_deaths"],
            "N_IVL": ivl["n_ivl"],
            "ivl_episodes_count": ivl["ivl_episodes_count"],
            "ivl_days": ivl["ivl_days"],
            "deaths_ivl": ivl["deaths_ivl"],
            "N_surg": operations["n_surg"],
            "operations_count": operations["operations_count"],
            "deaths_surg": operations["deaths_surg"],
            "N_transf": transfusion["n_transf"],
            "transfusion_units": transfusion["transfusion_units"],
            "volume_total": transfusion["volume_total"],
            "transf_by_type": transfusion["transf_by_type"],
            "deaths_transf": transfusion["deaths_transf"],
            "IVL_index": self._safe_div(ivl["n_ivl"], total_n),
            "Surgery_index": self._safe_div(operations["n_surg"], total_n),
            "Transfusion_index": self._safe_div(transfusion["n_transf"], total_n),
            "mean_patients": load["mean_patients"],
            "utilization": load["utilization"],
            "max_patients": load["max_patients"],
            "load_threshold": load["load_threshold"],
            "load_time_pct": load["load_time_pct"],
            "intensity_index": indexes["intensity_index"],
            "severity_index": indexes["severity_index"],
            "long_stay_pct": self._pct(los["long_stay_count"], total_n),
            "technology_index": indexes["technology_index"],
            "resource_use_index": indexes["resource_use_index"],
            "throughput": indexes["throughput"],
            "load_coefficient": indexes["load_coefficient"],
        }

    def _section_rows(self, section_key: str, s: dict):
        total_n = s["N"]
        deaths = s["deaths"]

        if section_key == "s1":
            return [
                ("1.1 Уникальные пациенты", "N", self._fmt_num(s["N_unique"], 0)),
                ("1.2 Госпитализации", "N", self._fmt_num(total_n, 0)),
                ("1.3 Койко-дни", "Койко-дни = Σ LOS", self._fmt_num(s["bed_days"])),
                ("1.4 Средняя длительность (ALOS)", "Средняя длительность = Σ LOS / N", self._fmt_num(s["alos"])),
                ("1.5 Медиана LOS", "Медиана (LOS)", self._fmt_num(s["los_median"])),
                ("1.6 Минимум LOS", "LOSмин", self._fmt_num(s["los_min"])),
                ("1.7 Максимум LOS", "LOSмакс", self._fmt_num(s["los_max"])),
            ]

        if section_key == "s2":
            return [
                ("2.1 Количество коек", "Койки", self._fmt_num(s["beds"], 0)),
                ("2.2 Коечный фонд", "Коечный фонд = Койки × Дни", self._fmt_num(s["bed_fund"])),
                ("2.3 Занятость (%)", "Занятость = Койко-дни / Коечный фонд × 100%", f"{self._fmt_num(s['occupancy'])}%"),
                ("2.4 Средняя занятость койки", "Средняя занятость койки (дни) = Койко-дни / Койки", self._fmt_num(s["bed_utilization_days"])),
                ("2.5 Оборот койки", "Оборот койки = N / Койки", self._fmt_num(s["turnover"])),
                ("2.6 Простой койки", "Простой койки = (Коечный фонд − Койко-дни) / N", self._fmt_num(s["bti"])),
            ]

        if section_key == "s3":
            age_lines = self._distribution_lines(
                s["age_groups"],
                total_n,
                forced_order=["до 1 года", "1–17", "18–44", "45–60", "61–75", "75+", "Не указан"],
            )
            gender_lines = self._distribution_lines(s["genders"], total_n)
            return [
                ("3.1 Средний возраст", "Средний возраст = Σ возраст / N", self._fmt_num(s["mean_age"])),
                ("3.2 Медианный возраст", "Медианный возраст", self._fmt_num(s["median_age"])),
                ("3.3 Возрастные группы", "Доля = n / N × 100%", age_lines),
                ("3.4 Пол", "Доля = n / N × 100%", gender_lines),
            ]

        if section_key == "s4":
            weekday_order = [
                "Понедельник",
                "Вторник",
                "Среда",
                "Четверг",
                "Пятница",
                "Суббота",
                "Воскресенье",
            ]
            return [
                ("4.1 По месяцам", "Доля = n / N × 100%", self._distribution_lines(s["months"], total_n)),
                ("4.2 По дням недели", "Доля = n / N × 100%", self._distribution_lines(s["weekdays"], total_n, forced_order=weekday_order)),
                ("4.3 По источнику", "Доля = n / N × 100%", self._distribution_lines(s["sources"], total_n)),
            ]

        if section_key == "s5":
            return [
                ("5.1 Частота диагнозов", "Частота = n / N × 100%", self._distribution_lines(s["diagnoses"], total_n)),
                ("5.2 По классам МКБ", "Частота = n / N × 100%", self._distribution_lines(s["mkb_classes"], total_n)),
            ]

        if section_key == "s6":
            return [
                ("6.1 Абсолютная летальность", "Умершие", self._fmt_num(deaths, 0)),
                ("6.2 Летальность (%)", "Летальность = Умершие / N × 100%", f"{self._fmt_num(s['mortality_pct'])}%"),
                ("6.3 На 1000 койко-дней", "Летальность = Умершие / Койко-дни × 1000", self._fmt_num(s["mortality_per_1000_bed_days"])),
                ("6.4 Исходы", "Доля = n / N × 100%", self._distribution_lines(s["outcomes"], total_n)),
            ]

        if section_key == "s7":
            return [
                ("7.1 Среднее время до смерти", "Среднее время = Σ время / Умершие", self._fmt_num(s["mean_time_to_death_days"])),
                ("7.2 Медиана времени до смерти", "Медиана времени", self._fmt_num(s["median_time_to_death_days"])),
                ("7.3 Ранняя летальность", "Ранняя летальность = Смерти <24ч / Умершие × 100%", self._fmt_pct(s["early_deaths"], deaths)),
                ("7.4 1–3 суток", "Доля = n / Умершие × 100%", self._fmt_pct(s["deaths_1_3_days"], deaths)),
                ("7.5 3–7 суток", "Доля = n / Умершие × 100%", self._fmt_pct(s["deaths_3_7_days"], deaths)),
                ("7.6 ≥7 суток", "Доля = n / Умершие × 100%", self._fmt_pct(s["deaths_ge_7_days"], deaths)),
            ]

        if section_key == "s8":
            lines = self._distribution_mortality_lines(s["age_groups"], s["age_groups_deaths"])
            return [
                ("8. Летальность по группам", "Летальность группы = Умершие_группы / N_группы × 100%", lines),
            ]

        if section_key == "s9":
            return [
                ("9.1 Пациенты на ИВЛ", "N_IVL", self._fmt_num(s["N_IVL"], 0)),
                ("9.2 Доля пациентов на ИВЛ", "Доля ИВЛ = N_IVL / N × 100%", self._fmt_pct(s["N_IVL"], total_n)),
                ("9.3 Эпизоды ИВЛ", "Эпизоды", self._fmt_num(s["ivl_episodes_count"], 0)),
                ("9.4 Средняя длительность ИВЛ", "Средняя ИВЛ = ИВЛ-дни / N_IVL", self._fmt_num(self._safe_div(s["ivl_days"], s["N_IVL"]))),
                ("9.5 ИВЛ-дни", "ИВЛ-дни", self._fmt_num(s["ivl_days"])),
                ("9.6 Летальность на ИВЛ", "Летальность ИВЛ = Умершие_ИВЛ / N_IVL × 100%", self._fmt_pct(s["deaths_ivl"], s["N_IVL"])),
            ]

        if section_key == "s10":
            return [
                ("10.1 Пациенты с операциями", "N_surg", self._fmt_num(s["N_surg"], 0)),
                ("10.2 Операции", "Операции", self._fmt_num(s["operations_count"], 0)),
                ("10.3 Частота операций", "Частота = N_surg / N", self._fmt_num(self._safe_div(s["N_surg"], total_n))),
                ("10.4 Летальность у оперированных", "Летальность = Умершие_опер / N_surg × 100%", self._fmt_pct(s["deaths_surg"], s["N_surg"])),
            ]

        if section_key == "s11":
            return [
                ("11.1 Число переливаний", "Переливания", self._fmt_num(s["transfusion_units"], 0)),
                ("11.2 Пациенты с переливаниями", "N_transf", self._fmt_num(s["N_transf"], 0)),
                ("11.3 Общий объем, мл", "Объем_общий", self._fmt_num(s["volume_total"])),
                ("11.4 Средний объем дозы, мл", "Средняя доза = Объем_общий / Дозы", self._fmt_num(self._safe_div(s["volume_total"], s["transfusion_units"]))),
                ("11.5 Летальность при переливаниях", "Летальность = Умершие_переливания / N_transf × 100%", self._fmt_pct(s["deaths_transf"], s["N_transf"])),
                ("11.6 По типам", "Доля = n / Дозы × 100%", self._distribution_lines(s["transf_by_type"], s["transfusion_units"])),
            ]

        if section_key == "s16":
            return [
                ("16.1 ИВЛ на пациента", "Индекс ИВЛ = N_IVL / N", self._fmt_num(s["IVL_index"])),
                ("16.2 Операции на пациента", "Индекс операций = N_surg / N", self._fmt_num(s["Surgery_index"])),
                ("16.3 Переливания на пациента", "Индекс переливаний = N_transf / N", self._fmt_num(s["Transfusion_index"])),
            ]

        if section_key == "s17":
            return [
                ("17.1 Оборот койки", "Оборот койки = N / Койки", self._fmt_num(s["turnover"])),
                ("17.2 Койко-дни на пациента", "ALOS", self._fmt_num(s["alos"])),
            ]

        if section_key == "s18":
            return [
                ("18.1 Доля ранней летальности", "Ранняя летальность = Смерти <24ч / Умершие × 100%", self._fmt_pct(s["early_deaths"], deaths)),
                ("18.2 Индекс интенсивности лечения", "Индекс интенсивности = (N_IVL + N_surg + N_transf) / N", self._fmt_num(s["intensity_index"])),
                ("18.3 Индекс тяжести потока", "Индекс тяжести = Ранние смерти / Умершие", self._fmt_num(s["severity_index"])),
                ("18.4 Длительное пребывание", "Длительное пребывание = N(>7 суток) / N × 100%", f"{self._fmt_num(s['long_stay_pct'])}%"),
                ("18.5 Индекс технологичности", "Индекс технологичности = N_с вмешательствами / N × 100%", f"{self._fmt_num(s['technology_index'])}%"),
            ]

        if section_key == "s19":
            return [
                ("19.1 Среднесуточная занятость", "Средние пациенты = Койко-дни / Дни", self._fmt_num(s["mean_patients"])),
                ("19.2 Использование коек", "Использование коек = Средние пациенты / Койки", self._fmt_num(s["utilization"])),
                ("19.3 Максимальная загрузка", "Макс. пациенты", self._fmt_num(s["max_patients"], 0)),
                (
                    "19.4 Высокая нагрузка",
                    "Время высокой нагрузки = Время ≥ порог / Общее время × 100%",
                    f"{self._fmt_num(s['load_time_pct'])}% (порог: ≥{self._fmt_num(s['load_threshold'], 0)} пациентов)",
                ),
            ]

        if section_key == "sx":
            return [
                ("Resource use index", "(ИВЛ-дни + Операции + Переливания) / Койко-дни", self._fmt_num(s["resource_use_index"])),
                ("Throughput", "N / Дни", self._fmt_num(s["throughput"])),
                ("Case severity proxy", "Ранняя летальность = Смерти <24ч / Умершие × 100%", self._fmt_pct(s["early_deaths"], deaths)),
                ("Load coefficient", "Пик пациентов / Койки", self._fmt_num(s["load_coefficient"])),
            ]

        return []

    def _section_title(self, section_key: str):
        for _group, items in self.section_groups.items():
            if section_key in items:
                return items[section_key]
        return section_key

    def _render_sections_html(self, selected_sections: list[str], s: dict):
        sections_html = []

        def _format_value_cell(value):
            if value is None:
                return "—"
            text = str(value).strip()
            if not text:
                return "—"
            # Для распределений оставляем переносы строк в HTML.
            if "<br/>" in text:
                return text
            return escape(text)

        for section_key in selected_sections:
            rows = self._section_rows(section_key, s)
            if not rows:
                continue

            rows_html = []
            for name, formula, value in rows:
                rows_html.append(
                    f"""
                    <tr>
                        <td>{escape(str(name))}</td>
                        <td>{escape(str(formula))}</td>
                        <td class="value">{_format_value_cell(value)}</td>
                    </tr>
                    """
                )

            section_html = f"""
                <h2>{escape(self._section_title(section_key))}</h2>
                <table>
                    <tr>
                        <th>Показатель</th>
                        <th>Формула</th>
                        <th class="value">Значение</th>
                    </tr>
                    {''.join(rows_html)}
                </table>
            """
            sections_html.append(section_html)
        return "\n".join(sections_html)

    def _generate_report_html(self, save_pdf=False):
        selected = [key for key, cb in self.checkboxes.items() if cb.isChecked()]
        if not selected:
            from rem_card.Rao_jornal.ui.custom_dialogs import CustomMessageBox

            CustomMessageBox.show_info(self, "Внимание", "Выберите хотя бы один раздел статистики.")
            return ""

        try:
            stats = self._calculate_statistics()

            html_body = self._render_sections_html(selected, stats)
            generated_at = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
            period = f"{self._start_dt.strftime('%d.%m.%Y')} - {self._end_dt.strftime('%d.%m.%Y')}"

            full_html = f"""
            <html>
            <head>
                <meta charset="UTF-8">
                <style>
                    html, body {{
                        font-family: 'Arial', sans-serif;
                        color: #111111;
                        background: #ffffff;
                        margin: 0;
                        padding: 0;
                    }}
                    .page {{
                        background: #ffffff;
                        padding: 18px 20px;
                    }}
                    h1 {{
                        margin: 0 0 6px 0;
                        font-size: 20px;
                        color: #222222;
                    }}
                    h2 {{
                        margin: 16px 0 7px 0;
                        font-size: 14px;
                        color: #2c2c2c;
                        text-transform: uppercase;
                    }}
                    .period {{
                        margin: 0 0 12px 0;
                        color: #333333;
                        font-weight: 600;
                    }}
                    table {{
                        width: 100%;
                        border-collapse: collapse;
                        margin-bottom: 12px;
                    }}
                    th, td {{
                        border: 1px solid #7a7a7a;
                        padding: 6px 8px;
                        text-align: left;
                        font-size: 12px;
                        vertical-align: top;
                        color: #111111;
                    }}
                    th {{
                        background: #efefef;
                        font-weight: 700;
                    }}
                    td.value, th.value {{
                        text-align: right;
                        width: 32%;
                    }}
                    .footnote {{
                        margin-top: 12px;
                        color: #444444;
                        font-size: 11px;
                    }}
                </style>
            </head>
            <body>
                <div class="page">
                    <h1>Статистический отчет ОАР №3</h1>
                    <p class="period">Период: {period}</p>
                    {html_body}
                    <p class="footnote">Сформировано автоматически: {generated_at}</p>
                </div>
            </body>
            </html>
            """

            if save_pdf:
                self._save_pdf(full_html)
            return full_html
        except Exception as exc:
            from rem_card.Rao_jornal.ui.custom_dialogs import CustomMessageBox

            CustomMessageBox.show_info(self, "Ошибка", f"Не удалось сформировать отчет:\n{exc}")
            return ""

    def _save_pdf(self, html: str):
        from rem_card.app.paths import REPORT_DIR
        from rem_card.Rao_jornal.ui.custom_dialogs import CustomMessageBox

        os.makedirs(REPORT_DIR, exist_ok=True)
        filename = f"statistics_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        pdf_path = os.path.join(REPORT_DIR, filename)

        document = QTextDocument()
        document.setHtml(html)

        writer = QPdfWriter(pdf_path)
        layout = QPageLayout(QPageSize(QPageSize.A4), QPageLayout.Portrait, QMarginsF(15, 15, 15, 15))
        writer.setPageLayout(layout)
        try:
            page_rect = layout.paintRect()
            document.setPageSize(page_rect.size())
        except Exception:
            pass

        if hasattr(document, "print_"):
            document.print_(writer)
        else:
            document.print(writer)

        CustomMessageBox.show_info(self, "Успех", f"Статистический отчет успешно сохранен:\n{filename}")
