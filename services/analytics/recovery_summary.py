from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from statistics import median
from typing import Any

from rem_card.services.patient_bed_management.recovery_beds import RECOVERY_BED_NUMBERS


DURATION_BUCKETS = (
    ("до 2 часов", 0.0, 2.0),
    ("2-6 часов", 2.0, 6.0),
    ("6-24 часа", 6.0, 24.0),
    ("более 24 часов", 24.0, None),
)


@dataclass(frozen=True)
class RecoveryBedAnalyticsSummary:
    has_admissions_table: bool = True
    total_admissions: int = 0
    recovery_admissions: int = 0
    unique_recovery_patients: int = 0
    recovery_share_pct: float = 0.0
    total_hours: float = 0.0
    total_days: float = 0.0
    average_hours: float = 0.0
    median_hours: float = 0.0
    min_hours: float = 0.0
    max_hours: float = 0.0
    transferred: int = 0
    deceased: int = 0
    active_or_unknown: int = 0
    duration_buckets: dict[str, int] = field(default_factory=dict)


def build_recovery_bed_summary(conn, start_date_str: str, end_date_str: str) -> RecoveryBedAnalyticsSummary:
    if conn is None or not _table_exists(conn, "admissions"):
        return RecoveryBedAnalyticsSummary(has_admissions_table=False)

    start_dt = _parse_datetime(start_date_str)
    end_dt = _parse_datetime(end_date_str)
    if start_dt is None or end_dt is None:
        return RecoveryBedAnalyticsSummary()
    if end_dt < start_dt:
        start_dt, end_dt = end_dt, start_dt

    rows = _fetch_admission_rows(conn, start_dt, end_dt)
    total_admissions = len(rows)
    recovery_rows = [row for row in rows if _is_recovery_admission(row)]
    recovery_count = len(recovery_rows)

    unique_patients = {
        row.get("patient_id")
        for row in recovery_rows
        if row.get("patient_id") is not None and str(row.get("patient_id")).strip()
    }
    durations_hours: list[float] = []
    duration_buckets = {label: 0 for label, _start, _end in DURATION_BUCKETS}
    transferred = 0
    deceased = 0
    active_or_unknown = 0

    for row in recovery_rows:
        admission_dt = _parse_datetime(row.get("admission_datetime"))
        if admission_dt is not None:
            effective_end = _effective_end_datetime(row, admission_dt, end_dt)
            hours = max(0.0, (effective_end - admission_dt).total_seconds() / 3600.0)
            durations_hours.append(hours)
            _add_duration_bucket(duration_buckets, hours)

        outcome_kind = _outcome_kind(row)
        if outcome_kind == "deceased":
            deceased += 1
        elif outcome_kind == "transferred":
            transferred += 1
        else:
            active_or_unknown += 1

    total_hours = sum(durations_hours)
    return RecoveryBedAnalyticsSummary(
        has_admissions_table=True,
        total_admissions=total_admissions,
        recovery_admissions=recovery_count,
        unique_recovery_patients=len(unique_patients),
        recovery_share_pct=_pct(recovery_count, total_admissions),
        total_hours=total_hours,
        total_days=total_hours / 24.0 if total_hours else 0.0,
        average_hours=(total_hours / len(durations_hours)) if durations_hours else 0.0,
        median_hours=median(durations_hours) if durations_hours else 0.0,
        min_hours=min(durations_hours) if durations_hours else 0.0,
        max_hours=max(durations_hours) if durations_hours else 0.0,
        transferred=transferred,
        deceased=deceased,
        active_or_unknown=active_or_unknown,
        duration_buckets=duration_buckets,
    )


def render_recovery_summary_table(summary: RecoveryBedAnalyticsSummary, *, include_recovery_beds: bool) -> str:
    mode_text = _mode_text(include_recovery_beds)
    if not summary.has_admissions_table:
        return f"""
            <h2>Пациенты через койки пробуждения</h2>
            <p class="recovery-note">{escape(mode_text)}</p>
            <p class="recovery-note">Таблица госпитализаций недоступна для расчета показателей пробуждения.</p>
        """

    rows = [
        (
            "Режим учета",
            "Как переключатель влияет на остальные разделы отчета",
            mode_text,
        ),
        (
            "Проведено через пробуждение",
            "Госпитализации с признаком пробуждения / все госпитализации периода",
            f"{_fmt_int(summary.recovery_admissions)} ({_fmt_pct_value(summary.recovery_share_pct)} от всех)",
        ),
        (
            "Всего госпитализаций периода",
            "Данные до применения фильтра пробуждения",
            _fmt_int(summary.total_admissions),
        ),
        (
            "Уникальные пациенты через пробуждение",
            "Уникальные patient_id среди госпитализаций пробуждения",
            _fmt_int(summary.unique_recovery_patients),
        ),
        (
            "Суммарное время в отделении",
            "От поступления до перевода, смерти или конца выбранного периода",
            f"{_fmt_duration(summary.total_hours)} ({_fmt_num(summary.total_days)} сут.)",
        ),
        (
            "Среднее время в отделении",
            "Среднее по пациентам, проведенным через пробуждение",
            _fmt_duration(summary.average_hours),
        ),
        (
            "Медианное время в отделении",
            "Медиана по пациентам, проведенным через пробуждение",
            _fmt_duration(summary.median_hours),
        ),
        (
            "Минимум / максимум",
            "Диапазон времени в отделении",
            f"{_fmt_duration(summary.min_hours)} / {_fmt_duration(summary.max_hours)}",
        ),
        (
            "Распределение по длительности",
            "Доля внутри группы пробуждения",
            _duration_distribution_lines(summary),
        ),
        (
            "Исходы группы пробуждения",
            "По данным госпитализации на момент формирования отчета",
            _outcome_distribution_text(summary),
        ),
    ]

    rows_html = []
    for name, formula, value in rows:
        rows_html.append(
            f"""
            <tr>
                <td>{escape(name)}</td>
                <td>{escape(formula)}</td>
                <td class="value">{value if '<br/>' in value else escape(str(value))}</td>
            </tr>
            """
        )

    return f"""
        <h2>Пациенты через койки пробуждения</h2>
        <table>
            <tr>
                <th>Показатель</th>
                <th>Расчет</th>
                <th class="value">Значение</th>
            </tr>
            {''.join(rows_html)}
        </table>
    """


