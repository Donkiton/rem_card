"""
Модуль генерации графиков 1-20:
- Поток пациентов (g1-g5)
- Использование коечного фонда (g6-g13)
- Пиковая нагрузка (g14-g18)
- Демографическая структура (g19-g22)
"""

from datetime import datetime

try:
    import pandas as pd
    import matplotlib.pyplot as plt
except ImportError:
    pd = None
    plt = None

from rem_card.services.analytics.constants import (
    RECOVERY_FLOW_DURATION_KEY,
    RECOVERY_FLOW_GRAPH_KEYS,
    RECOVERY_FLOW_MONTHS_KEY,
    RECOVERY_FLOW_OUTCOMES_KEY,
    RECOVERY_FLOW_TABLE_KEY,
    STATISTICAL_BED_COUNT,
    STATISTICAL_HIGH_LOAD_THRESHOLD,
)
from rem_card.services.analytics.period import parse_analytics_datetime
from rem_card.services.analytics.recovery_summary import (
    DURATION_BUCKETS,
    build_recovery_bed_summary,
    fetch_recovery_bed_admission_rows,
    render_recovery_summary_table,
)
from rem_card.ui.analytics.chart_renderer import plot_pie_with_legend, save_plot as _save_plot


def save_plot(title, img_paths, chart_colors=None):
    return _save_plot(title, img_paths)


def generate_g1_g5(
    selected,
    conn,
    params,
    chart_colors,
    img_paths,
    html_content,
    *,
    include_recovery_beds=False,
    period_dates=None,
):
    """Поток пациентов"""

    # 1. Поступления по месяцам
    if "g1" in selected:
        df = pd.read_sql_query(
            "SELECT strftime('%Y-%m', admission_datetime) as month, COUNT(id) as count "
            "FROM admissions WHERE admission_datetime >= ? AND admission_datetime < ? GROUP BY month ORDER BY month",
            conn, params=params)
        if not df.empty:
            df['count'] = pd.to_numeric(df['count'], errors='coerce').fillna(0)
            plt.figure(figsize=(8, 4))
            plt.bar(range(len(df)), df['count'], color=chart_colors[0])
            plt.xticks(range(len(df)), df['month'], rotation=45)
            plt.title("1. Поступления по месяцам")
            html_content += save_plot("1. Поступления по месяцам", img_paths)

    # 2. Поступления по дням недели
    if "g2" in selected:
        df = pd.read_sql_query(
            "SELECT strftime('%w', admission_datetime) as dow, COUNT(id) as count "
            "FROM admissions WHERE admission_datetime >= ? AND admission_datetime < ? GROUP BY dow",
            conn, params=params)
        if not df.empty:
            df['count'] = pd.to_numeric(df['count'], errors='coerce').fillna(0)
            days = {0: 'Вс', 1: 'Пн', 2: 'Вт', 3: 'Ср', 4: 'Чт', 5: 'Пт', 6: 'Сб'}
            df['day'] = df['dow'].astype(int).map(days)
            plt.figure(figsize=(8, 4))
            plt.bar(range(len(df)), df['count'], color=chart_colors[1])
            plt.xticks(range(len(df)), df['day'])
            plt.title("2. Поступления по дням недели")
            html_content += save_plot("2. Поступления по дням недели", img_paths)

    # 3. Динамика по дням
    if "g3" in selected:
        df = pd.read_sql_query(
            "SELECT date(admission_datetime) as day, COUNT(id) as count "
            "FROM admissions WHERE admission_datetime >= ? AND admission_datetime < ? GROUP BY day",
            conn, params=params)
        if not df.empty:
            df['count'] = pd.to_numeric(df['count'], errors='coerce').fillna(0)
            plt.figure(figsize=(10, 4))
            plt.plot(pd.to_datetime(df['day']), df['count'], marker='.', color=chart_colors[2])
            plt.title("3. Динамика поступлений по дням")
            html_content += save_plot("3. Динамика поступлений по дням", img_paths)

    # 4. Источники поступления (тип: приемное отделение/другое)
    if "g4" in selected:
        # Поскольку source_type нет в таблице, используем source_department
        df = pd.read_sql_query(
            "SELECT source_department, COUNT(id) as count FROM admissions "
            "WHERE admission_datetime >= ? AND admission_datetime < ? GROUP BY source_department",
            conn, params=params)
        if not df.empty:
            df['count'] = pd.to_numeric(df['count'], errors='coerce').fillna(0)
            df['source_department'] = df['source_department'].fillna('Не указано')
            plt.figure(figsize=(8, 8))
            plot_pie_with_legend(df['count'], df['source_department'], chart_colors, legend_title="Источник")
            plt.title("4. Источники поступления пациентов")
            html_content += save_plot("4. Источники поступления пациентов", img_paths)

    # 5. Распределение по профильным отделениям-источникам
    if "g5" in selected:
        df = pd.read_sql_query(
            "SELECT source_department, COUNT(id) as count FROM admissions "
            "WHERE admission_datetime >= ? AND admission_datetime < ? GROUP BY source_department",
            conn, params=params)
        if not df.empty:
            df['count'] = pd.to_numeric(df['count'], errors='coerce').fillna(0)
            df['source_department'] = df['source_department'].fillna('Не указано')
            # Отфильтруем пустые
            df = df[df['source_department'] != '']
            if not df.empty:
                plt.figure(figsize=(10, 6))
                plt.bar(range(len(df)), df['count'], color=chart_colors[3])
                plt.xticks(range(len(df)), df['source_department'], rotation=45, ha='right')
                plt.title("5. Поступления из профильных отделений")
                plt.ylabel("Количество пациентов")
                html_content += save_plot("5. Поступления из профильных отделений", img_paths)

    if RECOVERY_FLOW_GRAPH_KEYS.intersection(selected):
        html_content = _append_recovery_flow_items(
            selected,
            conn,
            period_dates or params,
            chart_colors,
            img_paths,
            html_content,
            include_recovery_beds=include_recovery_beds,
        )

    return html_content


