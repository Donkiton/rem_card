"""Канонический dispatch графиков g23–g45.

Номера являются частью клинического аналитического контракта. Kaplan–Meier и
все доли — только описательные артефакты исходных строк выбранного периода.
"""
from __future__ import annotations

from datetime import datetime

try:
    import pandas as pd
    import matplotlib.pyplot as plt
except ImportError:  # pragma: no cover
    pd = None
    plt = None

from rem_card.services.analytics.period import parse_analytics_datetime
from rem_card.ui.analytics.chart_renderer import plot_pie_with_legend
from rem_card.ui.analytics.graphs_generators_1 import save_plot


CANONICAL_GRAPH_KEYS = frozenset(f"g{number}" for number in range(23, 46))
_ADMISSION_FIELDS = (
    "id", "admission_datetime", "transfer_datetime", "death_datetime", "outcome",
    "patient_age", "patient_age_unit", "patient_gender", "source_department",
    "diagnosis_code", "diagnosis_text",
)


def _note(html, number, title, text="Нет данных для выбранной популяции."):
    return html + f"<div style='text-align:center'><h3>{number}. {title}</h3><p>{text}</p></div><br>"


def _admission_select_list(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(admissions)")}
    return ", ".join(
        name if name in columns else f"NULL AS {name}"
        for name in _ADMISSION_FIELDS
    )


def _admissions(conn, params):
    return pd.read_sql_query(
        f"""SELECT {_admission_select_list(conn)}
            FROM admissions WHERE DATETIME(admission_datetime) >= DATETIME(?)
              AND DATETIME(admission_datetime) < DATETIME(?)""",
        conn,
        params=params,
    )


def _overlapping_admissions(conn, params):
    """Census population for LOS: [admission, terminal) intersects period."""
    return pd.read_sql_query(
        f"""SELECT {_admission_select_list(conn)}
            FROM admissions
            WHERE DATETIME(admission_datetime) < DATETIME(?)
              AND (transfer_datetime IS NULL OR DATETIME(transfer_datetime) > DATETIME(?))
              AND (death_datetime IS NULL OR DATETIME(death_datetime) > DATETIME(?))""",
        conn,
        params=(params[1], params[0], params[0]),
    )


def _death_mask(frame, period_end=None):
    declared = frame["outcome"].fillna("").astype(str).str.casefold().isin({"умер", "death", "deceased"})
    if period_end is None:
        return frame["death_datetime"].notna() | declared
    death_times = frame["death_datetime"].map(parse_analytics_datetime)
    return death_times.map(lambda value: value is not None and value < period_end) & (declared | frame["death_datetime"].notna())


def _duration_days(row, reference=None, period_start=None, period_end=None):
    start = parse_analytics_datetime(row.get("admission_datetime"))
    end = parse_analytics_datetime(row.get("death_datetime")) or parse_analytics_datetime(row.get("transfer_datetime")) or reference
    if start is None or end is None: return None
    start = max(start, period_start) if period_start else start
    end = min(end, period_end) if period_end else end
    return max(0.0, (end - start).total_seconds() / 86400.0)


def _age_group(value):
    try: age = float(value)
    except (TypeError, ValueError): return "Не указан"
    if age < 1: return "до 1 г"
    if age < 18: return "1–17"
    if age <= 44: return "18–44"
    if age <= 60: return "45–60"
    if age <= 75: return "61–75"
    return "76+"


def _bar(number, title, labels, values, color, img_paths, html):
    if not len(labels) or not len(values):
        return _note(html, number, title)
    plt.figure(figsize=(9, 4.5)); plt.bar(range(len(labels)), values, color=color)
    plt.xticks(range(len(labels)), labels, rotation=45, ha="right"); plt.title(f"{number}. {title}"); plt.tight_layout()
    return html + save_plot(f"{number}. {title}", img_paths)


def _percentage_bar(number, title, frame, group_col, color, img_paths, html):
    values = []
    for label, group in frame.groupby(group_col, dropna=False):
        if len(group): values.append((str(label or "Не указан"), _death_mask(group).sum() * 100.0 / len(group)))
    return _bar(number, title, [item[0] for item in values], [item[1] for item in values], color, img_paths, html)


def generate_g23_g30(selected, conn, params, chart_colors, img_paths, html_content):
    """g23–27 diagnostics; g28–30 outcomes and mortality."""
    frame = _admissions(conn, params)
    diagnoses = frame.assign(diagnosis=frame["diagnosis_code"].fillna(frame["diagnosis_text"]).fillna("Не указан").astype(str))
    if "g23" in selected:
        counts = diagnoses[diagnoses.diagnosis != "Не указан"].diagnosis.value_counts().head(10)
        html_content = _bar(23, "Топ-10 диагнозов", list(counts.index), list(counts.values), chart_colors[0], img_paths, html_content)
    if "g24" in selected:
        counts = diagnoses[diagnoses.diagnosis != "Не указан"].assign(mkb=lambda x: x.diagnosis.str[:3]).mkb.value_counts().head(10)
        html_content = _bar(24, "Структура диагнозов по классам МКБ-10", list(counts.index), list(counts.values), chart_colors[1], img_paths, html_content)
    if "g25" in selected:
        counts = diagnoses[diagnoses.diagnosis != "Не указан"].diagnosis.value_counts().head(15)
        html_content = _bar(25, "Частота отдельных диагнозов", list(counts.index), list(counts.values), chart_colors[2], img_paths, html_content)
    if "g26" in selected:
        counts = diagnoses[_death_mask(diagnoses) & (diagnoses.diagnosis != "Не указан")].diagnosis.value_counts().head(10)
        html_content = _bar(26, "Диагнозы у умерших пациентов", list(counts.index), list(counts.values), chart_colors[3], img_paths, html_content)
    if "g27" in selected:
        html_content = _percentage_bar(27, "Летальность по диагнозам", diagnoses[diagnoses.diagnosis != "Не указан"], "diagnosis", chart_colors[4], img_paths, html_content)
    if "g28" in selected:
        outcomes = frame.outcome.fillna("Не указано").replace("", "Не указано").value_counts()
        if outcomes.empty: html_content = _note(html_content, 28, "Распределение исходов лечения")
        else:
            plt.figure(figsize=(8, 6)); plot_pie_with_legend(outcomes.values, outcomes.index, chart_colors, legend_title="Исход")
            plt.title("28. Распределение исходов лечения"); html_content += save_plot("28. Распределение исходов лечения", img_paths)
    if "g29" in selected:
        monthly = frame.assign(month=frame.admission_datetime.astype(str).str[:7]).groupby("month").apply(lambda x: _death_mask(x).sum() * 100.0 / len(x) if len(x) else 0)
        html_content = _bar(29, "Летальность по месяцам", list(monthly.index), list(monthly.values), chart_colors[2], img_paths, html_content)
    if "g30" in selected:
        html_content = _percentage_bar(30, "Летальность по полу", frame.assign(sex=frame.patient_gender.fillna("Не указан")), "sex", chart_colors[0], img_paths, html_content)
    return html_content


def generate_g31_g35(selected, conn, params, chart_colors, img_paths, html_content):
    """g31–32 mortality strata; g33–35 LOS."""
    frame = _admissions(conn, params)
    if "g31" in selected:
        html_content = _percentage_bar(31, "Летальность по возрастным группам", frame.assign(age_group=frame.patient_age.map(_age_group)), "age_group", chart_colors[1], img_paths, html_content)
    if "g32" in selected:
        html_content = _percentage_bar(32, "Летальность по источнику поступления", frame.assign(source=frame.source_department.fillna("Не указан")), "source", chart_colors[2], img_paths, html_content)
    period_start, period_end = parse_analytics_datetime(params[0]), parse_analytics_datetime(params[1])
    reference = period_end or datetime.now()
    duration_frame = _overlapping_admissions(conn, params)
    durations = [value for _, row in duration_frame.iterrows() if (value := _duration_days(row, reference, period_start, period_end)) is not None]
    if "g33" in selected:
        if durations:
            plt.figure(figsize=(9, 4.5)); plt.hist(durations, bins=min(30, max(1, len(durations))), color=chart_colors[3], edgecolor="white")
            plt.title("33. Распределение длительности пребывания"); plt.xlabel("Сутки"); plt.ylabel("Госпитализации")
            html_content += save_plot("33. Распределение длительности пребывания", img_paths)
        else: html_content = _note(html_content, 33, "Распределение длительности пребывания")
    if "g34" in selected:
        values = {}
        for _, row in duration_frame.iterrows():
            duration = _duration_days(row, reference, period_start, period_end)
            if duration is not None: values.setdefault(str(row.get("admission_datetime") or "")[:7], []).append(duration)
        html_content = _bar(34, "Длительность пребывания по месяцам", list(values), [sum(x) / len(x) for x in values.values()], chart_colors[4], img_paths, html_content)
    if "g35" in selected:
        total = len(durations); count = sum(value > 7 for value in durations)
        html_content = _bar(35, "Доля пациентов с пребыванием более 7 суток", ["> 7 суток", "≤ 7 суток"], [count * 100.0 / total if total else 0, (total - count) * 100.0 / total if total else 0], chart_colors[5], img_paths, html_content)
    return html_content


def generate_g36_g40(selected, conn, params, chart_colors, img_paths, adms, html_content):
    """g36 LOS >14d; g37–40 descriptive mortality timing."""
    frame = pd.DataFrame(adms) if adms is not None else _admissions(conn, params)
    if frame.empty: frame = _admissions(conn, params)
    duration_frame = _overlapping_admissions(conn, params)
    period_start, period_end = parse_analytics_datetime(params[0]), parse_analytics_datetime(params[1])
    reference = period_end or datetime.now()
    observations = [(row, value) for _, row in duration_frame.iterrows() if (value := _duration_days(row, reference, period_start, period_end)) is not None]
    if "g36" in selected:
        total = len(observations); count = sum(value > 14 for _, value in observations)
        html_content = _bar(36, "Доля пациентов с пребыванием более 14 суток", ["> 14 суток", "≤ 14 суток"], [count * 100.0 / total if total else 0, (total - count) * 100.0 / total if total else 0], chart_colors[0], img_paths, html_content)
    death_hours = [value * 24.0 for _, row in frame.iterrows() if (value := _duration_days(row, reference, period_start, period_end)) is not None and bool(_death_mask(pd.DataFrame([row]), period_end).iloc[0])]
    if "g37" in selected: html_content = _bar(37, "Время до смерти пациентов", [f"случай {i + 1}" for i in range(len(death_hours))], death_hours, chart_colors[2], img_paths, html_content)
    if "g38" in selected:
        html_content = _bar(38, "Структура летальности по срокам", ["<24 ч", "1–3 суток", "4–7 суток", ">7 суток"], [sum(x < 24 for x in death_hours), sum(24 <= x < 72 for x in death_hours), sum(72 <= x < 168 for x in death_hours), sum(x >= 168 for x in death_hours)], chart_colors[3], img_paths, html_content)
    if "g39" in selected:
        total = len(death_hours); early = sum(x < 24 for x in death_hours)
        html_content = _bar(39, "Доля ранней летальности", ["<24 ч", "≥24 ч"], [early * 100.0 / total if total else 0, (total - early) * 100.0 / total if total else 0], chart_colors[4], img_paths, html_content)
    if "g40" in selected:
        total = len(death_hours); early = sum(x < 24 for x in death_hours)
        html_content = _bar(40, "Индекс тяжести поступающего потока", ["Ранние смерти / все смерти"], [early * 100.0 / total if total else 0], chart_colors[5], img_paths, html_content)
    return html_content


def _table_exists(conn, name):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def generate_g41_g45(selected, conn, params, chart_colors, img_paths, html_content):
    """g41 Kaplan–Meier; g42–45 IVL episodes of selected admissions."""
    frame = _admissions(conn, params); period_start, period_end = parse_analytics_datetime(params[0]), parse_analytics_datetime(params[1]); reference = period_end or datetime.now()
    if "g41" in selected:
        observations = [(value, bool(_death_mask(pd.DataFrame([row]), period_end).iloc[0])) for _, row in frame.iterrows() if (value := _duration_days(row, reference, period_start, period_end)) is not None]
        if observations:
            at_risk = len(observations); survival = 1.0; xs, ys = [0.0], [1.0]
            for point in sorted({item[0] for item in observations}):
                events = sum(item[0] == point and item[1] for item in observations); censored = sum(item[0] == point and not item[1] for item in observations)
                if at_risk and events: survival *= 1.0 - events / at_risk
                xs.extend([point, point]); ys.extend([ys[-1], survival]); at_risk -= events + censored
            plt.figure(figsize=(9, 4.5)); plt.step(xs, ys, where="post", color=chart_colors[0]); plt.ylim(0, 1.05)
            plt.title("41. Кривая выживаемости Kaplan–Meier (описательно)"); plt.xlabel("Сутки от госпитализации"); plt.ylabel("Оценка выживаемости")
            html_content += save_plot("41. Кривая выживаемости Kaplan–Meier", img_paths)
        else: html_content = _note(html_content, 41, "Кривая выживаемости Kaplan–Meier")
    keys = ((42, "Доля пациентов на ИВЛ"), (43, "Число эпизодов ИВЛ"), (44, "Длительность ИВЛ"), (45, "ИВЛ-дни по месяцам"))
    if not any(f"g{number}" in selected for number, _ in keys): return html_content
    if not _table_exists(conn, "ivl_episodes"):
        for number, title in keys:
            if f"g{number}" in selected: html_content = _note(html_content, number, title, "В снимке отсутствует таблица эпизодов ИВЛ.")
        return html_content
    episodes = pd.read_sql_query(
        """SELECT e.admission_id, e.start_time, e.end_time FROM ivl_episodes e
           JOIN admissions a ON a.id=e.admission_id
           WHERE DATETIME(a.admission_datetime) >= DATETIME(?) AND DATETIME(a.admission_datetime) < DATETIME(?)
             AND DATETIME(e.start_time) < DATETIME(?)
             AND (e.end_time IS NULL OR DATETIME(e.end_time) > DATETIME(?))""",
        conn, params=(params[0], params[1], params[1], params[0]),
    )
    admitted = set(episodes.admission_id.dropna())
    if "g42" in selected:
        html_content = _bar(42, "Доля пациентов на ИВЛ", ["ИВЛ", "Без ИВЛ"], [len(admitted) * 100.0 / len(frame) if len(frame) else 0, (len(frame) - len(admitted)) * 100.0 / len(frame) if len(frame) else 0], chart_colors[1], img_paths, html_content)
    if "g43" in selected: html_content = _bar(43, "Число эпизодов ИВЛ", ["Эпизоды ИВЛ"], [len(episodes)], chart_colors[2], img_paths, html_content)
    values, by_month = [], {}
    for _, row in episodes.iterrows():
        start = parse_analytics_datetime(row.get("start_time")); end = parse_analytics_datetime(row.get("end_time")) or reference
        if start and end:
            start, end = max(start, period_start), min(end, period_end)
            if end <= start: continue
            days = (end - start).total_seconds() / 86400.0; values.append(days)
            cursor = start
            while cursor < end:
                next_month = datetime(cursor.year + (cursor.month == 12), 1 if cursor.month == 12 else cursor.month + 1, 1)
                piece_end = min(end, next_month)
                key = cursor.strftime("%Y-%m")
                by_month[key] = by_month.get(key, 0.0) + (piece_end - cursor).total_seconds() / 86400.0
                cursor = piece_end
    if "g44" in selected: html_content = _bar(44, "Длительность ИВЛ", [f"эпизод {i + 1}" for i in range(len(values))], values, chart_colors[3], img_paths, html_content)
    if "g45" in selected: html_content = _bar(45, "ИВЛ-дни по месяцам", list(by_month), list(by_month.values()), chart_colors[4], img_paths, html_content)
    return html_content
