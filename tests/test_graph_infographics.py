from copy import deepcopy

import pytest

from rem_card.services.analytics import graph_infographics as cards


def artifact(key="g10", values=(2.75,), kind="bar"):
    return {
        "metric_id": key,
        "title": "Среднее число пациентов",
        "unit": "пациентов",
        "chart_kind": kind,
        "source_case_ids": ("private:case:1",),
        "series": tuple({"label": f"Категория {index}", "value": value} for index, value in enumerate(values)),
    }


def test_scalar_uses_series_value_and_never_transmits_case_identifiers():
    payload = artifact()
    payload["numerator"] = 99  # The already calculated plotted scalar wins.
    original = deepcopy(payload)
    assert cards.scalar_card("g10", payload) == {
        "label": "Среднее число пациентов", "value": 2.75, "unit": "пациентов",
    }
    assert payload == original


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), "bad", True])
def test_undefined_scalar_does_not_become_zero(value):
    assert cards.scalar_card("g10", artifact(values=(value,))) is None


@pytest.mark.parametrize("key,kind", [("g1", "line"), ("g9", "bar"), ("g33", "histogram"), ("g41", "step")])
def test_single_observation_is_not_a_summary(key, kind):
    assert cards.scalar_card(key, artifact(key, kind=kind)) is None


def test_real_zero_is_preserved():
    assert cards.scalar_card("g43", artifact("g43", values=(0,)))["value"] == 0


def test_outcome_shares_use_only_the_shown_population_and_preserve_order():
    payload = artifact("g28", (7, 2, 1), "pie")
    payload["unit"] = "случаев"
    original = deepcopy(payload)
    rows = cards.outcome_cards("g28", payload)
    assert [row["value"] for row in rows] == [7, 2, 1]
    assert [row["caption"] for row in rows] == [
        "70.0% от показанных исходов", "20.0% от показанных исходов", "10.0% от показанных исходов",
    ]
    assert all(row["unit"] == "случаев" for row in rows)
    assert payload == original


@pytest.mark.parametrize("values", [(), (0, 0), (-1, 3), (None, 3), tuple(range(7))])
def test_unrenderable_composition_keeps_complete_matplotlib_fallback(values):
    assert cards.outcome_cards("g28", artifact("g28", values, "pie")) == []


def test_batch_success_skips_only_successfully_rendered_metrics(monkeypatch):
    from rem_card.services.analytics import infographic_renderer

    keys = sorted(cards.SCALAR_GRAPH_KEYS)
    payload = {key: artifact(key) for key in keys}
    calls = []

    def render(items, **kwargs):
        calls.append(items)
        return "graph_cards.png" if len(calls) == 1 else None

    monkeypatch.setattr(infographic_renderer, "render_infographic", render)
    paths = []
    html, rendered = cards.render_scalar_overview(keys, payload, ["#123456"], paths)
    assert [len(batch) for batch in calls] == [6, 5]
    assert rendered == set(keys[:6])
    assert paths == ["graph_cards.png"]
    assert "graph_cards.png" in html
    assert "private:case" not in repr(calls)


def test_worker_failure_is_not_repeated_for_every_block_in_one_report(monkeypatch):
    from rem_card.services.analytics import infographic_renderer

    calls = []
    monkeypatch.setattr(infographic_renderer, "render_infographic", lambda *_args, **_kwargs: calls.append(True))
    renderer = cards.InfographicReportRenderer()
    keys = sorted(cards.SCALAR_GRAPH_KEYS)
    payload = {key: artifact(key) for key in keys}
    paths = []
    html, rendered = cards.render_scalar_overview(keys, payload, [], paths, renderer=renderer)
    outcomes = cards.render_outcome_infographic("g28", artifact("g28", (7, 3), "pie"), [], paths, renderer=renderer)
    assert calls == [True]
    assert not html and not rendered and not paths and outcomes is None


def test_long_ranked_chart_is_paginated_without_losing_or_mutating_rows():
    from rem_card.services.analytics.graphs_service import _artifact_panels

    source = artifact("g61", tuple(range(24)))
    old = deepcopy(source)
    panels = _artifact_panels(source)
    assert [len(panel["series"]) for panel in panels] == [10, 10, 4]
    assert [row["value"] for panel in panels for row in panel["series"]] == list(reversed(range(24)))
    assert source == old


def test_department_histograms_keep_whole_groups_on_readable_pages():
    from rem_card.services.analytics.graphs_service import _artifact_panels

    source = artifact("g63", (), "ward_histograms")
    source["series"] = tuple({"group": f"Отделение {group}", "value": value} for group in range(8) for value in (1, 3, 5))
    panels = _artifact_panels(source)
    assert [len(panel["series"]) for panel in panels] == [18, 6]
    assert sum(len(panel["series"]) for panel in panels) == len(source["series"])
    assert set(row["group"] for row in panels[0]["series"]).isdisjoint(row["group"] for row in panels[1]["series"])


@pytest.mark.parametrize("success", [True, False])
def test_graph_dispatch_keeps_artifacts_and_uses_fallback_when_renderer_fails(monkeypatch, success):
    from rem_card.services.analytics import graphs_service, infographic_renderer
    from rem_card.ui.analytics import graphs_generators_1
    import matplotlib.pyplot as plt

    source = artifact("g43", (12,))
    source["summary"] = "Эпизоды ИВЛ: 12"
    old = deepcopy(source)
    monkeypatch.setattr(infographic_renderer, "render_infographic", lambda *_args, **_kwargs: "graph_cards.png" if success else None)
    fallback = []
    monkeypatch.setattr(graphs_service, "_render_standard_chart", lambda *_args: fallback.append(True))
    monkeypatch.setattr(graphs_generators_1, "save_plot", lambda *_args: "<p>Резервный график</p>")
    try:
        result = graphs_service.build_graphs_html(object(), "2026-01-01", "2026-01-31", ["g43"], authoritative_artifacts={"g43": source})
        assert result.artifacts == {"g43": old}
        assert source == old
        assert old["summary"] in result.html
        assert bool(fallback) is not success
        assert result.image_paths == (["graph_cards.png"] if success else [])
    finally:
        plt.close("all")