def _append_recovery_flow_items(
    selected,
    conn,
    params,
    chart_colors,
    img_paths,
    html_content,
    *,
    include_recovery_beds,
):
    start_date_str, end_date_str = params
    summary = build_recovery_bed_summary(conn, start_date_str, end_date_str)
    recovery_rows = None

    if RECOVERY_FLOW_TABLE_KEY in selected:
        html_content += render_recovery_summary_table(
            summary,
            include_recovery_beds=include_recovery_beds,
        )

    if RECOVERY_FLOW_MONTHS_KEY in selected:
        recovery_rows = _recovery_rows_once(recovery_rows, conn, start_date_str, end_date_str)
        html_content = _append_recovery_months_graph(recovery_rows, chart_colors, img_paths, html_content)

    if RECOVERY_FLOW_DURATION_KEY in selected:
        html_content = _append_recovery_duration_graph(summary, chart_colors, img_paths, html_content)

    if RECOVERY_FLOW_OUTCOMES_KEY in selected:
        html_content = _append_recovery_outcomes_graph(summary, chart_colors, img_paths, html_content)

    return html_content


def _recovery_rows_once(current_rows, conn, start_date_str, end_date_str):
    if current_rows is not None:
        return current_rows
    return fetch_recovery_bed_admission_rows(conn, start_date_str, end_date_str)


def _append_recovery_months_graph(recovery_rows, chart_colors, img_paths, html_content):
    if not recovery_rows:
        return _append_no_data_message(
            html_content,
            "Пробуждение: поступления по месяцам",
            "Нет пациентов через койки пробуждения за выбранный период.",
        )

    df = pd.DataFrame(recovery_rows)
    df["month"] = pd.to_datetime(df["admission_datetime"], errors="coerce").dt.strftime("%Y-%m")
    df = df.dropna(subset=["month"])
    if df.empty:
        return _append_no_data_message(
            html_content,
            "Пробуждение: поступления по месяцам",
            "Нет корректных дат поступления для пациентов через койки пробуждения.",
        )

    grouped = df.groupby("month").size().reset_index(name="count").sort_values("month")
    plt.figure(figsize=(8, 4))
    plt.bar(range(len(grouped)), grouped["count"], color=chart_colors[0])
    plt.xticks(range(len(grouped)), grouped["month"], rotation=45)
    plt.title("Пробуждение: поступления по месяцам")
    plt.ylabel("Количество пациентов")
    return html_content + save_plot("Пробуждение: поступления по месяцам", img_paths)


