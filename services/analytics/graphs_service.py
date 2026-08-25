from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Mapping, Sequence
from xml.sax.saxutils import escape as xml_escape

# RemCard only renders charts into files.  Selecting a GUI backend here makes
# pyplot try to create Qt windows from analytics worker threads and can leave
# the UI permanently waiting for their completion.
os.environ["MPLBACKEND"] = "Agg"

from rem_card.ui.analytics.chart_renderer import configure_chart_style
from rem_card.app.logger import logger
from rem_card.services.analytics.period import normalize_analytics_period
from rem_card.services.analytics.graph_catalog import GRAPH_GROUPS
from rem_card.ui.styles.theme import (
    ANALYTICS_CHART_COLORS,
    COLOR_PRIMARY_DARK,
    TEXT_PRIMARY,
)

DEFAULT_CHART_COLORS = list(ANALYTICS_CHART_COLORS)
_GRAPH_RENDER_LOCK = threading.RLock()
MAX_AXIS_TICKS = 10
# Public dispatch contract. Every selector is rendered from the platform
# GraphMetricArtifact; no catalog item maps to a legacy SQL generator.
def _dispatch_catalog() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for group in GRAPH_GROUPS.values():
        for key in group:
            if key.startswith("recovery_") or key.startswith("g"):
                mapping[key] = "GraphMetricArtifact"
    return mapping


PRODUCTION_GRAPH_DISPATCH = _dispatch_catalog()


@dataclass
class GraphsBuildResult:
    html: str
    image_paths: list[str]
    # The renderer and PDF hand-off retain the immutable calculation artifact
    # used for this report.  Images are a presentation of this payload, not a
    # second analytics contract.
    artifacts: Mapping[str, Mapping[str, object]] | None = None


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
    authoritative_artifacts: Mapping[str, Mapping[str, object]] | None = None,
    analytics_context: str | None = None,
) -> GraphsBuildResult:
    selected = list(selected or [])
    if not selected:
        raise ValueError("Выберите хотя бы один график для формирования.")
    unsupported = [key for key in selected if key not in PRODUCTION_GRAPH_DISPATCH]
    if unsupported:
        raise ValueError(f"Для графика не задан production dispatch: {', '.join(unsupported)}")

    chart_colors = list(chart_colors or DEFAULT_CHART_COLORS)
    period = normalize_analytics_period(start_date_str, end_date_str)
    selected_start_date_str = period.start_date.isoformat()
    selected_end_date_str = period.end_date.isoformat()
    manager, cleanup = _thread_local_manager(db_manager)
    img_paths: list[str] = []
    started_at = time.perf_counter()
    build_status = "error"
    html_content = (
        "<h2>Графический отчет ОАР №3</h2>"
        f"<p>Период: {selected_start_date_str} - {selected_end_date_str}</p>"
    )
    if analytics_context:
        html_content += str(analytics_context)
    try:
        artifacts = dict(authoritative_artifacts or {})
        if not artifacts:
            from rem_card.services.analytics.platform import CohortDefinition, MetricScope
            snapshot = build_graphs_snapshot(manager, start_date_str, end_date_str, selected,
                cohort=CohortDefinition(scope=MetricScope.RAO, include_recovery_beds=include_recovery_beds))
            artifacts = {key: result.artifact or {} for key, result in snapshot.results.items()}
        html_content += "<h3>Расчётные данные выбранных графиков</h3>"
        for key in selected:
            item = artifacts.get(key)
            if not item: raise ValueError(f"Для графика {key} отсутствует authoritative artifact")
            summary = xml_escape(str(item.get("summary") or key)); source_ids = ",".join(map(str, item.get("source_case_ids") or ()))
            html_content += f"<p data-graph-key='{xml_escape(key)}' data-source-case-ids='{xml_escape(source_ids)}'>{summary}</p>"
        # Matplotlib has process-global rendering state and is not thread-safe.
        # Agg is non-interactive; the lock also prevents two background report
        # requests from mutating pyplot state at the same time.
        with _GRAPH_RENDER_LOCK:
            configure_chart_style(chart_colors)
            html_content = _render_authoritative_artifacts(
                selected, artifacts, chart_colors, img_paths, html_content,
            )
        build_status = "ok"
        return GraphsBuildResult(html=html_content, image_paths=img_paths, artifacts=artifacts)
    except Exception:
        _cleanup_graph_image_files(img_paths)
        raise
    finally:
        logger.info(
            "Analytics graphs build status=%s selected=%s images=%s elapsed_ms=%.1f",
            build_status,
            len(selected),
            len(img_paths),
            (time.perf_counter() - started_at) * 1000.0,
        )
        if cleanup:
            cleanup()


