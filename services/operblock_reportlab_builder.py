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
from reportlab.platypus import Flowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from rem_card.ui.rem_card_sectors.s_print.reportlab_builder import ReportLabReportBuilder


class _VitalsChartFlowable(Flowable):
    VITAL_MAX = 250
    VITAL_MIN = 0

    def __init__(self, context: dict[str, Any], *, width: float, height: float):
        super().__init__()
        self.context = dict(context or {})
        self.width = float(width)
        self.height = float(height)
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
        for key in ("anesthesia_start", "anesthesia_end", "surgery_start", "surgery_end"):
            parsed = self._parse_dt((case or {}).get(key))
            if parsed is not None:
                times.append(parsed)
        for vital in self.context.get("vitals") or []:
            parsed = self._parse_dt((vital or {}).get("datetime"))
            if parsed is not None:
                times.append(parsed)

        start = (
            self._parse_dt((case or {}).get("anesthesia_start"))
            or self._parse_dt((case or {}).get("surgery_start"))
            or (min(times) if times else datetime.now())
        )
        end = (
            self._parse_dt((case or {}).get("anesthesia_end"))
            or self._parse_dt((case or {}).get("surgery_end"))
            or (max(times) if times else start + timedelta(minutes=10))
        )
        if end <= start:
            end = start + timedelta(minutes=10)
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

    @staticmethod
    def _ceil_dt_to_step(value: datetime, step_minutes: int) -> datetime:
        step = max(1, int(step_minutes))
        midnight = value.replace(hour=0, minute=0, second=0, microsecond=0)
        minutes = int((value - midnight).total_seconds() // 60)
        remainder = minutes % step
        if remainder:
            value = value + timedelta(minutes=step - remainder)
        return value.replace(second=0, microsecond=0)

    def _time_step_minutes(self, canvas, total_minutes: int, plot_width: float, font_size: float) -> int:
        min_label_width = canvas.stringWidth("00:00", self.font_regular, font_size) + 12.0
        max_labels = max(2, int(plot_width // max(1.0, min_label_width)))
        raw_step = max(1.0, float(total_minutes) / max(1, max_labels - 1))
        for step in (5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240, 360, 720):
            if step >= raw_step:
                return step
        return int(((raw_step + 719) // 720) * 720)

    def _time_ticks(self, start: datetime, end: datetime, step_minutes: int) -> list[datetime]:
        ticks = [start]
        tick = self._ceil_dt_to_step(start, step_minutes)
        if tick <= start:
            tick += timedelta(minutes=step_minutes)
        while tick < end:
            ticks.append(tick)
            tick += timedelta(minutes=step_minutes)
        if end != ticks[-1]:
            ticks.append(end)
        return ticks

    def _label_ticks(
        self,
        ticks: list[datetime],
        start: datetime,
        end: datetime,
        left: float,
        width: float,
        min_gap: float,
    ) -> set[datetime]:
        if len(ticks) <= 2:
            return set(ticks)
        end_x = self._x_for_dt(end, start, end, left, width)
        labels: list[datetime] = [ticks[0]]
        last_x = self._x_for_dt(ticks[0], start, end, left, width)
        for tick in ticks[1:-1]:
            x = self._x_for_dt(tick, start, end, left, width)
            if x - last_x < min_gap or end_x - x < min_gap:
                continue
            labels.append(tick)
            last_x = x
        labels.append(ticks[-1])
        return set(labels)

    def _draw(self, canvas) -> None:
        margin_left = 22.0
        margin_right = 5.0
        margin_top = 15.0
        margin_bottom = 6.0
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

        total_minutes = max(1, int((end - start).total_seconds() // 60))
        time_font_size = 6.7
        canvas.setFont(self.font_regular, time_font_size)
        step_minutes = self._time_step_minutes(canvas, total_minutes, plot_width, time_font_size)
        ticks = self._time_ticks(start, end, step_minutes)
        label_ticks = self._label_ticks(
            ticks,
            start,
            end,
            plot_left,
            plot_width,
            canvas.stringWidth("00:00", self.font_regular, time_font_size) + 8.0,
        )
        for tick in ticks:
            x = self._x_for_dt(tick, start, end, plot_left, plot_width)
            major = tick.minute == 0 or tick in (start, end)
            canvas.setStrokeColor(colors.HexColor("#AEB4BE") if major else colors.HexColor("#D9DDE4"))
            canvas.setLineWidth(0.55 if major else 0.3)
            canvas.line(x, plot_bottom, x, plot_bottom + plot_height)
            if tick not in label_ticks:
                continue
            canvas.setFillColor(colors.HexColor("#111827"))
            label = tick.strftime("%H:%M")
            label_y = plot_bottom + plot_height + 3
            if tick == start:
                canvas.drawString(x, label_y, label)
            elif tick == end:
                canvas.drawRightString(x, label_y, label)
            else:
                canvas.drawCentredString(x, label_y, label)

        self._draw_vital_line(canvas, "sys", colors.HexColor("#EF4444"), start, end, plot_left, plot_width, plot_bottom, plot_height, width_pt=1.0)
        self._draw_vital_line(canvas, "dia", colors.HexColor("#EF4444"), start, end, plot_left, plot_width, plot_bottom, plot_height, width_pt=0.65)
        self._draw_vital_line(canvas, "pulse", colors.HexColor("#1D4ED8"), start, end, plot_left, plot_width, plot_bottom, plot_height, width_pt=1.0)
        self._draw_vital_line(canvas, "spo2", colors.HexColor("#0284C7"), start, end, plot_left, plot_width, plot_bottom, plot_height, width_pt=1.0)

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
            "medication": ParagraphStyle("OperBlockMedication", fontName=regular, fontSize=6.7, leading=7.8, alignment=TA_LEFT),
        }

    @classmethod
    def _p(cls, text: Any, style: str = "normal") -> Paragraph:
        styles = cls._styles()
        return Paragraph(html.escape(str(text or "")).replace("\n", "<br/>"), styles[style])

    @classmethod
    def _story(cls, context: dict[str, Any], width: float) -> list:
        chart_width = width * 0.75
        stages_width = width - chart_width - 6
        story = [
            cls._p(cls._title_text(context), "title"),
            cls._header_table(context, width),
            Spacer(1, 4),
            cls._team_table(context, width),
            Spacer(1, 5),
            cls._chart_and_stages(context, chart_width, stages_width),
            Spacer(1, 5),
            cls._medications_table(context, width),
            Spacer(1, 4),
            cls._signature_table(context, width),
        ]
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
    def _chart_and_stages(cls, context: dict[str, Any], chart_width: float, stages_width: float) -> Table:
        chart = _VitalsChartFlowable(context, width=chart_width, height=72 * mm)
        stages = cls._stages_table(context, stages_width)
        wrapper = Table([[chart, stages]], colWidths=[chart_width, stages_width])
        wrapper.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]
            )
        )
        return wrapper

    @classmethod
    def _stages_table(cls, context: dict[str, Any], width: float) -> Table:
        rows = [[cls._p("Время", "small_bold"), cls._p("Этап операции", "small_bold")]]
        for stage in context.get("stages") or []:
            rows.append([cls._p(cls._time((stage or {}).get("event_time")), "small"), cls._p((stage or {}).get("label") or "Этап операции", "small")])
        if len(rows) == 1:
            rows.append([cls._p("—", "small"), cls._p("Этапы не указаны", "small")])
        time_width = max(34.0, min(width * 0.28, 52.0))
        table = Table(rows, colWidths=[time_width, max(1.0, width - time_width)])
        table.setStyle(cls._base_table_style(header=True))
        return table

    @classmethod
    def _medications_table(cls, context: dict[str, Any], width: float) -> Table:
        groups = cls._grouped_medications(context)
        rows: list[list[Any]] = [[cls._p("Препараты", "section"), "", "", ""]]
        if not groups:
            rows.append([cls._p("Нет введенных препаратов", "small"), "", "", ""])
        else:
            columns = 5
            for offset in range(0, len(groups), columns):
                row = [
                    cls._medication_group_paragraph(index + 1, group)
                    for index, group in enumerate(groups[offset : offset + columns], start=offset)
                ]
                row.extend([""] * (columns - len(row)))
                rows.append(row)

        table = Table(rows, colWidths=[width / 5] * 5, repeatRows=1)
        commands = [
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#CBD5E1")),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAF2FF")),
            ("SPAN", (0, 0), (-1, 0)),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
        if not groups:
            commands.append(("SPAN", (0, 1), (-1, 1)))
        table.setStyle(TableStyle(commands))
        return table

    @classmethod
    def _medication_group_paragraph(cls, number: int, group: dict[str, Any]) -> Paragraph:
        styles = cls._styles()
        name = html.escape(str(group.get("name") or "Препарат"))
        summary = html.escape(cls._medication_group_summary(group))
        lines = [f"<b>{int(number)}. {name}{summary}</b>"]
        items = list(group.get("items") or [])
        if not items:
            lines.append("&nbsp;&nbsp;&nbsp;&nbsp;—")
        for item in items:
            line = item.get("line") if isinstance(item, dict) else item
            lines.append(f"&nbsp;&nbsp;&nbsp;&nbsp;{html.escape(str(line or '—'))}")
        return Paragraph("<br/>".join(lines), styles["medication"])

    @classmethod
    def _grouped_medications(cls, context: dict[str, Any]) -> list[dict[str, Any]]:
        medications = context.get("medications") if isinstance(context.get("medications"), dict) else {}
        case = context.get("case") if isinstance(context.get("case"), dict) else {}
        report_end = cls._parse_dt((case or {}).get("anesthesia_end")) or cls._parse_dt((case or {}).get("surgery_end"))
        entries: list[tuple[datetime | None, int, str, dict[str, Any]]] = []
        sequence = 0

        for event in medications.get("boluses") or []:
            data = event or {}
            dt = cls._parse_dt(data.get("time"))
            name = cls._medication_name(data, fallback="Препарат")
            entries.append((dt, sequence, name, cls._bolus_medication_item(data, name)))
            sequence += 1

        for interval in medications.get("infusions") or []:
            data = interval or {}
            dt = cls._parse_dt(data.get("start"))
            interval_items = cls._infusion_medication_items(data, report_end=report_end)
            for name, item in interval_items:
                entries.append((dt, sequence, name, item))
                sequence += 1
            if not interval_items:
                name = cls._medication_name(data, fallback="Инфузия")
                entries.append((dt, sequence, name, cls._infusion_medication_item(data, name)))
                sequence += 1

        entries.sort(key=lambda item: (item[0] or datetime.max, item[1]))
        groups: list[dict[str, Any]] = []
        index_by_key: dict[str, int] = {}
        for _dt, _sequence, name, item in entries:
            key = re.sub(r"\s+", " ", name).strip().casefold()
            if key not in index_by_key:
                index_by_key[key] = len(groups)
                groups.append({"name": name, "items": []})
            groups[index_by_key[key]]["items"].append(item)
        return groups

    @staticmethod
    def _clean_text(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip())

    @classmethod
    def _medication_name(cls, data: dict[str, Any], *, fallback: str) -> str:
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        name = cls._clean_text(data.get("name") or data.get("display") or fallback)
        concentration = cls._clean_text(
            data.get("concentration_text")
            or (payload or {}).get("concentration")
            or (payload or {}).get("concentration_text")
        )
        if concentration and "%" in concentration and concentration.casefold() not in name.casefold():
            name = f"{name} {concentration}".strip()
        return name or fallback

    @classmethod
    def _value_unit_text(cls, value: Any, unit: Any) -> str:
        value_text = cls._clean_text(value)
        number = cls._decimal(value_text)
        if number is not None:
            value_text = cls._format_decimal(number)
        unit_text = cls._clean_text(unit)
        return f"{value_text} {unit_text}".strip() if value_text else ""

    @classmethod
    def _volume_text(cls, value: Any) -> str:
        text = cls._clean_text(value)
        if not text:
            return ""
        number = cls._decimal(re.sub(r"\s*мл\s*$", "", text, flags=re.IGNORECASE))
        if number is not None:
            return f"{cls._format_decimal(number)} мл"
        return text if "мл" in text.casefold() else f"{text} мл"

    @staticmethod
    def _dose_unit_key(unit: Any) -> str:
        raw = re.sub(r"\s+", "", str(unit or "").strip().casefold()).replace("ё", "е").replace("µ", "мк")
        raw = raw.rstrip(".")
        aliases = {
            "mg": "мг",
            "мг": "мг",
            "mkg": "мкг",
            "mcg": "мкг",
            "ug": "мкг",
            "мкг": "мкг",
            "мкгр": "мкг",
            "g": "г",
            "гр": "г",
            "г": "г",
            "ml": "мл",
            "мл": "мл",
        }
        return aliases.get(raw, raw)

    @classmethod
    def _dose_value_unit(cls, data: dict[str, Any], name: str) -> tuple[Decimal | None, str]:
        value = cls._decimal(data.get("dose_value"))
        unit = cls._clean_text(data.get("dose_unit"))
        if value is not None and unit:
            return value, unit

        suffix = cls._display_suffix(data.get("display"), name)
        match = re.search(
            r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>мкгр|мкг|мг|мл|гр|г|mcg|mkg|mg|ml|ug|g)\b",
            suffix,
            flags=re.IGNORECASE,
        )
        if not match:
            return None, ""
        parsed = cls._decimal(match.group("value"))
        return parsed, cls._clean_text(match.group("unit"))

    @classmethod
    def _format_value_unit(cls, value: Decimal | None, unit: str) -> str:
        if value is None:
            return ""
        unit_text = cls._clean_text(unit)
        return f"{cls._format_decimal(value)} {unit_text}".strip() if unit_text else cls._format_decimal(value)

    @classmethod
    def _volume_decimal_ml(cls, value: Any) -> Decimal | None:
        text = cls._clean_text(value)
        if not text:
            return None
        return cls._decimal(re.sub(r"\s*мл\s*$", "", text, flags=re.IGNORECASE))

    @classmethod
    def _format_volume_decimal(cls, value: Decimal | None) -> str:
        return f"{cls._format_decimal(value)} мл" if value is not None else ""

    @classmethod
    def _concentration_mg_per_ml(cls, data: dict[str, Any], name: str) -> Decimal | None:
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        source_text = " ".join(
            cls._clean_text(value)
            for value in (
                data.get("concentration_text"),
                (payload or {}).get("concentration"),
                (payload or {}).get("concentration_text"),
                name,
            )
            if cls._clean_text(value)
        )
        ratio_match = re.search(
            r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>мкг|мкгр|mcg|mkg|ug|мг|mg|г|гр|g)\s*/\s*(?:мл|ml)\b",
            source_text,
            flags=re.IGNORECASE,
        )
        if ratio_match:
            value = cls._decimal(ratio_match.group("value"))
            if value is None or value <= 0:
                return None
            unit_key = cls._dose_unit_key(ratio_match.group("unit"))
            if unit_key == "мг":
                return value
            if unit_key == "г":
                return value * Decimal("1000")
            if unit_key == "мкг":
                return value / Decimal("1000")
            return None
        match = re.search(r"(?P<percent>\d+(?:[.,]\d+)?)\s*%", source_text)
        if not match:
            return None
        percent = cls._decimal(match.group("percent"))
        if percent is None or percent <= 0:
            return None
        return percent * Decimal("10")

    @classmethod
    def _dose_volume_from_concentration(
        cls,
        data: dict[str, Any],
        name: str,
        dose_value: Decimal | None,
        dose_unit: str,
    ) -> Decimal | None:
        if dose_value is None:
            return None
        unit_key = cls._dose_unit_key(dose_unit)
        dose_mg: Decimal | None = None
        if unit_key == "мг":
            dose_mg = dose_value
        elif unit_key == "г":
            dose_mg = dose_value * Decimal("1000")
        elif unit_key == "мкг":
            dose_mg = dose_value / Decimal("1000")
        elif unit_key == "мл":
            return dose_value
        if dose_mg is None:
            return None
        mg_per_ml = cls._concentration_mg_per_ml(data, name)
        if mg_per_ml is None or mg_per_ml <= 0:
            return None
        return dose_mg / mg_per_ml

    @classmethod
    def _bolus_volume_decimal_ml(
        cls,
        event: dict[str, Any],
        name: str,
        dose_value: Decimal | None,
        dose_unit: str,
    ) -> Decimal | None:
        explicit = cls._volume_decimal_ml(event.get("volume_ml"))
        if explicit is not None:
            return explicit
        return cls._dose_volume_from_concentration(event, name, dose_value, dose_unit)

    @classmethod
    def _display_suffix(cls, display: Any, name: str) -> str:
        clean_display = cls._clean_text(display)
        clean_name = cls._clean_text(name)
        if not clean_display:
            return ""
        if clean_name and clean_display.casefold().startswith(clean_name.casefold()):
            suffix = clean_display[len(clean_name) :].strip(" -:;·")
            return suffix or ""
        base_name = re.sub(r"\s+\d+(?:[.,]\d+)?\s*%$", "", clean_name).strip()
        if base_name and base_name != clean_name and clean_display.casefold().startswith(base_name.casefold()):
            suffix = clean_display[len(base_name) :].strip(" -:;·")
            return suffix or ""
        return clean_display

    @classmethod
    def _bolus_medication_item(cls, event: dict[str, Any], name: str) -> dict[str, Any]:
        dose_value, dose_unit = cls._dose_value_unit(event, name)
        volume_ml = cls._bolus_volume_decimal_ml(event, name, dose_value, dose_unit)
        dose_text = cls._bolus_dose_text(event, name)
        return {
            "kind": "bolus",
            "line": f"{cls._time(event.get('time'))} - {dose_text}",
            "dose_value": dose_value,
            "dose_unit": cls._clean_text(dose_unit),
            "volume_ml": volume_ml,
        }

    @classmethod
    def _infusion_medication_items(cls, interval: dict[str, Any], *, report_end: datetime | None = None) -> list[tuple[str, dict[str, Any]]]:
        payload = interval.get("payload") if isinstance(interval.get("payload"), dict) else {}
        kind = str((payload or {}).get("kind") or "").strip().casefold()
        if kind == "gas":
            return cls._gas_medication_items(interval, report_end=report_end)
        if kind == "timed_infusion":
            return cls._timed_infusion_medication_items(interval)
        if kind == "solvent":
            item = cls._solvent_medication_item(interval)
            return [(item["name"], item)] if item else []
        return []

    @classmethod
    def _gas_medication_items(cls, interval: dict[str, Any], *, report_end: datetime | None = None) -> list[tuple[str, dict[str, Any]]]:
        name = cls._medication_name(interval, fallback="Газ")
        start = cls._parse_dt(interval.get("start"))
        if start is None:
            return []
        end = cls._parse_dt(interval.get("end")) or report_end or start
        if end < start:
            end = start
        events = cls._gas_dose_events(interval, start)
        if not events:
            return []

        items: list[tuple[str, dict[str, Any]]] = []
        for index, event in enumerate(events):
            event_time = max(start, event["time"])
            next_time = events[index + 1]["time"] if index + 1 < len(events) else end
            segment_end = max(event_time, min(end, next_time))
            duration_minutes = max(0, int((segment_end - event_time).total_seconds() // 60))
            dose_text = event["dose_text"]
            items.append(
                (
                    name,
                    {
                        "kind": "gas",
                        "line": f"{event_time.strftime('%H:%M')} - {dose_text}",
                        "mac_value": cls._gas_mac_value(dose_text),
                        "duration_minutes": duration_minutes,
                    },
                )
            )
        return items

    @classmethod
    def _gas_dose_events(cls, interval: dict[str, Any], start: datetime) -> list[dict[str, Any]]:
        payload = interval.get("payload") if isinstance(interval.get("payload"), dict) else {}
        rows: list[dict[str, Any]] = []
        for item in list(interval.get("dose_history") or []):
            event_time = cls._parse_dt((item or {}).get("event_time"))
            dose_text = cls._normalize_gas_dose_text((item or {}).get("dose_text"))
            if event_time is None or not dose_text:
                continue
            rows.append({"time": event_time, "dose_text": dose_text})
        if not rows:
            dose_text = cls._normalize_gas_dose_text((payload or {}).get("display_dose_text") or (payload or {}).get("dose_text"))
            if dose_text:
                rows.append({"time": start, "dose_text": dose_text})
        rows.sort(key=lambda item: item["time"])
        deduped: list[dict[str, Any]] = []
        for row in rows:
            if deduped and deduped[-1]["time"] == row["time"]:
                deduped[-1] = row
            else:
                deduped.append(row)
        if deduped and deduped[0]["time"] > start:
            first = dict(deduped[0])
            first["time"] = start
            deduped.insert(0, first)
        return deduped

    @classmethod
    def _normalize_gas_dose_text(cls, value: Any) -> str:
        clean = cls._clean_text(value)
        if not clean:
            return ""
        if re.search(r"(?i)(?:mac|мак)", clean):
            values = [cls._decimal(match) for match in re.findall(r"\d+(?:[.,]\d+)?", clean)]
            values = [value for value in values if value is not None]
            if values:
                return f"{'-'.join(cls._format_decimal(value) for value in values)} MAC"
            return re.sub(r"(?i)(?:mac|мак)", "MAC", clean)
        return clean

    @classmethod
    def _gas_mac_value(cls, dose_text: Any) -> Decimal | None:
        clean = cls._clean_text(dose_text)
        if not clean or not re.search(r"(?i)(?:mac|мак)", clean):
            return None
        values = [cls._decimal(match) for match in re.findall(r"\d+(?:[.,]\d+)?", clean)]
        values = [value for value in values if value is not None]
        if not values:
            return None
        return sum(values, Decimal("0")) / Decimal(len(values))

    @classmethod
    def _timed_infusion_medication_items(cls, interval: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        payload = interval.get("payload") if isinstance(interval.get("payload"), dict) else {}
        items: list[tuple[str, dict[str, Any]]] = []
        name = cls._medication_name(interval, fallback="Капельница")
        dose_text = cls._clean_text((payload or {}).get("dose_text") or (payload or {}).get("display_dose_text"))
        dose_value, dose_unit = cls._dose_value_unit({"display": dose_text}, "")
        if dose_value is not None and dose_unit:
            calculated_volume = cls._volume_decimal_ml((payload or {}).get("calculated_volume_ml"))
            volume_ml = calculated_volume
            if volume_ml is None:
                volume_ml = cls._dose_volume_from_concentration(interval, name, dose_value, dose_unit)
            dose_line = cls._format_value_unit(dose_value, dose_unit)
            volume_text = cls._format_volume_decimal(volume_ml)
            if volume_text and cls._dose_unit_key(dose_unit) != "мл":
                dose_line = f"{dose_line} ({volume_text})"
            items.append(
                (
                    name,
                    {
                        "kind": "timed_infusion_drug",
                        "line": f"{cls._time(interval.get('start'))} - {dose_line}",
                        "dose_value": dose_value,
                        "dose_unit": cls._clean_text(dose_unit),
                        "volume_ml": volume_ml,
                    },
                )
            )

        solvent_item = cls._solvent_medication_item(interval)
        if solvent_item:
            items.append((solvent_item["name"], solvent_item))
        return items

    @classmethod
    def _solvent_medication_item(cls, interval: dict[str, Any]) -> dict[str, Any] | None:
        payload = interval.get("payload") if isinstance(interval.get("payload"), dict) else {}
        name = cls._solvent_name(payload, interval)
        if not name:
            return None
        volume_ml = cls._solvent_volume_decimal_ml(interval)
        if volume_ml is None:
            return None
        return {
            "kind": "solvent",
            "name": name,
            "line": f"{cls._time(interval.get('start'))} - {cls._format_volume_decimal(volume_ml)}",
            "volume_ml": volume_ml,
        }

    @classmethod
    def _solvent_volume_decimal_ml(cls, interval: dict[str, Any]) -> Decimal | None:
        payload = interval.get("payload") if isinstance(interval.get("payload"), dict) else {}
        explicit = cls._volume_decimal_ml((payload or {}).get("solvent_volume_ml"))
        if explicit is not None:
            return explicit
        kind = str((payload or {}).get("kind") or "").strip().casefold()
        if kind == "solvent":
            return (
                cls._volume_decimal_ml(interval.get("volume_ml"))
                or cls._volume_decimal_ml((payload or {}).get("declared_total_volume_ml"))
                or cls._volume_decimal_ml((payload or {}).get("volume_ml"))
            )
        if cls._solvent_name(payload, interval):
            total = cls._volume_decimal_ml((payload or {}).get("declared_total_volume_ml")) or cls._volume_decimal_ml(interval.get("volume_ml"))
            drug_volume = cls._volume_decimal_ml((payload or {}).get("calculated_volume_ml"))
            if total is not None and drug_volume is not None and total >= drug_volume:
                return total - drug_volume
        return None

    @classmethod
    def _solvent_name(cls, payload: dict[str, Any], interval: dict[str, Any]) -> str:
        solvent_id = cls._clean_text((payload or {}).get("solvent_id")).casefold()
        label = cls._clean_text((payload or {}).get("solvent_label"))
        if not label and str((payload or {}).get("kind") or "").strip().casefold() == "solvent":
            label = cls._clean_text((payload or {}).get("display_name") or (payload or {}).get("label") or interval.get("name") or interval.get("display"))
        normalized = label.casefold().replace(",", ".")
        if solvent_id == "nacl_09" or "nacl 0.9" in normalized or "natrii chloridi 0.9" in normalized:
            return "S. NaCl 0.9%"
        if solvent_id == "glucose_5" or "glucose 5" in normalized or "glucosae 5" in normalized:
            return "S. Glucosae 5%"
        if solvent_id == "ringer" or "ringer" in normalized:
            return "S. Ringeri"
        return label

    @classmethod
    def _infusion_medication_item(cls, interval: dict[str, Any], name: str) -> dict[str, Any]:
        dose_text = cls._infusion_dose_text(interval, name)
        return {
            "kind": "infusion",
            "line": f"{cls._time_range(interval.get('start'), interval.get('end'))} - {dose_text}",
        }

    @classmethod
    def _medication_group_summary(cls, group: dict[str, Any]) -> str:
        items = [item for item in list(group.get("items") or []) if isinstance(item, dict)]
        if not items:
            return ""
        if all(item.get("kind") == "solvent" for item in items):
            volume_items = [item for item in items if isinstance(item.get("volume_ml"), Decimal)]
            if len(volume_items) == len(items):
                volume_total = sum((item["volume_ml"] for item in volume_items), Decimal("0"))
                return f" - {cls._format_volume_decimal(volume_total)}"
            return ""

        if all(item.get("kind") == "gas" for item in items):
            return cls._gas_group_summary(items)

        dose_group_kinds = {"bolus", "timed_infusion_drug"}
        dose_group = [item for item in items if item.get("kind") in dose_group_kinds]
        if not dose_group or len(dose_group) != len(items):
            return ""

        dose_items = [
            item
            for item in dose_group
            if isinstance(item.get("dose_value"), Decimal) and cls._clean_text(item.get("dose_unit"))
        ]
        dose_text = ""
        dose_unit = ""
        if len(dose_items) == len(dose_group):
            unit_keys = {cls._dose_unit_key(item.get("dose_unit")) for item in dose_items}
            if len(unit_keys) == 1:
                dose_unit = cls._clean_text(dose_items[0].get("dose_unit"))
                dose_total = sum((item["dose_value"] for item in dose_items), Decimal("0"))
                dose_text = cls._format_value_unit(dose_total, dose_unit)

        volume_items = [
            item
            for item in dose_group
            if isinstance(item.get("volume_ml"), Decimal)
        ]
        volume_text = ""
        if len(volume_items) == len(dose_group):
            volume_total = sum((item["volume_ml"] for item in volume_items), Decimal("0"))
            volume_text = cls._format_volume_decimal(volume_total)

        if dose_text and volume_text and cls._dose_unit_key(dose_unit) != "мл":
            return f" - {dose_text} ({volume_text})"
        if dose_text:
            return f" - {dose_text}"
        if volume_text:
            return f" - {volume_text}"
        return ""

    @classmethod
    def _gas_group_summary(cls, items: list[dict[str, Any]]) -> str:
        weighted_total = Decimal("0")
        duration_total = Decimal("0")
        values: list[Decimal] = []
        for item in items:
            mac_value = item.get("mac_value")
            if not isinstance(mac_value, Decimal):
                continue
            values.append(mac_value)
            minutes = item.get("duration_minutes")
            try:
                duration = Decimal(int(minutes or 0))
            except (TypeError, ValueError):
                duration = Decimal("0")
            if duration > 0:
                weighted_total += mac_value * duration
                duration_total += duration
        if duration_total > 0:
            return f" - {cls._format_decimal(weighted_total / duration_total)} MAC"
        if values:
            return f" - {cls._format_decimal(sum(values, Decimal('0')) / Decimal(len(values)))} MAC"
        return ""

    @classmethod
    def _bolus_dose_text(cls, event: dict[str, Any], name: str) -> str:
        dose_value, dose_unit = cls._dose_value_unit(event, name)
        dose = cls._format_value_unit(dose_value, dose_unit)
        if not dose:
            dose = cls._display_suffix(event.get("display"), name) or "введено"
        volume = cls._format_volume_decimal(cls._bolus_volume_decimal_ml(event, name, dose_value, dose_unit))
        if volume and cls._dose_unit_key(dose_unit) != "мл" and volume.casefold() not in dose.casefold():
            return f"{dose} ({volume})"
        return dose

    @classmethod
    def _infusion_dose_text(cls, interval: dict[str, Any], name: str) -> str:
        volume = cls._infusion_volume_ml(interval or {})
        if volume is not None:
            return f"{cls._format_decimal(volume)} мл"
        payload = interval.get("payload") if isinstance(interval.get("payload"), dict) else {}
        if str((payload or {}).get("kind") or "").strip().casefold() == "gas":
            history = list(interval.get("dose_history") or [])
            dose_text = cls._clean_text((history[-1] or {}).get("dose_text")) if history else ""
            dose_text = dose_text or cls._clean_text((payload or {}).get("display_dose_text") or (payload or {}).get("dose_text"))
            if dose_text:
                return dose_text
        rate = cls._value_unit_text(interval.get("rate_value"), interval.get("rate_unit"))
        if rate:
            return f"скорость {rate}"
        return cls._display_suffix(interval.get("display"), name) or "инфузия"

    @classmethod
    def _time_range(cls, start_value: Any, end_value: Any) -> str:
        start = cls._parse_dt(start_value)
        end = cls._parse_dt(end_value)
        if start is None:
            return "—"
        if end is None or end == start:
            return start.strftime("%H:%M")
        return f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}"

    @classmethod
    def _signature_table(cls, context: dict[str, Any], width: float) -> Table:
        case = context.get("case") if isinstance(context.get("case"), dict) else {}
        line = "________________"
        anesthesiologist = re.sub(r"\s+", " ", str((case or {}).get("anesthesiologist") or "—").strip()) or "—"
        anesthetist = re.sub(r"\s+", " ", str((case or {}).get("anesthetist") or "—").strip()) or "—"
        data = [
            [
                cls._p(f"Врач анестезиолог-реаниматолог: {anesthesiologist}\nПодпись: {line}", "small"),
                cls._p(f"Медицинская сестра-анестезист: {anesthetist}\nПодпись: {line}", "small"),
            ]
        ]
        table = Table(data, colWidths=[width / 2, width / 2])
        table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]
            )
        )
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
        return _VitalsChartFlowable._parse_dt(value)

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