def _append_recovery_duration_graph(summary, chart_colors, img_paths, html_content):
    if summary.recovery_admissions <= 0:
        return _append_no_data_message(
            html_content,
            "Пробуждение: распределение по длительности",
            "Нет пациентов через койки пробуждения за выбранный период.",
        )

    labels = [label for label, _lower, _upper in DURATION_BUCKETS]
    counts = [int(summary.duration_buckets.get(label, 0)) for label in labels]
    if not any(counts):
        return _append_no_data_message(
            html_content,
            "Пробуждение: распределение по длительности",
            "Нет данных о длительности пребывания на койках пробуждения.",
        )

    plt.figure(figsize=(8, 4))
    plt.bar(range(len(labels)), counts, color=chart_colors[1])
    plt.xticks(range(len(labels)), labels, rotation=20, ha="right")
    plt.title("Пробуждение: распределение по длительности")
    plt.ylabel("Количество пациентов")
    return html_content + save_plot("Пробуждение: распределение по длительности", img_paths)


def _append_recovery_outcomes_graph(summary, chart_colors, img_paths, html_content):
    values = [summary.transferred, summary.deceased, summary.active_or_unknown]
    if summary.recovery_admissions <= 0 or not any(values):
        return _append_no_data_message(
            html_content,
            "Пробуждение: исходы пациентов",
            "Нет пациентов через койки пробуждения за выбранный период.",
        )

    plt.figure(figsize=(8, 6))
    plot_pie_with_legend(
        values,
        ["Переведены", "Умерли", "Без конечного исхода"],
        chart_colors[:3],
        legend_title="Исход",
        value_formatter=lambda value: f"{int(value)}",
    )
    plt.title("Пробуждение: исходы пациентов")
    return html_content + save_plot("Пробуждение: исходы пациентов", img_paths)


def _append_no_data_message(html_content, title, message):
    return f"{html_content}<div style='text-align:center'><h3>{title}</h3><p>{message}</p></div><br>"


