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

from .balance import format_hourly_balance_value, format_signed_ml, get_current_balance_totals


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


class _RotatedProtocolFlowable(Flowable):
    def __init__(self, inner: Flowable):
        super().__init__()
        self.inner = inner
        self.inner_width = 1.0
        self.inner_height = 1.0
        self.scale = 1.0
        self.width = 1.0
        self.height = 1.0

    def wrap(self, availWidth, availHeight):
        self.inner_width, self.inner_height = self.inner.wrap(availHeight, availWidth)
        self.scale = min(
            1.0,
            float(availWidth) / max(float(self.inner_height), 1.0),
            float(availHeight) / max(float(self.inner_width), 1.0),
        )
        self.width = self.inner_height * self.scale
        self.height = self.inner_width * self.scale
        return self.width, self.height

    def draw(self):
        canvas = self.canv
        canvas.saveState()
        try:
            canvas.scale(self.scale, self.scale)
            canvas.rotate(90)
            self.inner.drawOn(canvas, 0, -self.inner_height)
        finally:
            canvas.restoreState()


class _TransfusionRegistrationSheetFlowable(Flowable):
    ROWS_PER_PAGE = 17
    HEADER_BG = colors.HexColor("#ccffff")
    NUMBER_BG = colors.HexColor("#ffffcc")

    def __init__(self, sheet: dict[str, Any], rows: list[dict[str, str]], *, font_regular: str, font_bold: str, font_narrow: str):
        super().__init__()
        self.sheet = dict(sheet or {})
        self.rows = list(rows or [])
        self.font_regular = font_regular
        self.font_bold = font_bold
        self.font_narrow = font_narrow
        self.width, self.height = landscape(A4)

    def wrap(self, availWidth, availHeight):
        return min(float(availWidth or self.width), self.width), min(float(availHeight or self.height), self.height)

    def drawOn(self, canvas, x, y, _sW=0):
        del x, y, _sW
        self.canv = canvas
        self.draw()

    def draw(self):
        canvas = self.canv
        canvas.saveState()
        try:
            self._draw_page_text(canvas)
            self._draw_table(canvas)
            self._draw_footer(canvas)
        finally:
            canvas.restoreState()

    def _style(self, *, size: float, bold: bool = False, alignment: int = TA_CENTER, leading: float | None = None):
        return ParagraphStyle(
            name=f"TransfusionRegistration-{size}-{bold}-{alignment}",
            fontName=self.font_bold if bold else self.font_narrow,
            fontSize=size,
            leading=leading if leading is not None else size + 1,
            alignment=alignment,
            splitLongWords=1,
            wordWrap="CJK",
            spaceBefore=0,
            spaceAfter=0,
        )

    def _paragraph(self, text: Any, *, size: float, bold: bool = False, alignment: int = TA_CENTER, leading: float | None = None):
        value = html.escape(str(text or ""))
        value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br/>")
        return Paragraph(value, self._style(size=size, bold=bold, alignment=alignment, leading=leading))

    def _draw_paragraph(self, canvas, text: Any, x1: float, y1: float, x2: float, y2: float, *, size: float, bold: bool = False, alignment: int = TA_CENTER, leading: float | None = None):
        paragraph = self._paragraph(text, size=size, bold=bold, alignment=alignment, leading=leading)
        width = max(1.0, x2 - x1 - 2.0)
        height = max(1.0, y2 - y1 - 1.0)
        _wrapped_w, wrapped_h = paragraph.wrap(width, height)
        draw_y = y1 + max(0.8, (height - wrapped_h) / 2.0)
        paragraph.drawOn(canvas, x1 + 1.0, draw_y)

    def _draw_line_value(self, canvas, label: str, value: str, x_label: float, x1: float, x2: float, y: float, *, bold_value: bool = False):
        canvas.setFont(self.font_regular, 7.0)
        canvas.drawString(x_label, y + 2.0, label)
        canvas.line(x1, y, x2, y)
        font = self.font_bold if bold_value else self.font_regular
        canvas.setFont(font, 7.0)
        canvas.drawCentredString((x1 + x2) / 2.0, y + 2.0, str(value or ""))

    def _draw_page_text(self, canvas):
        patient_name = str(self.sheet.get("patient_name") or "")
        history_number = str(self.sheet.get("history_number") or "")
        recipient_abo = str(self.sheet.get("recipient_abo") or "")
        recipient_rh = str(self.sheet.get("recipient_rh") or "")

        canvas.setFillColor(colors.black)
        canvas.setFont(self.font_regular, 7.5)
        canvas.drawString(682, 552, "Учетная медицинская документация")
        canvas.drawString(682, 540, "форма № 005/у-04 приказ № 1030")

        canvas.setFont(self.font_bold, 8.5)
        canvas.drawCentredString(421, 468, "ЛИСТ РЕГИСТРАЦИИ ПЕРЕЛИВАНИЯ ТРАНСФУЗИОННЫХ СРЕДСТВ")

        self._draw_line_value(canvas, "Фамилия, имя, отчество больного", patient_name, 44, 190, 545, 430, bold_value=True)
        self._draw_line_value(canvas, "Группа крови больного", recipient_abo, 44, 145, 545, 419)
        canvas.setFont(self.font_regular, 7.0)
        canvas.drawString(44, 408, "Медицинская карта №")
        canvas.line(145, 406, 265, 406)
        canvas.drawCentredString(205, 408, history_number)
        canvas.drawString(276, 408, "Резус-принадлежность")
        canvas.line(365, 406, 545, 406)
        canvas.drawCentredString(455, 408, recipient_rh)

        note = (
            "(каждое переливание крови производится только после\n"
            "подтверждения групп крови донора и реципиента двумя\n"
            "сериями стандартных изогемагглютинирующих сывороток,\n"
            "проведения пробы на индивидуальную совместимость и\n"
            "биологической пробы)"
        )
        self._draw_paragraph(canvas, note, 194, 354, 545, 405, size=7.5, leading=9)

    @staticmethod
    def _column_widths(total_width: float) -> list[float]:
        weights = [
            4.0, 6.57, 8.29, 5.97, 4.0, 6.0, 8.0, 11.0,
            11.43, 7.86, 8.29, 5.14, 5.43, 5.14, 6.88, 6.0,
        ]
        total = sum(weights)
        widths = [total_width * weight / total for weight in weights]
        widths[-1] += total_width - sum(widths)
        return widths

    @staticmethod
    def _row_chunks(rows: list[dict[str, str]]) -> list[dict[str, str]]:
        visible = list(rows or [])[: _TransfusionRegistrationSheetFlowable.ROWS_PER_PAGE]
        while len(visible) < _TransfusionRegistrationSheetFlowable.ROWS_PER_PAGE:
            visible.append({})
        return visible

    def _draw_table(self, canvas):
        x0 = 42.0
        table_top = 350.0
        table_width = 770.0
        h_group = 22.0
        h_sub = 55.0
        h_numbers = 12.0
        row_h = 11.2
        data_top = table_top - h_group - h_sub - h_numbers
        table_bottom = data_top - self.ROWS_PER_PAGE * row_h

        widths = self._column_widths(table_width)
        xs = [x0]
        for width in widths:
            xs.append(xs[-1] + width)

        canvas.setFillColor(self.HEADER_BG)
        canvas.rect(x0, table_top - h_group - h_sub, table_width, h_group + h_sub, stroke=0, fill=1)
        canvas.setFillColor(self.NUMBER_BG)
        canvas.rect(x0, data_top, table_width, h_numbers, stroke=0, fill=1)

        canvas.setStrokeColor(colors.black)
        canvas.setLineWidth(0.65)
        canvas.rect(x0, table_bottom, table_width, table_top - table_bottom, stroke=1, fill=0)
        for x in xs[1:-1]:
            canvas.line(x, table_bottom, x, table_top)

        y_group_bottom = table_top - h_group
        y_sub_bottom = table_top - h_group - h_sub
        canvas.line(xs[5], y_group_bottom, xs[14], y_group_bottom)
        canvas.line(xs[11], table_top - h_group - 24.0, xs[13], table_top - h_group - 24.0)
        canvas.line(x0, y_sub_bottom, xs[-1], y_sub_bottom)
        canvas.line(x0, data_top, xs[-1], data_top)
        for row_index in range(self.ROWS_PER_PAGE + 1):
            y = data_top - row_index * row_h
            canvas.line(x0, y, xs[-1], y)

        self._draw_table_headers(canvas, xs, table_top, y_group_bottom, y_sub_bottom, data_top)
        self._draw_table_rows(canvas, xs, data_top, row_h)

    def _draw_table_headers(self, canvas, xs, table_top, y_group_bottom, y_sub_bottom, data_top):
        h_group = table_top - y_group_bottom
        full_header_labels = {
            0: "№\nп/п",
            1: "Дата",
            2: "Показания к\nпереливанию",
            3: "Способ\nпереливания",
            4: "Количество,\nмл",
            14: "Реакция, Т,\nосложнения\n(какие именно),\nколичество\nмочи и ее\nанализ",
            15: "Подпись\nврача\n(разборчиво)",
        }
        for col, label in full_header_labels.items():
            self._draw_paragraph(canvas, label, xs[col], y_sub_bottom, xs[col + 1], table_top, size=6.0, leading=6.8)

        self._draw_paragraph(canvas, "Паспорт трансфузионной среды", xs[5], y_group_bottom, xs[11], table_top, size=7.0)
        self._draw_paragraph(canvas, "Пробы", xs[11], y_group_bottom, xs[14], table_top, size=7.0)

        sublabels = {
            5: "трансфузионная\nсреда",
            6: "групповая\nпринадлежность",
            7: "резус-\nпринадлежность",
            8: "№ этикетки, серия\nпрепарата",
            9: "дата\nзаготовки",
            10: "фамилия, имя,\nотчество",
            13: "Биологическая",
        }
        for col, label in sublabels.items():
            self._draw_paragraph(canvas, label, xs[col], y_sub_bottom, xs[col + 1], y_group_bottom, size=6.0, leading=6.8)
        self._draw_paragraph(canvas, "Индивидуальная\nсовместимость", xs[11], table_top - h_group - 24.0, xs[13], y_group_bottom, size=6.0, leading=6.8)
        self._draw_paragraph(canvas, "группа", xs[11], y_sub_bottom, xs[12], table_top - h_group - 24.0, size=6.0)
        self._draw_paragraph(canvas, "резус", xs[12], y_sub_bottom, xs[13], table_top - h_group - 24.0, size=6.0)

        for col in range(16):
            self._draw_paragraph(canvas, str(col + 1), xs[col], data_top, xs[col + 1], y_sub_bottom, size=5.8)

    @staticmethod
    def _short_doctor(value: str) -> str:
        parts = str(value or "").strip().split()
        if len(parts) >= 3:
            return f"{parts[0]} {parts[1][:1]}.{parts[2][:1]}."
        if len(parts) == 2:
            return f"{parts[0]} {parts[1][:1]}."
        return str(value or "").strip()

    @staticmethod
    def _cell_value(row: dict[str, str], key: str) -> str:
        return str((row or {}).get(key) or "")

    def _draw_table_rows(self, canvas, xs, data_top, row_h):
        keys = [
            None,
            "date",
            "indication",
            "method",
            "volume_ml",
            "component",
            "donor_abo",
            "donor_rh",
            "unit_number",
            "collection_date",
            "donor_code",
            "compat_group",
            "compat_rh",
            "biological_test",
            "reaction",
            "doctor",
        ]
        for row_index, row in enumerate(self._row_chunks(self.rows)):
            y1 = data_top - (row_index + 1) * row_h
            y2 = data_top - row_index * row_h
            for col, key in enumerate(keys):
                value = str(row_index + 1) if key is None else self._cell_value(row, key)
                if key == "doctor":
                    value = self._short_doctor(value)
                font_size = 4.8 if col in (8, 10, 15) else 5.1
                self._draw_paragraph(canvas, value, xs[col], y1, xs[col + 1], y2, size=font_size, leading=5.3)

    def _draw_footer(self, canvas):
        canvas.setFillColor(colors.black)
        canvas.setFont(self.font_regular, 5.6)
        canvas.drawString(55, 20, "© ИПС ЭКСПЕРТ")
        canvas.drawCentredString(421, 20, "(017) 254 78 51, 254 78 61")
        canvas.drawRightString(805, 20, "www.expert.by")


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
            story.extend(cls._transfusion_registration_flowables(data, config))
            story.extend(cls._transfusion_protocol_flowables(data))
            return story
        story = cls._build_day(data, config, table_width)
        story.extend(cls._transfusion_registration_flowables(data, config))
        story.extend(cls._transfusion_protocol_flowables(data))
        return story

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
    def _transfusion_registration_flowables(cls, data, config: dict) -> list:
        if not config.get("transfusion_registration", True):
            return []
        sheet = cls._resolve_transfusion_registration_sheet(data)
        rows = list((sheet or {}).get("rows") or [])
        if not rows:
            return []
        flowables = []
        for start in range(0, len(rows), _TransfusionRegistrationSheetFlowable.ROWS_PER_PAGE):
            flowables.append(PageBreak())
            flowables.append(
                _TransfusionRegistrationSheetFlowable(
                    sheet,
                    rows[start:start + _TransfusionRegistrationSheetFlowable.ROWS_PER_PAGE],
                    font_regular=cls.FONT_REGULAR,
                    font_bold=cls.FONT_BOLD,
                    font_narrow=cls.FONT_NARROW,
                )
            )
        return flowables

    @staticmethod
    def _resolve_transfusion_registration_sheet(data) -> dict:
        if isinstance(data, list):
            if data and data[0].get("transfusion_registration_all"):
                return dict(data[0]["transfusion_registration_all"])
            rows = []
            sheet = {}
            for day in data:
                current = day.get("transfusion_registration") or {}
                if current and not sheet:
                    sheet = dict(current)
                rows.extend(list(current.get("rows") or []))
            if sheet:
                sheet["rows"] = rows
            return sheet
        return dict(data.get("transfusion_registration") or {})

    @classmethod
    def _transfusion_protocol_flowables(cls, data) -> list:
        protocols = cls._resolve_pending_transfusion_protocols(data)
        if not protocols:
            return []
        from rem_card.services.procedures_reportlab_builder import ProcedureReportLabBuilder

        flowables = []
        for protocol in protocols:
            context = protocol.get("context") if isinstance(protocol, dict) else None
            if not isinstance(context, dict):
                continue
            flowables.append(PageBreak())
            flowables.extend(
                _RotatedProtocolFlowable(flowable)
                for flowable in ProcedureReportLabBuilder.transfusion_protocol_flowables(context)
            )
        return flowables

    @staticmethod
    def _resolve_pending_transfusion_protocols(data) -> list:
        if isinstance(data, list):
            protocols = []
            for day in data:
                protocols.extend(list((day or {}).get("pending_transfusion_protocols") or []))
            return protocols
        return list((data or {}).get("pending_transfusion_protocols") or [])

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
        report_title = str(data.get("report_title") or "РЕАНИМАЦИОННАЯ КАРТА")
        hydrobalance = ""
        if show_hydrobalance:
            totals = get_current_balance_totals(data)
            if totals is not None:
                hydrobalance = f' &nbsp;&nbsp; <b>Гидробаланс</b> = {html.escape(format_signed_ml(totals["net_balance"]))} мл'
        notice_number = html.escape(str(data.get("emergency_notice_number") or "").strip())
        notice = ""
        if notice_number:
            notice = f' &nbsp;&nbsp; <b>Экстренное извещение</b> N {notice_number}'

        details = (
            f"<b>Пациент:</b> {patient_name} &nbsp;&nbsp; <b>Диагноз:</b> {diagnosis}<br/>"
            f"<b>Сутки в РАО:</b> {icu_day} &nbsp;&nbsp; "
            f"<b>Период:</b> {html.escape(start_str)} - {html.escape(end_str)}{hydrobalance}{notice}"
        )
        table = Table(
            [
                [
                    cls._p(details, font_size=8.5, alignment=TA_LEFT, leading=10, allow_markup=True),
                    cls._p(report_title, font_size=10, bold=True, leading=11),
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
        return format_hourly_balance_value(value)

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
                    cls._p(row.get("indications", "—"), font_size=6.5, alignment=TA_LEFT, allow_markup=True),
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
        items = details.get("items") if isinstance(details.get("items"), list) else [details]
        flowables = []
        for item in items:
            if not isinstance(item, dict):
                continue
            outcome_kind = str(item.get("outcome_kind") or "death")
            if include_outcome:
                if flowables:
                    flowables.append(Spacer(1, cls.SECTION_GAP_PT))
                flowables.append(cls._death_outcome_table(item, table_width))
            if include_protocol and outcome_kind != "recovery":
                if flowables:
                    flowables.append(Spacer(1, cls.SECTION_GAP_PT))
                flowables.append(cls._death_protocol_table(item, table_width))
        return flowables

    @classmethod
    def _death_outcome_table(cls, details: dict, table_width: float):
        widths = cls._weighted_widths(table_width, [0.24, 0.76])
        outcome_kind = str(details.get("outcome_kind") or "death")
        rows = [[details.get("title") or "ОСТАНОВКА СЕРДЕЧНОЙ ДЕЯТЕЛЬНОСТИ. ИСХОД: СМЕРТЬ.", ""]]

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
        if outcome_kind == "recovery":
            row("Время восстановления кровообращения", details.get("recovery_time") or "—")
            row("Длительность СЛР", details.get("cpr_duration") or "—")
        else:
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
