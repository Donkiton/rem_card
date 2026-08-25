"""
Модуль генерации графиков 46-65:
- Интенсивность (g46-g50)
- Использование коечного фонда (g51-g55)
- Операции и переливания (g56-g60)
- Другие графики (g61-g65)
"""
from datetime import timedelta


try:
    import pandas as pd
    import matplotlib.pyplot as plt
except ImportError:
    pd = None
    plt = None

# Импортируем функцию save_plot из первого файла
from rem_card.ui.analytics.graphs_generators_1 import save_plot
# Импортируем вспомогательную функцию _calc_daily_counts
from rem_card.ui.analytics.graphs_generators_1 import _calc_daily_counts, _patient_count_axis_limit, _bed_days_by_month, _calendar_days_by_month, _overlapping_admissions
from rem_card.services.analytics.constants import STATISTICAL_BED_COUNT
from rem_card.services.analytics.period import parse_analytics_datetime
from rem_card.ui.analytics.chart_renderer import plot_pie_with_legend


def _period_bounds(params):
    return parse_analytics_datetime(params[0]), parse_analytics_datetime(params[1])


def _observed_duration_days(row, start, end):
    admitted = parse_analytics_datetime(row.get("admission_datetime"))
    terminal_values = [item for item in (parse_analytics_datetime(row.get("death_datetime")), parse_analytics_datetime(row.get("transfer_datetime"))) if item]
    terminal = min(terminal_values) if terminal_values else end
    if admitted is None or start is None or end is None:
        return None
    return max(0.0, (min(terminal, end) - max(admitted, start)).total_seconds() / 86400.0)


def _completed_or_observed_duration(row, start, end):
    """Observed [start,end) duration; open cases are censored at period end."""
    return _observed_duration_days(row, start, end)


