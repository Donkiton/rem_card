from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Sequence


DEFAULT_CHART_COLORS = [
    "#8a8a68",
    "#d97706",
    "#c0504d",
    "#5b9bd5",
    "#71a95a",
    "#705470",
    "#eeb211",
    "#4b5563",
    "#10b981",
    "#f43f5e",
]


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
                color: #222222;
                background: #ffffff;
                margin: 0;
                padding: 16px 18px;
                text-align: center;
            }}
            h2 {{ color: #333333; margin-bottom: 4px; }}
            h3 {{ color: #50503f; margin-top: 26px; margin-bottom: 8px; font-size: 14px; }}
            div {{ page-break-inside: avoid; }}
            img {{
                max-width: 100%;
                height: auto;
                border: 1px solid #e3e3d7;
                border-radius: 4px;
                padding: 3px;
                background: #ffffff;
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
                "figure.facecolor": "#ffffff",
                "axes.facecolor": "#fcfcfa",
                "axes.edgecolor": "#d4d4c4",
                "grid.color": "#e6e6dc",
                "grid.linestyle": "-",
                "grid.linewidth": 0.8,
                "axes.titleweight": "bold",
                "axes.labelcolor": "#2d2d24",
                "xtick.color": "#3c3c34",
                "ytick.color": "#3c3c34",
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
