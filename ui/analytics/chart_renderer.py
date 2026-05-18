from __future__ import annotations

import math
import os
import re
import tempfile
import textwrap
import uuid
import warnings
from html import escape
from typing import Callable, Sequence

from rem_card.ui.styles.theme import (
    BG_ALT_ROW,
    BG_CARD,
    BORDER_COLOR,
    BORDER_LIGHT,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)

CHART_DPI = 170
CHART_HTML_WIDTH = 860
MIN_FIGURE_WIDTH_IN = 8.5
MIN_FIGURE_HEIGHT_IN = 3.6


def configure_chart_style(chart_colors: Sequence[str]) -> None:
    import matplotlib

    matplotlib.use("Agg", force=True)

    import matplotlib as mpl
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    font_family = _preferred_font(font_manager)
    plt.style.use("default")
    mpl.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": [font_family, "Segoe UI", "Arial", "DejaVu Sans"],
            "font.size": 14,
            "figure.dpi": CHART_DPI,
            "savefig.dpi": CHART_DPI,
            "figure.facecolor": BG_CARD,
            "savefig.facecolor": BG_CARD,
            "axes.facecolor": BG_ALT_ROW,
            "axes.edgecolor": BORDER_COLOR,
            "axes.labelcolor": TEXT_PRIMARY,
            "axes.labelsize": 13.5,
            "axes.titlesize": 15,
            "axes.titleweight": "bold",
            "axes.titlepad": 12,
            "xtick.color": TEXT_SECONDARY,
            "ytick.color": TEXT_SECONDARY,
            "xtick.labelsize": 12,
            "ytick.labelsize": 12,
            "grid.color": BORDER_LIGHT,
            "grid.linestyle": "-",
            "grid.linewidth": 0.8,
            "legend.frameon": False,
            "legend.fontsize": 12,
            "text.color": TEXT_PRIMARY,
            "text.antialiased": True,
            "patch.antialiased": True,
            "lines.antialiased": True,
            "axes.unicode_minus": False,
        }
    )
    mpl.rcParams["axes.prop_cycle"] = mpl.cycler(color=list(chart_colors))


def save_plot(title: str, img_paths: list[str]) -> str:
    import matplotlib.pyplot as plt

    figure = plt.gcf()
    _prepare_figure(figure)
    filename = f"graph_{uuid.uuid4().hex}.png"
    path = os.path.join(tempfile.gettempdir(), filename)
    figure.savefig(path, dpi=CHART_DPI, facecolor=BG_CARD, edgecolor=BG_CARD)
    plt.close(figure)
    _trim_image_whitespace(path)
    img_paths.append(path)
    return chart_image_html(title, path)


def chart_image_html(title: str, path: str) -> str:
    safe_path = escape(str(path), quote=True)
    return (
        "<div class='analytics-chart-block' "
        "style='text-align:center; page-break-inside:avoid; margin:18px auto 28px auto;'>"
        f"<img src='{safe_path}' width='{CHART_HTML_WIDTH}' "
        f"style='max-width:{CHART_HTML_WIDTH}px; height:auto; "
        f"border:1px solid {BORDER_LIGHT}; border-radius:4px; padding:3px; background:{BG_CARD};'>"
        "</div>"
    )


def fit_chart_images_to_width(html: str, width: int, *, resize_images: bool = False) -> str:
    safe_width = max(180, min(CHART_HTML_WIDTH, int(width or CHART_HTML_WIDTH)))
    result = re.sub(r"(<img\b[^>]*?)\swidth='\d+'", rf"\1 width='{safe_width}'", str(html or ""))
    if not resize_images:
        return result

    def _replace_src(match: re.Match) -> str:
        prefix, path, suffix = match.group(1), match.group(2), match.group(3)
        return f"{prefix}{_resized_image_path(path, safe_width)}{suffix}"

    return re.sub(r"(<img\b[^>]*?\bsrc=')([^']+)('[^>]*>)", _replace_src, result)


def plot_pie_with_legend(
    values,
    labels,
    colors: Sequence[str],
    *,
    value_formatter: Callable[[float], str] | None = None,
    legend_title: str | None = None,
    autopct: str | None = "%1.1f%%",
) -> None:
    import matplotlib.pyplot as plt

    numeric_values = [_to_float(v) for v in values]
    text_labels = [_clean_label(label) for label in labels]
    pairs = [(label, value) for label, value in zip(text_labels, numeric_values) if value > 0]
    if not pairs:
        return

    pairs.sort(key=lambda item: item[1], reverse=True)
    text_labels, numeric_values = zip(*pairs)
    total = sum(numeric_values)
    figure = plt.gcf()
    figure.set_size_inches(
        max(figure.get_figwidth(), 9.0),
        max(figure.get_figheight(), 2.6 + 0.42 * len(numeric_values)),
        forward=True,
    )
    ax = plt.gca()
    ax.clear()
    ax.set_facecolor(BG_ALT_ROW)

    formatter = value_formatter or _format_number
    percentages = [(value / total * 100.0 if total else 0.0) for value in numeric_values]
    y_positions = list(range(len(numeric_values)))
    bar_colors = list(colors)[: len(numeric_values)]
    value_labels = [f"{formatter(value)} ({pct:.1f}%)" for value, pct in zip(numeric_values, percentages)]
    max_label_len = max((len(label) for label in value_labels), default=0)
    left_pad = 5.0
    right_pad = max(26.0, min(46.0, max_label_len * 1.25 + 8.0))
    x_right = max(100.0, max(percentages, default=0.0)) + right_pad

    bars = ax.barh(
        y_positions,
        percentages,
        height=0.62,
        color=bar_colors,
        edgecolor=BG_CARD,
        linewidth=1.0,
    )
    ax.set_yticks(y_positions)
    ax.set_yticklabels([_wrap_axis_label(label) for label in text_labels])
    ax.set_ylim(-0.65, len(numeric_values) - 0.35)
    ax.invert_yaxis()
    ax.set_xlabel("Доля, %")
    ax.set_xlim(-left_pad, x_right)
    ax.set_xticks([0, 20, 40, 60, 80, 100])
    ax.grid(axis="x", alpha=0.55)
    ax.grid(axis="y", visible=False)
    ax.set_ylabel("")

    for bar, value_label in zip(bars, value_labels):
        ax.text(
            min(bar.get_width() + 1.4, x_right - 1.0),
            bar.get_y() + bar.get_height() / 2,
            value_label,
            va="center",
            ha="left",
            fontsize=12,
            fontweight="700",
            color=TEXT_PRIMARY,
        )

    left_margin = 0.28 if max((len(label) for label in text_labels), default=0) <= 18 else 0.34
    figure.subplots_adjust(left=left_margin, right=0.95, top=0.86, bottom=0.18)
    setattr(figure, "_remcard_manual_layout", True)


