from __future__ import annotations

import os
from datetime import datetime
from html import escape

from rem_card.services.analytics.graphs_service import _thread_local_manager


def build_statistical_report_html(db_manager, start_dt: str, end_dt: str) -> str:
    manager, cleanup = _thread_local_manager(db_manager)
    conn = manager.get_connection()
    cursor = conn.cursor()
    period_params = (start_dt, end_dt)

    try:
        def _scalar(query: str, params: tuple = period_params):
            cursor.execute(query, params)
            row = cursor.fetchone()
            if not row or row[0] is None:
                return 0
            return row[0]

        total_admissions = int(
            _scalar("SELECT COUNT(*) FROM admissions WHERE admission_datetime BETWEEN ? AND ?", period_params)
        )
        in_department = int(
            _scalar(
                """
                SELECT COUNT(*)
                FROM admissions
                WHERE admission_datetime BETWEEN ? AND ?
                  AND (outcome IS NULL OR TRIM(outcome) = '')
                """,
                period_params,
            )
        )
        transferred = int(
            _scalar(
                """
                SELECT COUNT(*)
                FROM admissions
                WHERE admission_datetime BETWEEN ? AND ?
                  AND lower(TRIM(COALESCE(outcome, ''))) = 'переведен'
                """,
                period_params,
            )
        )
        deaths = int(
            _scalar(
                """
                SELECT COUNT(*)
                FROM admissions
                WHERE admission_datetime BETWEEN ? AND ?
                  AND lower(TRIM(COALESCE(outcome, ''))) = 'умер'
                """,
                period_params,
            )
        )

        bed_days = float(
            _scalar(
                """
                SELECT COALESCE(
                    SUM(
                        MAX(
                            0,
                            julianday(
                                CASE
                                    WHEN death_datetime IS NOT NULL AND death_datetime < ? THEN death_datetime
                                    WHEN transfer_datetime IS NOT NULL AND transfer_datetime < ? THEN transfer_datetime
                                    ELSE ?
                                END
                            ) - julianday(admission_datetime)
                        )
                    ),
                    0
                )
                FROM admissions
                WHERE admission_datetime BETWEEN ? AND ?
                """,
                (end_dt, end_dt, end_dt, start_dt, end_dt),
            )
        )
        avg_stay = float(
            _scalar(
                """
                SELECT COALESCE(
                    AVG(
                        MAX(
                            0,
                            julianday(
                                CASE
                                    WHEN death_datetime IS NOT NULL AND death_datetime < ? THEN death_datetime
                                    WHEN transfer_datetime IS NOT NULL AND transfer_datetime < ? THEN transfer_datetime
                                    ELSE ?
                                END
                            ) - julianday(admission_datetime)
                        )
                    ),
                    0
                )
                FROM admissions
                WHERE admission_datetime BETWEEN ? AND ?
                """,
                (end_dt, end_dt, end_dt, start_dt, end_dt),
            )
        )

        operations_count = int(_scalar("SELECT COUNT(*) FROM operations WHERE operation_datetime BETWEEN ? AND ?", period_params))
        cursor.execute(
            "SELECT COUNT(*), COALESCE(SUM(volume_ml), 0) FROM transfusions WHERE datetime BETWEEN ? AND ?",
            period_params,
        )
        transfusions_row = cursor.fetchone() or (0, 0)
        transfusions_count = int(transfusions_row[0] or 0)
        transfusions_volume_ml = int(transfusions_row[1] or 0)

        cursor.execute(
            """
            SELECT
                COUNT(*) AS ivl_count,
                COALESCE(
                    SUM(
                        MAX(
                            0,
                            (julianday(CASE WHEN end_time IS NOT NULL AND end_time < ? THEN end_time ELSE ? END) - julianday(start_time)) * 24.0
                        )
                    ),
                    0
                ) AS ivl_hours
            FROM ivl_episodes
            WHERE start_time BETWEEN ? AND ?
            """,
            (end_dt, end_dt, start_dt, end_dt),
        )
        ivl_row = cursor.fetchone() or (0, 0)
        ivl_count = int(ivl_row[0] or 0)
        ivl_hours = float(ivl_row[1] or 0.0)

        gender_rows = _group_rows(
            cursor,
            """
            SELECT COALESCE(NULLIF(TRIM(patient_gender), ''), 'Не указано') AS gender, COUNT(*) AS count
            FROM admissions
            WHERE admission_datetime BETWEEN ? AND ?
            GROUP BY COALESCE(NULLIF(TRIM(patient_gender), ''), 'Не указано')
            ORDER BY count DESC, gender
            """,
            period_params,
        )
        source_rows = _group_rows(
            cursor,
            """
            SELECT COALESCE(NULLIF(TRIM(source_department), ''), 'Не указано') AS source, COUNT(*) AS count
            FROM admissions
            WHERE admission_datetime BETWEEN ? AND ?
            GROUP BY COALESCE(NULLIF(TRIM(source_department), ''), 'Не указано')
            ORDER BY count DESC, source
            """,
            period_params,
        )
        diagnosis_rows = _group_rows(
            cursor,
            """
            SELECT
                COALESCE(NULLIF(TRIM(diagnosis_code), ''), '-') AS code,
                COALESCE(NULLIF(TRIM(diagnosis_text), ''), 'Без уточнения') AS diagnosis,
                COUNT(*) AS count
            FROM admissions
            WHERE admission_datetime BETWEEN ? AND ?
            GROUP BY
                COALESCE(NULLIF(TRIM(diagnosis_code), ''), '-'),
                COALESCE(NULLIF(TRIM(diagnosis_text), ''), 'Без уточнения')
            ORDER BY count DESC, code
            LIMIT 12
            """,
            period_params,
        )

        start_date = _parse_date(start_dt)
        end_date = _parse_date(end_dt)
        period_days = max(1, (end_date - start_date).days + 1) if start_date and end_date else 1
        num_beds = int(os.environ.get("REMCARD_NUM_BEDS", "12"))
        bed_capacity_days = num_beds * period_days
        occupancy = (bed_days / bed_capacity_days * 100.0) if bed_capacity_days else 0.0
        mortality = (deaths / total_admissions * 100.0) if total_admissions else 0.0

        start_label = start_date.strftime("%d.%m.%Y") if start_date else start_dt.split(" ")[0]
        end_label = end_date.strftime("%d.%m.%Y") if end_date else end_dt.split(" ")[0]

        return _render_report(
            start_label=start_label,
            end_label=end_label,
            total_admissions=total_admissions,
            in_department=in_department,
            transferred=transferred,
            deaths=deaths,
            mortality=mortality,
            bed_days=bed_days,
            avg_stay=avg_stay,
            occupancy=occupancy,
            operations_count=operations_count,
            transfusions_count=transfusions_count,
            transfusions_volume_ml=transfusions_volume_ml,
            ivl_count=ivl_count,
            ivl_hours=ivl_hours,
            gender_rows=gender_rows,
            source_rows=source_rows,
            diagnosis_rows=diagnosis_rows,
        )
    finally:
        if cleanup:
            cleanup()


