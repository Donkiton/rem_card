"""
Модуль генерации графиков 46-65:
- Интенсивность (g46-g50)
- Использование коечного фонда (g51-g55)
- Операции и переливания (g56-g60)
- Другие графики (g61-g65)
"""

import os
import tempfile
import uuid
from datetime import datetime

try:
    import pandas as pd
    import matplotlib.pyplot as plt
except ImportError:
    pd = None
    plt = None

# Импортируем функцию save_plot из первого файла
from rem_card.ui.analytics.graphs_generators_1 import save_plot
# Импортируем вспомогательную функцию _calc_daily_counts
from rem_card.ui.analytics.graphs_generators_1 import _calc_daily_counts, _patient_count_axis_limit
from rem_card.services.analytics.constants import STATISTICAL_BED_COUNT


def generate_g46_g50(selected, conn, params, chart_colors, img_paths, adms, html_content):
    """Интенсивность"""

    # 46. Средняя интенсивность использования к.ф. по месяцам
    if "g46" in selected:
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
            plt.figure(figsize=(10, 4))
            plt.plot(df['month'], df['intensity'], marker='o', color=chart_colors[2])
            plt.title("46. Средняя интенсивность использования к.ф. по месяцам (%)")
            plt.ylim(0, 110)
            plt.xticks(rotation=45, ha='right')
            html_content += save_plot("46. Средняя интенсивность использования к.ф. по месяцам", img_paths)

    # 47. Индекс интенсивности по дням недели
    if "g47" in selected:
        df = pd.read_sql_query("""
            SELECT strftime('%w', admission_datetime) as dow,
            SUM(julianday(COALESCE(death_datetime, transfer_datetime, datetime('now'))) - julianday(admission_datetime)) as bed_days
            FROM admissions WHERE admission_datetime BETWEEN ? AND ?
            GROUP BY dow
        """, conn, params=params)
        if not df.empty:
            df['bed_days'] = pd.to_numeric(df['bed_days'], errors='coerce').fillna(0)
            days = {0: 'Вс', 1: 'Пн', 2: 'Вт', 3: 'Ср', 4: 'Чт', 5: 'Пт', 6: 'Сб'}
            df['day'] = df['dow'].astype(int).map(days)
            # Рассчитываем среднее значение для каждого дня недели
            avg_bed_days = df.groupby('day')['bed_days'].mean().reindex(days.values())
            plt.figure(figsize=(8, 4))
            plt.bar(range(len(avg_bed_days)), avg_bed_days, color=chart_colors[3])
            plt.xticks(range(len(avg_bed_days)), avg_bed_days.index)
            plt.title("47. Средняя интенсивность по дням недели (койко-дни)")
            html_content += save_plot("47. Средняя интенсивность по дням недели", img_paths)

    # 48. Максимальная одномоментная интенсивность
    # (Этот график похож на g16, но с акцентом на интенсивность)
    if "g48" in selected:
        # Используем данные из g16, если они доступны, или пересчитываем
        # Для простоты, пересчитаем здесь
        events = []
        for row in adms:
            if row['admission_datetime']:
                try: events.append((datetime.strptime(row['admission_datetime'].split('.')[0], "%Y-%m-%d %H:%M:%S"), 1))
                except Exception: pass
            end_dt_str = row['death_datetime'] if row['outcome'] == 'умер' else row['transfer_datetime']
            if end_dt_str:
                try: events.append((datetime.strptime(end_dt_str.split('.')[0], "%Y-%m-%d %H:%M:%S"), -1))
                except Exception: pass
        events.sort()
        curr, max_p = 0, 0
        for t, c in events:
            curr += c
            if curr > max_p: max_p = curr

        html_content += (
            f"<div style='text-align: center;'><h3>48. Максимальная одномоментная интенсивность</h3>"
            f"<div style='font-size: 32px; font-weight: bold; color: {chart_colors[1]};'>{max_p} пациентов</div>"
            "<p>Максимальное количество пациентов, одновременно находившихся на лечении.</p></div><br>"
        )

    # 49. Средняя длительность пребывания среди умерших vs выписанных
    if "g49" in selected:
        df = pd.read_sql_query("""
            SELECT outcome, AVG(julianday(COALESCE(death_datetime, transfer_datetime, datetime('now'))) - julianday(admission_datetime)) as avg_duration
            FROM admissions WHERE admission_datetime BETWEEN ? AND ? AND outcome IN ('умер', 'выписан')
            GROUP BY outcome ORDER BY avg_duration DESC
        """, conn, params=params)
        if not df.empty:
            df['avg_duration'] = pd.to_numeric(df['avg_duration'], errors='coerce').fillna(0)
            plt.figure(figsize=(8, 4))
            plt.bar(range(len(df)), df['avg_duration'], color=[chart_colors[2], chart_colors[1]])
            plt.xticks(range(len(df)), df['outcome'])
            plt.title("49. Средняя длительность пребывания (Умершие vs Выписанные)")
            plt.ylabel("Дни")
            html_content += save_plot("49. Средняя длительность пребывания (Умершие vs Выписанные)", img_paths)

    # 50. Средняя длительность пребывания по диагнозам (топ-5)
    if "g50" in selected:
        df = pd.read_sql_query("""
            SELECT diagnosis_code, AVG(julianday(COALESCE(death_datetime, transfer_datetime, datetime('now'))) - julianday(admission_datetime)) as avg_duration
            FROM admissions WHERE admission_datetime BETWEEN ? AND ? AND diagnosis_code IS NOT NULL AND diagnosis_code != ''
            GROUP BY diagnosis_code ORDER BY avg_duration DESC LIMIT 5
        """, conn, params=params)
        if not df.empty:
            df['avg_duration'] = pd.to_numeric(df['avg_duration'], errors='coerce').fillna(0)
            plt.figure(figsize=(10, 5))
            plt.bar(range(len(df)), df['avg_duration'], color=chart_colors[5])
            plt.xticks(range(len(df)), df['diagnosis_code'], rotation=45, ha='right')
            plt.title("50. Топ-5 диагнозов по средней длительности лечения (дни)")
            plt.ylabel("Дни")
            html_content += save_plot("50. Топ-5 диагнозов по средней длительности лечения", img_paths)

    return html_content


