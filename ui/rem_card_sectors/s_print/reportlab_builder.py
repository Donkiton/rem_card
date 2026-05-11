from __future__ import annotations

import html
import os
import pathlib
from datetime import datetime, timedelta
from typing import Any, Iterable

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Flowable,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from rem_card.services.order_domain_service import (
    NURSE_MARK_EXECUTED,
    NURSE_MARK_NOT_EXECUTED,
)

from .balance import format_signed_ml, get_current_balance_totals


class _PrescriptionMarkFlowable(Flowable):
    def __init__(self, role: str, mark_type: str | None, *, size: float, mini_size: float):
        super().__init__()
        self.role = str(role or "")
        self.mark_type = mark_type
        self.size = float(size)
        self.mini_size = float(mini_size)
        self.width = max(16.0, self.size * 2.4)
        self.height = max(8.0, self.size + 1.0)

    def wrap(self, availWidth, availHeight):
        self.width = min(float(availWidth), self.width) if availWidth else self.width
        return self.width, self.height

    def draw(self):
        canvas = self.canv
        canvas.saveState()
        try:
            center_y = self.height / 2.0
            if self.role == "single":
                self._draw_icon(canvas, self.width / 2.0, center_y, self.size, self.mark_type)
            elif self.role == "start":
                self._draw_icon(canvas, self.size / 2.0 + 1.0, center_y, self.size, self.mark_type)
                self._draw_arrow(canvas, self.size + 2.5, center_y, max(self.width - self.size - 4.0, 5.0))
            elif self.role == "body":
                arrow_width = self.width - (self.mini_size + 3.0 if self.mark_type else 0.0)
                self._draw_arrow(canvas, 1.0, center_y, max(arrow_width, 6.0))
                if self.mark_type:
                    self._draw_icon(canvas, self.width - self.mini_size / 2.0 - 1.0, center_y, self.mini_size, self.mark_type)
            elif self.role == "end":
                self._draw_arrow(canvas, 1.0, center_y, max(self.width - self.size - 4.0, 6.0))
                self._draw_icon(canvas, self.width - self.size / 2.0 - 1.0, center_y, self.size, self.mark_type)
            elif self.mark_type:
                self._draw_icon(canvas, self.width / 2.0, center_y, self.size, self.mark_type)
        finally:
            canvas.restoreState()

    @staticmethod
    def _draw_arrow(canvas, x: float, y: float, width: float) -> None:
        width = max(4.0, float(width))
        end_x = x + width
        canvas.setStrokeColor(colors.black)
        canvas.setFillColor(colors.black)
        canvas.setLineWidth(0.45)
        canvas.line(x, y, end_x - 2.0, y)
        canvas.line(end_x - 2.0, y, end_x - 4.0, y + 1.5)
        canvas.line(end_x - 2.0, y, end_x - 4.0, y - 1.5)

    @staticmethod
    def _draw_icon(canvas, x: float, y: float, size: float, mark_type: str | None) -> None:
        if not mark_type:
            return
        radius = max(2.0, float(size) / 2.0)
        if mark_type == "planned":
            canvas.setFillColor(colors.white)
            canvas.setStrokeColor(colors.Color(0.45, 0.45, 0.45))
            canvas.setLineWidth(0.55)
            canvas.circle(x, y, radius * 0.72, stroke=1, fill=0)
            return

        if mark_type == "done":
            canvas.setFillColor(colors.Color(0.78, 0.91, 0.66))
            canvas.setStrokeColor(colors.Color(0.28, 0.53, 0.20))
        else:
            canvas.setFillColor(colors.Color(0.92, 0.24, 0.24))
            canvas.setStrokeColor(colors.Color(0.50, 0.05, 0.05))
        canvas.setLineWidth(0.35)
        canvas.circle(x, y, radius, stroke=1, fill=1)

        if mark_type == "done":
            canvas.setStrokeColor(colors.Color(0.16, 0.35, 0.12))
            canvas.setLineWidth(max(0.55, size * 0.12))
            canvas.line(x - radius * 0.46, y - radius * 0.02, x - radius * 0.13, y - radius * 0.34)
            canvas.line(x - radius * 0.13, y - radius * 0.34, x + radius * 0.48, y + radius * 0.34)
        else:
            canvas.setStrokeColor(colors.white)
            canvas.setLineWidth(max(0.75, size * 0.16))
            canvas.line(x - radius * 0.40, y - radius * 0.40, x + radius * 0.40, y + radius * 0.40)
            canvas.line(x - radius * 0.40, y + radius * 0.40, x + radius * 0.40, y - radius * 0.40)


