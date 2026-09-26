from __future__ import annotations

import os
import threading
import time
import math
import textwrap
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
_RANKED_NOMINAL_BAR_METRICS = frozenset({
    "g4", "g5", "g23", "g24", "g25", "g26", "g27", "g30", "g32",
    "g50", "g57", "g59", "g61", "g62",
})
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
    # The UI may use resized copies for its preview, while the report must
    # keep this original HTML so ReportLab embeds the full-resolution charts.
    # Kept after the existing fields to preserve positional construction.
    preview_html: str | None = None


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
    from rem_card.services.analytics.graph_infographics import (
        InfographicReportRenderer,
        render_outcome_infographic,
        render_scalar_overview,
    )

    try:
        import matplotlib.pyplot as plt
        from rem_card.ui.analytics.graphs_generators_1 import save_plot
    except ImportError as error:  # pragma: no cover - installation contract
        raise RuntimeError("Для построения графиков требуется matplotlib.") from error
    infographic_renderer = InfographicReportRenderer()
    overview_html, infographic_keys = render_scalar_overview(
        selected, artifacts, chart_colors, img_paths, renderer=infographic_renderer,
    )
    html_content += overview_html
    for index, key in enumerate(selected):
        if key in infographic_keys:
            continue
        artifact = artifacts[key]
        title = str(artifact.get("title") or key)
        chart_kind = str(artifact.get("chart_kind") or "bar")
        series = tuple(artifact.get("series") or ())
        if chart_kind == "table":
            html_content += _render_table_artifact(title, series)
            continue
        infographic_html = render_outcome_infographic(key, artifact, chart_colors, img_paths, renderer=infographic_renderer)
        if infographic_html is not None:
            html_content += infographic_html
            continue
        if not series:
            html_content += f"<div style='text-align:center'><h3>{xml_escape(title)}</h3><p>Нет данных для выбранной популяции.</p></div><br>"
            continue
        color = chart_colors[index % len(chart_colors)]
        panels = _artifact_panels(artifact)
        for panel_index, panel in enumerate(panels):
            panel_series = tuple(panel.get("series") or ())
            labels, numeric = _numeric_series(panel_series)
            labels, numeric = _trim_empty_time_edges(labels, numeric)
            display_labels = [_format_axis_label(label) for label in labels]
            panel_title = title if len(panels) == 1 else f"{title} · {panel_index + 1}/{len(panels)}"
            if chart_kind == "ward_histograms":
                _render_ward_histograms(plt, panel, panel_series, numeric, color, panel_title)
            else:
                _render_standard_chart(
                    plt, panel, panel_series, labels, display_labels, numeric,
                    chart_colors, color, panel_title, chart_kind,
                )
            html_content += save_plot(panel_title, img_paths)
    return html_content


def _artifact_panels(artifact) -> list[Mapping[str, object]]:
    """Paginate categorical figures for A4 without changing the source artifact."""
    series = tuple(artifact.get("series") or ())
    kind = str(artifact.get("chart_kind") or "bar")
    if kind == "ward_histograms":
        groups = sorted({str(row.get("group") or "Не указан") for row in series})
        if len(groups) <= 6:
            return [artifact]
        return [
            {**artifact, "series": tuple(row for row in series if str(row.get("group") or "Не указан") in groups[start:start + 6])}
            for start in range(0, len(groups), 6)
        ]
    labels, values = _numeric_series(series)
    if kind != "bar" or not _use_horizontal_bars(artifact, labels):
        return [artifact]
    rows = list(zip(series, values))
    if _is_ranked_nominal_bar(artifact):
        rows.sort(key=lambda pair: (not math.isfinite(pair[1]), -(pair[1] if math.isfinite(pair[1]) else 0)))
    panels, page_rows, line_count = [], [], 0
    for row, _value in rows:
        label_lines = max(1, len(_wrapped_category_label(str(row.get("label") or "Не указан")).splitlines()))
        if page_rows and (len(page_rows) >= 10 or line_count + label_lines > 18):
            panels.append({**artifact, "series": tuple(page_rows)})
            page_rows, line_count = [], 0
        page_rows.append(row)
        line_count += label_lines
    if page_rows:
        panels.append({**artifact, "series": tuple(page_rows)})
    return panels or [artifact]


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
        if item.get("value") is None:
            # No denominator/observations in this calendar bucket: break the
            # line instead of displaying a fabricated zero rate or duration.
            numeric.append(float("nan"))
            continue
        try:
            numeric.append(float(item.get("value")))
        except (TypeError, ValueError):
            numeric.append(0.0)
    return labels, numeric