def generate_g6_g13(selected, conn, params, chart_colors, img_paths, adms, start_date_str, end_date_str, html_content):
    """Использование коечного фонда"""
    if any(key in selected for key in ("g6", "g7", "g8", "g10", "g11", "g12", "g13")):
        adms = _overlapping_admissions(conn, params)

    # 6. Койко-дни по месяцам
    if "g6" in selected:
        monthly = _bed_days_by_month(adms, start_date_str, end_date_str)
        if monthly:
            plt.figure(figsize=(8, 4))
            plt.bar(range(len(monthly)), monthly.values(), color=chart_colors[4])
            plt.xticks(range(len(monthly)), monthly.keys(), rotation=45)
            plt.title("6. Койко-дни по месяцам")
            html_content += save_plot("6. Койко-дни по месяцам", img_paths)

    # 7. Загрузка коек по месяцам (%)
    if "g7" in selected:
        monthly = _bed_days_by_month(adms, start_date_str, end_date_str)
        if monthly:
            days = _calendar_days_by_month(start_date_str, end_date_str)
            load_pct = [value / (STATISTICAL_BED_COUNT * days.get(month, 1)) * 100 for month, value in monthly.items()]
            plt.figure(figsize=(8, 4))
            plt.bar(range(len(monthly)), load_pct, color=chart_colors[1])
            plt.xticks(range(len(monthly)), monthly.keys(), rotation=45)
            plt.title("7. Загрузка коек по месяцам (%)")
            plt.ylim(0, 110)
            html_content += save_plot("7. Загрузка коек по месяцам (%)", img_paths)

    # 8. Использование по номерам коек
    if "g8" in selected:
        df = pd.read_sql_query(
            "SELECT bed_number, COUNT(id) as count FROM admissions "
            "WHERE admission_datetime >= ? AND admission_datetime < ? GROUP BY bed_number",
            conn, params=params)
        if not df.empty:
            df['count'] = pd.to_numeric(df['count'], errors='coerce').fillna(0)
            plt.figure(figsize=(8, 4))
            plt.bar(range(len(df)), df['count'], color=chart_colors[3])
            plt.xticks(range(len(df)), df['bed_number'].astype(str))
            plt.title("8. Количество пациентов по номерам коек")
            plt.xlabel("Номер койки")
            html_content += save_plot("8. Использование коек по номерам", img_paths)

    # 9. Оборот койки
    if "g9" in selected:
        df = pd.read_sql_query(
            "SELECT strftime('%Y-%m', admission_datetime) as month, COUNT(id) as admissions_count "
            "FROM admissions WHERE admission_datetime >= ? AND admission_datetime < ? GROUP BY month",
            conn, params=params)
        if not df.empty:
            df['admissions_count'] = pd.to_numeric(df['admissions_count'], errors='coerce').fillna(0)
            df['turnover'] = df['admissions_count'] / STATISTICAL_BED_COUNT
            df['turnover'] = pd.to_numeric(df['turnover'], errors='coerce').fillna(0)
            plt.figure(figsize=(8, 4))
            plt.plot(df['month'], df['turnover'], marker='s', color=chart_colors[5])
            plt.title("9. Оборот койки (пац. на 1 койку)")
            plt.xticks(rotation=45)
            html_content += save_plot("9. Оборот койки", img_paths)

    # 10. Среднесуточная занятость коек
    if "g10" in selected:
        daily_counts, date_range = _calc_daily_counts(adms, start_date_str, end_date_str)
        plt.figure(figsize=(10, 4))
        plt.plot(date_range, daily_counts, color=chart_colors[0], linewidth=2)
        plt.fill_between(date_range, daily_counts, alpha=0.3, color=chart_colors[0])
        plt.title("10. Среднесуточная занятость коек (чел.)")
        plt.ylim(0, _patient_count_axis_limit(daily_counts))
        html_content += save_plot("10. Среднесуточная занятость коек", img_paths)

    # 11. Занятость коек по дням (другое отображение — столбчатый)
    if "g11" in selected:
        daily_counts, date_range = _calc_daily_counts(adms, start_date_str, end_date_str)
        plt.figure(figsize=(10, 4))
        # Используем pandas Series для построения, это надежнее в плане типов
        pd.Series(daily_counts, index=date_range).plot(kind='bar', color=chart_colors[4], width=1.0, ax=plt.gca())
        plt.title("11. Занятость коек по дням (столбчатый)")
        plt.ylim(0, _patient_count_axis_limit(daily_counts))
        # Уменьшаем количество тиков, если дней много
        if len(date_range) > 20:
            plt.gca().xaxis.set_major_locator(plt.MaxNLocator(10))
        html_content += save_plot("11. Занятость коек по дням", img_paths)

    # 12. Индекс интенсивности использования к.ф. (общий за период)
    if "g12" in selected:
        total_bd = sum(_calc_daily_counts(adms, start_date_str, end_date_str)[0])
        if total_bd >= 0:
            # Период в днях
            try:
                start_dt = datetime.strptime(start_date_str.split(' ')[0], "%Y-%m-%d")
                end_dt = datetime.strptime(end_date_str.split(' ')[0], "%Y-%m-%d")
                period_days = max((end_dt - start_dt).days + 1, 1)
            except Exception:
                period_days = 365
            bed_fund = STATISTICAL_BED_COUNT * period_days
            intensity = total_bd / bed_fund * 100 if bed_fund else 0
            html_content += (
                f"<div style='text-align: center;'><h3>12. Индекс интенсивности использования коечного фонда</h3>"
                f"<div style='font-size: 28px; font-weight: bold; color: {chart_colors[0]};'>{intensity:.1f}%</div>"
                f"<p>Общий койко-день: {total_bd:.1f} из {bed_fund} возможных</p></div><br>"
            )

    # 13. Индекс интенсивности по месяцам
    if "g13" in selected:
        monthly = _bed_days_by_month(adms, start_date_str, end_date_str)
        if monthly:
            days = _calendar_days_by_month(start_date_str, end_date_str)
            intensity = [value / (STATISTICAL_BED_COUNT * days.get(month, 1)) * 100 for month, value in monthly.items()]
            plt.figure(figsize=(8, 4))
            plt.plot(list(monthly), intensity, marker='o', color=chart_colors[2])
            plt.title("13. Индекс интенсивности использования к.ф. по месяцам (%)")
            plt.ylim(0, 110)
            plt.xticks(rotation=45)
            html_content += save_plot("13. Индекс интенсивности по месяцам", img_paths)

    return html_content


