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

from rem_card.services.analytics.constants import STATISTICAL_BED_COUNT, STATISTICAL_HIGH_LOAD_THRESHOLD
from rem_card.ui.analytics.chart_renderer import plot_pie_with_legend, save_plot as _save_plot


def save_plot(title, img_paths, chart_colors=None):
    return _save_plot(title, img_paths)


def generate_g1_g5(selected, conn, params, chart_colors, img_paths, html_content):
    """Поток пациентов"""

    # 1. Поступления по месяцам
    if "g1" in selected:
        df = pd.read_sql_query(
            "SELECT strftime('%Y-%m', admission_datetime) as month, COUNT(id) as count "
            "FROM admissions WHERE admission_datetime BETWEEN ? AND ? GROUP BY month ORDER BY month",
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
            "FROM admissions WHERE admission_datetime BETWEEN ? AND ? GROUP BY dow",
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
            "FROM admissions WHERE admission_datetime BETWEEN ? AND ? GROUP BY day",
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
            "WHERE admission_datetime BETWEEN ? AND ? GROUP BY source_department",
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
            "WHERE admission_datetime BETWEEN ? AND ? GROUP BY source_department",
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

    return html_content


def generate_g6_g13(selected, conn, params, chart_colors, img_paths, adms, start_date_str, end_date_str, html_content):
    """Использование коечного фонда"""

    # 6. Койко-дни по месяцам
    if "g6" in selected:
        df = pd.read_sql_query("""
            SELECT strftime('%Y-%m', admission_datetime) as month,
            SUM(julianday(COALESCE(death_datetime, transfer_datetime, datetime('now'))) - julianday(admission_datetime)) as bed_days
            FROM admissions WHERE admission_datetime BETWEEN ? AND ?
            GROUP BY month ORDER BY month
        """, conn, params=params)
        if not df.empty:
            df['bed_days'] = pd.to_numeric(df['bed_days'], errors='coerce').fillna(0)
            plt.figure(figsize=(8, 4))
            plt.bar(range(len(df)), df['bed_days'], color=chart_colors[4])
            plt.xticks(range(len(df)), df['month'], rotation=45)
            plt.title("6. Койко-дни по месяцам")
            html_content += save_plot("6. Койко-дни по месяцам", img_paths)

    # 7. Загрузка коек по месяцам (%)
    if "g7" in selected:
        df = pd.read_sql_query("""
            SELECT strftime('%Y-%m', admission_datetime) as month,
            SUM(julianday(COALESCE(death_datetime, transfer_datetime, datetime('now'))) - julianday(admission_datetime)) as bed_days
            FROM admissions WHERE admission_datetime BETWEEN ? AND ?
            GROUP BY month ORDER BY month
        """, conn, params=params)
        if not df.empty:
            df['bed_days'] = pd.to_numeric(df['bed_days'], errors='coerce').fillna(0)
            df['load_pct'] = df['bed_days'] / (STATISTICAL_BED_COUNT * 30.0) * 100
            plt.figure(figsize=(8, 4))
            plt.bar(range(len(df)), df['load_pct'], color=chart_colors[1])
            plt.xticks(range(len(df)), df['month'], rotation=45)
            plt.title("7. Загрузка коек по месяцам (%)")
            plt.ylim(0, 110)
            html_content += save_plot("7. Загрузка коек по месяцам (%)", img_paths)

    # 8. Использование по номерам коек
    if "g8" in selected:
        df = pd.read_sql_query(
            "SELECT bed_number, COUNT(id) as count FROM admissions "
            "WHERE admission_datetime BETWEEN ? AND ? GROUP BY bed_number",
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
            "FROM admissions WHERE admission_datetime BETWEEN ? AND ? GROUP BY month",
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
        df = pd.read_sql_query("""
            SELECT SUM(julianday(COALESCE(death_datetime, transfer_datetime, datetime('now'))) - julianday(admission_datetime)) as total_bed_days
            FROM admissions WHERE admission_datetime BETWEEN ? AND ?
        """, conn, params=params)
        if not df.empty and df['total_bed_days'].iloc[0] is not None:
            # Период в днях
            try:
                start_dt = datetime.strptime(start_date_str.split(' ')[0], "%Y-%m-%d")
                end_dt = datetime.strptime(end_date_str.split(' ')[0], "%Y-%m-%d")
                period_days = max((end_dt - start_dt).days + 1, 1)
            except Exception:
                period_days = 365
            total_bd = float(df['total_bed_days'].iloc[0])
            bed_fund = STATISTICAL_BED_COUNT * period_days
            intensity = total_bd / bed_fund * 100 if bed_fund else 0
            html_content += (
                f"<div style='text-align: center;'><h3>12. Индекс интенсивности использования коечного фонда</h3>"
                f"<div style='font-size: 28px; font-weight: bold; color: {chart_colors[0]};'>{intensity:.1f}%</div>"
                f"<p>Общий койко-день: {total_bd:.1f} из {bed_fund} возможных</p></div><br>"
            )

    # 13. Индекс интенсивности по месяцам
    if "g13" in selected:
        df = pd.read_sql_query("""
            SELECT strftime('%Y-%m', admission_datetime) as month,
            SUM(julianday(COALESCE(death_datetime, transfer_datetime, datetime('now'))) - julianday(admission_datetime)) as bed_days
            FROM admissions WHERE admission_datetime BETWEEN ? AND ?
            GROUP BY month ORDER BY month
        """, conn, params=params)
        if not df.empty:
            df['bed_days'] = pd.to_numeric(df['bed_days'], errors='coerce').fillna(0)
            df['intensity'] = df['bed_days'] / (STATISTICAL_BED_COUNT * 30.0) * 100
            df['intensity'] = pd.to_numeric(df['intensity'], errors='coerce').fillna(0)
            plt.figure(figsize=(8, 4))
            plt.plot(df['month'], df['intensity'], marker='o', color=chart_colors[2])
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
        events = []
        for row in adms:
            if row['admission_datetime']:
                try:
                    events.append((datetime.strptime(row['admission_datetime'].split('.')[0], "%Y-%m-%d %H:%M:%S"), 1))
                except Exception:
                    pass
            end_dt_str = row['death_datetime'] if row['outcome'] == 'умер' else row['transfer_datetime']
            if end_dt_str:
                try:
                    events.append((datetime.strptime(end_dt_str.split('.')[0], "%Y-%m-%d %H:%M:%S"), -1))
                except Exception:
                    pass
        events.sort()
        curr = 0
        max_p = 0
        times = []
        counts_ev = []
        for t, c in events:
            curr += c
            times.append(t)
            counts_ev.append(curr)
            if curr > max_p:
                max_p = curr
        if times:
            plt.figure(figsize=(10, 4))
            plt.step(times, counts_ev, where='post', color=chart_colors[1])
            plt.title(f"16. Динамика числа пациентов (Максимум: {max_p})")
            plt.ylim(0, _patient_count_axis_limit(counts_ev))
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
    """Вспомогательная функция — подсчёт числа пациентов по дням."""
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
                a_start = datetime.strptime(a['admission_datetime'].split('.')[0], "%Y-%m-%d %H:%M:%S")
                a_end_str = a['death_datetime'] if a['outcome'] == 'умер' else a['transfer_datetime']
                if a_end_str:
                    a_end = datetime.strptime(a_end_str.split('.')[0], "%Y-%m-%d %H:%M:%S")
                    if a_start.date() <= d.date() <= a_end.date():
                        count += 1
                else:
                    if a_start.date() <= d.date():
                        count += 1
            except Exception:
                pass
        daily_counts.append(count)
    return daily_counts, date_range


def _patient_count_axis_limit(counts):
    max_count = max([STATISTICAL_BED_COUNT, *[int(c or 0) for c in counts]], default=STATISTICAL_BED_COUNT)
    return max_count + 1