class ReportLabReportBuilder:
    HORIZONTAL_MARGIN_MM = 7
    VERTICAL_MARGIN_MM = 15
    SECTION_GAP_PT = 5
    TABLE_TITLE_BG = colors.HexColor("#f8f9fa")
    TABLE_TITLE_COLOR = colors.HexColor("#2c3e50")
    GRID_COLOR = colors.HexColor("#999999")
    LABEL_BG = colors.HexColor("#f8f9fa")
    FONT_REGULAR = "RemCardArial"
    FONT_BOLD = "RemCardArial-Bold"
    FONT_NARROW = "RemCardArialNarrow"
    PRESCRIPTION_MARK_ICON_SIZE_PT = 7.5
    PRESCRIPTION_MARK_ICON_MINI_SIZE_PT = 5.5

    @classmethod
    def build_pdf(cls, data, config, output_path):
        cls._ensure_fonts_registered()
        output_path = pathlib.Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        page_size = landscape(A4)
        margins = (
            cls.HORIZONTAL_MARGIN_MM * mm,
            cls.VERTICAL_MARGIN_MM * mm,
            cls.HORIZONTAL_MARGIN_MM * mm,
            cls.VERTICAL_MARGIN_MM * mm,
        )
        page_width = page_size[0] - margins[0] - margins[2]
        doc = SimpleDocTemplate(
            str(output_path),
            pagesize=page_size,
            leftMargin=margins[0],
            rightMargin=margins[2],
            topMargin=margins[1],
            bottomMargin=margins[3],
            pageCompression=1,
        )

        story = cls._build_story(data, config or {}, page_width)
        doc.build(story, onFirstPage=cls._draw_page_background, onLaterPages=cls._draw_page_background)
        if not output_path.exists() or output_path.stat().st_size <= 0:
            raise OSError(f"PDF file was not created: {output_path}")

    @staticmethod
    def _draw_page_background(canvas, doc) -> None:
        canvas.saveState()
        canvas.setFillColor(colors.white)
        canvas.rect(0, 0, doc.pagesize[0], doc.pagesize[1], stroke=0, fill=1)
        canvas.restoreState()

    @classmethod
    def _build_story(cls, data, config: dict, table_width: float):
        if isinstance(data, list):
            story = []
            for index, day_data in enumerate(data):
                if index:
                    story.append(PageBreak())
                story.extend(cls._build_day(day_data, config, table_width))
            return story
        return cls._build_day(data, config, table_width)

    @classmethod
    def _build_day(cls, data: dict, config: dict, table_width: float):
        hours = [str((8 + i) % 24) for i in range(24)]
        start_str = data["start_dt"].strftime("%d.%m.%Y %H:%M")
        end_str = data["end_dt"].strftime("%d.%m.%Y %H:%M")
        flowables = [
            cls._header_table(data, start_str, end_str, table_width, config.get("balance", True))
        ]

        sections = []
        if config.get("vitals", True):
            sections.append(cls._keep(cls._vitals_table(data, hours, table_width)))
        if config.get("prescriptions", True):
            sections.append(cls._prescriptions_table(data, hours, table_width))
        if config.get("balance", True):
            sections.append(cls._keep(cls._balance_flowables(data, hours, table_width)))
        if config.get("ventilation", False):
            ventilation = cls._ventilation_table(data, table_width)
            if ventilation:
                sections.append(cls._keep(ventilation))
        if config.get("events", True) and not data.get("hide_events_section", False):
            sections.append(cls._keep(cls._events_table(data, table_width)))

        include_death_outcome = config.get("death_outcome", False)
        include_death_protocol = config.get("death_protocol", include_death_outcome)
        death_sections = cls._death_flowables(
            data,
            table_width,
            include_outcome=include_death_outcome,
            include_protocol=include_death_protocol,
        )
        if death_sections:
            sections.append(cls._keep(death_sections))

        for section in sections:
            if flowables:
                flowables.append(Spacer(1, cls.SECTION_GAP_PT))
            if isinstance(section, list):
                flowables.extend(section)
            else:
                flowables.append(section)
        return flowables

    @classmethod
    def _ensure_fonts_registered(cls) -> None:
        if cls.FONT_REGULAR in pdfmetrics.getRegisteredFontNames():
            return

        windir = os.environ.get("WINDIR", r"C:\Windows")
        candidates = [
            (
                os.path.join(windir, "Fonts", "arial.ttf"),
                os.path.join(windir, "Fonts", "arialbd.ttf"),
                os.path.join(windir, "Fonts", "arialn.ttf"),
            ),
            (
                os.path.join(windir, "Fonts", "segoeui.ttf"),
                os.path.join(windir, "Fonts", "segoeuib.ttf"),
                os.path.join(windir, "Fonts", "segoeui.ttf"),
            ),
            (
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
            ),
        ]
        for regular, bold, narrow in candidates:
            if not os.path.exists(regular):
                continue
            pdfmetrics.registerFont(TTFont(cls.FONT_REGULAR, regular))
            pdfmetrics.registerFont(TTFont(cls.FONT_BOLD, bold if os.path.exists(bold) else regular))
            pdfmetrics.registerFont(TTFont(cls.FONT_NARROW, narrow if os.path.exists(narrow) else regular))
            return

        cls.FONT_REGULAR = "Helvetica"
        cls.FONT_BOLD = "Helvetica-Bold"
        cls.FONT_NARROW = "Helvetica"

    @classmethod
    def _style(
        cls,
        *,
        font_size: float = 6.0,
        bold: bool = False,
        alignment: int = TA_CENTER,
        leading: float | None = None,
    ) -> ParagraphStyle:
        return ParagraphStyle(
            name=f"RemCard-{font_size}-{bold}-{alignment}",
            fontName=cls.FONT_BOLD if bold else cls.FONT_REGULAR,
            fontSize=font_size,
            leading=leading if leading is not None else font_size + 1.0,
            alignment=alignment,
            splitLongWords=1,
            wordWrap="CJK",
            spaceBefore=0,
            spaceAfter=0,
        )

    @classmethod
    def _p(
        cls,
        value: Any,
        *,
        font_size: float = 6.0,
        bold: bool = False,
        alignment: int = TA_CENTER,
        leading: float | None = None,
        allow_markup: bool = False,
    ) -> Paragraph:
        if value in (None, ""):
            text = ""
        else:
            text = str(value)
        if not allow_markup:
            text = html.escape(text)
        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br/>")
        return Paragraph(text, cls._style(font_size=font_size, bold=bold, alignment=alignment, leading=leading))

    @staticmethod
    def _keep(flowables):
        if isinstance(flowables, list):
            return KeepTogether(flowables)
        return KeepTogether([flowables])

    @staticmethod
    def _hourly_widths(table_width: float, name_width: float = 120.0) -> list[float]:
        table_width = max(25.0, float(table_width))
        name_width = max(1.0, min(float(name_width), table_width - 24.0))
        remaining = table_width - name_width
        base = int(remaining // 24)
        extra = int(round(remaining - base * 24))
        widths = [name_width]
        for idx in range(24):
            widths.append(float(base + (1 if idx < extra else 0)))
        correction = table_width - sum(widths)
        widths[-1] += correction
        return widths

    @staticmethod
    def _weighted_widths(table_width: float, weights: Iterable[float]) -> list[float]:
        weights = list(weights)
        raw = [float(table_width) * float(weight) for weight in weights]
        widths = [int(value) for value in raw]
        remainder = int(round(float(table_width) - sum(widths)))
        fractions = sorted(
            enumerate(raw),
            key=lambda item: item[1] - int(item[1]),
            reverse=True,
        )
        for idx in range(max(0, remainder)):
            widths[fractions[idx % len(fractions)][0]] += 1
        if widths:
            widths[-1] += float(table_width) - sum(widths)
        return [float(width) for width in widths]

    @classmethod
    def _base_table_style(
        cls,
        *,
        title_row: bool = True,
        body_font_size: float = 6.0,
        header_font_size: float = 6.0,
        left_columns: tuple[int, ...] = (),
    ) -> list[tuple]:
        style = [
            ("GRID", (0, 0), (-1, -1), 0.25, cls.GRID_COLOR),
            ("FONTNAME", (0, 0), (-1, -1), cls.FONT_NARROW),
            ("FONTSIZE", (0, 0), (-1, -1), body_font_size),
            ("LEADING", (0, 0), (-1, -1), body_font_size + 1),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("LEFTPADDING", (0, 0), (-1, -1), 1),
            ("RIGHTPADDING", (0, 0), (-1, -1), 1),
            ("TOPPADDING", (0, 0), (-1, -1), 1),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ]
        if title_row:
            style.extend(
                [
                    ("SPAN", (0, 0), (-1, 0)),
                    ("BACKGROUND", (0, 0), (-1, 0), cls.TABLE_TITLE_BG),
                    ("TEXTCOLOR", (0, 0), (-1, 0), cls.TABLE_TITLE_COLOR),
                    ("FONTNAME", (0, 0), (-1, 0), cls.FONT_BOLD),
                    ("FONTSIZE", (0, 0), (-1, 0), 9),
                    ("LEADING", (0, 0), (-1, 0), 10),
                    ("TOPPADDING", (0, 0), (-1, 0), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, 0), 4),
                ]
            )
        header_row_index = 1 if title_row else 0
        style.extend(
            [
                ("FONTNAME", (0, header_row_index), (-1, header_row_index), cls.FONT_BOLD),
                ("FONTSIZE", (0, header_row_index), (-1, header_row_index), header_font_size),
            ]
        )
        for col in left_columns:
            style.append(("ALIGN", (col, header_row_index), (col, -1), "LEFT"))
        if title_row:
            style.append(("ALIGN", (0, 0), (-1, 0), "CENTER"))
        return style

    @classmethod
    def _header_table(cls, data: dict, start_str: str, end_str: str, table_width: float, show_hydrobalance: bool):
        patient_name = html.escape(str(data.get("patient_name", "—")))
        diagnosis = html.escape(str(data.get("diagnosis", "—")))
        icu_day = html.escape(str(data.get("icu_day", "—")))
        hydrobalance = ""
        if show_hydrobalance:
            totals = get_current_balance_totals(data)
            if totals is not None:
                hydrobalance = f' &nbsp;&nbsp; <b>Гидробаланс</b> = {html.escape(format_signed_ml(totals["net_balance"]))} мл'

        details = (
            f"<b>Пациент:</b> {patient_name} &nbsp;&nbsp; <b>Диагноз:</b> {diagnosis}<br/>"
            f"<b>Сутки в РАО:</b> {icu_day} &nbsp;&nbsp; "
            f"<b>Период:</b> {html.escape(start_str)} - {html.escape(end_str)}{hydrobalance}"
        )
        table = Table(
            [
                [
                    cls._p(details, font_size=8.5, alignment=TA_LEFT, leading=10, allow_markup=True),
                    cls._p("РЕАНИМАЦИОННАЯ КАРТА", font_size=10, bold=True, leading=11),
                ]
            ],
            colWidths=[table_width * 0.68, table_width * 0.32],
            hAlign="LEFT",
        )
        table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                    ("ALIGN", (1, 0), (1, 0), "CENTER"),
                ]
            )
        )
        return table

    @classmethod
    def _data_table(cls, rows, widths, *, style_commands=None, repeat_rows=2, split_by_row=1):
        table = Table(rows, colWidths=widths, repeatRows=repeat_rows, splitByRow=split_by_row, hAlign="LEFT")
        table.setStyle(TableStyle(style_commands or []))
        return table

    @staticmethod
    def _hourly_get(matrix: dict, hour_index: int, key: str, default: Any = "") -> Any:
        row = matrix.get(hour_index)
        if row is None:
            row = matrix.get(str(hour_index), {})
        if not isinstance(row, dict):
            return default
        return row.get(key, default)

    @classmethod
    def _vitals_table(cls, data: dict, hours: list[str], table_width: float):
        widths = cls._hourly_widths(table_width, 120.0)
        rows = [["ТАБЛИЦА ПОКАЗАТЕЛЕЙ", *[""] * 24]]
        rows.append([cls._p("Час", bold=True, alignment=TA_LEFT), *[cls._p(hour, bold=True) for hour in hours]])

        matrix = data.get("vitals_matrix", {}) or {}
        settings = data.get("vital_settings", {"ad": 1, "pulse": 1, "temp": 1, "spo2": 1, "rr": 0, "cvp": 0}) or {}
        indicators = [
            ("АД", "ad", settings.get("ad")),
            ("ЧСС", "hr", settings.get("pulse")),
            ("Темп", "temp", settings.get("temp")),
            ("ЧДД", "rr", settings.get("rr")),
            ("SpO2", "spo2", settings.get("spo2")),
            ("ЦВД", "cvp", settings.get("cvp")),
        ]
        for label, key, visible in indicators:
            if not visible:
                continue
            row = [cls._p(label, font_size=6, alignment=TA_LEFT)]
            for idx in range(24):
                if key == "ad":
                    sys_value = cls._hourly_get(matrix, idx, "sys", "")
                    dia_value = cls._hourly_get(matrix, idx, "dia", "")
                    value = ""
                    if sys_value not in (None, "") or dia_value not in (None, ""):
                        value = f"{sys_value or ''}\n{dia_value or ''}"
                    row.append(cls._p(value, font_size=5.5, leading=5.8))
                elif key == "cvp":
                    value = cls._hourly_get(matrix, idx, "cvp", "")
                    row.append("Н/Н" if value == -1 else str(value or ""))
                else:
                    value = cls._hourly_get(matrix, idx, key, "")
                    row.append(str(value or ""))
            rows.append(row)

        style = cls._base_table_style(title_row=True, body_font_size=6, header_font_size=6, left_columns=(0,))
        return cls._data_table(rows, widths, style_commands=style, repeat_rows=2)

    @staticmethod
    def _is_due(planned_time) -> bool:
        if planned_time is None:
            return True
        if isinstance(planned_time, datetime):
            planned_dt = planned_time
        else:
            try:
                planned_dt = datetime.fromisoformat(str(planned_time).replace(" ", "T"))
            except ValueError:
                return True
        return planned_dt + timedelta(hours=1) <= datetime.now()

    @classmethod
    def _mark_type(
        cls,
        nurse_mark: str,
        planned_time,
        *,
        fallback_to_x: bool,
        auto_not_done: bool = True,
    ) -> str | None:
        effective_mark = nurse_mark
        if auto_not_done and not effective_mark and not fallback_to_x and cls._is_due(planned_time):
            effective_mark = NURSE_MARK_NOT_EXECUTED
        if effective_mark == NURSE_MARK_EXECUTED:
            return "done"
        if effective_mark == NURSE_MARK_NOT_EXECUTED:
            return "notdone"
        return "notdone" if fallback_to_x else None

    @classmethod
    def _render_mark_cell(cls, mark):
        fallback_to_x = not isinstance(mark, dict)
        if isinstance(mark, dict):
            role = mark.get("role")
            nurse_mark = str(mark.get("nurse_mark") or mark.get("comment") or "")
            planned_time = mark.get("planned_time")
        else:
            role = mark
            nurse_mark = ""
            planned_time = None

        mark_type = cls._mark_type(
            nurse_mark,
            planned_time,
            fallback_to_x=fallback_to_x,
        )
        body_mark_type = cls._mark_type(
            nurse_mark,
            planned_time,
            fallback_to_x=False,
            auto_not_done=False,
        )
        if not mark_type and not fallback_to_x:
            mark_type = "planned"
        if role == "single":
            return _PrescriptionMarkFlowable(
                role,
                mark_type,
                size=cls.PRESCRIPTION_MARK_ICON_SIZE_PT,
                mini_size=cls.PRESCRIPTION_MARK_ICON_MINI_SIZE_PT,
            )
        elif role == "start":
            return _PrescriptionMarkFlowable(
                role,
                mark_type,
                size=cls.PRESCRIPTION_MARK_ICON_SIZE_PT,
                mini_size=cls.PRESCRIPTION_MARK_ICON_MINI_SIZE_PT,
            )
        elif role == "body":
            return _PrescriptionMarkFlowable(
                role,
                body_mark_type,
                size=cls.PRESCRIPTION_MARK_ICON_SIZE_PT,
                mini_size=cls.PRESCRIPTION_MARK_ICON_MINI_SIZE_PT,
            )
        elif role == "end":
            return _PrescriptionMarkFlowable(
                role,
                mark_type,
                size=cls.PRESCRIPTION_MARK_ICON_SIZE_PT,
                mini_size=cls.PRESCRIPTION_MARK_ICON_MINI_SIZE_PT,
            )
        return ""

    @staticmethod
    def _chain_key(mark):
        if not isinstance(mark, dict):
            return None
        return mark.get("chain_key") or mark.get("big_chain_id") or mark.get("chain_id")

    @staticmethod
    def _is_explicit_not_done(mark) -> bool:
        if not isinstance(mark, dict):
            return False
        return (mark.get("nurse_mark") or mark.get("comment") or "") == NURSE_MARK_NOT_EXECUTED

    @classmethod
    def _prescriptions_table(cls, data: dict, hours: list[str], table_width: float):
        widths = cls._hourly_widths(table_width, 120.0)
        rows = [["ЛИСТ НАЗНАЧЕНИЙ", *[""] * 24]]
        rows.append(
            [
                cls._p("Препарат / Дозировка", bold=True, alignment=TA_LEFT),
                *[cls._p(hour, bold=True) for hour in hours],
            ]
        )

        matrix = data.get("prescriptions_matrix", []) or []
        if not matrix:
            rows.append([cls._p("Нет назначений за период", font_size=8), *[""] * 24])
            spans = [("SPAN", (0, 2), (-1, 2))]
        else:
            spans = []
            for item in matrix:
                name_data = item.get("name", "")
                if isinstance(name_data, list):
                    name_text = "\n".join(str(part) for part in name_data)
                else:
                    name_text = str(name_data)
                row = [cls._p(name_text, font_size=6, alignment=TA_LEFT, leading=7)]
                stopped_chains = set()
                for mark in list(item.get("marks") or [])[:24]:
                    chain_key = cls._chain_key(mark)
                    if chain_key and chain_key in stopped_chains:
                        display = ""
                    else:
                        display = cls._render_mark_cell(mark)
                        if chain_key and cls._is_explicit_not_done(mark):
                            stopped_chains.add(chain_key)
                    row.append(display)
                while len(row) < 25:
                    row.append("")
                rows.append(row)

        style = cls._base_table_style(title_row=True, body_font_size=6, header_font_size=6, left_columns=(0,))
        style.extend(spans)
        style.append(("FONTNAME", (1, 2), (-1, -1), cls.FONT_REGULAR))
        return cls._data_table(rows, widths, style_commands=style, repeat_rows=2)

    @staticmethod
    def _format_hourly_value(value):
        if value:
            return str(int(round(float(value))))
        return ""

    @classmethod
    def _hourly_balance_table(cls, title: str, row_specs, hourly_data: dict, hours: list[str], table_width: float):
        widths = cls._hourly_widths(table_width, 120.0)
        rows = [[title, *[""] * 24]]
        rows.append([cls._p("Тип", bold=True, alignment=TA_LEFT), *[cls._p(hour, bold=True) for hour in hours]])
        for label, key in row_specs:
            row = [cls._p(label, font_size=6, alignment=TA_LEFT)]
            for idx in range(24):
                row.append(cls._format_hourly_value(cls._hourly_get(hourly_data, idx, key, "")))
            rows.append(row)
        style = cls._base_table_style(title_row=True, body_font_size=6, header_font_size=6, left_columns=(0,))
        return cls._data_table(rows, widths, style_commands=style, repeat_rows=2)

    @classmethod
    def _balance_flowables(cls, data: dict, hours: list[str], table_width: float):
        bf = data.get("balance_final", {}) or {}
        out_hourly = bf.get("out_hourly", {}) or {}
        in_hourly = bf.get("in_hourly", {}) or {}
        totals = get_current_balance_totals(data) or {"total_in": 0, "total_out": 0}
        in_rows = [
            ("Инфузия", "infusion"),
            ("Препараты", "preparats"),
            ("Кровь", "blood"),
            ("Плазма", "plasma"),
            ("Перорально", "oral"),
        ]
        out_rows = [
            ("Диурез", "urine"),
            ("Дренажи", "drain"),
            ("ЖКТ (зонд)", "ng"),
            ("Рвота", "stool"),
            ("Другое", "other"),
        ]
        return [
            cls._hourly_balance_table(f'ПОЧАСОВОЕ ВВЕДЕНИЕ. {totals["total_in"]} мл.', in_rows, in_hourly, hours, table_width),
            Spacer(1, 0),
            cls._hourly_balance_table(f'ПОЧАСОВОЕ ВЫВЕДЕНИЕ. {totals["total_out"]} мл.', out_rows, out_hourly, hours, table_width),
        ]

    @classmethod
    def _events_table(cls, data: dict, table_width: float):
        widths = cls._weighted_widths(table_width, [0.25, 0.20, 0.55])
        rows = [["ДВИЖЕНИЕ", "", ""]]
        rows.append(
            [
                cls._p("Дата и Время", bold=True),
                cls._p("Где / Статус", bold=True),
                cls._p("Комментарий", bold=True, alignment=TA_LEFT),
            ]
        )
        events = data.get("events_struct", []) or []
        if not events:
            rows.append([cls._p("Нет событий за период", font_size=8), "", ""])
            spans = [("SPAN", (0, 2), (-1, 2))]
        else:
            spans = []
            for event in events:
                rows.append(
                    [
                        cls._p(event.get("time", ""), font_size=6.5),
                        cls._p(event.get("status", ""), font_size=6.5),
                        cls._p(event.get("desc", "—"), font_size=6.5, alignment=TA_LEFT),
                    ]
                )
        style = cls._base_table_style(title_row=True, body_font_size=6.5, header_font_size=6.5, left_columns=(2,))
        style.extend(spans)
        return cls._data_table(rows, widths, style_commands=style, repeat_rows=2)

    @classmethod
    def _ventilation_table(cls, data: dict, table_width: float):
        vent_rows = data.get("ventilation_struct", []) or []
        if not vent_rows:
            return None
        widths = cls._weighted_widths(table_width, [0.20, 0.16, 0.16, 0.24, 0.24])
        rows = [["ИСТОРИЯ СОБЫТИЙ ИВЛ", "", "", "", ""]]
        rows.append(
            [
                cls._p("Время", bold=True),
                cls._p("Событие", bold=True),
                cls._p("Режим", bold=True),
                cls._p("Параметры", bold=True, alignment=TA_LEFT),
                cls._p("Показания", bold=True, alignment=TA_LEFT),
            ]
        )
        for row in vent_rows:
            rows.append(
                [
                    cls._p(row.get("time", "—"), font_size=6.5),
                    cls._p(row.get("event", "—"), font_size=6.5),
                    cls._p(row.get("mode", "—"), font_size=6.5),
                    cls._p(row.get("params", "—"), font_size=6.5, alignment=TA_LEFT),
                    cls._p(row.get("indications", "—"), font_size=6.5, alignment=TA_LEFT),
                ]
            )
        style = cls._base_table_style(title_row=True, body_font_size=6.5, header_font_size=6.5, left_columns=(3, 4))
        return cls._data_table(rows, widths, style_commands=style, repeat_rows=2)

    @staticmethod
    def _plain(value: Any) -> str:
        return str(value or "").strip()

    @classmethod
    def _measures_text(cls, measures: list[dict[str, str]]) -> str:
        rows = []
        for item in measures or []:
            name = cls._plain(item.get("name"))
            value = cls._plain(item.get("value"))
            if name and value:
                rows.append(f"{name}: {value}")
            elif name:
                rows.append(name)
            elif value:
                rows.append(value)
        return "\n".join(rows) if rows else "—"

    @classmethod
    def _death_flowables(
        cls,
        data: dict,
        table_width: float,
        *,
        include_outcome: bool,
        include_protocol: bool,
    ):
        details = data.get("death_outcome") or {}
        if not details or not (include_outcome or include_protocol):
            return []
        flowables = []
        if include_outcome:
            flowables.append(cls._death_outcome_table(details, table_width))
        if include_protocol:
            if flowables:
                flowables.append(Spacer(1, cls.SECTION_GAP_PT))
            flowables.append(cls._death_protocol_table(details, table_width))
        return flowables

    @classmethod
    def _death_outcome_table(cls, details: dict, table_width: float):
        widths = cls._weighted_widths(table_width, [0.24, 0.76])
        rows = [["ИСХОД: СМЕРТЬ", ""]]

        def row(label, value):
            rows.append(
                [
                    cls._p(label, font_size=7.5, bold=True, alignment=TA_LEFT),
                    cls._p(value, font_size=7.5, alignment=TA_LEFT, leading=9),
                ]
            )

        row("Время клинической смерти", details.get("clinical_time") or "—")
        row("Причина остановки сердца", details.get("cause") or "—")
        row("Мероприятия", cls._measures_text(details.get("measures") or []))
        row("Комментарий к причине остановки сердца", details.get("comment") or "—")
        row("Время биологической смерти", details.get("biological_time") or "—")
        row("Врач", f"{details.get('doctor') or '____________________________________________'}    Подпись ______________________________")

        style = cls._base_table_style(title_row=True, body_font_size=7.5, header_font_size=7.5, left_columns=(0, 1))
        style.extend(
            [
                ("SPAN", (0, 0), (-1, 0)),
                ("BACKGROUND", (0, 1), (0, -1), cls.LABEL_BG),
                ("FONTNAME", (0, 1), (0, -1), cls.FONT_BOLD),
                ("TOPPADDING", (0, 1), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 1), (-1, -1), 3),
            ]
        )
        return cls._data_table(rows, widths, style_commands=style, repeat_rows=1)

    @classmethod
    def _death_protocol_table(cls, details: dict, table_width: float):
        protocol = details.get("protocol") or {}
        widths = cls._weighted_widths(table_width, [0.18, 0.32, 0.22, 0.28])
        protocol_death_dt = " ".join(
            part
            for part in (
                cls._plain(protocol.get("biological_death_date")),
                cls._plain(protocol.get("biological_death_time")),
            )
            if part
        ) or cls._plain(details.get("biological_time"))
        rows = [["ПРОТОКОЛ УСТАНОВЛЕНИЯ СМЕРТИ ЧЕЛОВЕКА", "", "", ""]]

        def row(left_label, left_value, right_label="", right_value=""):
            rows.append(
                [
                    cls._p(left_label, font_size=7.3, bold=True, alignment=TA_LEFT),
                    cls._p(left_value or "—", font_size=7.3, alignment=TA_LEFT, leading=8.5),
                    cls._p(right_label, font_size=7.3, bold=True, alignment=TA_LEFT),
                    cls._p(right_value or "—", font_size=7.3, alignment=TA_LEFT, leading=8.5),
                ]
            )

        row("Основание", "Постановление Правительства РФ от 20.09.2012 № 950", "Дата рождения", protocol.get("birth_date"))
        row("Врач", protocol.get("doctor"), "Пол", protocol.get("gender"))
        row("Должность", protocol.get("position"), "СЛР остановлена по причине", protocol.get("cpr_stop_reason"))
        row("Место работы", protocol.get("workplace"), "Дата и время биологической смерти", protocol_death_dt)
        row("Пациент", protocol.get("patient"), "ФИО врача", protocol.get("signature_doctor") or protocol.get("doctor"))
        row("Номер истории", protocol.get("history_number"), "Подпись", "______________________________")
        if cls._plain(protocol.get("other")):
            row("Иное", protocol.get("other"), "", "")

        style = cls._base_table_style(title_row=True, body_font_size=7.3, header_font_size=7.3, left_columns=(0, 1, 2, 3))
        style.extend(
            [
                ("SPAN", (0, 0), (-1, 0)),
                ("BACKGROUND", (0, 1), (0, -1), cls.LABEL_BG),
                ("BACKGROUND", (2, 1), (2, -1), cls.LABEL_BG),
                ("FONTNAME", (0, 1), (0, -1), cls.FONT_BOLD),
                ("FONTNAME", (2, 1), (2, -1), cls.FONT_BOLD),
                ("TOPPADDING", (0, 1), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 1), (-1, -1), 3),
            ]
        )
        return cls._data_table(rows, widths, style_commands=style, repeat_rows=1)