def generate_g51_g55(selected, conn, params, chart_colors, img_paths, adms, start_date_str, end_date_str, html_content):
    """Использование коечного фонда (другие показатели)"""

    # 51. Средняя загрузка коек по дням недели
    if "g51" in selected:
        df = pd.read_sql_query("""
            SELECT strftime('%w', admission_datetime) as dow,
            SUM(julianday(COALESCE(death_datetime, transfer_datetime, datetime('now'))) - julianday(admission_datetime)) as bed_days
            FROM admissions WHERE admission_datetime BETWEEN ? AND ?
            GROUP BY dow
        """, conn, params=params)
        if not df.empty:
            df['bed_days'] = pd.to_numeric(df['bed_days'], errors='coerce').fillna(0)
            days = {0: 'Вс', 1: 'Пн', 2: 'Вт', 3: 'Ср', 4: 'Чт', 5: 'Пт', 6: 'Сб'}
            df['day'] = df['dow'].astype(int).map(days)
            # Рассчитываем среднюю загрузку для каждого дня недели
            avg_load = df.groupby('day')['bed_days'].mean().reindex(days.values()) / STATISTICAL_BED_COUNT * 100
            plt.figure(figsize=(8, 4))
            plt.bar(range(len(avg_load)), avg_load, color=chart_colors[6])
            plt.xticks(range(len(avg_load)), avg_load.index)
            plt.title("51. Средняя загрузка коек по дням недели (%)")
            plt.ylim(0, 110)
            html_content += save_plot("51. Средняя загрузка коек по дням недели", img_paths)

    # 52. Распределение пациентов по койкам (визуализация)
    if "g52" in selected:
        df = pd.read_sql_query(
            "SELECT bed_number, COUNT(id) as count FROM admissions "
            "WHERE admission_datetime BETWEEN ? AND ? GROUP BY bed_number",
            conn, params=params)
        if not df.empty:
            df['count'] = pd.to_numeric(df['count'], errors='coerce').fillna(0)
            plt.figure(figsize=(12, 6))
            plt.bar(range(len(df)), df['count'], color=chart_colors[7])
            plt.xticks(range(len(df)), df['bed_number'].astype(str))
            plt.title("52. Количество пациентов по номерам коек")
            plt.xlabel("Номер койки")
            plt.ylabel("Количество пациентов")
            html_content += save_plot("52. Количество пациентов по номерам коек", img_paths)

    # 53. Динамика занятости коек (с детализацией по дням) - похож на g11
    if "g53" in selected:
        daily_counts, date_range = _calc_daily_counts(adms, start_date_str, end_date_str)
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
        durations = []
        for row in adms:
            try:
                start = datetime.strptime(row['admission_datetime'].split('.')[0], "%Y-%m-%d %H:%M:%S")
                end_str = row['death_datetime'] if row['outcome'] == 'умер' else row['transfer_datetime']
                if end_str:
                    end = datetime.strptime(end_str.split('.')[0], "%Y-%m-%d %H:%M:%S")
                    duration = (end - start).days
                    if 0 <= duration < 3: # Менее 3 дней
                        durations.append(duration)
            except Exception: pass
        if durations:
            avg_duration_short = sum(durations) / len(durations)
            html_content += (
                f"<div style='text-align: center;'><h3>54. Средняя длительность пребывания (краткосрочные)</h3>"
                f"<div style='font-size: 32px; font-weight: bold; color: {chart_colors[0]};'>{avg_duration_short:.1f} дней</div>"
                f"<p>Пациенты, находившиеся на койке менее 3 дней.</p></div><br>"
            )
        else:
            html_content += "<div style='text-align:center'><h3>54. Средняя длительность пребывания (краткосрочные)</h3><p>Нет данных для расчета</p></div><br>"

    # 55. Средняя длительность пребывания пациентов, находящихся на койках > Y дней
    if "g55" in selected:
        durations = []
        for row in adms:
            try:
                start = datetime.strptime(row['admission_datetime'].split('.')[0], "%Y-%m-%d %H:%M:%S")
                end_str = row['death_datetime'] if row['outcome'] == 'умер' else row['transfer_datetime']
                if end_str:
                    end = datetime.strptime(end_str.split('.')[0], "%Y-%m-%d %H:%M:%S")
                    duration = (end - start).days
                    if duration >= 14: # 14 дней и более
                        durations.append(duration)
            except Exception: pass
        if durations:
            avg_duration_long = sum(durations) / len(durations)
            html_content += (
                f"<div style='text-align: center;'><h3>55. Средняя длительность пребывания (долгосрочные)</h3>"
                f"<div style='font-size: 32px; font-weight: bold; color: {chart_colors[2]};'>{avg_duration_long:.1f} дней</div>"
                f"<p>Пациенты, находившиеся на койке 14 дней и более.</p></div><br>"
            )
        else:
            html_content += "<div style='text-align:center'><h3>55. Средняя длительность пребывания (долгосрочные)</h3><p>Нет данных для расчета</p></div><br>"

    return html_content


