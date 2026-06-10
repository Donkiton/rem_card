from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import html
import pathlib
import re
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Flowable, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from rem_card.ui.rem_card_sectors.s_print.reportlab_builder import ReportLabReportBuilder


class _VitalsMedicationChartFlowable(Flowable):
    VITAL_MAX = 250
    VITAL_MIN = 0

    def __init__(self, context: dict[str, Any], *, width: float, height: float, full_page: bool = False):
        super().__init__()
        self.context = dict(context or {})
        self.width = float(width)
        self.height = float(height)
        self.full_page = bool(full_page)
        self.font_regular = ReportLabReportBuilder.FONT_REGULAR
        self.font_bold = ReportLabReportBuilder.FONT_BOLD

    def wrap(self, availWidth, availHeight):
        self.width = min(self.width, float(availWidth or self.width))
        self.height = min(self.height, float(availHeight or self.height))
        return self.width, self.height

    def draw(self):
        canvas = self.canv
        canvas.saveState()
        try:
            self._draw(canvas)
        finally:
            canvas.restoreState()

    @staticmethod
    def _parse_dt(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value.replace(second=0, microsecond=0)
        text = str(value or "").strip().replace("T", " ")
        if not text:
            return None
        if "." in text:
            text = text.split(".", 1)[0]
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M"):
            try:
                return datetime.strptime(text[:19], fmt).replace(second=0, microsecond=0)
            except Exception:
                continue
        try:
            return datetime.fromisoformat(text.replace(" ", "T")).replace(second=0, microsecond=0)
        except Exception:
            return None

    @staticmethod
    def _time_text(value: datetime | None) -> str:
        return value.strftime("%H:%M") if isinstance(value, datetime) else ""

    def _bounds(self) -> tuple[datetime, datetime]:
        case = self.context.get("case") if isinstance(self.context.get("case"), dict) else {}
        times: list[datetime] = []
        for key in ("anesthesia_start", "anesthesia_end", "surgery_start", "surgery_end", "admission_started_at", "closed_at"):
            parsed = self._parse_dt((case or {}).get(key))
            if parsed is not None:
                times.append(parsed)
        for vital in self.context.get("vitals") or []:
            parsed = self._parse_dt((vital or {}).get("datetime"))
            if parsed is not None:
                times.append(parsed)
        medications = self.context.get("medications") if isinstance(self.context.get("medications"), dict) else {}
        for event in medications.get("boluses") or []:
            parsed = self._parse_dt((event or {}).get("time"))
            if parsed is not None:
                times.append(parsed)
        for interval in medications.get("infusions") or []:
            for key in ("start", "end"):
                parsed = self._parse_dt((interval or {}).get(key))
                if parsed is not None:
                    times.append(parsed)
        for stage in self.context.get("stages") or []:
            parsed = self._parse_dt((stage or {}).get("event_time"))
            if parsed is not None:
                times.append(parsed)

        start = self._parse_dt((case or {}).get("anesthesia_start")) or (min(times) if times else datetime.now())
        end = self._parse_dt((case or {}).get("anesthesia_end")) or (max(times) if times else start + timedelta(hours=3))
        if end <= start:
            end = start + timedelta(minutes=15)
        min_minutes = 180 if not self.full_page else 60
        if (end - start).total_seconds() / 60.0 < min_minutes:
            end = start + timedelta(minutes=min_minutes)
        return start, end

    def _x_for_dt(self, value: datetime, start: datetime, end: datetime, left: float, width: float) -> float:
        total = max(1.0, (end - start).total_seconds())
        return left + max(0.0, min(1.0, (value - start).total_seconds() / total)) * width

    def _y_for_value(self, value: Any, bottom: float, height: float) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        number = max(self.VITAL_MIN, min(self.VITAL_MAX, number))
        return bottom + (number - self.VITAL_MIN) / (self.VITAL_MAX - self.VITAL_MIN) * height

    def _draw(self, canvas) -> None:
        margin_left = 26.0
        margin_right = 8.0
        margin_top = 14.0
        medication_band = 34.0 if not self.full_page else 42.0
        margin_bottom = medication_band + 12.0
        plot_left = margin_left
        plot_bottom = margin_bottom
        plot_width = max(40.0, self.width - margin_left - margin_right)
        plot_height = max(60.0, self.height - margin_top - margin_bottom)
        start, end = self._bounds()

        canvas.setStrokeColor(colors.HexColor("#6B7280"))
        canvas.setLineWidth(0.7)
        canvas.rect(plot_left, plot_bottom, plot_width, plot_height, stroke=1, fill=0)

        canvas.setFont(self.font_regular, 6.5)
        canvas.setStrokeColor(colors.HexColor("#C9CED6"))
        canvas.setFillColor(colors.HexColor("#111827"))
        for value in range(self.VITAL_MIN, self.VITAL_MAX + 1, 10):
            y = self._y_for_value(value, plot_bottom, plot_height)
            if y is None:
                continue
            canvas.setLineWidth(0.28 if value % 50 else 0.55)
            canvas.line(plot_left, y, plot_left + plot_width, y)
            if value % 10 == 0:
                canvas.drawRightString(plot_left - 3, y - 2, str(value))

        total_minutes = max(15, int((end - start).total_seconds() // 60))
        step_minutes = 15 if total_minutes <= 360 else 30 if total_minutes <= 720 else 60
        tick = start.replace(second=0, microsecond=0)
        minute_mod = tick.minute % step_minutes
        if minute_mod:
            tick += timedelta(minutes=step_minutes - minute_mod)
        while tick <= end:
            x = self._x_for_dt(tick, start, end, plot_left, plot_width)
            major = tick.minute == 0 or tick == start
            canvas.setStrokeColor(colors.HexColor("#AEB4BE") if major else colors.HexColor("#D9DDE4"))
            canvas.setLineWidth(0.55 if major else 0.3)
            canvas.line(x, plot_bottom, x, plot_bottom + plot_height)
            if major or total_minutes <= 240:
                canvas.setFillColor(colors.HexColor("#111827"))
                canvas.drawCentredString(x, plot_bottom + plot_height + 3, tick.strftime("%H:%M"))
            tick += timedelta(minutes=step_minutes)

        self._draw_vital_line(canvas, "sys", colors.HexColor("#EF4444"), start, end, plot_left, plot_width, plot_bottom, plot_height, width_pt=1.0)
        self._draw_vital_line(canvas, "dia", colors.HexColor("#EF4444"), start, end, plot_left, plot_width, plot_bottom, plot_height, width_pt=0.65)
        self._draw_vital_line(canvas, "pulse", colors.HexColor("#1D4ED8"), start, end, plot_left, plot_width, plot_bottom, plot_height, width_pt=1.0)
        self._draw_vital_line(canvas, "spo2", colors.HexColor("#0284C7"), start, end, plot_left, plot_width, plot_bottom, plot_height, width_pt=1.0)
        self._draw_stages(canvas, start, end, plot_left, plot_width, plot_bottom, plot_height)
        self._draw_medications(canvas, start, end, plot_left, plot_width, plot_bottom)
        self._draw_legend(canvas, plot_left, self.height - 9)

    def _draw_vital_line(self, canvas, key: str, color, start: datetime, end: datetime, left: float, width: float, bottom: float, height: float, *, width_pt: float):
        points: list[tuple[float, float]] = []
        for vital in self.context.get("vitals") or []:
            dt = self._parse_dt((vital or {}).get("datetime"))
            y = self._y_for_value((vital or {}).get(key), bottom, height)
            if dt is None or y is None or dt < start or dt > end:
                continue
            points.append((self._x_for_dt(dt, start, end, left, width), y))
        if not points:
            return
        points.sort(key=lambda point: point[0])
        canvas.setStrokeColor(color)
        canvas.setFillColor(color)
        canvas.setLineWidth(width_pt)
        for index, (x, y) in enumerate(points):
            if index:
                prev_x, prev_y = points[index - 1]
                canvas.line(prev_x, prev_y, x, y)
            canvas.circle(x, y, 1.25, stroke=0, fill=1)

    def _draw_stages(self, canvas, start: datetime, end: datetime, left: float, width: float, bottom: float, height: float) -> None:
        colors_by_kind = {
            "anesthesia_start": colors.HexColor("#4B5563"),
            "anesthesia_end": colors.HexColor("#7F1D1D"),
            "surgery_start": colors.HexColor("#2563EB"),
            "surgery_end": colors.HexColor("#C2410C"),
            "custom": colors.HexColor("#0F766E"),
        }
        canvas.setFont(self.font_bold, 6.2 if self.full_page else 5.6)
        for stage in self.context.get("stages") or []:
            dt = self._parse_dt((stage or {}).get("event_time"))
            if dt is None or dt < start or dt > end:
                continue
            kind = str((stage or {}).get("kind") or "")
            label = re.sub(r"\s+", " ", str((stage or {}).get("label") or "Этап").strip())
            label = label[:70 if self.full_page else 56]
            x = self._x_for_dt(dt, start, end, left, width)
            color = colors_by_kind.get(kind, colors.HexColor("#0F766E"))
            canvas.setStrokeColor(color)
            canvas.setDash(2, 2)
            canvas.setLineWidth(0.65)
            canvas.line(x, bottom, x, bottom + height)
            canvas.setDash()
            canvas.saveState()
            canvas.setFillColor(color)
            canvas.translate(x + 2.0, bottom + height - 3.0)
            canvas.rotate(90)
            canvas.drawRightString(0, 0, label)
            canvas.restoreState()

    def _draw_medications(self, canvas, start: datetime, end: datetime, left: float, width: float, plot_bottom: float) -> None:
        medications = self.context.get("medications") if isinstance(self.context.get("medications"), dict) else {}
        canvas.setFont(self.font_regular, 5.8 if not self.full_page else 6.3)
        y = plot_bottom - 10.0
        lane_step = 8.5 if not self.full_page else 9.5
        for index, event in enumerate((medications.get("boluses") or [])[:28 if not self.full_page else 48]):
            dt = self._parse_dt((event or {}).get("time"))
            if dt is None or dt < start or dt > end:
                continue
            x = self._x_for_dt(dt, start, end, left, width)
            lane_y = y - (index % 4) * lane_step
            label = re.sub(r"\s+", " ", str((event or {}).get("display") or (event or {}).get("name") or "").strip())
            canvas.setFillColor(colors.HexColor("#7C3AED"))
            canvas.circle(x, lane_y + 2, 1.8, stroke=0, fill=1)
            canvas.drawString(x + 3, lane_y, label[:32 if self.full_page else 22])
        for interval in (medications.get("infusions") or [])[:10 if not self.full_page else 16]:
            start_dt = self._parse_dt((interval or {}).get("start"))
            if start_dt is None or start_dt > end:
                continue
            end_dt = self._parse_dt((interval or {}).get("end")) or end
            if end_dt < start:
                continue
            x1 = self._x_for_dt(max(start_dt, start), start, end, left, width)
            x2 = self._x_for_dt(min(end_dt, end), start, end, left, width)
            bar_y = plot_bottom - (32.0 if not self.full_page else 37.0)
            canvas.setStrokeColor(colors.HexColor("#059669"))
            canvas.setLineWidth(2.0)
            canvas.line(x1, bar_y, max(x2, x1 + 2.0), bar_y)
            canvas.setFont(self.font_bold, 5.8 if not self.full_page else 6.3)
            name = re.sub(r"\s+", " ", str((interval or {}).get("name") or "").strip())
            canvas.drawString(x1 + 2, bar_y + 3, name[:34 if self.full_page else 24])

    def _draw_legend(self, canvas, x: float, y: float) -> None:
        entries = [
            ("АД сист.", "#EF4444"),
            ("АД диаст.", "#EF4444"),
            ("ЧСС", "#1D4ED8"),
            ("SpO2", "#0284C7"),
            ("этапы", "#4B5563"),
            ("препараты", "#7C3AED"),
        ]
        canvas.setFont(self.font_regular, 6.6)
        cursor_x = x
        for label, color in entries:
            canvas.setFillColor(colors.HexColor(color))
            canvas.rect(cursor_x, y - 4, 5, 3, stroke=0, fill=1)
            canvas.setFillColor(colors.HexColor("#111827"))
            canvas.drawString(cursor_x + 7, y - 5, label)
            cursor_x += 47


class OperBlockReportLabBuilder:
    @classmethod
    def build_pdf(cls, context: dict[str, Any], output_path) -> None:
        ReportLabReportBuilder._ensure_fonts_registered()
        output_path = pathlib.Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        page_size = landscape(A4)
        margins = (8 * mm, 8 * mm, 8 * mm, 8 * mm)
        doc = SimpleDocTemplate(
            str(output_path),
            pagesize=page_size,
            leftMargin=margins[0],
            rightMargin=margins[2],
            topMargin=margins[1],
            bottomMargin=margins[3],
            pageCompression=1,
        )
        width = page_size[0] - margins[0] - margins[2]
        story = cls._story(context or {}, width)
        doc.build(story, onFirstPage=cls._draw_page_background, onLaterPages=cls._draw_page_background)
        if not output_path.exists() or output_path.stat().st_size <= 0:
            raise OSError(f"PDF file was not created: {output_path}")

    @staticmethod
    def _draw_page_background(canvas, doc) -> None:
        canvas.saveState()
        canvas.setFillColor(colors.white)
        canvas.rect(0, 0, doc.pagesize[0], doc.pagesize[1], stroke=0, fill=1)
        canvas.setFont(ReportLabReportBuilder.FONT_REGULAR, 7)
        canvas.setFillColor(colors.HexColor("#64748B"))
        canvas.drawRightString(doc.pagesize[0] - 8 * mm, 5 * mm, f"Страница {doc.page}")
        canvas.restoreState()

    @classmethod
    def _styles(cls) -> dict[str, ParagraphStyle]:
        regular = ReportLabReportBuilder.FONT_REGULAR
        bold = ReportLabReportBuilder.FONT_BOLD
        return {
            "title": ParagraphStyle("OperBlockTitle", fontName=bold, fontSize=13, leading=15, alignment=TA_CENTER, spaceAfter=4),
            "section": ParagraphStyle("OperBlockSection", fontName=bold, fontSize=9.5, leading=11, textColor=colors.HexColor("#0F172A")),
            "normal": ParagraphStyle("OperBlockNormal", fontName=regular, fontSize=8, leading=9.5, alignment=TA_LEFT),
            "small": ParagraphStyle("OperBlockSmall", fontName=regular, fontSize=7, leading=8.2, alignment=TA_LEFT),
            "small_bold": ParagraphStyle("OperBlockSmallBold", fontName=bold, fontSize=7, leading=8.2, alignment=TA_LEFT),
        }

    @classmethod
    def _p(cls, text: Any, style: str = "normal") -> Paragraph:
        styles = cls._styles()
        return Paragraph(html.escape(str(text or "")).replace("\n", "<br/>"), styles[style])

    @classmethod
    def _story(cls, context: dict[str, Any], width: float) -> list:
        case = context.get("case") if isinstance(context.get("case"), dict) else {}
        long_chart = cls._duration_minutes(case.get("anesthesia_start"), case.get("anesthesia_end")) > 180
        chart_width = width * 0.75
        totals_width = width - chart_width - 6
        story = [
            cls._p(cls._title_text(context), "title"),
            cls._header_table(context, width),
            Spacer(1, 4),
            cls._team_table(context, width),
            Spacer(1, 5),
        ]
        if long_chart:
            story.extend([cls._totals_table(context, width), Spacer(1, 5)])
        else:
            story.extend([KeepTogether(cls._chart_and_totals(context, chart_width, totals_width)), Spacer(1, 5)])
        story.extend([cls._stages_table(context, width), Spacer(1, 5), cls._signature_table(width)])
        if long_chart:
            story.extend(
                [
                    PageBreak(),
                    cls._p("График витальных функций и препаратов", "title"),
                    _VitalsMedicationChartFlowable(context, width=width, height=132 * mm, full_page=True),
                    Spacer(1, 6),
                    cls._totals_table(context, width),
                    Spacer(1, 7),
                    cls._signature_table(width),
                ]
            )
        return story

    @classmethod
    def _title_text(cls, context: dict[str, Any]) -> str:
        case = context.get("case") if isinstance(context.get("case"), dict) else {}
        protocol = str((case or {}).get("protocol_display") or "без номера")
        return f"Отчет за операцию. Протокол анестезии № {protocol}"

    @classmethod
    def _header_table(cls, context: dict[str, Any], width: float) -> Table:
        patient = context.get("patient") if isinstance(context.get("patient"), dict) else {}
        case = context.get("case") if isinstance(context.get("case"), dict) else {}
        diagnosis = cls._diagnosis_text(patient)
        data = [
            [cls._p("Пациент", "small_bold"), cls._p(patient.get("full_name") or "—", "small"), cls._p("История", "small_bold"), cls._p(patient.get("history_number") or "—", "small")],
            [cls._p("Возраст", "small_bold"), cls._p(patient.get("age") or "—", "small"), cls._p("Отделение", "small_bold"), cls._p(patient.get("department_profile") or "—", "small")],
            [cls._p("Диагноз", "small_bold"), cls._p(diagnosis, "small"), cls._p("Переведен", "small_bold"), cls._p(cls._transfer_text(case), "small")],
            [cls._p("Операция", "small_bold"), cls._p(case.get("operation_name") or "—", "small"), cls._p("Вид анестезии", "small_bold"), cls._p(case.get("anesthesia_type") or "—", "small")],
            [cls._p("Начало операции", "small_bold"), cls._p(cls._dt(case.get("surgery_start")), "small"), cls._p("Конец операции", "small_bold"), cls._p(cls._dt(case.get("surgery_end")), "small")],
            [cls._p("Начало анестезии", "small_bold"), cls._p(cls._dt(case.get("anesthesia_start")), "small"), cls._p("Конец анестезии", "small_bold"), cls._p(cls._dt(case.get("anesthesia_end")), "small")],
        ]
        table = Table(data, colWidths=[width * 0.13, width * 0.38, width * 0.13, width * 0.36])
        table.setStyle(cls._base_table_style())
        return table

    @classmethod
    def _team_table(cls, context: dict[str, Any], width: float) -> Table:
        case = context.get("case") if isinstance(context.get("case"), dict) else {}
        surgeons = ", ".join(str(item) for item in (case.get("surgeons") or []) if item) or "—"
        data = [
            [cls._p("Хирурги", "small_bold"), cls._p(surgeons, "small"), cls._p("Оперсестра", "small_bold"), cls._p(case.get("operating_nurse") or "—", "small")],
            [cls._p("Анестезиолог", "small_bold"), cls._p(case.get("anesthesiologist") or "—", "small"), cls._p("Анестезист", "small_bold"), cls._p(case.get("anesthetist") or "—", "small")],
            [cls._p("Длительность операции", "small_bold"), cls._p(cls._duration_text(case.get("surgery_duration_minutes")), "small"), cls._p("Длительность анестезии", "small_bold"), cls._p(cls._duration_text(case.get("anesthesia_duration_minutes")), "small")],
        ]
        table = Table(data, colWidths=[width * 0.13, width * 0.38, width * 0.13, width * 0.36])
        table.setStyle(cls._base_table_style())
        return table

    @classmethod
    def _chart_and_totals(cls, context: dict[str, Any], chart_width: float, totals_width: float, *, compact: bool = False) -> list:
        chart_height = 66 * mm if compact else 90 * mm
        chart = _VitalsMedicationChartFlowable(context, width=chart_width, height=chart_height, full_page=False)
        totals = cls._totals_table(context, totals_width)
        wrapper = Table([[chart, totals]], colWidths=[chart_width, totals_width])
        wrapper.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
        return [wrapper]

    @classmethod
    def _totals_table(cls, context: dict[str, Any], width: float) -> Table:
        rows = [[cls._p("Итого препараты", "section")]]
        totals = cls._medication_totals(context)
        for line in totals[:18]:
            rows.append([cls._p(line, "small")])
        if len(totals) > 18:
            rows.append([cls._p(f"Еще позиций: {len(totals) - 18}", "small")])
        if len(rows) == 1:
            rows.append([cls._p("Нет введенных препаратов", "small")])
        table = Table(rows, colWidths=[width])
        table.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#CBD5E1")),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAF2FF")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]
            )
        )
        return table

    @classmethod
    def _stages_table(cls, context: dict[str, Any], width: float) -> Table:
        rows = [[cls._p("Время", "small_bold"), cls._p("Этап операции", "small_bold")]]
        for stage in context.get("stages") or []:
            rows.append([cls._p(cls._time((stage or {}).get("event_time")), "small"), cls._p((stage or {}).get("label") or "Этап операции", "small")])
        if len(rows) == 1:
            rows.append([cls._p("—", "small"), cls._p("Этапы не указаны", "small")])
        table = Table(rows[:14], colWidths=[width * 0.13, width * 0.87])
        table.setStyle(cls._base_table_style(header=True))
        return table

    @classmethod
    def _signature_table(cls, width: float) -> Table:
        line = "____________________________"
        data = [[cls._p(f"Врач анестезиолог-реаниматолог: {line}", "small"), cls._p(f"Медицинская сестра-анестезист: {line}", "small")]]
        table = Table(data, colWidths=[width / 2, width / 2])
        table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 8)]))
        return table

    @staticmethod
    def _base_table_style(*, header: bool = False) -> TableStyle:
        commands = [
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#CBD5E1")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
        if header:
            commands.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAF2FF")))
        else:
            commands.extend(
                [
                    ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F8FAFC")),
                    ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#F8FAFC")),
                ]
            )
        return TableStyle(commands)

    @staticmethod
    def _parse_dt(value: Any) -> datetime | None:
        return _VitalsMedicationChartFlowable._parse_dt(value)

    @classmethod
    def _dt(cls, value: Any) -> str:
        parsed = cls._parse_dt(value)
        return parsed.strftime("%d.%m.%Y %H:%M") if parsed else "—"

    @classmethod
    def _time(cls, value: Any) -> str:
        parsed = cls._parse_dt(value)
        return parsed.strftime("%H:%M") if parsed else "—"

    @staticmethod
    def _duration_text(value: Any) -> str:
        try:
            minutes = int(value)
        except (TypeError, ValueError):
            return "—"
        hours, mins = divmod(max(0, minutes), 60)
        if hours and mins:
            return f"{hours} ч {mins} мин"
        if hours:
            return f"{hours} ч"
        return f"{mins} мин"

    @classmethod
    def _duration_minutes(cls, start_value: Any, end_value: Any) -> int:
        start = cls._parse_dt(start_value)
        end = cls._parse_dt(end_value)
        if start is None or end is None or end <= start:
            return 0
        return int((end - start).total_seconds() // 60)

    @staticmethod
    def _diagnosis_text(patient: dict[str, Any]) -> str:
        code = str((patient or {}).get("diagnosis_code") or "").strip()
        text = str((patient or {}).get("diagnosis_text") or "").strip()
        return f"{code}: {text}" if code and text else text or code or "—"

    @staticmethod
    def _transfer_text(case: dict[str, Any]) -> str:
        target = str((case or {}).get("transfer_department_target") or "").strip()
        department = str((case or {}).get("transfer_department") or "").strip()
        return f"в {target}" if target else department or "—"

    @staticmethod
    def _decimal(value: Any) -> Decimal | None:
        text = str(value or "").strip().replace(",", ".")
        if not text:
            return None
        try:
            return Decimal(text)
        except (InvalidOperation, ValueError):
            return None

    @staticmethod
    def _format_decimal(value: Decimal) -> str:
        quantized = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if quantized == quantized.to_integral_value():
            return str(int(quantized))
        return format(quantized.normalize(), "f").rstrip("0").rstrip(".").replace(".", ",")

    @classmethod
    def _infusion_volume_ml(cls, interval: dict[str, Any]) -> Decimal | None:
        payload = interval.get("payload") if isinstance(interval.get("payload"), dict) else {}
        rate_unit = str((interval or {}).get("rate_unit") or "").strip().casefold()
        if str((payload or {}).get("kind") or "").strip().casefold() == "gas" or rate_unit in {"mac", "мак"}:
            return None
        direct = cls._decimal((interval or {}).get("volume_ml"))
        if direct is not None:
            return direct
        start = cls._parse_dt((interval or {}).get("start"))
        end = cls._parse_dt((interval or {}).get("end"))
        rate = cls._decimal((interval or {}).get("rate_value"))
        if start is None or end is None or rate is None or end <= start:
            return None
        minutes = Decimal(int((end - start).total_seconds() // 60))
        return rate * minutes / Decimal("60")

    @classmethod
    def _medication_totals(cls, context: dict[str, Any]) -> list[str]:
        medications = context.get("medications") if isinstance(context.get("medications"), dict) else {}
        totals: dict[tuple[str, str], Decimal] = {}
        unparsed: dict[str, int] = {}
        for event in medications.get("boluses") or []:
            name = re.sub(r"\s+", " ", str((event or {}).get("name") or (event or {}).get("display") or "Препарат").strip())
            unit = re.sub(r"\s+", " ", str((event or {}).get("dose_unit") or "").strip())
            value = cls._decimal((event or {}).get("dose_value"))
            if value is not None and unit:
                totals[(name, unit)] = totals.get((name, unit), Decimal("0")) + value
            else:
                unparsed[name] = int(unparsed.get(name, 0)) + 1
        lines = [f"{name}: {cls._format_decimal(value)} {unit}" for (name, unit), value in sorted(totals.items(), key=lambda item: item[0][0].casefold())]
        lines.extend(f"{name}: {count} введ." for name, count in sorted(unparsed.items(), key=lambda item: item[0].casefold()))
        for interval in medications.get("infusions") or []:
            name = re.sub(r"\s+", " ", str((interval or {}).get("name") or "Инфузия").strip())
            volume = cls._infusion_volume_ml(interval or {})
            if volume is not None:
                lines.append(f"{name}: {cls._format_decimal(volume)} мл")
            elif (interval or {}).get("rate_value"):
                lines.append(f"{name}: скорость {(interval or {}).get('rate_value')} {(interval or {}).get('rate_unit') or ''}".strip())
            else:
                lines.append(f"{name}: инфузия")
        return lines
