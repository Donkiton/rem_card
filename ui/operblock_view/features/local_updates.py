from __future__ import annotations

from datetime import datetime
from rem_card.services.operblock_timeline import build_infusion_intervals_from_timeline_events
from rem_card.services.operblock_timeline import legacy_order_row_to_medication_event
from rem_card.services.operblock_timeline import timeline_event_row_to_medication_event
import re
from rem_card.ui.operblock_view.operblock_helpers import (
    _infusion_display_drug_name,
    _minute_floor_dt,
    _normalize_gas_dose_text,
    _normalize_order_route_code,
    _normalize_oxygen_flow_text,
    _normalize_volume_ml_text,
    _order_comment_with_route,
    _order_route_code,
    _oxygen_payload_fields,
    _parse_datetime_value,
    _safe_int,
    _split_order_drug_and_dose,
    _stable_ui_hash,
    _stored_order_route_value,
)


class OperBlockLocalUpdatesMixin:
    def _current_order_row_by_id(self, order_id: int) -> dict | None:
        target_id = int(order_id)
        for row in list(getattr(self, "_current_orders_rows", []) or []):
            if _safe_int((row or {}).get("id")) == target_id:
                return dict(row or {})
        return None

    def _fresh_order_row(self, row: dict) -> dict:
        order_id = _safe_int((row or {}).get("id"))
        if not order_id:
            return dict(row or {})
        return self._current_order_row_by_id(order_id) or dict(row or {})

    def _patch_timeline_snapshot_order_route(self, order_id: int, route_code: str) -> bool:
        snapshot = dict(getattr(self, "_current_timeline_snapshot", None) or {})
        events = list(snapshot.get("bolus_events") or [])
        if not events:
            return False
        route_value = _stored_order_route_value(route_code)
        changed = False
        patched_events: list[dict] = []
        for event in events:
            data = dict(event or {})
            event_order_id = _safe_int(data.get("source_id"))
            if str(data.get("source") or "") == "legacy_order" and event_order_id == int(order_id):
                payload = dict(data.get("payload") or {})
                data["route"] = route_value
                if route_value:
                    payload["route"] = route_value
                else:
                    payload.pop("route", None)
                data["payload"] = payload
                data["revision"] = int(data.get("revision") or 0) + 1
                changed = True
            patched_events.append(data)
        if not changed:
            return False
        snapshot["bolus_events"] = patched_events
        hash_payload = dict(snapshot)
        hash_payload.pop("generated_at", None)
        snapshot["content_hash"] = _stable_ui_hash(hash_payload)
        self._current_timeline_snapshot = snapshot
        return True

    @staticmethod
    def _refresh_timeline_snapshot_hash(snapshot: dict) -> dict:
        updated = dict(snapshot or {})
        hash_payload = dict(updated)
        hash_payload.pop("generated_at", None)
        hash_payload.pop("content_hash", None)
        updated["content_hash"] = _stable_ui_hash(hash_payload)
        return updated

    def _patch_timeline_snapshot_order(
        self,
        order_id: int,
        *,
        text: str | None = None,
        order_datetime: str | None = None,
        route_code: str | None = None,
    ) -> bool:
        snapshot = dict(getattr(self, "_current_timeline_snapshot", None) or {})
        events = list(snapshot.get("bolus_events") or [])
        if not events:
            return False
        changed = False
        patched_events: list[dict] = []
        drug_name, dose_text = _split_order_drug_and_dose(text or "") if text is not None else ("", "")
        event_time = self._local_iso_minute_text(order_datetime) if order_datetime is not None else None
        route_value = _stored_order_route_value(route_code) if route_code is not None else None
        for event in events:
            data = dict(event or {})
            event_order_id = _safe_int(data.get("source_id"))
            if str(data.get("source") or "") == "legacy_order" and event_order_id == int(order_id):
                payload = dict(data.get("payload") or {})
                if text is not None:
                    data["drug_label"] = drug_name
                    data["dose_text"] = dose_text
                    payload["text"] = text
                    payload["drug_label"] = drug_name
                    payload["dose_text"] = dose_text
                if event_time is not None:
                    data["event_time"] = event_time
                if route_code is not None:
                    data["route"] = route_value
                    if route_value:
                        payload["route"] = route_value
                    else:
                        payload.pop("route", None)
                data["payload"] = payload
                data["revision"] = int(data.get("revision") or 0) + 1
                changed = True
            patched_events.append(data)
        if not changed:
            return False
        snapshot["bolus_events"] = patched_events
        self._current_timeline_snapshot = self._refresh_timeline_snapshot_hash(snapshot)
        return True

    @staticmethod
    def _timeline_snapshot_source_versions_with(
        snapshot: dict,
        source_key: str,
        source_id: int | None,
        revision: int | None,
    ) -> dict:
        versions = dict((snapshot or {}).get("source_versions") or {})
        current = dict(versions.get(source_key) or {})
        ids = [
            int(value)
            for value in list(current.get("ids") or [])
            if _safe_int(value) is not None
        ]
        if source_id is not None and int(source_id) not in ids:
            ids.append(int(source_id))
        current["ids"] = ids
        current["count"] = len(ids)
        if revision is not None:
            current["max_revision"] = max(int(current.get("max_revision") or 0), int(revision or 0))
        versions[source_key] = current
        return versions

    def _timeline_table_code_for_local_event(self) -> str | None:
        snapshot = getattr(self, "_current_timeline_snapshot", None) or {}
        for section in ("operation_events", "bolus_events"):
            for event in list(snapshot.get(section) or []):
                table_code = str((event or {}).get("table_code") or "").strip()
                if table_code:
                    return table_code
        for interval in list(snapshot.get("infusion_intervals") or []):
            table_code = str((interval or {}).get("table_code") or "").strip()
            if table_code:
                return table_code
        return None

    def _patch_timeline_snapshot_added_order(self, order_row: dict) -> bool:
        order_id = _safe_int((order_row or {}).get("id"))
        if not order_id or not self._current_admission_id:
            return False
        event = legacy_order_row_to_medication_event(
            dict(order_row or {}),
            admission_id=int(self._current_admission_id),
            operation_case_id=int(self._current_operation_case_id or 0) or None,
            table_code=self._timeline_table_code_for_local_event(),
        )
        if event is None:
            return False
        event_data = event.to_dict()
        snapshot = dict(getattr(self, "_current_timeline_snapshot", None) or {})
        if not snapshot:
            snapshot = {
                "admission_id": int(self._current_admission_id),
                "operation_case_id": int(self._current_operation_case_id or 0) or None,
            }
        events = [
            dict(item or {})
            for item in list(snapshot.get("bolus_events") or [])
            if not (str((item or {}).get("source") or "") == "legacy_order" and _safe_int((item or {}).get("source_id")) == int(order_id))
        ]
        events.append(event_data)
        events.sort(
            key=lambda item: (
                _parse_datetime_value((item or {}).get("event_time")) or datetime.min,
                _safe_int((item or {}).get("source_id")) or 0,
            )
        )
        snapshot["bolus_events"] = events
        snapshot["source_versions"] = self._timeline_snapshot_source_versions_with(
            snapshot,
            "legacy_orders",
            int(order_id),
            int((order_row or {}).get("revision") or 0),
        )
        self._current_timeline_snapshot = self._refresh_timeline_snapshot_hash(snapshot)
        return True

    def _apply_added_order_locally(self, order_row: dict) -> bool:
        row = dict(order_row or {})
        order_id = _safe_int(row.get("id"))
        order_dt = _minute_floor_dt(_parse_datetime_value(row.get("datetime")))
        if not order_id or order_dt is None:
            return False
        row["route"] = _order_route_code(row)
        rows = [
            dict(existing or {})
            for existing in list(getattr(self, "_current_orders_rows", []) or [])
            if _safe_int((existing or {}).get("id")) != int(order_id)
        ]
        rows.append(row)
        rows.sort(
            key=lambda item: (
                _parse_datetime_value((item or {}).get("datetime")) or datetime.min,
                _safe_int((item or {}).get("id")) or 0,
            ),
            reverse=True,
        )
        rows = rows[:100]
        if not self._patch_timeline_snapshot_added_order(row):
            return False
        self._current_orders_rows = rows
        self._apply_orders({"orders": rows})
        return True

    def _patch_timeline_snapshot_started_infusion(self, event_row: dict) -> bool:
        event = timeline_event_row_to_medication_event(dict(event_row or {}))
        if event is None or event.event_type != "infusion_start":
            return False
        intervals = build_infusion_intervals_from_timeline_events([event])
        if not intervals:
            return False
        interval = intervals[0].to_dict()
        snapshot = dict(getattr(self, "_current_timeline_snapshot", None) or {})
        if not snapshot:
            snapshot = {
                "admission_id": int(self._current_admission_id or event.admission_id or 0),
                "operation_case_id": int(self._current_operation_case_id or event.operation_case_id or 0) or None,
            }
        interval_id = str(interval.get("interval_id") or "")
        current_intervals = [
            dict(item or {})
            for item in list(snapshot.get("infusion_intervals") or [])
            if str((item or {}).get("interval_id") or "") != interval_id
        ]
        current_intervals.append(interval)
        current_intervals.sort(
            key=lambda item: (
                _parse_datetime_value((item or {}).get("start_time")) or datetime.min,
                str((item or {}).get("interval_id") or ""),
            )
        )
        snapshot["infusion_intervals"] = current_intervals
        snapshot["source_versions"] = self._timeline_snapshot_source_versions_with(
            snapshot,
            "timeline_events",
            int(event.source_id),
            int(event.revision or 0),
        )
        self._current_timeline_snapshot = self._refresh_timeline_snapshot_hash(snapshot)
        return True

    def _apply_started_infusion_locally(self, event_row: dict) -> bool:
        if not self._patch_timeline_snapshot_started_infusion(event_row):
            return False
        self._apply_active_infusions()
        self._apply_orders({"orders": getattr(self, "_current_orders_rows", [])})
        return True

    def _apply_order_route_change_locally(self, order_id: int, route_code: str) -> bool:
        normalized_route = _normalize_order_route_code(route_code)
        rows = []
        found = False
        for row in list(getattr(self, "_current_orders_rows", []) or []):
            updated = dict(row or {})
            if _safe_int(updated.get("id")) == int(order_id):
                updated["route"] = normalized_route
                updated["comment"] = _order_comment_with_route(str(updated.get("comment") or ""), normalized_route)
                updated["revision"] = int(updated.get("revision") or 0) + 1
                updated["updated_at"] = datetime.now().isoformat(timespec="seconds")
                found = True
            rows.append(updated)
        if not found:
            return False
        self._patch_timeline_snapshot_order_route(order_id, normalized_route)
        self._current_orders_rows = rows
        self._patch_rendered_order_detail_text(order_id)
        self._sync_orders_render_signatures_from_current_rows()
        self._update_vitals_chart_order_markers()
        return True

    def _apply_order_edit_locally(
        self,
        order_id: int,
        text: str,
        *,
        order_datetime: str | None = None,
        route_code: str | None = None,
    ) -> bool:
        clean_text = re.sub(r"\s+", " ", str(text or "").strip())
        if not clean_text:
            return False
        rows = []
        found = False
        normalized_route = _normalize_order_route_code(route_code) if route_code is not None else None
        for row in list(getattr(self, "_current_orders_rows", []) or []):
            updated = dict(row or {})
            if _safe_int(updated.get("id")) == int(order_id):
                updated["text"] = clean_text
                if order_datetime is not None:
                    updated["datetime"] = self._local_iso_minute_text(order_datetime)
                if normalized_route is not None:
                    updated["route"] = normalized_route
                    updated["comment"] = _order_comment_with_route(str(updated.get("comment") or ""), normalized_route)
                updated["revision"] = int(updated.get("revision") or 0) + 1
                updated["updated_at"] = datetime.now().isoformat(timespec="seconds")
                raw_drug_name, dose_text = _split_order_drug_and_dose(clean_text)
                updated["drug_name"] = self._order_display_drug_name(updated, raw_drug_name)
                updated["raw_drug_name"] = raw_drug_name
                updated["dose_text"] = dose_text
                found = True
            rows.append(updated)
        if not found:
            return False
        self._patch_timeline_snapshot_order(
            order_id,
            text=clean_text,
            order_datetime=order_datetime,
            route_code=normalized_route,
        )
        self._current_orders_rows = rows
        self._apply_orders({"orders": rows})
        self._update_vitals_chart_order_markers()
        return True

    def _on_order_route_saved(self, order_id: int, route_code: str):
        self._write_pending = False
        if not self._apply_order_route_change_locally(order_id, route_code):
            self._set_protocol_write_controls_enabled(True)
            self.refresh_protocol(force=True)

    def _on_order_edit_saved(
        self,
        order_id: int,
        text: str,
        *,
        order_datetime: str | None = None,
        route_code: str | None = None,
    ):
        self._write_pending = False
        self._set_protocol_write_controls_enabled(True)
        if not self._apply_order_edit_locally(order_id, text, order_datetime=order_datetime, route_code=route_code):
            self.refresh_protocol(force=True)

    @staticmethod
    def _timeline_event_key(event_id: int | None) -> str:
        return f"timeline_event:{int(event_id)}" if event_id else ""

    def _patch_current_infusion_interval(self, start_event_id: int, updater) -> bool:
        snapshot = dict(getattr(self, "_current_timeline_snapshot", None) or {})
        intervals = list(snapshot.get("infusion_intervals") or [])
        if not intervals:
            return False
        patched_intervals: list[dict] = []
        changed = False
        for interval in intervals:
            data = dict(interval or {})
            interval_start_id, _revision = self._infusion_identity(data)
            if interval_start_id == int(start_event_id):
                data = updater(data) or data
                changed = True
            patched_intervals.append(data)
        if not changed:
            return False
        snapshot["infusion_intervals"] = patched_intervals
        self._current_timeline_snapshot = self._refresh_timeline_snapshot_hash(snapshot)
        return True

    @staticmethod
    def _bump_infusion_interval_start_revision(interval: dict) -> dict:
        payload = dict((interval or {}).get("payload") or {})
        payload["start_revision"] = int(payload.get("start_revision") or 0) + 1
        interval["payload"] = payload
        return interval

    def _set_infusion_interval_start_time(self, interval: dict, start_event_id: int, event_time: str | None) -> dict:
        if event_time is None:
            return interval
        event_time_text = self._local_iso_minute_text(event_time)
        interval["start_time"] = event_time_text
        start_key = self._timeline_event_key(start_event_id)
        for history_key in ("rate_history", "dose_history"):
            history = []
            for index, item in enumerate(list(interval.get(history_key) or [])):
                updated_item = dict(item or {})
                if index == 0 or (start_key and str(updated_item.get("event_id") or "") == start_key):
                    updated_item["event_time"] = event_time_text
                    updated_item["revision"] = int(updated_item.get("revision") or 0) + 1
                history.append(updated_item)
            if history:
                interval[history_key] = history
        return interval

    def _apply_infusion_interval_locally(self, start_event_id: int, updater) -> bool:
        if not self._patch_current_infusion_interval(start_event_id, updater):
            return False
        self._apply_active_infusions()
        self._apply_orders({"orders": getattr(self, "_current_orders_rows", [])})
        self._update_vitals_chart_order_markers()
        return True

    def _complete_local_infusion_mutation(self, applied: bool) -> None:
        quick_scroll_state = getattr(self, "_pending_quick_orders_scroll_state", None)
        self._pending_quick_orders_scroll_state = None
        self._write_pending = False
        self._set_protocol_write_controls_enabled(True)
        if not applied:
            self.refresh_protocol(force=True)
        self._restore_quick_orders_scroll_state_later(quick_scroll_state)

    def _on_infusion_start_time_saved_locally(self, start_event_id: int, event_time: str | None) -> None:
        def updater(interval: dict) -> dict:
            interval = self._set_infusion_interval_start_time(interval, start_event_id, event_time)
            return self._bump_infusion_interval_start_revision(interval)

        self._complete_local_infusion_mutation(self._apply_infusion_interval_locally(start_event_id, updater))

    def _on_infusion_rate_saved_locally(
        self,
        result,
        start_event_id: int,
        *,
        rate_value: str,
        rate_unit: str,
        change_event_time: str,
        start_event_time: str | None,
    ) -> None:
        change_event_id = _safe_int(result)
        change_event_key = self._timeline_event_key(change_event_id) or f"local_rate:{int(start_event_id)}:{change_event_time}"

        def updater(interval: dict) -> dict:
            interval = self._set_infusion_interval_start_time(interval, start_event_id, start_event_time)
            interval["current_rate_value"] = str(rate_value or "").strip()
            interval["current_rate_unit"] = str(rate_unit or "").strip()
            history = [dict(item or {}) for item in list(interval.get("rate_history") or [])]
            history = [item for item in history if str(item.get("event_id") or "") != change_event_key]
            history.append(
                {
                    "event_id": change_event_key,
                    "event_time": self._local_iso_minute_text(change_event_time),
                    "rate_value": str(rate_value or "").strip(),
                    "rate_unit": str(rate_unit or "").strip(),
                    "revision": 1,
                }
            )
            history.sort(key=lambda item: (_parse_datetime_value(item.get("event_time")) or datetime.min, str(item.get("event_id") or "")))
            interval["rate_history"] = history
            event_ids = [str(item) for item in list(interval.get("event_ids") or []) if item]
            if change_event_key not in event_ids:
                event_ids.append(change_event_key)
            interval["event_ids"] = event_ids
            return self._bump_infusion_interval_start_revision(interval)

        self._complete_local_infusion_mutation(self._apply_infusion_interval_locally(start_event_id, updater))

    def _on_gas_dose_saved_locally(
        self,
        result,
        start_event_id: int,
        *,
        dose_text: str,
        change_event_time: str,
        start_event_time: str | None,
        is_oxygen: bool = False,
    ) -> None:
        change_event_id = _safe_int(result)
        change_event_key = self._timeline_event_key(change_event_id) or f"local_gas:{int(start_event_id)}:{change_event_time}"
        clean_dose = _normalize_oxygen_flow_text(dose_text) if is_oxygen else _normalize_gas_dose_text(dose_text)

        def updater(interval: dict) -> dict:
            interval = self._set_infusion_interval_start_time(interval, start_event_id, start_event_time)
            interval["current_rate_value"] = None
            interval["current_rate_unit"] = None
            interval["rate_history"] = []
            payload = dict(interval.get("payload") or {})
            if is_oxygen:
                payload = _oxygen_payload_fields(payload, clean_dose)
            payload["kind"] = "gas"
            payload["dose_text"] = clean_dose
            payload["display_dose_text"] = clean_dose
            interval["payload"] = payload
            history = [dict(item or {}) for item in list(interval.get("dose_history") or [])]
            history = [item for item in history if str(item.get("event_id") or "") != change_event_key]
            history.append(
                {
                    "event_id": change_event_key,
                    "event_time": self._local_iso_minute_text(change_event_time),
                    "dose_text": clean_dose,
                    "revision": 1,
                }
            )
            history.sort(key=lambda item: (_parse_datetime_value(item.get("event_time")) or datetime.min, str(item.get("event_id") or "")))
            interval["dose_history"] = history
            event_ids = [str(item) for item in list(interval.get("event_ids") or []) if item]
            if change_event_key not in event_ids:
                event_ids.append(change_event_key)
            interval["event_ids"] = event_ids
            return self._bump_infusion_interval_start_revision(interval)

        self._complete_local_infusion_mutation(self._apply_infusion_interval_locally(start_event_id, updater))

    def _on_infusion_volume_saved_locally(
        self,
        start_event_id: int,
        *,
        volume_ml: str,
        event_time: str | None,
    ) -> None:
        clean_volume = _normalize_volume_ml_text(volume_ml)

        def updater(interval: dict) -> dict:
            interval = self._set_infusion_interval_start_time(interval, start_event_id, event_time)
            interval["volume_ml"] = clean_volume
            drug_label = _infusion_display_drug_name(interval, "Капельница")
            interval["display_label"] = f"{drug_label} {clean_volume} мл".strip()
            payload = dict(interval.get("payload") or {})
            payload["volume_ml"] = clean_volume
            payload["declared_total_volume_ml"] = clean_volume
            interval["payload"] = payload
            return self._bump_infusion_interval_start_revision(interval)

        self._complete_local_infusion_mutation(self._apply_infusion_interval_locally(start_event_id, updater))
