from __future__ import annotations

import os
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Sequence
from xml.sax.saxutils import escape as xml_escape

from rem_card.ui.analytics.chart_renderer import configure_chart_style
from rem_card.services.analytics.recovery_filter import recovery_bed_analytics_filter
from rem_card.ui.styles.theme import (
    ANALYTICS_CHART_COLORS,
    BG_CARD,
    COLOR_PRIMARY_DARK,
    TEXT_PRIMARY,
)

DEFAULT_CHART_COLORS = list(ANALYTICS_CHART_COLORS)


@dataclass
class GraphsBuildResult:
    html: str
    image_paths: list[str]


@dataclass(frozen=True)
class _GraphsPdfItem:
    kind: str
    value: str


def build_graphs_html(
    db_manager,
    start_date_str: str,
    end_date_str: str,
    selected: Sequence[str],
    chart_colors: Sequence[str] | None = None,
    *,
    include_recovery_beds: bool = False,
) -> GraphsBuildResult:
    selected = list(selected or [])
    if not selected:
        raise ValueError("Выберите хотя бы один график для формирования.")

    chart_colors = list(chart_colors or DEFAULT_CHART_COLORS)
    manager, cleanup = _thread_local_manager(db_manager)
    conn = manager.get_connection()
    params = (start_date_str, end_date_str)
    img_paths: list[str] = []
    html_content = (
        "<h2>Графический отчет ОАР №3</h2>"
        f"<p>Период: {start_date_str.split(' ')[0]} - {end_date_str.split(' ')[0]}</p>"
    )

    try:
        with recovery_bed_analytics_filter(conn, include_recovery_beds=include_recovery_beds):
            _configure_plot_style(chart_colors)
            (
                generate_g1_g5,
                generate_g6_g13,
                generate_g14_g18,
                generate_g19_g22,
                generate_g23_g30,
                generate_g31_g35,
                generate_g36_g40,
                generate_g41_g45,
                generate_g46_g50,
                generate_g51_g55,
                generate_g56_g60,
                generate_g61_g65,
            ) = _load_generators()

            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, patient_id, admission_datetime, transfer_datetime, death_datetime,
                       outcome, patient_age, patient_age_unit, patient_gender,
                       source_department, diagnosis_code, diagnosis_text
                FROM admissions
                WHERE admission_datetime BETWEEN ? AND ?
                """,
                params,
            )
            adms = [dict(zip([column[0] for column in cursor.description], row)) for row in cursor.fetchall()]

            html_content = generate_g1_g5(selected, conn, params, chart_colors, img_paths, html_content)
            html_content = generate_g6_g13(
                selected, conn, params, chart_colors, img_paths, adms, start_date_str, end_date_str, html_content
            )
            html_content = generate_g14_g18(
                selected, conn, params, chart_colors, img_paths, adms, start_date_str, end_date_str, html_content
            )
            html_content = generate_g19_g22(selected, conn, params, chart_colors, img_paths, adms, html_content)
            html_content = generate_g23_g30(selected, conn, params, chart_colors, img_paths, html_content)
            html_content = generate_g31_g35(selected, conn, params, chart_colors, img_paths, html_content)
            html_content = generate_g36_g40(selected, conn, params, chart_colors, img_paths, adms, html_content)
            html_content = generate_g41_g45(selected, conn, params, chart_colors, img_paths, html_content)
            html_content = generate_g46_g50(selected, conn, params, chart_colors, img_paths, adms, html_content)
            html_content = generate_g51_g55(
                selected, conn, params, chart_colors, img_paths, adms, start_date_str, end_date_str, html_content
            )
            html_content = generate_g56_g60(selected, conn, params, chart_colors, img_paths, html_content)
            html_content = generate_g61_g65(selected, conn, params, chart_colors, img_paths, html_content)
            return GraphsBuildResult(html=html_content, image_paths=img_paths)
    finally:
        if cleanup:
            cleanup()


def build_graphs_pdf(html_content: str, output_path) -> str:
    try:
        from PIL import Image as PILImage
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer

        from rem_card.ui.rem_card_sectors.s_print.reportlab_builder import ReportLabReportBuilder
    except Exception as exc:
        raise RuntimeError("Библиотека reportlab или Pillow не установлена.") from exc

    ReportLabReportBuilder._ensure_fonts_registered()
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(
        str(output),
        pagesize=A4,
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        pageCompression=1,
    )
    styles = _graphs_pdf_styles(ReportLabReportBuilder, colors, TA_CENTER, ParagraphStyle)
    story = []
    expected_images = 0
    rendered_images = 0

    for item in _parse_graphs_pdf_items(html_content):
        if item.kind == "image":
            expected_images += 1
            image_flowable = _graphs_pdf_image_flowable(item.value, doc.width, doc.height - 8, PILImage, Image)
            story.append(image_flowable)
            story.append(Spacer(1, 10))
            rendered_images += 1
            continue

        style_name = {
            "h2": "title",
            "h3": "section",
            "metric": "metric",
            "p": "note",
        }.get(item.kind, "note")
        story.append(_graphs_pdf_paragraph(item.value, styles[style_name], Paragraph))
        story.append(Spacer(1, 5 if item.kind != "h2" else 8))

    if not story:
        raise ValueError("Нет данных для формирования PDF с графиками.")
    if expected_images and rendered_images != expected_images:
        raise ValueError(f"Не все графики добавлены в PDF: {rendered_images} из {expected_images}.")

    doc.build(story, onFirstPage=_draw_graphs_pdf_background, onLaterPages=_draw_graphs_pdf_background)
    if not output.exists() or output.stat().st_size <= 0:
        raise OSError(f"PDF file was not created: {output}")
    return str(output)


def _thread_local_manager(db_manager):
    db_path = os.path.abspath(str(getattr(db_manager, "db_path", "") or ""))
    if db_path and os.path.isfile(db_path):
        from rem_card.services.analytics.multi_db_analytics import create_readonly_analytics_manager

        manager = create_readonly_analytics_manager(db_path)
        return manager, manager.close_connection
    return db_manager, None


def _parse_graphs_pdf_items(html_content: str) -> list[_GraphsPdfItem]:
    parser = _GraphsPdfHtmlParser()
    parser.feed(str(html_content or ""))
    parser.close()
    return parser.items


class _GraphsPdfHtmlParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items: list[_GraphsPdfItem] = []
        self._text_stack: list[tuple[str, list[str]]] = []

    def handle_starttag(self, tag, attrs):
        tag = str(tag or "").lower()
        attrs_map = {str(key).lower(): str(value or "") for key, value in attrs}
        if tag == "img":
            src = attrs_map.get("src", "").strip()
            if src:
                self.items.append(_GraphsPdfItem("image", unescape(src)))
            return
        if tag in {"h2", "h3", "p"}:
            self._text_stack.append((tag, []))
            return
        if tag == "div":
            style = attrs_map.get("style", "").lower().replace(" ", "")
            if "font-size:" in style and "font-weight:bold" in style:
                self._text_stack.append(("metric", []))

    def handle_data(self, data):
        if self._text_stack:
            self._text_stack[-1][1].append(str(data or ""))

    def handle_endtag(self, tag):
        if not self._text_stack:
            return
        tag = str(tag or "").lower()
        kind, chunks = self._text_stack[-1]
        if tag != kind and not (tag == "div" and kind == "metric"):
            return
        self._text_stack.pop()
        text = " ".join("".join(chunks).split())
        if text:
            self.items.append(_GraphsPdfItem(kind, text))


def _graphs_pdf_styles(report_builder, colors, alignment, paragraph_style_cls):
    return {
        "title": paragraph_style_cls(
            "GraphsTitle",
            fontName=report_builder.FONT_BOLD,
            fontSize=13,
            leading=16,
            textColor=colors.HexColor(TEXT_PRIMARY),
            alignment=alignment,
            spaceAfter=2,
        ),
        "section": paragraph_style_cls(
            "GraphsSection",
            fontName=report_builder.FONT_BOLD,
            fontSize=10,
            leading=13,
            textColor=colors.HexColor(COLOR_PRIMARY_DARK),
            alignment=alignment,
            spaceBefore=6,
        ),
        "metric": paragraph_style_cls(
            "GraphsMetric",
            fontName=report_builder.FONT_BOLD,
            fontSize=18,
            leading=22,
            textColor=colors.HexColor(COLOR_PRIMARY_DARK),
            alignment=alignment,
        ),
        "note": paragraph_style_cls(
            "GraphsNote",
            fontName=report_builder.FONT_REGULAR,
            fontSize=9,
            leading=12,
            textColor=colors.HexColor(TEXT_PRIMARY),
            alignment=alignment,
        ),
    }


def _graphs_pdf_paragraph(text: str, style, paragraph_cls):
    safe_text = xml_escape(str(text or "")).replace("\n", "<br/>")
    return paragraph_cls(safe_text, style)


def _graphs_pdf_image_flowable(src: str, max_width: float, max_height: float, pil_image_cls, image_cls):
    image_path = Path(str(src or ""))
    if not image_path.exists():
        raise FileNotFoundError(f"Файл графика не найден: {image_path}")

    with pil_image_cls.open(image_path) as image:
        width_px, height_px = image.size
    if width_px <= 0 or height_px <= 0:
        raise ValueError(f"Некорректный размер файла графика: {image_path}")

    scale = min(float(max_width) / float(width_px), float(max_height) / float(height_px))
    draw_width = max(1.0, width_px * scale)
    draw_height = max(1.0, height_px * scale)
    flowable = image_cls(str(image_path), width=draw_width, height=draw_height)
    flowable.hAlign = "CENTER"
    return flowable


def _draw_graphs_pdf_background(canvas, doc) -> None:
    canvas.saveState()
    try:
        from reportlab.lib import colors

        canvas.setFillColor(colors.white)
        canvas.rect(0, 0, doc.pagesize[0], doc.pagesize[1], stroke=0, fill=1)
    finally:
        canvas.restoreState()


def _configure_plot_style(chart_colors: Sequence[str]):
    try:
        configure_chart_style(chart_colors)
    except Exception as exc:
        raise RuntimeError("Библиотеки pandas или matplotlib не установлены.") from exc


def _load_generators():
    try:
        import pandas  # noqa: F401
    except Exception as exc:
        raise RuntimeError("Библиотеки pandas или matplotlib не установлены.") from exc

    from rem_card.ui.analytics.graphs_generators_1 import (
        generate_g1_g5,
        generate_g6_g13,
        generate_g14_g18,
        generate_g19_g22,
    )
    from rem_card.ui.analytics.graphs_generators_2 import (
        generate_g23_g30,
        generate_g31_g35,
        generate_g36_g40,
        generate_g41_g45,
    )
    from rem_card.ui.analytics.graphs_generators_3 import (
        generate_g46_g50,
        generate_g51_g55,
        generate_g56_g60,
        generate_g61_g65,
    )

    return (
        generate_g1_g5,
        generate_g6_g13,
        generate_g14_g18,
        generate_g19_g22,
        generate_g23_g30,
        generate_g31_g35,
        generate_g36_g40,
        generate_g41_g45,
        generate_g46_g50,
        generate_g51_g55,
        generate_g56_g60,
        generate_g61_g65,
    )