def render_recovery_summary_graphs_html(
    summary: RecoveryBedAnalyticsSummary,
    *,
    include_recovery_beds: bool,
    chart_colors: list[str] | tuple[str, ...] | None = None,
) -> str:
    colors = list(chart_colors or [])
    primary = colors[0] if colors else "#2d6cdf"
    accent = colors[1] if len(colors) > 1 else primary
    muted = "#64748b"
    mode_text = _mode_text(include_recovery_beds)

    if not summary.has_admissions_table:
        return (
            "<div style='margin: 12px 0 18px 0;'>"
            "<h3>Пациенты через койки пробуждения</h3>"
            f"<p>{escape(mode_text)}</p>"
            "<p>Таблица госпитализаций недоступна для расчета показателей пробуждения.</p>"
            "</div>"
        )

    metrics = [
        (
            "Проведено через пробуждение",
            f"{_fmt_int(summary.recovery_admissions)} ({_fmt_pct_value(summary.recovery_share_pct)} от всех)",
            primary,
        ),
        (
            "Уникальные пациенты",
            _fmt_int(summary.unique_recovery_patients),
            accent,
        ),
        (
            "Суммарное время в отделении",
            f"{_fmt_duration(summary.total_hours)} ({_fmt_num(summary.total_days)} сут.)",
            primary,
        ),
        (
            "Среднее / медиана",
            f"{_fmt_duration(summary.average_hours)} / {_fmt_duration(summary.median_hours)}",
            accent,
        ),
        (
            "Исходы",
            _outcome_distribution_plain(summary),
            muted,
        ),
    ]

    metric_html = []
    for label, value, color in metrics:
        metric_html.append(
            "<div style='margin: 6px 0;'>"
            f"<div style='font-size: 20px; font-weight: bold; color: {escape(str(color))};'>"
            f"<span style='font-size: 13px; font-weight: normal; color: {muted};'>{escape(label)}: </span>"
            f"{escape(str(value))}</div>"
            "</div>"
        )

    return (
        "<div style='margin: 12px 0 18px 0; padding: 12px; "
        "border: 1px solid #d8dee9; border-radius: 8px; background: #f8fafc;'>"
        "<h3>Пациенты через койки пробуждения</h3>"
        f"<p>{escape(mode_text)}</p>"
        f"{''.join(metric_html)}"
        "</div><br>"
    )


