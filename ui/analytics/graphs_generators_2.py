"""
Модуль генерации графиков 23-45:
- Диагностика (g23-g30)
- Исходы (g31-g35)
- Длительность пребывания (g36-g40)
- Смертность (g41-g45)
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


def generate_g23_g30(selected, conn, params, chart_colors, img_paths, html_content):
    """Диагностика"""

    # 23. Основные диагнозы (топ-5)
    if "g23" in selected:
        df = pd.read_sql_query(
            "SELECT diagnosis_code, COUNT(id) as count FROM admissions "
            "WHERE admission_datetime BETWEEN ? AND ? AND diagnosis_code IS NOT NULL AND diagnosis_code != '' "
            "GROUP BY diagnosis_code ORDER BY count DESC LIMIT 5", conn, params=params)
        if not df.empty:
            df['count'] = pd.to_numeric(df['count'], errors='coerce').fillna(0)
            plt.figure(figsize=(8, 4))
            plt.bar(range(len(df)), df['count'], color=chart_colors[0])
            plt.xticks(range(len(df)), df['diagnosis_code'], rotation=45, ha='right')
            plt.title("23. Топ-5 диагнозов (по коду)")
            html_content += save_plot("23. Топ-5 диагнозов", img_paths)

    # 24. Количество пациентов по МКБ-10 кодам (топ-5)
    if "g24" in selected:
        df = pd.read_sql_query(
            "SELECT diagnosis_code as mkb_code, COUNT(id) as count FROM admissions "
            "WHERE admission_datetime BETWEEN ? AND ? AND diagnosis_code IS NOT NULL AND diagnosis_code != '' "
            "GROUP BY diagnosis_code ORDER BY count DESC LIMIT 5", conn, params=params)
        if not df.empty:
            df['count'] = pd.to_numeric(df['count'], errors='coerce').fillna(0)
            plt.figure(figsize=(8, 4))
            plt.bar(range(len(df)), df['count'], color=chart_colors[1])
            plt.xticks(range(len(df)), df['mkb_code'], rotation=45, ha='right')
            plt.title("24. Топ-5 кодов МКБ-10")
            html_content += save_plot("24. Топ-5 кодов МКБ-10", img_paths)

    # 25. Количество пациентов по МКБ-10 кодам (с группировкой по первой букве)
    if "g25" in selected:
        df = pd.read_sql_query(
            "SELECT SUBSTR(diagnosis_code, 1, 3) as mkb_group, COUNT(id) as count FROM admissions "
            "WHERE admission_datetime BETWEEN ? AND ? AND diagnosis_code IS NOT NULL AND diagnosis_code != '' "
            "GROUP BY mkb_group ORDER BY mkb_group", conn, params=params)
        if not df.empty:
            df['count'] = pd.to_numeric(df['count'], errors='coerce').fillna(0)
            if df['count'].sum() > 0:
                plt.figure(figsize=(10, 6))
                plt.pie(df['count'], labels=df['mkb_group'], autopct='%1.1f%%', colors=chart_colors)
                plt.title("25. Распределение по группам МКБ-10 (первые 3 символа)")
                html_content += save_plot("25. Группы МКБ-10", img_paths)

    # 26. Диагнозы, связанные с COVID-19 (ищем в diagnosis_text, если есть)
    if "g26" in selected:
        df = pd.read_sql_query(
            "SELECT diagnosis_code, COUNT(id) as count FROM admissions "
            "WHERE admission_datetime BETWEEN ? AND ? AND diagnosis_text LIKE '%COVID%' "
            "GROUP BY diagnosis_code ORDER BY count DESC", conn, params=params)
        if not df.empty:
            df['count'] = pd.to_numeric(df['count'], errors='coerce').fillna(0)
            plt.figure(figsize=(8, 4))
            plt.bar(range(len(df)), df['count'], color=chart_colors[2])
            plt.xticks(range(len(df)), df['diagnosis_code'].fillna('N/A'), rotation=45, ha='right')
            plt.title("26. Диагнозы, связанные с COVID-19")
            html_content += save_plot("26. Диагнозы, связанные с COVID-19", img_paths)

    # 27. Распределение диагнозов по полу
    if "g27" in selected:
        df = pd.read_sql_query("""
            SELECT patient_gender, diagnosis_code, COUNT(id) as count
            FROM admissions WHERE admission_datetime BETWEEN ? AND ? AND diagnosis_code IS NOT NULL AND diagnosis_code != ''
            GROUP BY patient_gender, diagnosis_code ORDER BY patient_gender, count DESC
        """, conn, params=params)

        if not df.empty:
            df['count'] = pd.to_numeric(df['count'], errors='coerce').fillna(0)
            # Удобнее представить как топ-3 для каждого пола
            male_df = df[df['patient_gender'] == 'Мужской'].head(3).copy()
            female_df = df[df['patient_gender'] == 'Женский'].head(3).copy()

            fig, axes = plt.subplots(1, 2, figsize=(12, 5))

            if not male_df.empty:
                axes[0].bar(male_df['diagnosis_code'], male_df['count'], color=chart_colors[3])
                axes[0].set_title("27. Диагнозы у мужчин (Топ-3)")
                axes[0].tick_params(axis='x', rotation=45)
            else:
                axes[0].text(0.5, 0.5, "Нет данных", ha='center', va='center')

            if not female_df.empty:
                axes[1].bar(female_df['diagnosis_code'], female_df['count'], color=chart_colors[4])
                axes[1].set_title("27. Диагнозы у женщин (Топ-3)")
                axes[1].tick_params(axis='x', rotation=45)
            else:
                axes[1].text(0.5, 0.5, "Нет данных", ha='center', va='center')

            plt.tight_layout()
            filename = f"graph_{uuid.uuid4().hex}.png"
            path = os.path.join(tempfile.gettempdir(), filename)
            plt.savefig(path, dpi=150, bbox_inches='tight')
            plt.close()
            img_paths.append(path)
            html_content += f"<div style='text-align: center;'><h3>27. Диагнозы по полу (Топ-3)</h3><img src='{path}' width='600'></div><br><br>"

    # 28. Распределение диагнозов по возрасту (группы)
    if "g28" in selected:
        df = pd.read_sql_query("""
            SELECT
                CASE
                    WHEN patient_age < 1 THEN 'до 1г'
                    WHEN patient_age < 18 THEN '1-17'
                    WHEN patient_age <= 44 THEN '18-44'
                    WHEN patient_age <= 60 THEN '45-60'
                    WHEN patient_age <= 75 THEN '60-75'
                    ELSE '75+'
                END as age_group,
                CASE WHEN patient_age_unit = 'месяцы' THEN patient_age / 12.0 ELSE patient_age END as age,
                diagnosis_code
            FROM admissions WHERE admission_datetime BETWEEN ? AND ? AND diagnosis_code IS NOT NULL AND diagnosis_code != ''
        """, conn, params=params)

        if not df.empty:
            grouped_data = df.groupby(['age_group', 'diagnosis_code']).size().reset_index(name='count')
            grouped_data['count'] = pd.to_numeric(grouped_data['count'], errors='coerce').fillna(0)
            # Для простоты, показываем топ-3 диагноза в каждой возрастной группе
            top_diag_per_group = grouped_data.sort_values(by=['age_group', 'count'], ascending=[True, False]) \
                                            .groupby('age_group').head(3)

            # Создаем график
            plt.figure(figsize=(12, 7))
            for i, age_group in enumerate(top_diag_per_group['age_group'].unique()):
                subset = top_diag_per_group[top_diag_per_group['age_group'] == age_group].copy()
                plt.subplot(2, 3, i + 1)
                plt.bar(range(len(subset)), subset['count'], color=chart_colors[5])
                plt.xticks(range(len(subset)), subset['diagnosis_code'], rotation=45, ha='right')
                plt.title(f"28. {age_group}")
                plt.ylabel("Количество")

            plt.tight_layout()
            filename = f"graph_{uuid.uuid4().hex}.png"
            path = os.path.join(tempfile.gettempdir(), filename)
            plt.savefig(path, dpi=150, bbox_inches='tight')
            plt.close()
            img_paths.append(path)
            html_content += f"<div style='text-align: center;'><h3>28. Диагнозы по возрастным группам (Топ-3)</h3><img src='{path}' width='600'></div><br><br>"

    # 29. Частота повторных госпитализаций с тем же диагнозом
    if "g29" in selected:
        # Для простоты, если нет previous_admission_diagnosis, мы пропустим этот сложный SQL
        pass
        #html_content += "<div style='text-align:center'><h3>29. Частота повторных госпитализаций</h3><p>Для этого расчета нужны данные о предыдущих госпитализациях</p></div><br>"

    # 30. Средняя длительность лечения по диагнозам
    if "g30" in selected:
        df = pd.read_sql_query("""
            SELECT diagnosis_code, AVG(julianday(COALESCE(death_datetime, transfer_datetime, datetime('now'))) - julianday(admission_datetime)) as avg_duration
            FROM admissions WHERE admission_datetime BETWEEN ? AND ? AND diagnosis_code IS NOT NULL AND diagnosis_code != ''
            GROUP BY diagnosis_code ORDER BY avg_duration DESC
        """, conn, params=params)
        if not df.empty:
            df['avg_duration'] = pd.to_numeric(df['avg_duration'], errors='coerce').fillna(0)
            df = df.head(5) # Топ-5 по длительности
            plt.figure(figsize=(8, 4))
            plt.bar(range(len(df)), df['avg_duration'], color=chart_colors[7])
            plt.xticks(range(len(df)), df['diagnosis_code'], rotation=45, ha='right')
            plt.title("30. Топ-5 диагнозов по средней длительности лечения (дни)")
            plt.ylabel("Дни")
            html_content += save_plot("30. Средняя длительность лечения по диагнозам", img_paths)

    return html_content


def generate_g31_g35(selected, conn, params, chart_colors, img_paths, html_content):
    """Исходы"""

    # 31. Исход лечения (общая статистика)
    if "g31" in selected:
        df = pd.read_sql_query(
            "SELECT outcome, COUNT(id) as count FROM admissions "
            "WHERE admission_datetime BETWEEN ? AND ? GROUP BY outcome",
            conn, params=params)
        if not df.empty:
            df['count'] = pd.to_numeric(df['count'], errors='coerce').fillna(0)
            if df['count'].sum() > 0:
                plt.figure(figsize=(8, 8))
                plt.pie(df['count'], labels=df['outcome'], autopct='%1.1f%%', colors=chart_colors[:4])
                plt.title("31. Исход лечения пациентов")
                html_content += save_plot("31. Исход лечения пациентов", img_paths)

    # 32. Исход лечения по месяцам
    if "g32" in selected:
        df = pd.read_sql_query("""
            SELECT strftime('%Y-%m', admission_datetime) as month, outcome, COUNT(id) as count
            FROM admissions WHERE admission_datetime BETWEEN ? AND ?
            GROUP BY month, outcome ORDER BY month
        """, conn, params=params)
        if not df.empty:
            df['count'] = pd.to_numeric(df['count'], errors='coerce').fillna(0)
            df['month_dt'] = pd.to_datetime(df['month']) # для сортировки
            df = df.sort_values('month_dt')

            # Заполним пустые исходы 'Неизвестно', если они есть
            df['outcome'] = df['outcome'].fillna('Неизвестно')

            # Создаем сводную таблицу
            pivot_df = df.pivot_table(index='month', columns='outcome', values='count', fill_value=0)

            if not pivot_df.empty:
                plt.figure(figsize=(12, 6))

                # Задаем цвета динамически в зависимости от колонок (исходов)
                available_colors = chart_colors
                plot_colors = available_colors[:len(pivot_df.columns)]

                pivot_df.plot(kind='bar', stacked=True, ax=plt.gca(), color=plot_colors)
                plt.title("32. Исход лечения по месяцам")
                plt.xlabel("Месяц")
                plt.ylabel("Количество пациентов")
                plt.xticks(rotation=45, ha='right')
                plt.legend(title="Исход")
                html_content += save_plot("32. Исход лечения по месяцам", img_paths)

    # 33. Соотношение койко-дней и исходов
    if "g33" in selected:
        df = pd.read_sql_query("""
            SELECT outcome, SUM(MAX(0, julianday(COALESCE(death_datetime, transfer_datetime, datetime('now'))) - julianday(admission_datetime))) as bed_days
            FROM admissions WHERE admission_datetime BETWEEN ? AND ?
            GROUP BY outcome ORDER BY bed_days DESC
        """, conn, params=params)
        if not df.empty:
            df['bed_days'] = pd.to_numeric(df['bed_days'], errors='coerce').fillna(0)
            # Убеждаемся, что нет отрицательных значений для круговой диаграммы
            df = df[df['bed_days'] >= 0]
            if not df.empty and df['bed_days'].sum() > 0:
                plt.figure(figsize=(8, 8))
                plt.pie(df['bed_days'], labels=df['outcome'], autopct='%1.1f%%', colors=chart_colors[:4])
            plt.title("33. Соотношение койко-дней по исходам")
            html_content += save_plot("33. Соотношение койко-дней по исходам", img_paths)

    # 34. Средняя длительность пребывания по исходам
    if "g34" in selected:
        df = pd.read_sql_query("""
            SELECT outcome, AVG(julianday(COALESCE(death_datetime, transfer_datetime, datetime('now'))) - julianday(admission_datetime)) as avg_duration
            FROM admissions WHERE admission_datetime BETWEEN ? AND ?
            GROUP BY outcome ORDER BY avg_duration DESC
        """, conn, params=params)
        if not df.empty:
            df['avg_duration'] = pd.to_numeric(df['avg_duration'], errors='coerce').fillna(0)
            plt.figure(figsize=(8, 4))
            plt.bar(range(len(df)), df['avg_duration'], color=chart_colors[:4])
            plt.xticks(range(len(df)), df['outcome'])
            plt.title("34. Средняя длительность пребывания по исходам (дни)")
            plt.ylabel("Дни")
            html_content += save_plot("34. Средняя длительность пребывания по исходам", img_paths)

    # 35. Соотношение смертей к общему числу пациентов
    if "g35" in selected:
        df = pd.read_sql_query("""
            SELECT COUNT(id) as total_patients, SUM(CASE WHEN outcome = 'умер' THEN 1 ELSE 0 END) as deaths
            FROM admissions WHERE admission_datetime BETWEEN ? AND ?
        """, conn, params=params)
        if not df.empty and df['total_patients'].iloc[0] > 0:
            total_patients = df['total_patients'].iloc[0]
            deaths = df['deaths'].iloc[0]
            death_rate = (deaths / total_patients) * 100 if total_patients > 0 else 0
            html_content += (
                "<div style='text-align: center;'><h3>35. Смертность</h3>"
                f"<div style='font-size: 32px; font-weight: bold; color: {chart_colors[2]};'>{death_rate:.1f}%</div>"
                f"<p>Умерло: {deaths} из {total_patients} пациентов</p></div><br>"
            )
        else:
            html_content += "<div style='text-align:center'><h3>35. Смертность</h3><p>Нет данных для расчета</p></div><br>"

    return html_content


def generate_g36_g40(selected, conn, params, chart_colors, img_paths, adms, html_content):
    """Длительность пребывания"""

    # 36. Распределение длительности пребывания (гистограмма)
    if "g36" in selected:
        durations = []
        for row in adms:
            try:
                start = datetime.strptime(row['admission_datetime'].split('.')[0], "%Y-%m-%d %H:%M:%S")
                end_str = row['death_datetime'] if row['outcome'] == 'умер' else row['transfer_datetime']
                if end_str:
                    end = datetime.strptime(end_str.split('.')[0], "%Y-%m-%d %H:%M:%S")
                    duration = (end - start).days
                    if duration >= 0: # Исключаем некорректные значения
                        durations.append(duration)
                else: # Если пациент еще находится на лечении
                    duration = (datetime.now() - start).days
                    if duration >= 0:
                        durations.append(duration)
            except Exception:
                pass # Пропускаем записи с ошибками

        if durations:
            plt.figure(figsize=(10, 5))
            plt.hist(durations, bins=30, color=chart_colors[4], edgecolor='white')
            plt.title("36. Распределение длительности пребывания пациентов (дни)")
            plt.xlabel("Длительность (дни)")
            plt.ylabel("Количество пациентов")
            html_content += save_plot("36. Распределение длительности пребывания", img_paths)
        else:
            html_content += "<div style='text-align:center'><h3>36. Распределение длительности пребывания</h3><p>Нет данных для расчета</p></div><br>"

    # 37. Средняя длительность пребывания по месяцам
    if "g37" in selected:
        df = pd.read_sql_query("""
            SELECT strftime('%Y-%m', admission_datetime) as month,
            AVG(julianday(COALESCE(death_datetime, transfer_datetime, datetime('now'))) - julianday(admission_datetime)) as avg_duration
            FROM admissions WHERE admission_datetime BETWEEN ? AND ?
            GROUP BY month ORDER BY month
        """, conn, params=params)
        if not df.empty:
            df['avg_duration'] = pd.to_numeric(df['avg_duration'], errors='coerce').fillna(0)
            plt.figure(figsize=(10, 4))
            plt.plot(df['month'], df['avg_duration'], marker='o', color=chart_colors[5])
            plt.title("37. Средняя длительность пребывания по месяцам (дни)")
            plt.xticks(rotation=45, ha='right')
            plt.ylabel("Дни")
            html_content += save_plot("37. Средняя длительность пребывания по месяцам", img_paths)

    # 38. Медианная длительность пребывания
    if "g38" in selected:
        durations = []
        for row in adms:
            try:
                start = datetime.strptime(row['admission_datetime'].split('.')[0], "%Y-%m-%d %H:%M:%S")
                end_str = row['death_datetime'] if row['outcome'] == 'умер' else row['transfer_datetime']
                if end_str:
                    end = datetime.strptime(end_str.split('.')[0], "%Y-%m-%d %H:%M:%S")
                    duration = (end - start).days
                    if duration >= 0:
                        durations.append(duration)
                else:
                    duration = (datetime.now() - start).days
                    if duration >= 0:
                        durations.append(duration)
            except Exception:
                pass
        if durations:
            median_duration = sorted(durations)[len(durations) // 2]
            html_content += (
                f"<div style='text-align: center;'><h3>38. Медианная длительность пребывания</h3>"
                f"<div style='font-size: 32px; font-weight: bold; color: {chart_colors[1]};'>{median_duration} дней</div>"
                f"<p>Используются продолжительности всех пациентов.</p></div><br>"
            )
        else:
             html_content += "<div style='text-align:center'><h3>38. Медианная длительность пребывания</h3><p>Нет данных для расчета</p></div><br>"

    # 39. Длительность пребывания для умерших vs выписанных
    if "g39" in selected:
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
            plt.title("39. Средняя длительность пребывания (Умершие vs Выписанные)")
            plt.ylabel("Дни")
            html_content += save_plot("39. Средняя длительность пребывания (Умершие vs Выписанные)", img_paths)

    # 40. Распределение длительности пребывания для умерших (гистограмма)
    if "g40" in selected:
        durations_d = []
        for row in adms:
            if row['outcome'] == 'умер':
                try:
                    start = datetime.strptime(row['admission_datetime'].split('.')[0], "%Y-%m-%d %H:%M:%S")
                    end_str = row['death_datetime']
                    if end_str:
                        end = datetime.strptime(end_str.split('.')[0], "%Y-%m-%d %H:%M:%S")
                        duration = (end - start).days
                        if duration >= 0:
                            durations_d.append(duration)
                except Exception:
                    pass
        if durations_d:
            plt.figure(figsize=(10, 5))
            plt.hist(durations_d, bins=20, color=chart_colors[2], edgecolor='white')
            plt.title("40. Распределение длительности пребывания умерших (дни)")
            plt.xlabel("Длительность (дни)")
            plt.ylabel("Количество пациентов")
            html_content += save_plot("40. Распределение длительности пребывания умерших", img_paths)
        else:
             html_content += "<div style='text-align:center'><h3>40. Распределение длительности пребывания умерших</h3><p>Нет данных об умерших для расчета</p></div><br>"

    return html_content


def generate_g41_g45(selected, conn, params, chart_colors, img_paths, html_content):
    """Смертность"""

    # 41. Коэффициент летальности (общий)
    if "g41" in selected:
        df = pd.read_sql_query("""
            SELECT SUM(CASE WHEN outcome = 'умер' THEN 1 ELSE 0 END) as deaths, COUNT(id) as total
            FROM admissions WHERE admission_datetime BETWEEN ? AND ?
        """, conn, params=params)
        if not df.empty and df['total'].iloc[0] > 0:
            total_patients = df['total'].iloc[0]
            deaths = df['deaths'].iloc[0]
            lethality_rate = (deaths / total_patients) * 100
            html_content += (
                f"<div style='text-align: center;'><h3>41. Общий коэффициент летальности</h3>"
                f"<div style='font-size: 32px; font-weight: bold; color: {chart_colors[2]};'>{lethality_rate:.1f}%</div>"
                f"<p>Умерло: {deaths} из {total_patients} пациентов</p></div><br>"
            )
        else:
            html_content += "<div style='text-align:center'><h3>41. Общий коэффициент летальности</h3><p>Нет данных для расчета</p></div><br>"

    # 42. Коэффициент летальности по месяцам
    if "g42" in selected:
        df = pd.read_sql_query("""
            SELECT strftime('%Y-%m', admission_datetime) as month,
            SUM(CASE WHEN outcome = 'умер' THEN 1 ELSE 0 END) as deaths, COUNT(id) as total
            FROM admissions WHERE admission_datetime BETWEEN ? AND ?
            GROUP BY month ORDER BY month
        """, conn, params=params)
        if not df.empty:
            df['lethality'] = (df['deaths'] / df['total']) * 100
            plt.figure(figsize=(10, 4))
            plt.plot(df['month'], df['lethality'], marker='s', color=chart_colors[2])
            plt.title("42. Коэффициент летальности по месяцам (%)")
            plt.xlabel("Месяц")
            plt.ylabel("Летальность (%)")
            plt.ylim(0, 60) # Ограничим для лучшей визуализации
            plt.xticks(rotation=45, ha='right')
            html_content += save_plot("42. Коэффициент летальности по месяцам", img_paths)

    # 43. Коэффициент летальности по возрасту (группы)
    if "g43" in selected:
        df = pd.read_sql_query("""
            SELECT
                CASE
                    WHEN patient_age < 1 THEN 'до 1г'
                    WHEN patient_age < 18 THEN '1-17'
                    WHEN patient_age <= 44 THEN '18-44'
                    WHEN patient_age <= 60 THEN '45-60'
                    WHEN patient_age <= 75 THEN '60-75'
                    ELSE '75+'
                END as age_group,
                CASE
                    WHEN patient_age < 1 THEN 1
                    WHEN patient_age < 18 THEN 2
                    WHEN patient_age <= 44 THEN 3
                    WHEN patient_age <= 60 THEN 4
                    WHEN patient_age <= 75 THEN 5
                    ELSE 6
                END as age_order,
                SUM(CASE WHEN outcome = 'умер' THEN 1 ELSE 0 END) as deaths,
                COUNT(id) as total
            FROM admissions WHERE admission_datetime BETWEEN ? AND ?
            GROUP BY age_group ORDER BY age_order
        """, conn, params=params)
        if not df.empty:
            df['lethality'] = (df['deaths'] / df['total']) * 100
            plt.figure(figsize=(10, 5))
            plt.bar(range(len(df)), df['lethality'], color=chart_colors[2])
            plt.xticks(range(len(df)), df['age_group'])
            plt.title("43. Коэффициент летальности по возрастным группам (%)")
            plt.ylabel("Летальность (%)")
            plt.ylim(0, 100)
            html_content += save_plot("43. Коэффициент летальности по возрастным группам", img_paths)

    # 44. Коэффициент летальности по полу
    if "g44" in selected:
        df = pd.read_sql_query("""
            SELECT patient_gender,
            SUM(CASE WHEN outcome = 'умер' THEN 1 ELSE 0 END) as deaths,
            COUNT(id) as total
            FROM admissions WHERE admission_datetime BETWEEN ? AND ?
            GROUP BY patient_gender
        """, conn, params=params)
        if not df.empty:
            df['lethality'] = (df['deaths'] / df['total']) * 100
            if df['lethality'].sum() > 0:
                plt.figure(figsize=(6, 6))
                plt.pie(df['lethality'], labels=[f"{row['patient_gender']}\n{row['lethality']:.1f}%" for index, row in df.iterrows()],
                        autopct='', colors=[chart_colors[0], chart_colors[3]]) # autopct не нужен, т.к. значения уже в label
                plt.title("44. Коэффициент летальности по полу (%)")
                html_content += save_plot("44. Коэффициент летальности по полу", img_paths)

    # 45. Коэффициент летальности по диагнозам (топ-5)
    if "g45" in selected:
        df = pd.read_sql_query("""
            SELECT diagnosis_code,
            SUM(CASE WHEN outcome = 'умер' THEN 1 ELSE 0 END) as deaths,
            COUNT(id) as total
            FROM admissions WHERE admission_datetime BETWEEN ? AND ? AND diagnosis_code IS NOT NULL AND diagnosis_code != ''
            GROUP BY diagnosis_code ORDER BY total DESC LIMIT 5
        """, conn, params=params)
        if not df.empty:
            df['lethality'] = (df['deaths'] / df['total']) * 100
            plt.figure(figsize=(10, 5))
            plt.bar(range(len(df)), df['lethality'], color=chart_colors[2])
            plt.xticks(range(len(df)), df['diagnosis_code'], rotation=45, ha='right')
            plt.title("45. Коэффициент летальности по топ-5 диагнозам (%)")
            plt.ylabel("Летальность (%)")
            plt.ylim(0, 100)
            html_content += save_plot("45. Коэффициент летальности по топ-5 диагнозам", img_paths)

    return html_content