def generate_g46_g50(selected, conn, params, chart_colors, img_paths, adms, html_content):
    """Интенсивность"""
    census_adms = _overlapping_admissions(conn, params)

    # 46. Средняя интенсивность использования к.ф. по месяцам
    if "g46" in selected:
        inclusive_end = (parse_analytics_datetime(params[1]) - timedelta(days=1)).date().isoformat()
        monthly = _bed_days_by_month(census_adms, params[0], inclusive_end)
        if monthly:
            days = _calendar_days_by_month(params[0], inclusive_end)
            intensity = [value / (STATISTICAL_BED_COUNT * days.get(month, 1)) * 100 for month, value in monthly.items()]
            plt.figure(figsize=(10, 4))
            plt.plot(list(monthly), intensity, marker='o', color=chart_colors[2])
            plt.title("46. Средняя интенсивность использования к.ф. по месяцам (%)")
            plt.ylim(0, 110)
            plt.xticks(rotation=45, ha='right')
            html_content += save_plot("46. Средняя интенсивность использования к.ф. по месяцам", img_paths)

    # 47. Индекс интенсивности по дням недели
    if "g47" in selected:
        start, end = _period_bounds(params); inclusive_end = (end - timedelta(days=1)).date().isoformat()
        counts, dates = _calc_daily_counts(census_adms, start.date().isoformat(), inclusive_end)
        if counts:
            days = {0: 'Вс', 1: 'Пн', 2: 'Вт', 3: 'Ср', 4: 'Чт', 5: 'Пт', 6: 'Сб'}
            grouped = {name: [] for name in days.values()}
            for date_value, count in zip(dates, counts): grouped[days[(date_value.dayofweek + 1) % 7]].append(count)
            avg_bed_days = [sum(items) / len(items) / STATISTICAL_BED_COUNT * 100 if items else 0 for items in grouped.values()]
            plt.figure(figsize=(8, 4))
            plt.bar(range(len(avg_bed_days)), avg_bed_days, color=chart_colors[3])
            plt.xticks(range(len(avg_bed_days)), grouped.keys())
            plt.title("47. Средняя загрузка коек по дням недели (%)")
            html_content += save_plot("47. Средняя интенсивность по дням недели", img_paths)

    # 48. Максимальная одномоментная интенсивность
    # (Этот график похож на g16, но с акцентом на интенсивность)
    if "g48" in selected:
        inclusive_end = (parse_analytics_datetime(params[1]) - timedelta(days=1)).date().isoformat()
        daily_counts, _ = _calc_daily_counts(census_adms, params[0], inclusive_end)
        max_p = max(daily_counts, default=0)

        html_content += (
            f"<div style='text-align: center;'><h3>48. Максимальная одномоментная интенсивность</h3>"
            f"<div style='font-size: 32px; font-weight: bold; color: {chart_colors[1]};'>{max_p} пациентов</div>"
            "<p>Максимальное количество пациентов, одновременно находившихся на лечении.</p></div><br>"
        )

    # 49. Средняя длительность пребывания среди умерших vs выписанных
    if "g49" in selected:
        start, end = _period_bounds(params); grouped = {}
        for row in census_adms:
            outcome = str(row.get("outcome") or "")
            duration = _observed_duration_days(row, start, end)
            if outcome in {"умер", "выписан"} and duration is not None: grouped.setdefault(outcome, []).append(duration)
        if grouped:
            plt.figure(figsize=(8, 4))
            plt.bar(range(len(grouped)), [sum(x) / len(x) for x in grouped.values()], color=[chart_colors[2], chart_colors[1]][:len(grouped)])
            plt.xticks(range(len(grouped)), grouped.keys())
            plt.title("49. Средняя длительность пребывания (Умершие vs Выписанные)")
            plt.ylabel("Дни")
            html_content += save_plot("49. Средняя длительность пребывания (Умершие vs Выписанные)", img_paths)

    # 50. Средняя длительность пребывания по диагнозам (топ-5)
    if "g50" in selected:
        start, end = _period_bounds(params); grouped = {}
        for row in census_adms:
            diagnosis = row.get("diagnosis_code")
            duration = _observed_duration_days(row, start, end)
            if diagnosis and duration is not None: grouped.setdefault(str(diagnosis), []).append(duration)
        if grouped:
            ordered = sorted(((key, sum(value) / len(value)) for key, value in grouped.items()), key=lambda item: item[1], reverse=True)[:5]
            plt.figure(figsize=(10, 5))
            plt.bar(range(len(ordered)), [item[1] for item in ordered], color=chart_colors[5])
            plt.xticks(range(len(ordered)), [item[0] for item in ordered], rotation=45, ha='right')
            plt.title("50. Топ-5 диагнозов по средней длительности лечения (дни)")
            plt.ylabel("Дни")
            html_content += save_plot("50. Топ-5 диагнозов по средней длительности лечения", img_paths)

    return html_content


