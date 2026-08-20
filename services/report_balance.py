from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, Optional

from rem_card.app.logger import logger
from rem_card.services.balance_calculator import BalanceCalculator


INPUT_KEYS = ("infusion", "preparats", "blood", "plasma", "oral")
OUTPUT_KEYS = (
    ("urine", "urine"),
    ("drain", "drain_output"),
    ("ng", "ng_output"),
    ("stool", "stool"),
    ("other", "other_output"),
)


def _empty_input_hourly() -> Dict[int, Dict[str, float]]:
    return {i: {key: 0.0 for key in INPUT_KEYS} for i in range(24)}


def _hour_index(moment: datetime, start_dt: datetime) -> int:
    return int((moment - start_dt).total_seconds() // 3600)


def _merge_input_hourly(base: Dict[int, Dict[str, float]], extra: Dict[int, Dict[str, float]]):
    for hour, values in (extra or {}).items():
        if hour not in base:
            continue
        for key, value in (values or {}).items():
            if key in base[hour]:
                base[hour][key] += float(value or 0.0)


def _add_oral_events_to_hourly(
    hourly: Dict[int, Dict[str, float]],
    events: Iterable[Any],
    start_dt: datetime,
    current_time: datetime,
):
    for event in events or []:
        event_time = getattr(event, "event_time", None)
        if not isinstance(event_time, datetime):
            continue
        if event_time > current_time:
            continue
        idx = _hour_index(event_time, start_dt)
        if 0 <= idx < 24:
            hourly[idx]["oral"] += float(getattr(event, "amount_ml", 0.0) or 0.0)


def _round_input_hourly(hourly: Dict[int, Dict[str, float]]):
    for values in hourly.values():
        for key in INPUT_KEYS:
            values[key] = round(float(values.get(key, 0.0) or 0.0), 1)


def _sum_input_hourly(hourly: Dict[int, Dict[str, float]]) -> Dict[str, float]:
    totals = {key: 0.0 for key in INPUT_KEYS}
    for values in hourly.values():
        for key in INPUT_KEYS:
            totals[key] += float(values.get(key, 0.0) or 0.0)
    for key in INPUT_KEYS:
        totals[key] = round(totals[key], 1)
    totals["total"] = round(sum(totals[key] for key in INPUT_KEYS), 1)
    return totals


def _as_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value).replace(" ", "T"))
    except Exception:
        return None


def _resolve_terminal_times(remcard_service, admission_id: Optional[int]) -> tuple[Optional[datetime], Optional[datetime]]:
    if not admission_id:
        return None, None

    transfer_time = None
    outcome_time = None
    context = {}
    status_service = getattr(remcard_service, "status_service", None)
    if status_service and hasattr(status_service, "get_admission_outcome_context"):
        try:
            context = status_service.get_admission_outcome_context(int(admission_id)) or {}
        except Exception as exc:
            logger.warning("Failed to load outcome context for print balance: %s", exc)

    if context:
        current_status = str(context.get("current_status") or "").upper()
        outcome = str(context.get("outcome") or "").lower()
        status_start = _as_datetime(context.get("current_status_start_time"))
        transfer_dt = _as_datetime(context.get("transfer_datetime"))
        death_dt = _as_datetime(context.get("death_datetime"))

        if current_status == "DEAD":
            outcome_time = status_start or death_dt or transfer_dt
        elif current_status == "TRANSFERRED":
            transfer_time = status_start or transfer_dt
        elif death_dt and ("умер" in outcome or outcome == "dead"):
            outcome_time = death_dt
        elif transfer_dt:
            transfer_time = transfer_dt
        elif death_dt:
            outcome_time = death_dt

    if transfer_time or outcome_time:
        return transfer_time, outcome_time

    if hasattr(remcard_service, "get_patient"):
        try:
            patient = remcard_service.get_patient(int(admission_id))
            transfer_time = _as_datetime(getattr(patient, "transfer_datetime", None))
        except Exception as exc:
            logger.warning("Failed to load patient terminal time for print balance: %s", exc)

    return transfer_time, outcome_time


def build_print_balance_final(
    *,
    orders,
    fluids,
    remcard_service,
    config: dict,
    admission_id: Optional[int],
    start_dt: datetime,
    current_time: datetime,
    end_dt: datetime,
) -> dict:
    transfer_time, outcome_time = _resolve_terminal_times(remcard_service, admission_id)
    balance_res = BalanceCalculator.calculate(
        orders,
        current_time,
        end_dt,
        transfer_time=transfer_time,
        outcome_time=outcome_time,
    )
    in_hourly = _empty_input_hourly()
    input_cutoff_time = min(current_time + timedelta(hours=1), end_dt)
    order_hourly = BalanceCalculator.calculate_hourly_actual_input(
        orders,
        start_dt,
        input_cutoff_time,
        end_dt,
        transfer_time=transfer_time,
        outcome_time=outcome_time,
    )
    _merge_input_hourly(in_hourly, order_hourly)

    oral_cur = 0
    oral_day = 0
    if config.get("balance", True) and admission_id and hasattr(remcard_service, "get_oral_intake_totals"):
        try:
            oral_totals = remcard_service.get_oral_intake_totals(admission_id, start_dt, current_time=current_time)
            oral_cur = oral_totals.get("current", 0) or 0
            oral_day = oral_totals.get("daily", 0) or 0
        except Exception as exc:
            logger.warning("Failed to load oral intake totals for print balance: %s", exc)

    if config.get("balance", True) and admission_id and hasattr(remcard_service, "get_oral_intake_events"):
        try:
            oral_events = remcard_service.get_oral_intake_events(admission_id, start_dt)
            _add_oral_events_to_hourly(in_hourly, oral_events, start_dt, current_time)
        except Exception as exc:
            logger.warning("Failed to load oral intake events for print balance: %s", exc)

    _round_input_hourly(in_hourly)
    in_cur = _sum_input_hourly(in_hourly)

    balance_res["current"]["oral"] = round(oral_cur, 1)
    balance_res["daily"]["oral"] = round(oral_day, 1)
    balance_res["current"]["total"] = round((balance_res["current"].get("total", 0) or 0) + oral_cur, 1)
    balance_res["daily"]["total"] = round((balance_res["daily"].get("total", 0) or 0) + oral_day, 1)

    f_detail_cur = {"urine": 0, "drain": 0, "ng": 0, "stool": 0, "other": 0}
    f_detail_full = {"urine": 0, "drain": 0, "ng": 0, "stool": 0, "other": 0}
    out_hourly = {}

    report_end = start_dt + timedelta(hours=24)
    for f in fluids or []:
        timestamp = getattr(f, "timestamp", None)
        if not isinstance(timestamp, datetime):
            continue
        if timestamp < start_dt or timestamp >= report_end:
            continue

        idx = _hour_index(timestamp, start_dt)
        if 0 <= idx < 24:
            out_hourly.setdefault(idx, {"urine": 0, "drain": 0, "ng": 0, "stool": 0, "other": 0})
            for key, field_name in OUTPUT_KEYS:
                val = getattr(f, field_name, 0) or 0
                out_hourly[idx][key] += val
                if timestamp <= current_time:
                    f_detail_cur[key] += val
                f_detail_full[key] += val

    return {
        "current": balance_res["current"],
        "full": balance_res["daily"],
        "out_cur": f_detail_cur,
        "out_full": f_detail_full,
        "out_hourly": out_hourly,
        "in_hourly": in_hourly,
        "in_cur": in_cur,
    }
