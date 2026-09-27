import math


def _render(artifact):
    from rem_card.services.analytics.graphs_service import _numeric_series, _render_standard_chart
    from rem_card.ui.analytics.chart_renderer import configure_chart_style

    configure_chart_style(["#2f6690", "#d97706"])
    import matplotlib.pyplot as plt

    labels, numeric = _numeric_series(tuple(artifact["series"]))
    _render_standard_chart(
        plt, artifact, tuple(artifact["series"]), labels, labels, numeric,
        ["#2f6690", "#d97706"], "#2f6690", artifact["title"], artifact.get("chart_kind", "bar"),
    )
    return plt


def test_explicit_scalar_bar_fallback_is_a_kpi_card():
    plt = _render({
        "metric_id": "g48", "title": "Максимальная одномоментная интенсивность", "unit": "пациентов",
        "series": ({"label": "Максимум", "value": 12},),
    })
    try:
        axis = plt.gcf().axes[0]
        assert not axis.axison
        assert {text.get_text() for text in axis.texts} >= {"12", "пациентов"}
    finally:
        plt.close("all")


def test_one_month_bar_remains_a_labelled_chart():
    plt = _render({
        "metric_id": "g9", "title": "Средняя нагрузка", "unit": "пациентов на койку",
        "series": ({"label": "2026-01", "value": 1.5},),
    })
    try:
        axis = plt.gcf().axes[0]
        assert axis.axison
        assert len(axis.patches) == 1
        assert axis.get_ylabel() == "Значение, пациентов на койку"
    finally:
        plt.close("all")


def test_long_nominal_categories_are_ranked_and_all_remain_visible():
    labels = (
        "Длительное наименование отделения интенсивной терапии № 1",
        "Длительное наименование отделения интенсивной терапии № 2",
        "Отделение 3",
    )
    plt = _render({
        "metric_id": "g61", "title": "Распределение пациентов по отделениям", "unit": "случаев",
        "series": tuple({"label": label, "value": value} for label, value in zip(labels, (2, 9, 4))),
    })
    try:
        axis = plt.gcf().axes[0]
        visible_labels = [label.get_text().replace("\n", " ") for label in axis.get_yticklabels()]
        assert len(visible_labels) == len(labels)
        assert visible_labels[0].startswith("Длительное наименование отделения интенсивной терапии № 2")
        assert axis.yaxis_inverted()
        assert [round(bar.get_width()) for bar in axis.patches] == [9, 4, 2]
    finally:
        plt.close("all")


def test_outcome_composition_uses_exact_counts_and_ignores_empty_values():
    plt = _render({
        "metric_id": "g28", "title": "Исходы лечения", "unit": "случаев", "chart_kind": "pie",
        "series": (
            {"label": "Выписаны", "value": 3},
            {"label": "Умерли", "value": 0},
            {"label": "Не указан", "value": None},
        ),
    })
    try:
        axis = plt.gcf().axes[0]
        assert len(axis.patches) == 3
        assert axis.get_xlabel() == "Доля, %"
        assert any("3 (100.0%)" in text.get_text() for text in axis.texts)
        assert any("0 (0.0%)" in text.get_text() for text in axis.texts)
        assert any(text.get_text() == "—" for text in axis.texts)
        assert math.isclose(axis.patches[0].get_width(), 100.0)
    finally:
        plt.close("all")


def test_empty_outcome_denominator_does_not_claim_a_percentage():
    plt = _render({
        "metric_id": "g28", "title": "Исходы", "unit": "случаев", "chart_kind": "pie",
        "series": ({"label": "Выписаны", "value": 0}, {"label": "Умерли", "value": 0}),
    })
    try:
        assert [text.get_text() for text in plt.gca().texts] == ["0 (—)", "0 (—)"]
    finally:
        plt.close("all")