def generate_g56_g60(selected, conn, params, chart_colors, img_paths, html_content):
    """Операции и переливания"""

    # 56. Количество операций по месяцам
    if "g56" in selected:
        df = pd.read_sql_query("""
            SELECT strftime('%Y-%m', operation_datetime) as month, COUNT(id) as count
            FROM operations WHERE operation_datetime BETWEEN ? AND ?
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
            SELECT description as operation_type, COUNT(id) as count FROM operations
            WHERE operation_datetime BETWEEN ? AND ? AND description IS NOT NULL AND description != ''
            GROUP BY description ORDER BY count DESC LIMIT 5
        """, conn, params=params)
        if not df.empty:
            df['count'] = pd.to_numeric(df['count'], errors='coerce').fillna(0)
            if df['count'].sum() > 0:
                plt.figure(figsize=(10, 6))
                plt.pie(df['count'], labels=df['operation_type'], autopct='%1.1f%%', colors=chart_colors)
                plt.title("57. Топ-5 операций")
                html_content += save_plot("57. Топ-5 операций", img_paths)

    # 58. Количество переливаний по месяцам
    if "g58" in selected:
        df = pd.read_sql_query("""
            SELECT strftime('%Y-%m', datetime) as month, COUNT(id) as count
            FROM transfusions WHERE datetime BETWEEN ? AND ?
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
            SELECT type as transfusion_type, COUNT(id) as count FROM transfusions
            WHERE datetime BETWEEN ? AND ? AND type IS NOT NULL AND type != ''
            GROUP BY type ORDER BY count DESC LIMIT 5
        """, conn, params=params)
        if not df.empty:
            df['count'] = pd.to_numeric(df['count'], errors='coerce').fillna(0)
            if df['count'].sum() > 0:
                plt.figure(figsize=(10, 6))
                plt.pie(df['count'], labels=df['transfusion_type'], autopct='%1.1f%%', colors=chart_colors)
                plt.title("59. Топ-5 типов переливаний")
                html_content += save_plot("59. Топ-5 типов переливаний", img_paths)

    # 60. Средняя длительность пребывания пациентов, которым проводились операции
    if "g60" in selected:
        df = pd.read_sql_query("""
            SELECT AVG(julianday(COALESCE(t2.death_datetime, t2.transfer_datetime, datetime('now'))) - julianday(t2.admission_datetime)) as avg_duration
            FROM operations t1 INNER JOIN admissions t2 ON t1.admission_id = t2.id
            WHERE t1.operation_datetime BETWEEN ? AND ?
        """, conn, params=params)
        if not df.empty and df['avg_duration'].iloc[0] is not None:
            avg_duration = df['avg_duration'].iloc[0]
            html_content += (
                f"<div style='text-align: center;'><h3>60. Средняя длительность пребывания пациентов после операций</h3>"
                f"<div style='font-size: 32px; font-weight: bold; color: {chart_colors[0]};'>{avg_duration:.1f} дней</div>"
                f"<p>Учитываются только пациенты, которым проводились операции в указанный период.</p></div><br>"
            )
        else:
            html_content += "<div style='text-align:center'><h3>60. Средняя длительность пребывания пациентов после операций</h3><p>Нет данных для расчета</p></div><br>"

    return html_content


