from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Sequence

from rem_card.ui.styles.theme import (
    ANALYTICS_CHART_COLORS,
    BG_ALT_ROW,
    BG_CARD,
    BORDER_COLOR,
    BORDER_LIGHT,
    COLOR_PRIMARY_DARK,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)

DEFAULT_CHART_COLORS = list(ANALYTICS_CHART_COLORS)


@dataclass
class GraphsBuildResult:
    html: str
    image_paths: list[str]


def build_graphs_html(
    db_manager,
    start_date_str: str,
    end_date_str: str,
    selected: Sequence[str],
    chart_colors: Sequence[str] | None = None,
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


def wrap_graphs_pdf_html(html_content: str) -> str:
    return f"""
    <html>
    <head>
        <meta charset="UTF-8">
        <style>
            body {{
                font-family: 'Arial', sans-serif;
                color: {TEXT_PRIMARY};
                background: {BG_CARD};
                margin: 0;
                padding: 16px 18px;
                text-align: center;
            }}
            h2 {{ color: {TEXT_PRIMARY}; margin-bottom: 4px; }}
            h3 {{ color: {COLOR_PRIMARY_DARK}; margin-top: 26px; margin-bottom: 8px; font-size: 14px; }}
            div {{ page-break-inside: avoid; }}
            img {{
                max-width: 100%;
                height: auto;
                border: 1px solid {BORDER_LIGHT};
                border-radius: 4px;
                padding: 3px;
                background: {BG_CARD};
            }}
        </style>
    </head>
    <body>
        {html_content}
    </body>
    </html>
    """


def _thread_local_manager(db_manager):
    db_path = os.path.abspath(str(getattr(db_manager, "db_path", "") or ""))
    if db_path and os.path.isfile(db_path):
        from rem_card.services.analytics.multi_db_analytics import create_readonly_analytics_manager

        manager = create_readonly_analytics_manager(db_path)
        return manager, manager.close_connection
    return db_manager, None


def _configure_plot_style(chart_colors: Sequence[str]):
    try:
        import matplotlib

        matplotlib.use("Agg", force=True)
        import matplotlib.pyplot as plt
    except Exception as exc:
        raise RuntimeError("Библиотеки pandas или matplotlib не установлены.") from exc

    try:
        import seaborn as sns
    except Exception:
        sns = None

    if sns:
        sns.set_theme(
            style="whitegrid",
            context="talk",
            font_scale=0.72,
            rc={
                "figure.facecolor": BG_CARD,
                "axes.facecolor": BG_ALT_ROW,
                "axes.edgecolor": BORDER_COLOR,
                "grid.color": BORDER_LIGHT,
                "grid.linestyle": "-",
                "grid.linewidth": 0.8,
                "axes.titleweight": "bold",
                "axes.labelcolor": TEXT_PRIMARY,
                "xtick.color": TEXT_SECONDARY,
                "ytick.color": TEXT_SECONDARY,
            },
        )
    else:
        plt.style.use("ggplot")


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