def _resized_image_path(path: str, target_width: int) -> str:
    try:
        from PIL import Image
    except Exception:
        return path

    try:
        with Image.open(path) as image:
            width, height = image.size
            if width <= 0 or height <= 0:
                return path
            target_width = int(target_width)
            target_height = max(1, round(height * (target_width / width)))
            if abs(width - target_width) <= 1:
                return path
            resized = image.resize((target_width, target_height), Image.Resampling.LANCZOS)
            filename = f"graph_preview_{target_width}_{uuid.uuid4().hex}.png"
            resized_path = os.path.join(tempfile.gettempdir(), filename)
            resized.save(resized_path, format="PNG", optimize=True)
            return resized_path
    except Exception:
        return path


def _trim_image_whitespace(path: str, padding: int = 26) -> None:
    try:
        from PIL import Image, ImageChops
    except Exception:
        return

    try:
        with Image.open(path) as image:
            source = image.convert("RGB")
            background_color = source.getpixel((0, 0))
            background = Image.new("RGB", source.size, background_color)
            diff = ImageChops.difference(source, background).convert("L")
            mask = diff.point(lambda px: 255 if px > 8 else 0)
            bbox = mask.getbbox()
            if not bbox:
                return
            left = max(0, bbox[0] - padding)
            top = max(0, bbox[1] - padding)
            right = min(source.width, bbox[2] + padding)
            bottom = min(source.height, bbox[3] + padding)
            if left == 0 and top == 0 and right == source.width and bottom == source.height:
                return
            image.crop((left, top, right, bottom)).save(path, format="PNG", optimize=True)
    except Exception:
        return


def _wrap_axis_label(label: str) -> str:
    text = _clean_label(label)
    if len(text) <= 28:
        return text
    return "\n".join(
        textwrap.wrap(
            text,
            width=28,
            max_lines=2,
            placeholder="...",
            break_long_words=False,
        )
    )


def _preferred_font(font_manager) -> str:
    available = {font.name for font in font_manager.fontManager.ttflist}
    for font_name in ("Segoe UI", "Arial", "DejaVu Sans"):
        if font_name in available:
            return font_name
    return "DejaVu Sans"


def _prepare_figure(figure) -> None:
    import matplotlib.pyplot as plt

    figure.patch.set_facecolor(BG_CARD)
    width, height = figure.get_size_inches()
    if width < MIN_FIGURE_WIDTH_IN or height < MIN_FIGURE_HEIGHT_IN:
        figure.set_size_inches(max(width, MIN_FIGURE_WIDTH_IN), max(height, MIN_FIGURE_HEIGHT_IN), forward=True)
    for ax in figure.axes:
        ax.title.set_color(TEXT_PRIMARY)
        ax.xaxis.label.set_color(TEXT_PRIMARY)
        ax.yaxis.label.set_color(TEXT_PRIMARY)
        ax.tick_params(axis="both", colors=TEXT_SECONDARY)
        for spine in ax.spines.values():
            spine.set_color(BORDER_COLOR)
        if ax.has_data():
            try:
                ax.margins(x=0.04, y=0.08)
            except Exception:
                pass

    if not getattr(figure, "_remcard_manual_layout", False):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                figure.tight_layout(pad=1.6)
        except Exception:
            pass
        if _has_rotated_x_labels(figure):
            params = figure.subplotpars
            try:
                figure.subplots_adjust(left=max(params.left, 0.14), bottom=max(params.bottom, 0.25))
            except Exception:
                pass


def _to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _clean_label(value, fallback: str = "Не указано") -> str:
    if value is None:
        return fallback
    try:
        if value != value:
            return fallback
    except Exception:
        pass
    if not isinstance(value, str):
        try:
            if math.isnan(float(value)):
                return fallback
        except (TypeError, ValueError, OverflowError):
            pass
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "nat", "<na>"}:
        return fallback
    return text


def _format_number(value: float) -> str:
    if abs(value - int(value)) < 0.0001:
        return str(int(value))
    return f"{value:.1f}"


def _has_rotated_x_labels(figure) -> bool:
    for ax in figure.axes:
        for label in ax.get_xticklabels():
            if str(label.get_text() or "").strip() and abs(float(label.get_rotation() or 0.0)) >= 20.0:
                return True
    return False