def _group_rows(cursor, query: str, params: tuple):
    cursor.execute(query, params)
    return cursor.fetchall()


def _parse_date(value: str):
    try:
        return datetime.strptime(str(value).split(" ")[0], "%Y-%m-%d").date()
    except Exception:
        return None


def _distribution_rows(rows):
    if not rows:
        return "<tr><td colspan='2'>Нет данных</td></tr>"
    return "".join(
        f"<tr><td>{escape(str(r[0]))}</td><td class='num'>{int(r[1] or 0)}</td></tr>"
        for r in rows
    )


def _diagnosis_rows(rows):
    if not rows:
        return "<tr><td colspan='3'>Нет данных</td></tr>"
    return "".join(
        f"<tr><td>{escape(str(r[0]))}</td><td>{escape(str(r[1]))}</td><td class='num'>{int(r[2] or 0)}</td></tr>"
        for r in rows
    )


def _render_report(**data) -> str:
    return f"""
    <html>
    <head>
        <meta charset="UTF-8">
        <style>
            body {{ font-family: 'Arial', sans-serif; color: #2d2d24; margin: 0; padding: 0; }}
            .page {{ padding: 24px 28px; }}
            h1 {{ margin: 0 0 6px 0; font-size: 20px; color: #4a4a3a; }}
            h2 {{ margin: 18px 0 8px 0; font-size: 14px; color: #6b6b52; text-transform: uppercase; }}
            .period {{ margin: 0 0 10px 0; color: #5d5d4a; }}
            table {{ width: 100%; border-collapse: collapse; margin-bottom: 12px; }}
            th, td {{ border: 1px solid #d9d9c8; padding: 6px 8px; text-align: left; font-size: 12px; }}
            th {{ background: #f0f0e0; color: #4a4a3a; font-weight: 700; }}
            .num {{ text-align: right; }}
            .footnote {{ margin-top: 12px; color: #6d6d58; font-size: 11px; }}
        </style>
    </head>
    <body>
        <div class="page">
            <h1>Статистический отчет ОАР №3</h1>
            <p class="period">Период: {data['start_label']} - {data['end_label']}</p>

            <h2>Ключевые показатели</h2>
            <table>
                <tr><th>Показатель</th><th class="num">Значение</th></tr>
                <tr><td>Поступило пациентов</td><td class="num">{data['total_admissions']}</td></tr>
                <tr><td>Находятся в отделении</td><td class="num">{data['in_department']}</td></tr>
                <tr><td>Переведено</td><td class="num">{data['transferred']}</td></tr>
                <tr><td>Умерло</td><td class="num">{data['deaths']}</td></tr>
                <tr><td>Летальность, %</td><td class="num">{data['mortality']:.1f}</td></tr>
                <tr><td>Койко-дни</td><td class="num">{data['bed_days']:.1f}</td></tr>
                <tr><td>Средняя длительность лечения, дней</td><td class="num">{data['avg_stay']:.2f}</td></tr>
                <tr><td>Занятость коечного фонда, %</td><td class="num">{data['occupancy']:.1f}</td></tr>
                <tr><td>Операций выполнено</td><td class="num">{data['operations_count']}</td></tr>
                <tr><td>Трансфузий выполнено</td><td class="num">{data['transfusions_count']}</td></tr>
                <tr><td>Перелито компонентов крови, мл</td><td class="num">{data['transfusions_volume_ml']}</td></tr>
                <tr><td>Эпизодов ИВЛ</td><td class="num">{data['ivl_count']}</td></tr>
                <tr><td>Суммарная длительность ИВЛ, часов</td><td class="num">{data['ivl_hours']:.1f}</td></tr>
            </table>

            <h2>Распределение по полу</h2>
            <table>
                <tr><th>Пол</th><th class="num">Количество</th></tr>
                {_distribution_rows(data['gender_rows'])}
            </table>

            <h2>Источники поступления</h2>
            <table>
                <tr><th>Источник</th><th class="num">Количество</th></tr>
                {_distribution_rows(data['source_rows'])}
            </table>

            <h2>Топ диагнозов</h2>
            <table>
                <tr><th>Код МКБ</th><th>Диагноз</th><th class="num">Случаев</th></tr>
                {_diagnosis_rows(data['diagnosis_rows'])}
            </table>

            <p class="footnote">Отчет сформирован автоматически: {datetime.now().strftime("%d.%m.%Y %H:%M:%S")}</p>
        </div>
    </body>
    </html>
    """
