from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from rem_card.services.operblock_icon_defaults import type_icon_key
import re
from rem_card.ui.operblock_view.operblock_helpers import (
    OXYGEN_ICON_FILE,
    _counted_infusion_volume_ml,
    _decimal_from_ru_number,
    _format_infusion_declared_volume,
    _format_infusion_executed_volume,
    _format_infusion_rate,
    _format_infusion_volume_ml,
    _format_oxygen_liters,
    _gas_dose_events,
    _gas_dose_text,
    _infusion_declared_volume_ml,
    _infusion_display_drug_name,
    _infusion_has_rate,
    _is_gas_infusion,
    _is_oxygen_infusion,
    _is_volume_only_infusion,
    _minute_floor_dt,
    _order_dose_text_with_route,
    _order_route_code,
    _oxygen_consumed_liters,
    _parse_datetime_value,
    _safe_int,
    _split_order_drug_and_dose,
    _stable_ui_hash,
    _summarize_order_total,
    _text_is_oxygen,
)


class OperBlockMedicationGroupsMixin:
    @staticmethod
    def _medication_entry_sort_key(entry: dict) -> tuple:
        return (
            _minute_floor_dt(_parse_datetime_value(entry.get("time"))) or datetime.min,
            int(entry.get("sort_id") or 0),
            str(entry.get("detail") or "").casefold(),
        )

    def _sorted_medication_entries(self, entries: list[dict], sort_mode: str) -> list[dict]:
        return sorted(entries, key=self._medication_entry_sort_key, reverse=sort_mode != "time_asc")

    @staticmethod
    def _medication_group_time_sort_key(group: dict) -> tuple:
        latest_dt = group.get("latest_dt")
        if not isinstance(latest_dt, datetime):
            latest_dt = datetime.min
        return (
            latest_dt,
            int(group.get("first_id") or 0),
            str(group.get("drug_name") or "").casefold(),
        )

    def _sort_medication_order_groups(self, groups: list[dict], sort_mode: str) -> None:
        if sort_mode == "time_asc":
            groups.sort(key=self._medication_group_time_sort_key)
            return
        if sort_mode == "drug":
            groups.sort(key=lambda group: str(group.get("drug_name") or "").casefold())
            return
        groups.sort(key=self._medication_group_time_sort_key, reverse=True)

    @staticmethod
    def _medication_entry_is_active(entry: dict) -> bool:
        row = entry.get("row") if isinstance(entry.get("row"), dict) else {}
        interval = entry.get("interval") if isinstance(entry.get("interval"), dict) else {}
        status = str((interval or row or {}).get("status") or "")
        return status == "active"

    @staticmethod
    def _medication_entry_matches_orders_filter(entry: dict, filter_kind: str, hide_deleted: bool) -> bool:
        kind = str(entry.get("kind") or "")
        row = entry.get("row") if isinstance(entry.get("row"), dict) else {}
        interval = entry.get("interval") if isinstance(entry.get("interval"), dict) else {}
        status = str((row or interval or {}).get("status") or "")
        if hide_deleted and status in {"deleted", "cancelled"}:
            return False
        if filter_kind == "all":
            return True
        if filter_kind == "bolus":
            return kind == "order" and str(entry.get("order_kind") or "bolus") != "gas"
        if filter_kind == "gas":
            return (kind == "order" and str(entry.get("order_kind") or "") == "gas") or (
                kind == "infusion" and _is_gas_infusion(interval)
            )
        if filter_kind == "continuous_infusion":
            return kind == "infusion" and _infusion_has_rate(interval)
        if filter_kind == "timed_infusion":
            return kind == "infusion" and not _infusion_has_rate(interval) and not _is_gas_infusion(interval)
        if filter_kind == "active":
            return kind == "infusion" and status == "active"
        return True

    @staticmethod
    def _timeline_event_numeric_id(value) -> int:
        match = re.search(r"(\d+)$", str(value or ""))
        return int(match.group(1)) if match else 0

    def _timeline_order_events(self, rows) -> list[dict]:
        events: list[dict] = []
        for raw_row in rows or []:
            row = dict(raw_row or {})
            order_dt = _minute_floor_dt(_parse_datetime_value(row.get("datetime")))
            if order_dt is None:
                continue
            raw_drug_name, dose = _split_order_drug_and_dose(str(row.get("text") or ""))
            drug_name = self._order_display_drug_name(row, raw_drug_name)
            row["drug_name"] = drug_name
            row["raw_drug_name"] = raw_drug_name
            row["dose_text"] = dose
            row["route"] = _order_route_code(row)
            order_kind = self._order_preset_kind(row)
            row["order_kind"] = order_kind
            events.append(
                {
                    "kind": "order",
                    "order_kind": order_kind,
                    "time": order_dt,
                    "drug": drug_name,
                    "detail": _order_dose_text_with_route(dose, row, short=False)
                    or str(row.get("text") or "").strip(),
                    "badge": "Газ" if order_kind == "gas" else "Болюс",
                    "row": row,
                    "sort_id": _safe_int(row.get("id")) or 0,
                }
            )
        return events

    def _timeline_infusion_change_events(
        self,
        interval: dict,
        *,
        start_dt: datetime,
        drug_name: str,
        is_oxygen: bool,
    ) -> list[dict]:
        events: list[dict] = []
        if _is_gas_infusion(interval):
            for index, change in enumerate(_gas_dose_events(interval)):
                change_dt = _minute_floor_dt(_parse_datetime_value((change or {}).get("event_time")))
                if change_dt is None or (index == 0 and change_dt == start_dt):
                    continue
                change_dose = str((change or {}).get("dose_text") or "").strip()
                events.append(
                    {
                        "kind": "infusion",
                        "role": "change",
                        "time": change_dt,
                        "drug": drug_name,
                        "detail": (
                            f"поток {change_dose}"
                            if is_oxygen and change_dose
                            else f"доза {change_dose}"
                            if change_dose
                            else "изменение потока"
                            if is_oxygen
                            else "изменение дозы"
                        ),
                        "badge": "Изм. поток" if is_oxygen else "Изм. доза",
                        "interval": interval,
                        "sort_id": self._timeline_event_numeric_id((change or {}).get("event_id")),
                    }
                )
            return events

        for index, change in enumerate(list(interval.get("rate_history") or [])):
            change_dt = _minute_floor_dt(_parse_datetime_value((change or {}).get("event_time")))
            if change_dt is None or (index == 0 and change_dt == start_dt):
                continue
            change_rate = _format_infusion_rate((change or {}).get("rate_value"), (change or {}).get("rate_unit"))
            events.append(
                {
                    "kind": "infusion",
                    "role": "change",
                    "time": change_dt,
                    "drug": drug_name,
                    "detail": f"скорость {change_rate}" if change_rate else "изменение скорости",
                    "badge": "Изм. скорость",
                    "interval": interval,
                    "sort_id": self._timeline_event_numeric_id((change or {}).get("event_id")),
                }
            )
        return events

    def _timeline_events_for_infusion(self, raw_interval) -> list[dict]:
        interval = dict(raw_interval or {})
        status = str(interval.get("status") or "")
        if status not in {"active", "stopped"}:
            return []
        start_dt = _minute_floor_dt(_parse_datetime_value(interval.get("start_time")))
        if start_dt is None:
            return []

        is_gas = _is_gas_infusion(interval)
        drug_name = _infusion_display_drug_name(interval, "Дозатор")
        rate = (
            ""
            if is_gas
            else _format_infusion_rate(interval.get("current_rate_value"), interval.get("current_rate_unit"))
        )
        declared_volume = _format_infusion_declared_volume(interval)
        gas_dose = _gas_dose_text(interval) if is_gas else ""
        is_oxygen = _is_oxygen_infusion(interval)
        is_rate_infusion = _infusion_has_rate(interval)
        badge = "Кислород" if is_oxygen else "Газ" if gas_dose else "Дозатор" if is_rate_infusion else "Капельница"
        detail = (
            f"старт {rate}"
            if rate
            else f"старт поток {gas_dose}"
            if is_oxygen and gas_dose
            else f"старт {gas_dose}"
            if gas_dose
            else declared_volume or "старт"
        )
        start_event_id, _revision = self._infusion_identity(interval)
        events = [
            {
                "kind": "infusion",
                "role": "start",
                "time": start_dt,
                "drug": drug_name,
                "detail": detail,
                "badge": badge,
                "interval": interval,
                "sort_id": start_event_id or 0,
            }
        ]
        events.extend(
            self._timeline_infusion_change_events(
                interval,
                start_dt=start_dt,
                drug_name=drug_name,
                is_oxygen=is_oxygen,
            )
        )
        end_dt = _minute_floor_dt(_parse_datetime_value(interval.get("end_time")))
        if end_dt is not None and status == "stopped":
            events.append(
                {
                    "kind": "infusion",
                    "role": "stop",
                    "time": end_dt,
                    "drug": drug_name,
                    "detail": "стоп",
                    "badge": "Стоп",
                    "interval": interval,
                    "sort_id": start_event_id or 0,
                }
            )
        return events

    def _build_timeline_events(self, rows) -> list[dict]:
        events = self._timeline_order_events(rows)
        snapshot = getattr(self, "_current_timeline_snapshot", None) or {}
        for interval in snapshot.get("infusion_intervals") or []:
            events.extend(self._timeline_events_for_infusion(interval))
        events.sort(
            key=lambda item: (
                _minute_floor_dt(_parse_datetime_value(item.get("time"))) or datetime.min,
                int(item.get("sort_id") or 0),
                str(item.get("badge") or ""),
            ),
            reverse=True,
        )
        return events

    def _build_medication_order_groups(self, rows) -> list[dict]:
        groups: dict[str, dict] = {}

        def ensure_group(drug_name: str) -> dict:
            clean_name = str(drug_name or "").strip() or "Без названия"
            key = clean_name.casefold()
            return groups.setdefault(
                key,
                {
                    "key": key,
                    "drug_name": clean_name,
                    "entries": [],
                    "order_rows": [],
                    "infusion_rows": [],
                    "latest_dt": datetime.min,
                    "first_id": None,
                    "has_bolus_order": False,
                    "has_gas_order": False,
                    "has_gas_infusion": False,
                    "has_rate_infusion": False,
                    "has_volume_infusion": False,
                },
            )

        for raw_row in rows or []:
            row = dict(raw_row or {})
            row_dt = _minute_floor_dt(_parse_datetime_value(row.get("datetime")))
            if row_dt is None:
                continue
            raw_drug_name, dose = _split_order_drug_and_dose(str(row.get("text") or ""))
            drug_name = self._order_display_drug_name(row, raw_drug_name)
            row["drug_name"] = drug_name
            row["raw_drug_name"] = raw_drug_name
            row["dose_text"] = dose
            row["route"] = _order_route_code(row)
            order_kind = self._order_preset_kind(row)
            row["order_kind"] = order_kind
            group = ensure_group(drug_name)
            group["order_rows"].append(row)
            group["has_bolus_order"] = bool(group["has_bolus_order"] or order_kind == "bolus")
            group["has_gas_order"] = bool(group["has_gas_order"] or order_kind == "gas")
            row_id = _safe_int(row.get("id")) or 0
            group["entries"].append(
                {
                    "kind": "order",
                    "order_kind": order_kind,
                    "time": row_dt,
                    "detail": _order_dose_text_with_route(dose, row, short=False)
                    or str(row.get("text") or "").strip(),
                    "row": row,
                    "sort_id": row_id,
                }
            )
            if row_dt > group["latest_dt"]:
                group["latest_dt"] = row_dt
            if row_id and (group["first_id"] is None or row_id < group["first_id"]):
                group["first_id"] = row_id

        snapshot = getattr(self, "_current_timeline_snapshot", None) or {}
        for raw_interval in snapshot.get("infusion_intervals") or []:
            interval = dict(raw_interval or {})
            status = str(interval.get("status") or "")
            if status not in {"active", "stopped"}:
                continue
            start_dt = _minute_floor_dt(_parse_datetime_value(interval.get("start_time")))
            end_dt = _minute_floor_dt(_parse_datetime_value(interval.get("end_time")))
            row_dt = end_dt or start_dt
            if row_dt is None:
                continue
            drug_name = _infusion_display_drug_name(interval, "Дозатор")
            group = ensure_group(drug_name)
            group["infusion_rows"].append(interval)
            group["has_gas_infusion"] = bool(group["has_gas_infusion"] or _is_gas_infusion(interval))
            group["has_rate_infusion"] = bool(group["has_rate_infusion"] or _infusion_has_rate(interval))
            group["has_volume_infusion"] = bool(group["has_volume_infusion"] or _is_volume_only_infusion(interval))
            start_event_id, _revision = self._infusion_identity(interval)
            group["entries"].append(
                {
                    "kind": "infusion",
                    "time": row_dt,
                    "detail": self._infusion_history_entry_text(interval),
                    "interval": interval,
                    "sort_id": start_event_id or 0,
                }
            )
            if row_dt > group["latest_dt"]:
                group["latest_dt"] = row_dt
            if start_event_id and (group["first_id"] is None or start_event_id < group["first_id"]):
                group["first_id"] = start_event_id

        result = list(groups.values())
        for group in result:
            group["entries"].sort(
                key=lambda item: (
                    _minute_floor_dt(_parse_datetime_value(item.get("time"))) or datetime.min,
                    int(item.get("sort_id") or 0),
                ),
                reverse=True,
            )
            group["total_text"] = self._medication_group_total_text(group)
            group["source_signature"] = self._medication_group_source_signature(group)
        result.sort(
            key=lambda group: (
                group.get("latest_dt") if isinstance(group.get("latest_dt"), datetime) else datetime.min,
                int(group.get("first_id") or 0),
                str(group.get("drug_name") or "").casefold(),
            ),
            reverse=True,
        )
        return result

    def _medication_group_source_signature(self, group: dict) -> str:
        order_rows = []
        for row in group.get("order_rows") or []:
            order_rows.append(
                {
                    "id": row.get("id"),
                    "datetime": row.get("datetime"),
                    "text": row.get("text"),
                    "drug_key": row.get("drug_key"),
                    "drug_display_name": row.get("drug_display_name"),
                    "order_kind": row.get("order_kind"),
                    "concentration": self._order_row_concentration_text(row),
                    "comment": row.get("comment"),
                    "route": _order_route_code(row),
                    "status": row.get("status"),
                    "revision": row.get("revision"),
                    "updated_at": row.get("updated_at"),
                }
            )
        infusion_rows = []
        for interval in group.get("infusion_rows") or []:
            infusion_rows.append(
                {
                    "interval_id": interval.get("interval_id"),
                    "drug_label": interval.get("drug_label"),
                    "display_label": interval.get("display_label"),
                    "start_time": interval.get("start_time"),
                    "end_time": interval.get("end_time"),
                    "status": interval.get("status"),
                    "volume_ml": interval.get("volume_ml"),
                    "current_rate_value": interval.get("current_rate_value"),
                    "current_rate_unit": interval.get("current_rate_unit"),
                    "rate_history": interval.get("rate_history") or [],
                    "event_ids": interval.get("event_ids") or [],
                    "payload": interval.get("payload") or {},
                }
            )
        return _stable_ui_hash(
            {
                "key": group.get("key"),
                "drug_name": group.get("drug_name"),
                "orders": order_rows,
                "infusions": infusion_rows,
            }
        )

    def _medication_group_render_signature(self, group: dict) -> str:
        key = str(group.get("key") or "").casefold()
        return _stable_ui_hash(
            {
                "source": str(group.get("source_signature") or ""),
                "collapsed": key in getattr(self, "_collapsed_order_group_keys", set()),
            }
        )

    def _medication_group_total_text(self, group: dict) -> str:
        parts: list[str] = []
        order_rows = list(group.get("order_rows") or [])
        infusion_rows = list(group.get("infusion_rows") or [])
        order_total = (
            _summarize_order_total(order_rows, concentration_for_row=self._order_row_concentration_text)
            if order_rows
            else ""
        )
        if order_total.startswith("Итого: "):
            parts.append(order_total.removeprefix("Итого: "))
        elif order_total:
            parts.append(order_total)

        total_volume = Decimal("0")
        has_volume = False
        total_oxygen_liters = Decimal("0")
        has_oxygen_liters = False
        active_count = 0
        for interval in infusion_rows:
            if str((interval or {}).get("status") or "") == "active":
                active_count += 1
            if _is_oxygen_infusion(interval or {}):
                oxygen_liters = _oxygen_consumed_liters(interval or {})
                if oxygen_liters is not None:
                    total_oxygen_liters += oxygen_liters
                    has_oxygen_liters = True
                continue
            volume_text = _format_infusion_executed_volume(interval) if _infusion_has_rate(interval) else ""
            volume = _counted_infusion_volume_ml(interval or {})
            if volume is None and volume_text:
                volume = _decimal_from_ru_number(volume_text.replace("мл", ""))
            if (
                volume is None
                and _is_volume_only_infusion(interval or {})
                and str((interval or {}).get("status") or "") != "active"
            ):
                volume = _infusion_declared_volume_ml(interval or {})
            if volume is None:
                continue
            total_volume += volume
            has_volume = True
        if has_volume:
            parts.append(_format_infusion_volume_ml(total_volume))
        if has_oxygen_liters:
            parts.append(_format_oxygen_liters(total_oxygen_liters))
        elif active_count:
            parts.append(f"активно: {active_count}")

        return f"Итого: {', '.join(parts)}" if parts else "Итого: нет дозы"

    def _medication_visual(self, group: dict) -> dict:
        name = str(group.get("drug_name") or "").casefold()
        if _text_is_oxygen(name):
            return {
                "color": "#E0F2FE",
                "icon": "drop_white",
                "icon_file": OXYGEN_ICON_FILE,
                "icon_fallback_file": OXYGEN_ICON_FILE,
                "icon_size": 30,
                "time_bg": "#E0F2FE",
                "time_fg": "#0369A1",
            }
        if (
            group.get("has_gas_order")
            or group.get("has_gas_infusion")
            or "газ" in name
            or "sevo" in name
            or "сево" in name
            or "desfl" in name
            or "десфл" in name
            or "isofl" in name
            or "изофл" in name
            or "oxygen" in name
            or "кислород" in name
        ):
            return {
                "color": "#E0F2FE",
                "icon": "drop_white",
                "icon_file": type_icon_key("gas"),
                "icon_size": 30,
                "time_bg": "#E0F2FE",
                "time_fg": "#0369A1",
            }
        if group.get("has_rate_infusion") or "noradren" in name or "норадрен" in name:
            return {
                "color": "#FFF7ED",
                "icon": "infusion_white",
                "icon_file": type_icon_key("continuous_infusion"),
                "icon_size": 30,
                "time_bg": "#DBEAFE",
                "time_fg": "#1D4ED8",
            }
        if group.get("has_volume_infusion") or "nacl" in name or "натрия" in name or "раств" in name:
            return {
                "color": "#EAF3FF",
                "icon": "drop_white",
                "icon_file": type_icon_key("timed_infusion"),
                "icon_size": 30,
                "time_bg": "#CCFBF1",
                "time_fg": "#0F766E",
            }
        if group.get("has_bolus_order"):
            return {
                "color": "#EAFBF5",
                "icon": "syringe_white",
                "icon_file": type_icon_key("bolus"),
                "icon_size": 30,
                "time_bg": "#DCFCE7",
                "time_fg": "#15803D",
            }
        return {
            "color": "#EAFBF5",
            "icon": "syringe_white",
            "icon_file": type_icon_key("bolus"),
            "icon_size": 30,
            "time_bg": "#DCFCE7",
            "time_fg": "#15803D",
        }