def generate_g14_g18(selected, conn, params, chart_colors, img_paths, adms, start_date_str, end_date_str, html_content):
    """Пиковая нагрузка"""

    needs_calc = any(k in selected for k in ["g14", "g15", "g16", "g17", "g18"])
    if not needs_calc:
        return html_content
    adms = _overlapping_admissions(conn, params)

    daily_counts, date_range = _calc_daily_counts(adms, start_date_str, end_date_str)
    high_load = [1 if c >= STATISTICAL_HIGH_LOAD_THRESHOLD else 0 for c in daily_counts]

    # 14. Периоды повышенной загрузки
    if "g14" in selected:
        plt.figure(figsize=(10, 2))
        pd.Series(high_load, index=date_range).plot(kind='bar', color=chart_colors[2], width=1.0, ax=plt.gca())
        plt.title(f"14. Дни повышенной загрузки (≥{STATISTICAL_HIGH_LOAD_THRESHOLD} пациентов)")
        plt.yticks([0, 1], ["Норма", "ПИК"])
        if len(date_range) > 20:
            plt.gca().xaxis.set_major_locator(plt.MaxNLocator(10))
        html_content += save_plot(f"14. Периоды повышенной загрузки (≥{STATISTICAL_HIGH_LOAD_THRESHOLD})", img_paths)

    # 15. Длительность периодов пиковой загрузки (гистограмма длин непрерывных периодов)
    if "g15" in selected:
        # Считаем длины подряд идущих пиковых дней
        periods = []
        count = 0
        for h in high_load:
            if h == 1:
                count += 1
            else:
                if count > 0:
                    periods.append(count)
                count = 0
        if count > 0:
            periods.append(count)

        if periods:
            plt.figure(figsize=(8, 4))
            bins = max(len(set(periods)), 5)
            plt.hist(periods, bins=bins, color=chart_colors[5], edgecolor='white')
            plt.title("15. Длительность периодов пиковой загрузки (сут.)")
            plt.xlabel("Длительность периода (дней)")
            plt.ylabel("Количество периодов")
            html_content += save_plot("15. Длительность периодов пиковой загрузки", img_paths)
        else:
            html_content += "<div style='text-align:center'><h3>15. Длительность периодов пиковой загрузки</h3><p>Пиковых периодов не обнаружено</p></div><br>"

    # 16. Макс. число пациентов одновременно
    if "g16" in selected:
        if len(date_range):
            max_p = max(daily_counts, default=0)
            plt.figure(figsize=(10, 4))
            plt.step(date_range, daily_counts, where='post', color=chart_colors[1])
            plt.title(f"16. Динамика числа пациентов (Максимум: {max_p})")
            plt.ylim(0, _patient_count_axis_limit(daily_counts))
            html_content += save_plot("16. Максимальное число пациентов одновременно", img_paths)

    # 17. Доля времени повышенной загрузки
    if "g17" in selected:
        high_load_days = sum(high_load)
        total_days = len(high_load)
        perc = (high_load_days / total_days * 100) if total_days > 0 else 0
        normal_days = total_days - high_load_days
        plt.figure(figsize=(6, 6))
        plot_pie_with_legend(
            [high_load_days, normal_days],
            ["Пиковая нагрузка", "Нормальная нагрузка"],
            [chart_colors[2], chart_colors[1]],
            legend_title="Периоды",
            value_formatter=lambda value: f"{int(value)} дн.",
        )
        plt.title(f"17. Доля времени повышенной загрузки (≥{STATISTICAL_HIGH_LOAD_THRESHOLD} пац.): {perc:.1f}%")
        html_content += save_plot("17. Доля времени повышенной загрузки", img_paths)

    # 18. Динамика одновременно находящихся
    if "g18" in selected:
        plt.figure(figsize=(10, 4))
        plt.plot(date_range, daily_counts, color=chart_colors[3], linewidth=1.5)
        plt.axhline(y=STATISTICAL_HIGH_LOAD_THRESHOLD, color='red', linestyle='--', alpha=0.7, label=f'Порог {STATISTICAL_HIGH_LOAD_THRESHOLD} пац.')
        plt.fill_between(date_range, daily_counts, STATISTICAL_HIGH_LOAD_THRESHOLD,
                         where=[c >= STATISTICAL_HIGH_LOAD_THRESHOLD for c in daily_counts],
                         alpha=0.3, color='red', label='Пиковые дни')
        plt.legend()
        plt.title("18. Динамика одновременно находящихся пациентов")
        plt.ylim(0, _patient_count_axis_limit(daily_counts))
        html_content += save_plot("18. Динамика одновременно находящихся", img_paths)

    return html_content


