from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QFrame
from PySide6.QtWidgets import QGraphicsDropShadowEffect
from PySide6.QtWidgets import QGridLayout
from PySide6.QtWidgets import QHBoxLayout
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QScrollArea
from PySide6.QtWidgets import QSizePolicy
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from decimal import InvalidOperation
from decimal import ROUND_HALF_UP
from rem_card.app import operblock_startup_metrics
from rem_card.app.logger import logger
from rem_card.services.operblock_team import load_operblock_team
from rem_card.services.operblock_team import normalize_operblock_team_text
from rem_card.services.patient_departments import normalize_profile_department
from rem_card.ui.operblock_view.operblock_control_styles import operblock_vertical_scrollbar_style as _operblock_vertical_scrollbar_style
from rem_card.ui.styles.theme_runtime import set_widget_style
import re
import weakref
from rem_card.ui.operblock_view.operblock_helpers import (
    _format_dt,
    _format_order_time,
    _minute_floor_dt,
    _parse_datetime_value,
)
from rem_card.ui.operblock_view.operblock_visual_primitives import (
    OPERBLOCK_BOARD_MEDICATION_SCROLL_MAX_HEIGHT,
    PATIENT_CARD_STYLE,
    _OperBlockBoardProgressStepper,
)


class OperBlockBoardDetailsMixin:
    @staticmethod
    def _board_scroll_area(
        *,
        object_name: str,
        scrollbar_object_name: str,
        maximum_height: int,
        single_step: int = 36,
        page_step: int = 144,
    ) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setObjectName(object_name)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setMaximumHeight(maximum_height)
        scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        set_widget_style(scroll, f"""
            QScrollArea#{object_name} {{
                background: transparent;
                border: none;
            }}
            QScrollArea#{object_name} > QWidget > QWidget {{
                background: transparent;
            }}
            """)
        scrollbar = scroll.verticalScrollBar()
        scrollbar.setObjectName(scrollbar_object_name)
        scrollbar.setFixedWidth(14)
        scrollbar.setSingleStep(single_step)
        scrollbar.setPageStep(page_step)
        set_widget_style(scrollbar, _operblock_vertical_scrollbar_style(
                scrollbar_object_name,
                width_px=14,
                left_margin_px=3,
                right_margin_px=2,
            ))
        return scroll

    @staticmethod
    def _scroll_board_area_to_bottom_when_ready(scroll: QScrollArea) -> None:
        scroll_ref = weakref.ref(scroll)
        scrollbar = scroll.verticalScrollBar()
        connection = {"active": True}

        def scroll_to_bottom(*_args) -> None:
            if not connection.get("active"):
                return
            scroll_widget = scroll_ref()
            if scroll_widget is None:
                connection["active"] = False
                return
            try:
                maximum = int(scrollbar.maximum())
                minimum = int(scrollbar.minimum())
                if maximum <= minimum:
                    return
                scrollbar.setValue(maximum)
                connection["active"] = False
                try:
                    scrollbar.rangeChanged.disconnect(scroll_to_bottom)
                except (RuntimeError, TypeError):
                    pass
            except RuntimeError:
                connection["active"] = False

        try:
            scrollbar.rangeChanged.connect(scroll_to_bottom)
        except RuntimeError:
            return
        QTimer.singleShot(0, scroll_to_bottom)

    @staticmethod
    def _board_format_weight(value) -> str:
        if value in (None, ""):
            return "—"
        text = str(value).strip()
        if isinstance(value, float):
            text = text.rstrip("0").rstrip(".")
        return f"{text} кг"

    @staticmethod
    def _board_format_bmi(height_cm, weight_kg) -> str:
        if height_cm in (None, "") or weight_kg in (None, ""):
            return "—"
        try:
            height = Decimal(str(height_cm).replace(",", "."))
            weight = Decimal(str(weight_kg).replace(",", "."))
        except (InvalidOperation, ValueError):
            return "—"
        if height <= 0 or weight <= 0:
            return "—"
        height_m = height / Decimal("100")
        try:
            bmi = weight / (height_m * height_m)
        except (InvalidOperation, ZeroDivisionError):
            return "—"
        return str(bmi.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)).replace(".", ",")

    @staticmethod
    def _board_elapsed_text(started_at) -> str:
        start = _minute_floor_dt(_parse_datetime_value(started_at))
        if start is None:
            return "—"
        delta = max(timedelta(0), datetime.now().replace(second=0, microsecond=0) - start)
        total_minutes = int(delta.total_seconds() // 60)
        hours, minutes = divmod(total_minutes, 60)
        if hours <= 0:
            return f"{minutes} мин"
        return f"{hours} ч {minutes:02d} мин"

    @staticmethod
    def _board_diagnosis_text(patient: dict) -> str:
        diagnosis = str(patient.get("diagnosis_text") or "—").strip()
        code = str(patient.get("diagnosis_code") or "").strip()
        return f"{code}: {diagnosis}" if code else diagnosis

    @staticmethod
    def _board_ru_plural(value: int, forms: tuple[str, str, str]) -> str:
        number = abs(int(value)) % 100
        if 11 <= number <= 14:
            return forms[2]
        last_digit = number % 10
        if last_digit == 1:
            return forms[0]
        if 2 <= last_digit <= 4:
            return forms[1]
        return forms[2]

    @classmethod
    def _board_elapsed_short_text(cls, started_at) -> str:
        start = _minute_floor_dt(_parse_datetime_value(started_at))
        if start is None:
            return ""
        delta = max(timedelta(0), datetime.now().replace(second=0, microsecond=0) - start)
        total_minutes = int(delta.total_seconds() // 60)
        hours, minutes = divmod(total_minutes, 60)
        if hours <= 0:
            return f"{minutes} мин"
        if minutes <= 0:
            return f"{hours} {cls._board_ru_plural(hours, ('час', 'часа', 'часов'))}"
        return f"{hours} ч {minutes:02d} мин"

    @classmethod
    def _board_current_time_text(cls, started_at) -> str:
        current = datetime.now().replace(second=0, microsecond=0).strftime("%d.%m.%Y %H:%M")
        elapsed = cls._board_elapsed_short_text(started_at)
        return f"{current} - {elapsed}" if elapsed else current

    @staticmethod
    def _board_operating_room_text(value) -> str:
        text = normalize_operblock_team_text(value)
        if not text:
            return "—"
        text = re.sub(r"\bоперационн(?:ая|ой|ую|ые|ых|ое|ого|ому|ым|ом)?\b", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", text).strip(" -—:").strip()
        return text or "—"

    @staticmethod
    def _board_surgeon_group_label(position: str) -> str:
        normalized = normalize_operblock_team_text(position).casefold()
        if "гинеколог" in normalized or "акушер" in normalized:
            return "Гинекологи"
        if "лор" in normalized or "оториноларинголог" in normalized:
            return "Лор"
        if "травматолог" in normalized or "ортопед" in normalized:
            return "Травматологи"
        return "Хирурги"

    def _board_team_text(self, patient: dict) -> str:
        lines: list[str] = []
        anesthesiologist = normalize_operblock_team_text(patient.get("anesthesiologist"))
        if anesthesiologist:
            lines.append(f"Анестезиологи: {anesthesiologist}")

        position_by_name: dict[str, str] = {}
        try:
            team_items = load_operblock_team()
        except Exception as exc:
            logger.warning("operblock board team load failed: %s", exc, exc_info=True)
            team_items = []
        for item in team_items:
            name = normalize_operblock_team_text((item or {}).get("name"))
            position = normalize_operblock_team_text((item or {}).get("position"))
            if name and position:
                position_by_name.setdefault(name.casefold(), position)

        groups: dict[str, list[str]] = {}
        group_order: list[str] = []
        seen_surgeons: set[str] = set()
        for value in patient.get("surgeons") or []:
            surgeon = normalize_operblock_team_text(value)
            surgeon_key = surgeon.casefold()
            if not surgeon or surgeon_key in seen_surgeons:
                continue
            seen_surgeons.add(surgeon_key)
            label = self._board_surgeon_group_label(position_by_name.get(surgeon_key, ""))
            if label not in groups:
                groups[label] = []
                group_order.append(label)
            groups[label].append(surgeon)

        for label in group_order:
            lines.append(f"{label}: {', '.join(groups[label])}")
        return "\n".join(lines) if lines else "—"

    def _board_patient_block(self, patient: dict) -> QFrame:
        block, layout = self._board_block("")
        layout.setSpacing(11)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(14)
        photo = QLabel()
        photo.setFixedSize(232, 268)
        photo.setAlignment(Qt.AlignCenter)
        set_widget_style(photo, "border-radius: 4px;")
        self._set_patient_photo(photo, patient.get("gender"))
        top.addWidget(photo, 0, Qt.AlignTop)

        main = QVBoxLayout()
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(9)
        status = QLabel("В ОПЕРАЦИОННОЙ")
        status.setAlignment(Qt.AlignCenter)
        status.setFixedHeight(24)
        set_widget_style(status, "color: #FFFFFF; background-color: #EF4444; border-radius: 5px; "
            "font-size: 12px; font-weight: 800; padding: 3px 8px;")
        main.addWidget(status, 0, Qt.AlignLeft)
        main.addWidget(self._board_value_label(f"ИБ № {patient.get('history_number') or '—'}", size=15, color="#0F5CC9"))
        name = self._board_value_label(patient.get("full_name") or "Неизвестно", size=21, weight=800)
        name.setMaximumHeight(82)
        main.addWidget(name)
        main.addStretch(1)
        top.addLayout(main, 1)
        layout.addLayout(top)

        grid = QGridLayout()
        grid.setContentsMargins(0, 2, 0, 0)
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(8)
        blood_parts = [
            str(value).strip()
            for value in (patient.get("blood_group"), patient.get("blood_rh"))
            if str(value or "").strip()
        ]
        rows = [
            ("Возраст:", patient.get("age") or "—"),
            ("Пол:", patient.get("gender") or "—"),
            ("Вес:", self._board_format_weight(patient.get("weight_kg"))),
            ("Рост:", f"{patient.get('height_cm')} см" if patient.get("height_cm") not in (None, "") else "—"),
            ("ИМТ:", self._board_format_bmi(patient.get("height_cm"), patient.get("weight_kg"))),
            ("Группа крови:", " ".join(blood_parts) if blood_parts else "—"),
        ]
        for row, (label_text, value_text) in enumerate(rows):
            grid.addWidget(self._board_muted_label(label_text, size=13), row, 0)
            grid.addWidget(self._board_value_label(str(value_text), size=13, weight=550), row, 1)
        layout.addLayout(grid)

        layout.addStretch(1)
        self._disable_context_menu_for_widget_tree(block)
        return block

    def _board_vitals_block(self, patient: dict) -> QFrame:
        latest = patient.get("latest") or {}
        ad = str(latest.get("ad") or "").strip("/")
        pulse = latest.get("pulse")
        spo2 = latest.get("spo2")
        has_data = bool(ad) or pulse not in (None, "") or spo2 not in (None, "")
        source = str(latest.get("source") or "")
        title = "Текущие показатели" if source == "current" else "Исходные показатели"
        block, layout = self._board_block(title, icon_kind="pulse", icon_color="#71839A")
        values = [
            ("bp", "АД", f"{ad} мм рт. ст." if ad else "-/- мм рт. ст."),
            ("heart", "ЧСС", f"{pulse} уд/мин" if pulse not in (None, "") else "- уд/мин"),
            ("spo2", "SpO₂", f"{spo2} %" if spo2 not in (None, "") else "- %"),
        ]
        for index, (icon_kind, name, value) in enumerate(values):
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(10)
            row.addWidget(self._board_line_icon(icon_kind, color="#71839A", size=22), 0)
            row.addWidget(self._board_value_label(name, size=13, weight=550), 0)
            value_label = self._board_value_label(value, size=16, weight=800, color="#1F2D3D")
            value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            row.addWidget(value_label, 1)
            layout.addLayout(row)
            if index < len(values) - 1:
                layout.addWidget(self._board_separator())

        if not has_data:
            layout.addSpacing(10)
            notice = QFrame()
            notice.setObjectName("OperBlockVitalsNotice")
            set_widget_style(notice, """
                QFrame#OperBlockVitalsNotice {
                    background-color: #EFF6FF;
                    border: 1px solid #93C5FD;
                    border-radius: 6px;
                }
                QLabel {
                    background: transparent;
                    border: none;
                }
                """)
            notice_layout = QHBoxLayout(notice)
            notice_layout.setContentsMargins(14, 10, 14, 10)
            notice_layout.setSpacing(10)
            icon = QLabel("i")
            icon.setFixedSize(18, 18)
            icon.setAlignment(Qt.AlignCenter)
            set_widget_style(icon, "color: #2563EB; border: 1px solid #2563EB; border-radius: 9px; "
                "font-size: 12px; font-weight: 800;")
            text = QLabel("Показатели будут отображаться после начала мониторинга")
            text.setWordWrap(True)
            set_widget_style(text, "color: #2563EB; font-size: 13px; font-weight: 700;")
            notice_layout.addWidget(icon, 0, Qt.AlignTop)
            notice_layout.addWidget(text, 1)
            layout.addWidget(notice)
        self._disable_context_menu_for_widget_tree(block)
        return block

    @staticmethod
    def _board_department_profile_text(value) -> str:
        return normalize_profile_department(value, clear_legacy_operblock=True) or "—"

    def _board_admission_block(self, table: dict, patient: dict) -> QFrame:
        block, layout = self._board_block("Диагноз при поступлении", shadow=False)
        diagnosis = self._board_value_label(self._board_diagnosis_text(patient), size=15, weight=700)
        diagnosis.setMaximumHeight(76)
        layout.addWidget(diagnosis)
        layout.addWidget(self._board_separator())

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(26)
        grid.setVerticalSpacing(18)
        team = self._board_team_text(patient)
        current_time_text = self._board_current_time_text(patient.get("started_at"))
        values = [
            ("calendar", "Поступление", _format_dt(patient.get("started_at"))),
            ("clock", "Текущее время в операционной", current_time_text),
            ("room", "Отделение", self._board_department_profile_text(patient.get("department_profile"))),
            ("team", "Бригада", team),
        ]
        for index, (icon_kind, name, value) in enumerate(values):
            row = index // 2
            col = index % 2
            item = QWidget()
            set_widget_style(item, "background: transparent; border: none;")
            item_layout = QHBoxLayout(item)
            item_layout.setContentsMargins(0, 0, 0, 0)
            item_layout.setSpacing(10)
            text_layout = QVBoxLayout()
            text_layout.setContentsMargins(0, 0, 0, 0)
            text_layout.setSpacing(6)
            text_layout.addWidget(self._board_value_label(name, size=13, weight=800))
            text_layout.addWidget(self._board_value_label(str(value or "—"), size=14, weight=500))
            item_layout.addWidget(self._board_line_icon(icon_kind, color="#2563EB", size=24), 0, Qt.AlignTop)
            item_layout.addLayout(text_layout, 1)
            grid.addWidget(item, row, col)
        layout.addLayout(grid)
        self._disable_context_menu_for_widget_tree(block)
        return block

    @staticmethod
    def _board_progress_state(operation_events: list[dict]) -> tuple[int, float, str]:
        kinds = [str((event or {}).get("kind") or "") for event in operation_events]
        if "surgery_end" in kinds:
            return 3, 1.0, "surgery_end"
        if "surgery_start" in kinds:
            return 2, 5.0 / 6.0, "surgery_start"
        if "anesthesia_start" in kinds:
            return 1, 0.5, "anesthesia_start"
        return 0, 0.14, "preparation"

    @staticmethod
    def _board_stage_label(kind: str, fallback: str = "") -> str:
        clean_fallback = re.sub(r"\s+", " ", str(fallback or "").strip())
        labels = {
            "anesthesia_start": "Начало анестезии",
            "anesthesia_end": "Завершение анестезии",
            "surgery_start": "Начало операции",
            "surgery_end": "Завершение операции",
            "custom": clean_fallback or "Этап операции",
        }
        default = labels.get(kind, clean_fallback or "Этап операции")
        if kind == "anesthesia_end" and clean_fallback and clean_fallback not in {"Конец пособия", default}:
            return clean_fallback
        return default

    @staticmethod
    def _board_stage_history(patient: dict) -> list[dict]:
        from rem_card.ui.operblock_view.operblock_main_widget import OperBlockMainWidget

        events = [dict(event or {}) for event in (patient.get("operation_events") or [])]
        history = [
            {
                "event_time": event.get("event_time"),
                "label": OperBlockMainWidget._board_stage_label(str(event.get("kind") or ""), str(event.get("label") or "")),
                "kind": str(event.get("kind") or ""),
            }
            for event in events
        ]
        history.sort(key=lambda item: _parse_datetime_value(item.get("event_time")) or datetime.min)
        return history

    @staticmethod
    def _board_empty_notice(text: str, *, object_name: str) -> QFrame:
        notice = QFrame()
        notice.setObjectName(object_name)
        notice.setMinimumHeight(66)
        set_widget_style(notice, f"""
            QFrame#{object_name} {{
                background-color: #EFF6FF;
                border: 1px solid #8FBEFF;
                border-radius: 6px;
            }}
            QLabel {{
                background: transparent;
                border: none;
            }}
            """)
        notice_layout = QHBoxLayout(notice)
        notice_layout.setContentsMargins(16, 0, 16, 0)
        notice_layout.setSpacing(10)
        icon = QLabel("i")
        icon.setFixedSize(18, 18)
        icon.setAlignment(Qt.AlignCenter)
        set_widget_style(icon, "color: #2563EB; border: 1px solid #2563EB; border-radius: 9px; "
            "font-size: 12px; font-weight: 800;")
        notice_text = QLabel(text)
        set_widget_style(notice_text, "color: #2563EB; font-size: 13px; font-weight: 800;")
        notice_layout.addWidget(icon, 0)
        notice_layout.addWidget(notice_text, 1)
        return notice

    @staticmethod
    def _board_operation_stages_empty_notice() -> QFrame:
        from rem_card.ui.operblock_view.operblock_main_widget import OperBlockMainWidget

        return OperBlockMainWidget._board_empty_notice(
            "Операция еще не начата",
            object_name="OperBlockStagesEmptyNotice",
        )

    def _board_progress_block(self, patient: dict) -> QFrame:
        operation_name = normalize_operblock_team_text(patient.get("operation_name"))
        title = f"Ход операции: {operation_name}" if operation_name else "Ход операции"
        block, layout = self._board_block(title)
        events = [dict(event or {}) for event in (patient.get("operation_events") or [])]
        active_index, fill_fraction, _active_kind = self._board_progress_state(events)
        stages = ["Подготовка", "Анестезия", "Операция", "Завершение"]

        stepper_area = QWidget()
        stepper_area.setObjectName("OperBlockBoardProgressStepperArea")
        stepper_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        set_widget_style(stepper_area, "background: transparent; border: none;")
        stepper_layout = QVBoxLayout(stepper_area)
        stepper_layout.setContentsMargins(0, 0, 0, 0)
        stepper_layout.setSpacing(0)
        stepper_layout.addStretch(1)
        stepper_layout.addWidget(_OperBlockBoardProgressStepper(stages, active_index, fill_fraction), 0)
        stepper_layout.addStretch(1)
        layout.addWidget(stepper_area, 1)
        return block

    def _board_operation_stages_block(self, patient: dict) -> QFrame:
        block, layout = self._board_block("Этапы операции")
        layout.setAlignment(Qt.AlignTop)
        stages_panel = QFrame()
        stages_panel.setObjectName("OperBlockBoardStagesPanel")
        set_widget_style(stages_panel, """
            QFrame#OperBlockBoardStagesPanel {
                background-color: #F8FBFF;
                border: 1px solid #CFE3FF;
                border-radius: 8px;
            }
            QLabel {
                background: transparent;
                border: none;
            }
            """)
        stages_layout = QVBoxLayout(stages_panel)
        stages_layout.setContentsMargins(12, 10, 12, 10)
        stages_layout.setSpacing(8)
        history = self._board_stage_history(patient)
        if not history:
            stages_layout.addWidget(self._board_operation_stages_empty_notice())
            layout.addWidget(stages_panel, 0, Qt.AlignTop)
            return block
        for item in history:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            dot = QLabel("•")
            dot.setFixedWidth(12)
            dot.setAlignment(Qt.AlignCenter)
            set_widget_style(dot, "font-size: 18px; color: #1F2D3D; font-weight: 900;")
            time_label = QLabel(_format_order_time(item.get("event_time")))
            time_label.setFixedWidth(44)
            set_widget_style(time_label, "font-size: 12px; color: #64748B; font-weight: 700;")
            text_label = self._board_value_label(
                str(item.get("label") or "Этап операции"),
                size=13,
                weight=700,
                color="#1F2D3D",
            )
            row.addWidget(dot, 0)
            row.addWidget(time_label, 0)
            row.addWidget(text_label, 1)
            stages_layout.addLayout(row)
        layout.addWidget(stages_panel, 0, Qt.AlignTop)
        return block

    def _board_medications_block(self, patient: dict) -> QFrame:
        block, layout = self._board_block("Назначения и препараты")
        layout.setSpacing(6)
        layout.setAlignment(Qt.AlignTop)
        items = [dict(item or {}) for item in (patient.get("medication_history") or [])]
        if not items:
            layout.addWidget(
                self._board_empty_notice(
                    "Нет введённых препаратов",
                    object_name="OperBlockMedicationsEmptyNotice",
                ),
                0,
                Qt.AlignTop,
            )
            return block
        scroll = self._board_scroll_area(
            object_name="OperBlockBoardMedicationsScroll",
            scrollbar_object_name="OperBlockBoardMedicationsScrollBar",
            maximum_height=OPERBLOCK_BOARD_MEDICATION_SCROLL_MAX_HEIGHT,
            single_step=34,
            page_step=136,
        )
        content = QWidget()
        set_widget_style(content, "background: transparent;")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(8)
        for item in items:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(8)
            time_label = QLabel(_format_order_time(item.get("time")))
            time_label.setFixedWidth(44)
            set_widget_style(time_label, "font-size: 12px; color: #64748B; font-weight: 800;")
            text_label = self._board_value_label(str(item.get("label") or "Препарат"), size=13, weight=700)
            pill = QLabel(str(item.get("kind_label") or "Болюс"))
            pill.setAlignment(Qt.AlignCenter)
            set_widget_style(pill, "background: #EEF2FF; color: #2563EB; border: 1px solid #C7D2FE; border-radius: 5px; padding: 2px 6px; font-size: 11px; font-weight: 800;")
            row.addWidget(time_label, 0)
            row.addWidget(text_label, 1)
            row.addWidget(pill, 0)
            content_layout.addLayout(row)
        content_layout.addStretch(1)
        scroll.setWidget(content)
        layout.addWidget(scroll, 0, Qt.AlignTop)
        self._scroll_board_area_to_bottom_when_ready(scroll)
        return block

    def _board_allergies_block(self, patient: dict) -> QFrame:
        allergies = normalize_operblock_team_text(patient.get("allergies"))
        block, layout = self._board_block("Аллергии", title_color="#EF4444")
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        if allergies:
            label = self._board_value_label(allergies, size=14, weight=800, color="#EF4444")
            row.addWidget(label, 1)
            row.addWidget(self._board_allergy_status_icon(has_allergies=True), 0, Qt.AlignRight | Qt.AlignTop)
        else:
            text = self._board_value_label("Не известны", size=14, weight=500)
            row.addWidget(text, 1)
            row.addWidget(self._board_allergy_status_icon(has_allergies=False), 0, Qt.AlignRight | Qt.AlignTop)
        layout.addLayout(row)
        self._disable_context_menu_for_widget_tree(block)
        return block

    def _board_special_notes_block(self, patient: dict) -> QFrame:
        block, layout = self._board_block("Особые отметки")
        layout.addWidget(self._board_muted_label("—", size=15))
        return block

    def _card_header(self, display_name: str) -> QLabel:
        apply_metrics = self._current_board_apply_metrics
        metric_fields = dict((apply_metrics or {}).get("current_card_fields") or {})
        metric_started = operblock_startup_metrics.timer_start() if apply_metrics is not None else 0.0
        label = QLabel(display_name)
        label.setFixedHeight(54)
        label.setAlignment(Qt.AlignCenter)
        set_widget_style(label, """
            QLabel {
                background-color: #F5F7FA;
                color: #1F2D3D;
                font-size: 22px;
                font-weight: 800;
                border-bottom: 1px solid #DDE3EA;
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
            }
            """)
        operblock_startup_metrics.record_since(
            "board_apply_card_header_ms",
            metric_started,
            source="operblock_widget",
            **metric_fields,
        )
        return label

    def _base_card(self) -> QFrame:
        apply_metrics = self._current_board_apply_metrics
        metric_fields = dict((apply_metrics or {}).get("current_card_fields") or {})
        metric_started = operblock_startup_metrics.timer_start() if apply_metrics is not None else 0.0
        frame = QFrame()
        frame.setObjectName("operblockTableCard")
        frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        stylesheet_started = operblock_startup_metrics.timer_start() if apply_metrics is not None else 0.0
        set_widget_style(frame, PATIENT_CARD_STYLE)
        operblock_startup_metrics.record_since(
            "board_apply_card_stylesheet_ms",
            stylesheet_started,
            source="operblock_widget",
            **metric_fields,
        )
        shadow_started = operblock_startup_metrics.timer_start() if apply_metrics is not None else 0.0
        shadow = QGraphicsDropShadowEffect(frame)
        shadow.setBlurRadius(18)
        shadow.setColor(QColor(0, 0, 0, 22))
        shadow.setOffset(3, 4)
        frame.setGraphicsEffect(shadow)
        operblock_startup_metrics.record_since(
            "board_apply_card_shadow_effect_ms",
            shadow_started,
            source="operblock_widget",
            **metric_fields,
        )
        operblock_startup_metrics.record_since(
            "board_apply_card_widget_create_ms",
            metric_started,
            source="operblock_widget",
            **metric_fields,
        )
        return frame