def generate_g51_g55(selected, conn, params, chart_colors, img_paths, adms, start_date_str, end_date_str, html_content):
    """Использование коечного фонда (другие показатели)"""
    census_adms = _overlapping_admissions(conn, params)

    # 51. Средняя загрузка коек по дням недели
    if "g51" in selected:
        counts, dates = _calc_daily_counts(census_adms, start_date_str, end_date_str)
        if counts:
            days = {0: 'Вс', 1: 'Пн', 2: 'Вт', 3: 'Ср', 4: 'Чт', 5: 'Пт', 6: 'Сб'}
            grouped = {name: [] for name in days.values()}
            for date_value, count in zip(dates, counts): grouped[days[(date_value.dayofweek + 1) % 7]].append(count)
            avg_load = [sum(items) / len(items) / STATISTICAL_BED_COUNT * 100 if items else 0 for items in grouped.values()]
            plt.figure(figsize=(8, 4))
            plt.bar(range(len(avg_load)), avg_load, color=chart_colors[6])
            plt.xticks(range(len(avg_load)), grouped.keys())
            plt.title("51. Средняя загрузка коек по дням недели (%)")
            plt.ylim(0, 110)
            html_content += save_plot("51. Средняя загрузка коек по дням недели", img_paths)

    # 52. Распределение пациентов по койкам (визуализация)
    if "g52" in selected:
        beds = {}
        for row in census_adms:
            bed = row.get('bed_number')
            beds[str(bed if bed is not None else 'Не указана')] = beds.get(str(bed if bed is not None else 'Не указана'), 0) + 1
        if beds:
            plt.figure(figsize=(12, 6))
            plt.bar(range(len(beds)), beds.values(), color=chart_colors[7])
            plt.xticks(range(len(beds)), beds.keys())
            plt.title("52. Количество пациентов по номерам коек")
            plt.xlabel("Номер койки")
            plt.ylabel("Количество пациентов")
            html_content += save_plot("52. Количество пациентов по номерам коек", img_paths)

    # 53. Динамика занятости коек (с детализацией по дням) - похож на g11
    if "g53" in selected:
        daily_counts, date_range = _calc_daily_counts(census_adms, start_date_str, end_date_str)
        plt.figure(figsize=(12, 5))
        pd.Series(daily_counts, index=date_range).plot(kind='bar', color=chart_colors[0], width=1.0, ax=plt.gca())
        plt.title("53. Динамика занятости коек (столбчатый)")
        plt.ylim(0, _patient_count_axis_limit(daily_counts))
        if len(date_range) > 20:
            plt.gca().xaxis.set_major_locator(plt.MaxNLocator(10))
        plt.xticks(rotation=45, ha='right')
        html_content += save_plot("53. Динамика занятости коек", img_paths)

    # 54. Средняя длительность пребывания пациентов, находящихся на койках < X дней
    if "g54" in selected:
        start, end = _period_bounds(params)
        durations = [duration for row in census_adms if (duration := _completed_or_observed_duration(row, start, end)) is not None and duration < 3]
        if durations:
            avg_duration_short = sum(durations) / len(durations)
            html_content += (
                f"<div style='text-align: center;'><h3>54. Средняя длительность пребывания (краткосрочные)</h3>"
                f"<div style='font-size: 32px; font-weight: bold; color: {chart_colors[0]};'>{avg_duration_short:.1f} дней</div>"
                f"<p>Наблюдаемая длительность пересечения с выбранным периодом менее 3 дней; незавершённые случаи цензурируются концом периода.</p></div><br>"
            )
        else:
            html_content += "<div style='text-align:center'><h3>54. Средняя длительность пребывания (краткосрочные)</h3><p>Нет данных для расчета</p></div><br>"

    # 55. Средняя длительность пребывания пациентов, находящихся на койках > Y дней
    if "g55" in selected:
        start, end = _period_bounds(params)
        durations = [duration for row in census_adms if (duration := _completed_or_observed_duration(row, start, end)) is not None and duration >= 14]
        if durations:
            avg_duration_long = sum(durations) / len(durations)
            html_content += (
                f"<div style='text-align: center;'><h3>55. Средняя длительность пребывания (долгосрочные)</h3>"
                f"<div style='font-size: 32px; font-weight: bold; color: {chart_colors[2]};'>{avg_duration_long:.1f} дней</div>"
                f"<p>Наблюдаемая длительность пересечения с выбранным периодом 14 дней и более; незавершённые случаи цензурируются концом периода.</p></div><br>"
            )
        else:
            html_content += "<div style='text-align:center'><h3>55. Средняя длительность пребывания (долгосрочные)</h3><p>Нет данных для расчета</p></div><br>"

    return html_content