def generate_g19_g22(selected, conn, params, chart_colors, img_paths, adms, html_content):
    """Демографическая структура — в правильном порядке"""

    # 19. Возрастная структура пациентов
    if "g19" in selected:
        ages = []
        for row in adms:
            if row['patient_age'] is not None:
                val = row['patient_age'] / 12.0 if row['patient_age_unit'] == 'месяцы' else float(row['patient_age'])
                ages.append(val)
        if ages:
            plt.figure(figsize=(8, 4))
            plt.hist(ages, bins=15, color=chart_colors[2], edgecolor='white')
            plt.title("19. Возрастная структура пациентов")
            plt.xlabel("Возраст (лет)")
            html_content += save_plot("19. Возрастная структура пациентов", img_paths)

    # 20. Распределение по полу
    if "g20" in selected:
        m = sum(1 for r in adms if r['patient_gender'] == 'Мужской')
        f = sum(1 for r in adms if r['patient_gender'] == 'Женский')
        if (m + f) > 0:
            plt.figure(figsize=(6, 6))
            plot_pie_with_legend([m, f], ["Мужчины", "Женщины"], [chart_colors[0], chart_colors[3]], legend_title="Пол")
            plt.title("20. Распределение пациентов по полу")
            html_content += save_plot("20. Распределение пациентов по полу", img_paths)

    # 21. Возрастная структура умерших
    if "g21" in selected:
        ages_d = []
        for row in adms:
            if row['outcome'] == 'умер' and row['patient_age'] is not None:
                val = row['patient_age'] / 12.0 if row['patient_age_unit'] == 'месяцы' else float(row['patient_age'])
                ages_d.append(val)
        if ages_d:
            plt.figure(figsize=(8, 4))
            plt.hist(ages_d, bins=10, color=chart_colors[2], edgecolor='white')
            plt.title("21. Возрастная структура умерших")
            plt.xlabel("Возраст (лет)")
            html_content += save_plot("21. Возрастная структура умерших", img_paths)
        else:
            html_content += "<div style='text-align:center'><h3>21. Возрастная структура умерших</h3><p>Нет данных об умерших</p></div><br>"

    # 22. Возрастные группы
    if "g22" in selected:
        age_groups = {'до 1г': 0, '1-17': 0, '18-44': 0, '45-60': 0, '60-75': 0, '75+': 0}
        for row in adms:
            if row['patient_age'] is not None:
                a = row['patient_age'] / 12.0 if row['patient_age_unit'] == 'месяцы' else float(row['patient_age'])
                if a < 1:
                    age_groups['до 1г'] += 1
                elif a < 18:
                    age_groups['1-17'] += 1
                elif a <= 44:
                    age_groups['18-44'] += 1
                elif a <= 60:
                    age_groups['45-60'] += 1
                elif a <= 75:
                    age_groups['60-75'] += 1
                else:
                    age_groups['75+'] += 1
        plt.figure(figsize=(8, 4))
        plt.bar(range(len(age_groups)), age_groups.values(), color=chart_colors[6])
        plt.xticks(range(len(age_groups)), age_groups.keys())
        plt.title("22. Распределение по возрастным группам")
        html_content += save_plot("22. Возрастные группы", img_paths)

    return html_content