def _render_authoritative_artifacts(selected, artifacts, chart_colors, img_paths, html_content: str) -> str:
    """Render only serialized engine data; never query a clinical connection."""
    try:
        import matplotlib.pyplot as plt
        from rem_card.ui.analytics.graphs_generators_1 import save_plot
    except ImportError as error:  # pragma: no cover - installation contract
        raise RuntimeError("Для построения графиков требуется matplotlib.") from error
    for index, key in enumerate(selected):
        artifact = artifacts[key]
        title = str(artifact.get("title") or key)
        chart_kind = str(artifact.get("chart_kind") or "bar")
        series = tuple(artifact.get("series") or ())
        if chart_kind == "table":
            html_content += _render_table_artifact(title, series)
            continue
        labels, numeric = _numeric_series(series)
        labels, numeric = _trim_empty_time_edges(labels, numeric)
        display_labels = [_format_axis_label(label) for label in labels]
        if not labels:
            html_content += f"<div style='text-align:center'><h3>{xml_escape(title)}</h3><p>Нет данных для выбранной популяции.</p></div><br>"
            continue
        color = chart_colors[index % len(chart_colors)]
        if chart_kind == "ward_histograms":
            _render_ward_histograms(plt, artifact, series, numeric, color, title)
        else:
            _render_standard_chart(
                plt, artifact, series, labels, display_labels, numeric,
                chart_colors, color, title, chart_kind,
            )
        html_content += save_plot(title, img_paths)
    return html_content


def _render_table_artifact(title: str, series) -> str:
    if not series:
        return f"<div style='text-align:center'><h3>{xml_escape(title)}</h3><p>Нет данных для выбранной популяции.</p></div><br>"
    table_rows = []
    for item in series:
        label = xml_escape(str(item.get("label") or "Показатель"))
        value = xml_escape(str(item.get("value") if item.get("value") is not None else "—"))
        unit = xml_escape(str(item.get("unit") or ""))
        table_rows.append(
            f"<tr><td style='padding:8px 12px;border-bottom:1px solid #d6dde5'>{label}</td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #d6dde5;text-align:right'>{value}</td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #d6dde5'>{unit}</td></tr>"
        )
    return (
        f"<section><h3>{xml_escape(title)}</h3>"
        "<table style='width:100%;border-collapse:collapse'>"
        "<thead><tr><th style='text-align:left;padding:8px 12px'>Показатель</th>"
        "<th style='text-align:right;padding:8px 12px'>Значение</th>"
        "<th style='text-align:left;padding:8px 12px'>Единица</th></tr></thead><tbody>"
        + "".join(table_rows)
        + "</tbody></table></section><br>"
    )


def _numeric_series(series) -> tuple[list[str], list[float]]:
    labels = [
        str(item.get("label") or item.get("source_case_id") or f"{position + 1}")
        for position, item in enumerate(series)
    ]
    numeric = []
    for item in series:
        try:
            numeric.append(float(item.get("value")))
        except (TypeError, ValueError):
            numeric.append(0.0)
    return labels, numeric