def _render_ward_histograms(plt, artifact, series, numeric, color: str, title: str) -> None:
    from matplotlib.ticker import MaxNLocator

    groups: dict[str, list[float]] = {}
    for item, value in zip(series, numeric):
        groups.setdefault(str(item.get("group") or "Не указан"), []).append(value)
    columns = 2
    row_count = max(1, (len(groups) + columns - 1) // columns)
    figure, axes = plt.subplots(row_count, columns, figsize=(12, max(4, row_count * 3.2)))
    flat_axes = list(getattr(axes, "flat", [axes]))
    for axis, (group, group_values) in zip(flat_axes, sorted(groups.items())):
        finite_values = [value for value in group_values if math.isfinite(value)]
        axis.hist(finite_values, bins=min(20, max(1, len(finite_values))), color=color, edgecolor="white")
        axis.set_title(_wrapped_title(group, 36))
        axis.set_xlabel(str(artifact.get("unit") or "суток"))
        axis.set_ylabel("Число случаев")
        axis.yaxis.set_major_locator(MaxNLocator(integer=True))
        _style_report_axis(axis, grid_axis="y")
    for axis in flat_axes[len(groups):]:
        axis.set_visible(False)
    figure.suptitle(_wrapped_title(title))
    figure.tight_layout()


def _render_standard_chart(
    plt, artifact, series, labels, display_labels, numeric,
    chart_colors, color: str, title: str, chart_kind: str,
) -> None:
    finite = [(label, value) for label, value in zip(labels, numeric) if math.isfinite(value)]
    if chart_kind == "bar" and _is_scalar_bar_artifact(artifact, finite):
        _render_scalar_kpi_card(plt, artifact, finite[0], title, color)
        return

    plt.figure(figsize=(9.6, _chart_height(labels, chart_kind, artifact)))
    axis = plt.gca()
    if chart_kind == "pie":
        # Outcomes are easier to compare as bars.  The renderer writes the
        # canonical count beside the presentation-only percentage.
        from rem_card.ui.analytics.chart_renderer import plot_pie_with_legend

        plot_pie_with_legend(numeric, labels, chart_colors, preserve_order=True)
        axis = plt.gca()
        axis.set_title(_wrapped_title(title))
    elif chart_kind == "step":
        _render_step_chart(plt, series, labels, display_labels, numeric, color)
        axis = plt.gca()
        axis.set_title(_wrapped_title(title))
        _style_report_axis(axis, grid_axis="y")
    elif chart_kind == "histogram":
        from matplotlib.ticker import MaxNLocator

        values = [item[1] for item in finite]
        if values:
            plt.hist(values, bins=min(20, max(1, len(values))), color=color, edgecolor="white")
        else:
            axis.text(0.5, 0.5, "Нет наблюдений для построения распределения", ha="center", va="center", transform=axis.transAxes, color=TEXT_PRIMARY)
        axis.set_xlabel(_value_axis_label(artifact, "Значение"))
        axis.set_ylabel("Число случаев")
        axis.yaxis.set_major_locator(MaxNLocator(integer=True))
        axis.set_title(_wrapped_title(title))
        _style_report_axis(axis, grid_axis="y")
    elif chart_kind == "line":
        dates = [_parse_axis_date(label) for label in labels]
        positions = (
            [(date - dates[0]).days for date in dates]
            if dates and all(date is not None for date in dates)
            else list(range(len(labels)))
        )
        marker = "o" if len(labels) <= 60 else None
        axis.plot(positions, numeric, marker=marker, color=color, linewidth=2.1)
        _set_sparse_ticks(plt, positions, display_labels)
        axis.set_ylabel(_value_axis_label(artifact))
        axis.set_title(_wrapped_title(title))
        _style_report_axis(axis, grid_axis="y")
    else:
        chart_rows = list(zip(labels, display_labels, numeric))
        if _is_ranked_nominal_bar(artifact):
            chart_rows.sort(key=lambda row: (not math.isfinite(row[2]), -(row[2] if math.isfinite(row[2]) else 0)))
        labels, display_labels, numeric = map(list, zip(*chart_rows))
        horizontal = _use_horizontal_bars(artifact, display_labels)
        if horizontal:
            _render_horizontal_bars(axis, display_labels, numeric, color, artifact)
        else:
            positions = list(range(len(labels)))
            bars = axis.bar(positions, numeric, color=color, edgecolor="white", linewidth=0.8)
            _set_sparse_ticks(plt, positions, display_labels)
            axis.set_ylabel(_value_axis_label(artifact))
            _annotate_vertical_bars(axis, bars, numeric)
            _style_report_axis(axis, grid_axis="y")
        axis.set_title(_wrapped_title(title))
    plt.tight_layout(pad=1.35)


def _render_scalar_kpi_card(plt, artifact, item, title: str, color: str) -> None:
    """Present a single calculated bar as a readable report KPI."""
    label, value = item
    figure, axis = plt.subplots(figsize=(9.6, 3.8))
    axis.set_axis_off()
    figure.patch.set_facecolor("white")
    axis.set_facecolor("white")
    unit = str(artifact.get("unit") or "").strip()
    axis.text(0.5, 0.77, _wrapped_title(title, 56), ha="center", va="center", fontsize=16, fontweight="bold")
    axis.text(0.5, 0.43, _format_chart_value(value), ha="center", va="center", fontsize=36, fontweight="bold", color=color)
    axis.text(0.5, 0.25, unit or _clean_chart_label(label), ha="center", va="center", fontsize=13, color=TEXT_PRIMARY)
    figure.subplots_adjust(left=0.05, right=0.95, top=0.92, bottom=0.08)
    setattr(figure, "_remcard_manual_layout", True)


def _render_horizontal_bars(axis, labels, values, color: str, artifact) -> None:
    positions = list(range(len(labels)))
    bars = axis.barh(positions, values, color=color, edgecolor="white", linewidth=0.8, height=0.64)
    axis.set_yticks(positions)
    axis.set_yticklabels([_wrapped_category_label(label) for label in labels])
    axis.invert_yaxis()
    axis.set_xlabel(_value_axis_label(artifact))
    _style_report_axis(axis, grid_axis="x")
    _annotate_horizontal_bars(axis, bars, values)


def _annotate_vertical_bars(axis, bars, values) -> None:
    finite = [value for value in values if math.isfinite(value)]
    if not finite or len(bars) > 24:
        return
    span = max(abs(value) for value in finite) or 1
    for bar, value in zip(bars, values):
        if math.isfinite(value):
            axis.text(bar.get_x() + bar.get_width() / 2, value + span * 0.025, _format_chart_value(value), ha="center", va="bottom", fontsize=10, color=TEXT_PRIMARY)
    axis.margins(y=0.14)
    setattr(axis, "_remcard_preserve_annotation_margin", True)


def _annotate_horizontal_bars(axis, bars, values) -> None:
    finite = [value for value in values if math.isfinite(value)]
    if not finite:
        return
    span = max(abs(value) for value in finite) or 1
    for bar, value in zip(bars, values):
        if math.isfinite(value):
            axis.text(value + span * 0.018, bar.get_y() + bar.get_height() / 2, _format_chart_value(value), ha="left", va="center", fontsize=10, color=TEXT_PRIMARY)
    axis.margins(x=0.18)
    setattr(axis, "_remcard_preserve_annotation_margin", True)


def _style_report_axis(axis, *, grid_axis: str) -> None:
    axis.grid(axis=grid_axis, color="#d6dde5", linewidth=0.7, alpha=0.65)
    axis.grid(axis="y" if grid_axis == "x" else "x", visible=False)
    axis.set_axisbelow(True)
    axis.spines["top"].set_visible(False)
    axis.spines["right"].set_visible(False)


def _value_axis_label(artifact, prefix: str = "Значение") -> str:
    unit = str(artifact.get("unit") or "").strip()
    if unit == "%":
        if artifact.get("metric_id") in {"g7", "g12", "g13", "g46", "g47", "g51"}:
            return "Загрузка, %"
        return "Доля, %"
    return f"{prefix}, {unit}" if unit else prefix


def _is_ranked_nominal_bar(artifact) -> bool:
    return str(artifact.get("metric_id") or "") in _RANKED_NOMINAL_BAR_METRICS


def _is_scalar_bar_artifact(artifact, finite) -> bool:
    """Keep calendar/category one-point series as labelled charts.

    The infographic path normally consumes these explicit scalar metrics.  If
    that renderer is unavailable, the Matplotlib fallback remains a KPI card
    without guessing that a one-month series is a scalar.
    """
    if len(finite) != 1:
        return False
    try:
        from rem_card.services.analytics.graph_infographics import SCALAR_GRAPH_KEYS
    except ImportError:  # pragma: no cover - compatibility for partial installs
        return False
    return str(artifact.get("metric_id") or "") in SCALAR_GRAPH_KEYS


def _use_horizontal_bars(artifact, labels) -> bool:
    metric_id = str(artifact.get("metric_id") or "")
    if metric_id in _RANKED_NOMINAL_BAR_METRICS and len(labels) > 1:
        return True
    longest = max(map(len, labels), default=0)
    # A one-point calendar series still carries its temporal position.  A
    # single long nominal category instead needs the full horizontal label.
    return longest > 14 and not all(_parse_axis_date(label) for label in labels)


def _chart_height(labels, chart_kind: str, artifact) -> float:
    if chart_kind == "bar" and _use_horizontal_bars(artifact, labels):
        line_count = sum(max(1, len(_wrapped_category_label(label).splitlines())) for label in labels)
        return max(4.4, 2.4 + 0.44 * line_count)
    return 5.0


def _wrapped_category_label(label: str) -> str:
    return "\n".join(textwrap.wrap(_clean_chart_label(label), width=28, break_long_words=False))


def _wrapped_title(title: str, width: int = 58) -> str:
    return "\n".join(textwrap.wrap(str(title or ""), width=width, break_long_words=False))


def _clean_chart_label(value) -> str:
    text = str(value or "").strip()
    return text if text else "Не указано"


def _format_chart_value(value: float) -> str:
    if not math.isfinite(value):
        return "—"
    if abs(value - round(value)) < 0.0001:
        return str(int(round(value)))
    return f"{value:.2f}".rstrip("0").rstrip(".").replace(".", ",")


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