def test_outcome_fallback_keeps_the_same_order_as_antv_cards():
    from rem_card.services.analytics.graph_infographics import outcome_cards

    artifact = {
        "metric_id": "g28", "title": "Исходы", "unit": "случаев", "chart_kind": "pie",
        "series": tuple({"label": label, "value": value} for label, value in zip(
            ("Переведены", "Выписаны", "Умерли"), (1, 9, 4),
        )),
    }
    expected = [card["label"] for card in outcome_cards("g28", artifact)]
    plt = _render(artifact)
    try:
        assert [tick.get_text() for tick in plt.gca().get_yticklabels()] == expected
        assert [text.get_text().split()[0] for text in plt.gca().texts] == ["1", "9", "4"]
    finally:
        plt.close("all")


def test_composition_helper_distinguishes_missing_values_from_real_zero():
    import matplotlib.pyplot as plt
    from rem_card.ui.analytics.chart_renderer import plot_pie_with_legend

    try:
        plt.figure()
        plot_pie_with_legend([3, 0, None, "invalid"], ["A", "B", "C", "D"], ["#123456"], preserve_order=True)
        assert [text.get_text() for text in plt.gca().texts] == ["3 (100.0%)", "0 (0.0%)", "—", "—"]
    finally:
        plt.close("all")


def test_histogram_with_no_finite_observations_does_not_invent_a_zero_bin():
    plt = _render({
        "metric_id": "g33", "title": "Длительность пребывания", "unit": "суток", "chart_kind": "histogram",
        "series": ({"label": "Наблюдение", "value": None},),
    })
    try:
        axis = plt.gcf().axes[0]
        assert not axis.patches
        assert any("Нет наблюдений" in text.get_text() for text in axis.texts)
    finally:
        plt.close("all")


def test_ordered_death_timing_categories_are_not_sorted_by_count():
    labels = ("До 24 часов", "1–3 суток", "4–7 суток", "Более 7 суток")
    plt = _render({
        "metric_id": "g38", "title": "Сроки летальности", "unit": "случаев",
        "series": tuple({"label": label, "value": value} for label, value in zip(labels, (1, 7, 3, 2))),
    })
    try:
        axis = plt.gcf().axes[0]
        assert [tick.get_text() for tick in axis.get_xticklabels()] == list(labels)
        assert [bar.get_height() for bar in axis.patches] == [1, 7, 3, 2]
    finally:
        plt.close("all")


def test_one_long_nominal_category_remains_readable():
    plt = _render({
        "metric_id": "g61", "title": "Отделения", "unit": "случаев",
        "series": ({"label": "Отделение специализированной хирургической помощи", "value": 4},),
    })
    try:
        axis = plt.gcf().axes[0]
        assert axis.get_xlabel() == "Значение, случаев"
        assert len(axis.get_yticklabels()) == 1
        assert axis.patches[0].get_width() == 4
    finally:
        plt.close("all")


def test_report_chart_export_is_300_dpi_and_keeps_preview_separate():
    import matplotlib as mpl
    from rem_card.ui.analytics.chart_renderer import CHART_DPI, configure_chart_style

    configure_chart_style(["#2f6690"])
    assert CHART_DPI == 300
    assert mpl.rcParams["savefig.dpi"] == 300


def test_export_preserves_narrow_subplot_title_wrapping():
    import matplotlib.pyplot as plt
    from rem_card.ui.analytics.chart_renderer import _prepare_figure
    from rem_card.services.analytics.graphs_service import _render_ward_histograms

    names = [f"Отделение специализированной хирургической помощи № {i}" for i in range(2)]
    series = tuple({"group": name, "value": 2} for name in names)
    try:
        _render_ward_histograms(plt, {"unit": "суток"}, series, [2, 2], "#123456", "Распределения")
        fig = plt.gcf()
        before = [axis.get_title() for axis in fig.axes]
        _prepare_figure(fig)
        assert all("\n" in title for title in before)
        assert [axis.get_title() for axis in fig.axes] == before
        fig.canvas.draw()
        bounds = [axis.title.get_window_extent(fig.canvas.get_renderer()) for axis in fig.axes]
        assert bounds[0].x1 < bounds[1].x0
    finally:
        plt.close("all")