def generate_g56_g60(selected, conn, params, chart_colors, img_paths, html_content):
    """Операции и переливания"""

    # 56. Количество операций по месяцам
    if "g56" in selected:
        df = pd.read_sql_query("""
            SELECT strftime('%Y-%m', o.operation_datetime) as month, COUNT(o.id) as count
            FROM operations o JOIN admissions a ON a.id=o.admission_id WHERE o.operation_datetime >= ? AND o.operation_datetime < ?
            GROUP BY month ORDER BY month
        """, conn, params=params)
        if not df.empty:
            df['count'] = pd.to_numeric(df['count'], errors='coerce').fillna(0)
            plt.figure(figsize=(10, 4))
            plt.bar(range(len(df)), df['count'], color=chart_colors[0])
            plt.xticks(range(len(df)), df['month'], rotation=45, ha='right')
            plt.title("56. Количество операций по месяцам")
            html_content += save_plot("56. Количество операций по месяцам", img_paths)

    # 57. Типы проведенных операций (топ-5)
    if "g57" in selected:
        df = pd.read_sql_query("""
            SELECT o.description as operation_type, COUNT(o.id) as count FROM operations o JOIN admissions a ON a.id=o.admission_id
            WHERE o.operation_datetime >= ? AND o.operation_datetime < ? AND o.description IS NOT NULL AND o.description != ''
            GROUP BY o.description ORDER BY count DESC LIMIT 5
        """, conn, params=params)
        if not df.empty:
            df['count'] = pd.to_numeric(df['count'], errors='coerce').fillna(0)
            if df['count'].sum() > 0:
                plt.figure(figsize=(10, 6))
                plot_pie_with_legend(df['count'], df['operation_type'], chart_colors, legend_title="Операция")
                plt.title("57. Топ-5 операций")
                html_content += save_plot("57. Топ-5 операций", img_paths)

    # 58. Количество переливаний по месяцам
    if "g58" in selected:
        df = pd.read_sql_query("""
            SELECT strftime('%Y-%m', t.datetime) as month, COUNT(t.id) as count
            FROM transfusions t JOIN admissions a ON a.id=t.admission_id WHERE t.datetime >= ? AND t.datetime < ?
            GROUP BY month ORDER BY month
        """, conn, params=params)
        if not df.empty:
            df['count'] = pd.to_numeric(df['count'], errors='coerce').fillna(0)
            plt.figure(figsize=(10, 4))
            plt.plot(df['month'], df['count'], marker='s', color=chart_colors[1])
            plt.title("58. Количество переливаний по месяцам")
            plt.xticks(rotation=45, ha='right')
            html_content += save_plot("58. Количество переливаний по месяцам", img_paths)

    # 59. Типы проведенных переливаний (топ-5)
    if "g59" in selected:
        df = pd.read_sql_query("""
            SELECT t.type as transfusion_type, COUNT(t.id) as count FROM transfusions t JOIN admissions a ON a.id=t.admission_id
            WHERE t.datetime >= ? AND t.datetime < ? AND t.type IS NOT NULL AND t.type != ''
            GROUP BY t.type ORDER BY count DESC LIMIT 5
        """, conn, params=params)
        if not df.empty:
            df['count'] = pd.to_numeric(df['count'], errors='coerce').fillna(0)
            if df['count'].sum() > 0:
                plt.figure(figsize=(10, 6))
                plot_pie_with_legend(df['count'], df['transfusion_type'], chart_colors, legend_title="Тип")
                plt.title("59. Топ-5 типов переливаний")
                html_content += save_plot("59. Топ-5 типов переливаний", img_paths)

    # 60. Средняя длительность пребывания пациентов, которым проводились операции
    if "g60" in selected:
        rows = conn.execute("""SELECT o.operation_datetime, a.transfer_datetime, a.death_datetime
            FROM operations o JOIN admissions a ON a.id=o.admission_id
            WHERE DATETIME(o.operation_datetime) >= DATETIME(?) AND DATETIME(o.operation_datetime) < DATETIME(?)""", params).fetchall()
        _start, period_end = _period_bounds(params); durations = []
        for operation_dt, transfer_dt, death_dt in rows:
            started = parse_analytics_datetime(operation_dt)
            terminals = [item for item in (parse_analytics_datetime(transfer_dt), parse_analytics_datetime(death_dt)) if item]
            terminal = min(terminals) if terminals else period_end
            if started and terminal:
                durations.append(max(0.0, (min(terminal, period_end) - started).total_seconds() / 86400.0))
        if durations:
            avg_duration = sum(durations) / len(durations)
            html_content += (
                f"<div style='text-align: center;'><h3>60. Средняя длительность пребывания пациентов после операций</h3>"
                f"<div style='font-size: 32px; font-weight: bold; color: {chart_colors[0]};'>{avg_duration:.1f} дней</div>"
                f"<p>От даты операции до перевода, смерти или конца выбранного half-open периода.</p></div><br>"
            )
        else:
            html_content += "<div style='text-align:center'><h3>60. Средняя длительность пребывания пациентов после операций</h3><p>Нет данных для расчета</p></div><br>"

    return html_content