def _fetch_admission_rows(conn, start_dt: datetime, end_dt: datetime) -> list[dict[str, Any]]:
    columns = set(_columns(conn, "admissions"))
    select_parts = [
        _select_expr(columns, "id", "id"),
        _select_expr(columns, "patient_id", "patient_id"),
        _select_expr(columns, "admission_datetime", "admission_datetime"),
        _select_expr(columns, "transfer_datetime", "transfer_datetime"),
        _select_expr(columns, "death_datetime", "death_datetime"),
        _select_expr(columns, "outcome", "outcome"),
        _select_expr(columns, "bed_number", "bed_number"),
        _select_expr(columns, "recovery_bed_stay", "recovery_bed_stay", default="0"),
    ]
    query = f"""
        SELECT {', '.join(select_parts)}
        FROM main.admissions
        WHERE datetime(admission_datetime) BETWEEN datetime(?) AND datetime(?)
    """
    cursor = conn.execute(
        query,
        (
            start_dt.strftime("%Y-%m-%d %H:%M:%S"),
            end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    result_columns = [column[0] for column in cursor.description]
    return [dict(zip(result_columns, row)) for row in cursor.fetchall()]


def _select_expr(columns: set[str], column_name: str, alias: str, *, default: str = "NULL") -> str:
    if column_name in columns:
        return f'"{column_name}" AS "{alias}"'
    return f'{default} AS "{alias}"'


def _is_recovery_admission(row: dict[str, Any]) -> bool:
    if _truthy(row.get("recovery_bed_stay")):
        return True
    try:
        return int(row.get("bed_number") or 0) in RECOVERY_BED_NUMBERS
    except (TypeError, ValueError):
        return False


def _effective_end_datetime(row: dict[str, Any], admission_dt: datetime, period_end: datetime) -> datetime:
    candidates = [period_end]
    transfer_dt = _parse_datetime(row.get("transfer_datetime"))
    death_dt = _parse_datetime(row.get("death_datetime"))
    if transfer_dt is not None:
        candidates.append(transfer_dt)
    if death_dt is not None:
        candidates.append(death_dt)
    effective_end = min(candidates)
    return effective_end if effective_end >= admission_dt else admission_dt


def _outcome_kind(row: dict[str, Any]) -> str:
    outcome = str(row.get("outcome") or "").strip().lower()
    if _parse_datetime(row.get("death_datetime")) is not None or "умер" in outcome:
        return "deceased"
    if _parse_datetime(row.get("transfer_datetime")) is not None or "перев" in outcome:
        return "transferred"
    return "active"


def _add_duration_bucket(buckets: dict[str, int], hours: float) -> None:
    for label, lower, upper in DURATION_BUCKETS:
        if hours >= lower and (upper is None or hours < upper):
            buckets[label] = buckets.get(label, 0) + 1
            return


def _duration_distribution_lines(summary: RecoveryBedAnalyticsSummary) -> str:
    if summary.recovery_admissions <= 0:
        return "н/д"
    lines = []
    for label, _lower, _upper in DURATION_BUCKETS:
        count = int(summary.duration_buckets.get(label, 0))
        lines.append(f"{escape(label)}: {_fmt_int(count)} ({_fmt_pct(count, summary.recovery_admissions)})")
    return "<br/>".join(lines)


def _outcome_distribution_text(summary: RecoveryBedAnalyticsSummary) -> str:
    if summary.recovery_admissions <= 0:
        return "н/д"
    return "<br/>".join(
        [
            f"Переведены: {_fmt_int(summary.transferred)} ({_fmt_pct(summary.transferred, summary.recovery_admissions)})",
            f"Умерли: {_fmt_int(summary.deceased)} ({_fmt_pct(summary.deceased, summary.recovery_admissions)})",
            (
                "Без конечного исхода: "
                f"{_fmt_int(summary.active_or_unknown)} ({_fmt_pct(summary.active_or_unknown, summary.recovery_admissions)})"
            ),
        ]
    )


def _outcome_distribution_plain(summary: RecoveryBedAnalyticsSummary) -> str:
    if summary.recovery_admissions <= 0:
        return "н/д"
    return (
        f"переведены: {_fmt_int(summary.transferred)}, "
        f"умерли: {_fmt_int(summary.deceased)}, "
        f"без исхода: {_fmt_int(summary.active_or_unknown)}"
    )


def _mode_text(include_recovery_beds: bool) -> str:
    if include_recovery_beds:
        return "Пробуждение включено в основные показатели отчета."
    return "Пробуждение исключено из основных показателей отчета; блок ниже показывает исключенную когорту."


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("T", " ")
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y",
    ):
        try:
            parsed = datetime.strptime(text, fmt)
            if fmt in ("%Y-%m-%d", "%d.%m.%Y"):
                return parsed.replace(hour=0, minute=0, second=0, microsecond=0)
            return parsed
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "да"}


def _table_exists(conn, table_name: str) -> bool:
    return bool(
        conn.execute(
            """
            SELECT 1
            FROM main.sqlite_master
            WHERE type = 'table' AND name = ?
            LIMIT 1
            """,
            (table_name,),
        ).fetchone()
    )


def _columns(conn, table_name: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f'PRAGMA main.table_info("{table_name}")').fetchall()]


def _pct(value: int | float, total: int | float) -> float:
    try:
        total_float = float(total)
        if total_float == 0.0:
            return 0.0
        return float(value) / total_float * 100.0
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _fmt_pct(value: int | float, total: int | float) -> str:
    return _fmt_pct_value(_pct(value, total))


def _fmt_pct_value(value: int | float) -> str:
    return f"{float(value):.1f}%"


def _fmt_int(value: int | float) -> str:
    return f"{int(value or 0)}"


def _fmt_num(value: int | float, digits: int = 1) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return f"{0.0:.{digits}f}"


def _fmt_duration(hours: int | float) -> str:
    try:
        total_minutes = int(round(float(hours) * 60.0))
    except (TypeError, ValueError):
        total_minutes = 0
    if total_minutes <= 0:
        return "0 мин"

    days, remainder = divmod(total_minutes, 24 * 60)
    hours_part, minutes = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days} сут.")
    if hours_part or days:
        parts.append(f"{hours_part} ч")
    if minutes or not parts:
        parts.append(f"{minutes} мин")
    return " ".join(parts)