def generate_g61_g65(selected, conn, params, chart_colors, img_paths, html_content):
    """Другие графики"""

    # 61. Распределение пациентов по отделениям
    if "g61" in selected:
        df = pd.read_sql_query("""
            SELECT source_department as department, COUNT(id) as count FROM admissions
            WHERE admission_datetime BETWEEN ? AND ? AND source_department IS NOT NULL AND source_department != ''
            GROUP BY source_department ORDER BY count DESC
        """, conn, params=params)
        if not df.empty:
            df['count'] = pd.to_numeric(df['count'], errors='coerce').fillna(0)
            if df['count'].sum() > 0:
                plt.figure(figsize=(10, 6))
                plt.pie(df['count'], labels=df['department'], autopct='%1.1f%%', colors=chart_colors)
                plt.title("61. Распределение пациентов по отделениям")
                html_content += save_plot("61. Распределение пациентов по отделениям", img_paths)

    # 62. Средняя длительность пребывания по отделениям
    if "g62" in selected:
        df = pd.read_sql_query("""
            SELECT source_department as department, AVG(julianday(COALESCE(death_datetime, transfer_datetime, datetime('now'))) - julianday(admission_datetime)) as avg_duration
            FROM admissions WHERE admission_datetime BETWEEN ? AND ? AND source_department IS NOT NULL AND source_department != ''
            GROUP BY source_department ORDER BY avg_duration DESC
        """, conn, params=params)
        if not df.empty:
            df['avg_duration'] = pd.to_numeric(df['avg_duration'], errors='coerce').fillna(0)
            plt.figure(figsize=(10, 5))
            plt.bar(range(len(df)), df['avg_duration'], color=chart_colors[3])
            plt.xticks(range(len(df)), df['department'], rotation=45, ha='right')
            plt.title("62. Средняя длительность пребывания по отделениям (дни)")
            plt.ylabel("Дни")
            html_content += save_plot("62. Средняя длительность пребывания по отделениям", img_paths)

    # 63. Распределение длительности пребывания по отделениям (гистограмма)
    if "g63" in selected:
        df = pd.read_sql_query("""
            SELECT source_department as department, julianday(COALESCE(death_datetime, transfer_datetime, datetime('now'))) - julianday(admission_datetime) as duration
            FROM admissions WHERE admission_datetime BETWEEN ? AND ? AND source_department IS NOT NULL AND source_department != ''
        """, conn, params=params)
        if not df.empty:
            departments = df['department'].unique()
            plt.figure(figsize=(12, 8))
            for i, dept in enumerate(departments):
                subset = df[df['department'] == dept]['duration']
                plt.subplot(3, 2, i + 1)
                if not subset.empty:
                    plt.hist(subset, bins=10, color=chart_colors[4])
                    plt.title(f"{i+1}. {dept}")
                    plt.xlabel("Дни")
                else:
                    plt.title(f"{i+1}. {dept}")
                    plt.text(0.5, 0.5, "Нет данных", ha='center', va='center')


            plt.tight_layout()
            filename = f"graph_{uuid.uuid4().hex}.png"
            path = os.path.join(tempfile.gettempdir(), filename)
            plt.savefig(path, dpi=150, bbox_inches='tight')
            plt.close()
            img_paths.append(path)
            html_content += f"<div style='text-align: center;'><h3>63. Распределение длительности пребывания по отделениям</h3><img src='{path}' width='600'></div><br><br>"

    # 65. Распределение пациентов по времени суток поступления
    # (График g65, так как g64 не определен или не используется)
    if "g65" in selected:
        df = pd.read_sql_query(
            "SELECT strftime('%H', admission_datetime) as hour, COUNT(id) as count "
            "FROM admissions WHERE admission_datetime BETWEEN ? AND ? GROUP BY hour ORDER BY hour",
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