def generate_g61_g65(selected, conn, params, chart_colors, img_paths, html_content):
    """Другие графики"""
    census_adms = _overlapping_admissions(conn, params)

    # 61. Распределение пациентов по отделениям
    if "g61" in selected:
        df = pd.read_sql_query("""
            SELECT source_department as department, COUNT(id) as count FROM admissions
            WHERE admission_datetime >= ? AND admission_datetime < ? AND source_department IS NOT NULL AND source_department != ''
            GROUP BY source_department ORDER BY count DESC
        """, conn, params=params)
        if not df.empty:
            df['count'] = pd.to_numeric(df['count'], errors='coerce').fillna(0)
            if df['count'].sum() > 0:
                plt.figure(figsize=(10, 6))
                plot_pie_with_legend(df['count'], df['department'], chart_colors, legend_title="Отделение")
                plt.title("61. Распределение пациентов по отделениям")
                html_content += save_plot("61. Распределение пациентов по отделениям", img_paths)

    # 62. Средняя длительность пребывания по отделениям
    if "g62" in selected:
        start, end = _period_bounds(params); groups = {}
        for row in census_adms:
            department = row.get("source_department"); duration = _observed_duration_days(row, start, end)
            if department and duration is not None: groups.setdefault(str(department), []).append(duration)
        if groups:
            ordered = sorted(((key, sum(values) / len(values)) for key, values in groups.items()), key=lambda item: item[1], reverse=True)
            plt.figure(figsize=(10, 5))
            plt.bar(range(len(ordered)), [item[1] for item in ordered], color=chart_colors[3])
            plt.xticks(range(len(ordered)), [item[0] for item in ordered], rotation=45, ha='right')
            plt.title("62. Средняя длительность пребывания по отделениям (дни)")
            plt.ylabel("Дни")
            html_content += save_plot("62. Средняя длительность пребывания по отделениям", img_paths)

    # 63. Распределение длительности пребывания по отделениям (гистограмма)
    if "g63" in selected:
        start, end = _period_bounds(params); groups = {}
        for row in census_adms:
            department = row.get("source_department"); duration = _observed_duration_days(row, start, end)
            if department and duration is not None: groups.setdefault(str(department), []).append(duration)
        if groups:
            departments = list(groups)
            columns = 2
            rows = max(1, (len(departments) + columns - 1) // columns)
            plt.figure(figsize=(12, max(4, rows * 3.2)))
            for i, dept in enumerate(departments):
                subset = groups[dept]
                plt.subplot(rows, columns, i + 1)
                if subset:
                    plt.hist(subset, bins=10, color=chart_colors[4])
                    plt.title(f"{i+1}. {dept}")
                    plt.xlabel("Дни")
                else:
                    plt.title(f"{i+1}. {dept}")
                    plt.text(0.5, 0.5, "Нет данных", ha='center', va='center')


            html_content += save_plot("63. Распределение длительности пребывания по отделениям", img_paths)

    # 65. Распределение пациентов по времени суток поступления
    # (График g65, так как g64 не определен или не используется)
    if "g65" in selected:
        df = pd.read_sql_query(
            "SELECT strftime('%H', admission_datetime) as hour, COUNT(id) as count "
            "FROM admissions WHERE admission_datetime >= ? AND admission_datetime < ? GROUP BY hour ORDER BY hour",
            conn, params=params)
        if not df.empty:
            df['count'] = pd.to_numeric(df['count'], errors='coerce').fillna(0)
            plt.figure(figsize=(12, 4))
            plt.bar(range(len(df)), df['count'], color=chart_colors[5])
            plt.xticks(range(len(df)), df['hour'])
            plt.title("65. Распределение пациентов по времени суток поступления")
            plt.xlabel("Час суток (0-23)")
            plt.ylabel("Количество пациентов")
            html_content += save_plot("65. Распределение пациентов по времени суток поступления", img_paths)

    return html_content