def _render_ward_histograms(plt, artifact, series, numeric, color: str, title: str) -> None:
    groups: dict[str, list[float]] = {}
    for item, value in zip(series, numeric):
        groups.setdefault(str(item.get("group") or "Не указан"), []).append(value)
    columns = 2
    row_count = max(1, (len(groups) + columns - 1) // columns)
    figure, axes = plt.subplots(row_count, columns, figsize=(12, max(4, row_count * 3.2)))
    flat_axes = list(getattr(axes, "flat", [axes]))
    for axis, (group, group_values) in zip(flat_axes, sorted(groups.items())):
        axis.hist(group_values, bins=min(20, max(1, len(group_values))), color=color, edgecolor="white")
        axis.set_title(group)
        axis.set_xlabel(str(artifact.get("unit") or "суток"))
    for axis in flat_axes[len(groups):]:
        axis.set_visible(False)
    figure.suptitle(title)
    figure.tight_layout()


def _render_standard_chart(
    plt, artifact, series, labels, display_labels, numeric,
    chart_colors, color: str, title: str, chart_kind: str,
) -> None:
    plt.figure(figsize=(9, 4.5))
    if chart_kind == "pie":
        colors = [chart_colors[position % len(chart_colors)] for position in range(len(labels))]
        plt.pie(numeric, labels=labels, colors=colors, autopct="%1.0f%%")
    elif chart_kind == "step":
        _render_step_chart(plt, series, labels, display_labels, numeric, color)
    elif chart_kind == "histogram" and len(numeric) > 1:
        plt.hist(numeric, bins=min(20, max(1, len(numeric))), color=color, edgecolor="white")
        plt.xlabel(str(artifact.get("unit") or "значение"))
    elif chart_kind == "line":
        positions = list(range(len(labels)))
        marker = "o" if len(labels) <= 60 else None
        plt.plot(positions, numeric, marker=marker, color=color, linewidth=1.8)
        _set_sparse_ticks(plt, positions, display_labels)
    else:
        positions = list(range(len(labels)))
        plt.bar(positions, numeric, color=color)
        _set_sparse_ticks(plt, positions, display_labels)
    plt.title(title)
    plt.ylabel(str(artifact.get("unit") or ""))
    plt.tight_layout()


def _render_step_chart(plt, series, labels, display_labels, numeric, color: str) -> None:
    x_values = []
    for position, item in enumerate(series):
        try:
            x_values.append(float(item.get("x")))
        except (TypeError, ValueError):
            x_values.append(float(position))
    plt.step(x_values, numeric, where="post", color=color)
    _set_sparse_ticks(plt, x_values, display_labels)
    plt.ylabel("Оценка выживаемости")


def _set_sparse_ticks(plt, positions, display_labels) -> None:
    tick_indexes = _sparse_tick_indexes(len(display_labels))
    plt.xticks(
        [positions[position] for position in tick_indexes],
        [display_labels[position] for position in tick_indexes],
        rotation=35,
        ha="right",
    )


def _parse_axis_date(label: str) -> datetime | None:
    for pattern in ("%Y-%m-%d", "%Y-%m"):
        try:
            return datetime.strptime(str(label), pattern)
        except ValueError:
            continue
    return None


def _trim_empty_time_edges(labels: list[str], values: list[float]) -> tuple[list[str], list[float]]:
    """Hide uninformative zero-only years without altering the artifact."""
    if not labels or len(labels) != len(values) or not all(_parse_axis_date(label) for label in labels):
        return labels, values
    nonzero = [index for index, value in enumerate(values) if value != 0]
    if not nonzero:
        if len(labels) <= 2:
            return labels, values
        return [labels[0], labels[-1]], [values[0], values[-1]]
    start = max(0, nonzero[0] - 1)
    end = min(len(labels), nonzero[-1] + 2)
    return labels[start:end], values[start:end]


def _sparse_tick_indexes(count: int, limit: int = MAX_AXIS_TICKS) -> list[int]:
    if count <= 0:
        return []
    if count <= limit:
        return list(range(count))
    indexes = {
        round(position * (count - 1) / (limit - 1))
        for position in range(limit)
    }
    return sorted(indexes)


def _format_axis_label(label: str) -> str:
    parsed = _parse_axis_date(label)
    if parsed is None:
        return str(label)
    return parsed.strftime("%d.%m.%Y" if len(str(label)) == 10 else "%m.%Y")


def build_graphs_snapshot(
    db_manager,
    start_date_str: str,
    end_date_str: str,
    selected: Sequence[str],
    *,
    cohort=None,
    db_paths: Sequence[str] = (),
):
    """Структурированное сопровождение выбранных графиков (единый реестр)."""
    from rem_card.services.analytics.platform import AnalyticsEngine, AnalyticsPeriod, CohortDefinition, MetricScope, StatisticsRepository
    return AnalyticsEngine(StatisticsRepository(db_manager, db_paths=db_paths)).snapshot(
        MetricScope.RAO, AnalyticsPeriod.from_values(start_date_str, end_date_str),
        cohort or CohortDefinition(scope=MetricScope.RAO), tuple(selected),
    )


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


def _cleanup_graph_image_files(paths: Sequence[str]) -> None:
    for path in paths or ():
        try:
            os.remove(str(path))
        except FileNotFoundError:
            pass
        except Exception:
            pass


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
        if tag == "br" and self._text_stack:
            self._text_stack[-1][1].append("\n")
            return
        if tag == "img":
            src = attrs_map.get("src", "").strip()
            if src:
                self.items.append(_GraphsPdfItem("image", unescape(src)))
            return
        if tag in {"h2", "h3", "p"}:
            self._text_stack.append((tag, []))
            return
        if tag == "tr":
            self._text_stack.append(("tr", []))
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
            self.items.append(_GraphsPdfItem("p" if kind == "tr" else kind, text))


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