def _calc_daily_counts(adms, start_date_str, end_date_str):
    """Daily census with exact half-open intersections.

    `adms` may include carry-in rows.  A transfer/death exactly at midnight of
    a day is excluded from that day, which prevents a false census at the
    selected period boundary.
    """
    import pandas as pd
    from datetime import datetime

    start_date = datetime.strptime(start_date_str.split(' ')[0], "%Y-%m-%d")
    end_date = datetime.strptime(end_date_str.split(' ')[0], "%Y-%m-%d")
    date_range = pd.date_range(start_date, end_date)
    daily_counts = []
    for d in date_range:
        count = 0
        for a in adms:
            try:
                a_start = parse_analytics_datetime(a['admission_datetime'])
                if a_start is None:
                    continue
                transfer = parse_analytics_datetime(a.get('transfer_datetime'))
                death = parse_analytics_datetime(a.get('death_datetime'))
                terminals = [item for item in (transfer, death) if item is not None]
                a_end = min(terminals) if terminals else None
                day_start = d.to_pydatetime()
                day_end = day_start + pd.Timedelta(days=1)
                if a_start < day_end and (a_end is None or a_end > day_start):
                    count += 1
            except Exception:
                pass
        daily_counts.append(count)
    return daily_counts, date_range


def _bed_days_by_month(adms, start_date_str, end_date_str):
    counts, dates = _calc_daily_counts(adms, start_date_str, end_date_str)
    result = {}
    for day, count in zip(dates, counts):
        key = day.strftime('%Y-%m')
        result[key] = result.get(key, 0) + count
    return result


def _calendar_days_by_month(start_date_str, end_date_str):
    import pandas as pd
    start, end = parse_analytics_datetime(start_date_str), parse_analytics_datetime(end_date_str)
    if start is None or end is None:
        return {}
    result = {}
    for day in pd.date_range(start.date(), end.date()):
        key = day.strftime('%Y-%m'); result[key] = result.get(key, 0) + 1
    return result


def _overlapping_admissions(conn, params):
    """Read the census population, unlike flow graphs which use starts only."""
    cursor = conn.execute(
        """SELECT id, patient_id, admission_datetime, transfer_datetime, death_datetime,
                  outcome, patient_age, patient_age_unit, patient_gender, source_department,
                  diagnosis_code, diagnosis_text, bed_number
           FROM admissions WHERE DATETIME(admission_datetime) < DATETIME(?)
             AND ((transfer_datetime IS NULL AND death_datetime IS NULL) OR DATETIME(
                    CASE WHEN transfer_datetime IS NULL THEN death_datetime
                         WHEN death_datetime IS NULL THEN transfer_datetime
                         WHEN DATETIME(transfer_datetime) <= DATETIME(death_datetime) THEN transfer_datetime
                         ELSE death_datetime END) > DATETIME(?))""",
        (params[1], params[0]),
    )
    return [dict(zip([column[0] for column in cursor.description], row)) for row in cursor.fetchall()]


def _patient_count_axis_limit(counts):
    max_count = max([STATISTICAL_BED_COUNT, *[int(c or 0) for c in counts]], default=STATISTICAL_BED_COUNT)
    return max_count + 1
