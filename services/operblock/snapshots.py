from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any
from rem_card.services.operblock_medication_presets import (
    load_operblock_medication_presets,
    operblock_medication_preset_display_name,
)
from rem_card.services.operblock_anesthesia_types import normalize_operblock_anesthesia_type_label
from rem_card.services.operblock_timeline import (
    OperBlockTimelineSnapshot,
    build_timeline_snapshot_from_legacy_orders,
    with_timeline_content_hash,
)
from .common import (
    OperBlockConflictError,
    _now_text,
    _parse_dt,
    _minute_floor,
    format_operblock_protocol_display,
    transfer_department_target_text,
    _hash_payload,
    _stage_rows_from_timeline_rows,
    _iso_or_none,
    _build_stage_intervals,
    _surgeons_from_json,
    _age_text,
    _row_to_dict,
)


class OperBlockSnapshotsMixin:
    def _build_operblock_patient_header_snapshot_from_case(
        self,
        case: dict[str, Any],
        *,
        latest: dict[str, Any] | None = None,
        stage_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        latest = latest if latest is not None else self.get_latest_vital_values(int(case["admission_id"]))
        stage_state = stage_state if stage_state is not None else self.build_operation_stage_state(int(case["operation_case_id"]))
        payload = {
            "operation_case_id": int(case["operation_case_id"]),
            "patient_id": int(case["patient_id"]),
            "admission_id": int(case["admission_id"]),
            "status": case["case_status"],
            "status_text": "В операционной" if case["case_status"] == "active" else "Случай закрыт",
            "table_code": case["table_code"],
            "table_display_name": case["table_display_name"],
            "history_number": case["history_number"],
            "full_name": case["full_name"],
            "age": _age_text(case),
            "gender": case["patient_gender"],
            "birth_date": case["birth_date"],
            "diagnosis_code": case["diagnosis_code"],
            "diagnosis_text": case["diagnosis_text"],
            "department_profile": case.get("department_profile") or "",
            "started_at": case["started_at"],
            "ended_at": case["ended_at"],
            "protocol_number": case.get("anesthesia_protocol_number"),
            "protocol_date": case.get("anesthesia_protocol_date"),
            "protocol_display": format_operblock_protocol_display(
                case.get("anesthesia_protocol_number"),
                case.get("anesthesia_protocol_date"),
            ),
            "operation_name": case.get("planned_operation_name") or "",
            "anesthesia_assistance_type": normalize_operblock_anesthesia_type_label(
                case.get("planned_anesthesia_assistance_type")
            ),
            "surgeons": _surgeons_from_json(case.get("planned_surgeons_json")),
            "operating_nurse": case.get("planned_operating_nurse") or "",
            "anesthesiologist": case.get("planned_anesthesiologist") or "",
            "anesthetist": case.get("planned_anesthetist") or "",
            "transfer_department": case.get("transfer_department") or "",
            "transfer_department_target": transfer_department_target_text(case.get("transfer_department") or ""),
            "latest": latest,
            "stage_state": stage_state,
        }
        payload["content_hash"] = _hash_payload(payload)
        return payload

    def build_operblock_patient_header_snapshot(self, operation_case_id: int) -> dict[str, Any]:
        case = self._get_case_row(operation_case_id)
        return self._build_operblock_patient_header_snapshot_from_case(case)

    def _build_operation_stage_state_from_case_rows(
        self,
        case: dict[str, Any],
        rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        stage_rows = _stage_rows_from_timeline_rows(rows)
        state = _build_stage_intervals(stage_rows)
        state["events"] = [
            {
                "id": int(row.get("id") or 0),
                "kind": str(row.get("stage_kind") or ""),
                "label": str(row.get("stage_label") or ""),
                "event_time": _iso_or_none(row.get("event_dt")),
                "revision": int(row.get("revision") or 0),
            }
            for row in stage_rows
        ]
        state["case_active"] = str(case.get("case_status") or "") == "active"
        state["case_started_at"] = case.get("started_at")
        state["case_ended_at"] = case.get("ended_at")
        state["planned_anesthesia_assistance_type"] = normalize_operblock_anesthesia_type_label(
            case.get("planned_anesthesia_assistance_type")
        )
        return state

    def build_operation_stage_state(self, operation_case_id: int) -> dict[str, Any]:
        case = self._get_case_row(operation_case_id)
        rows = self._fetch_operblock_timeline_event_rows(
            int(case["admission_id"]),
            operation_case_id=int(operation_case_id),
        )
        return self._build_operation_stage_state_from_case_rows(case, rows)

    def _build_operblock_vitals_snapshot_for_case(
        self,
        admission_id: int,
        *,
        case: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params: list[Any] = [int(admission_id)]
        bounds_clause = ""
        if case:
            if int(case.get("admission_id") or 0) != int(admission_id):
                raise OperBlockConflictError("Операция не принадлежит выбранной госпитализации.")
            started_at = _parse_dt(case.get("started_at"))
            if started_at is None:
                bounds_clause = "AND 1 = 0"
            else:
                bounds_clause = 'AND DATETIME("datetime") >= DATETIME(?)'
                params.append(_minute_floor(started_at).isoformat())
                ended_at = _parse_dt(case.get("ended_at"))
                if ended_at is not None:
                    bounds_clause += ' AND DATETIME("datetime") <= DATETIME(?)'
                    params.append(_minute_floor(ended_at).isoformat())
        rows = self.db.fetch_all_remcard(
            f"""
            SELECT id, admission_id, datetime, sys, dia, pulse, spo2, COALESCE(revision, 0) AS revision
            FROM vitals
            WHERE admission_id = ?
              {bounds_clause}
            ORDER BY DATETIME("datetime") DESC, id DESC
            LIMIT 50
            """,
            tuple(params),
        )
        vitals = []
        for row in rows:
            data = _row_to_dict(row)
            vitals.append(
                {
                    "id": data.get("id"),
                    "datetime": data.get("datetime"),
                    "ad": self._format_ad(data.get("sys"), data.get("dia")),
                    "sys": data.get("sys"),
                    "dia": data.get("dia"),
                    "pulse": data.get("pulse"),
                    "spo2": data.get("spo2"),
                    "revision": int(data.get("revision") or 0),
                }
            )
        payload = {"admission_id": int(admission_id), "vitals": vitals}
        payload["content_hash"] = _hash_payload(payload)
        return payload

    def build_operblock_vitals_snapshot(
        self,
        admission_id: int,
        *,
        operation_case_id: int | None = None,
    ) -> dict[str, Any]:
        case = self._get_case_row(operation_case_id) if operation_case_id else None
        return self._build_operblock_vitals_snapshot_for_case(admission_id, case=case)

    def _build_operblock_orders_snapshot_for_case(
        self,
        admission_id: int,
        *,
        case: dict[str, Any] | None = None,
        timeline_rows: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if case:
            if int(case.get("admission_id") or 0) != int(admission_id):
                raise OperBlockConflictError("Операция не принадлежит выбранной госпитализации.")
        rows = self.db.fetch_all_remcard(
            """
            SELECT
                id,
                datetime,
                text,
                drug_key,
                status,
                is_committed,
                comment,
                created_at,
                updated_at,
                COALESCE(revision, 0) AS revision
            FROM orders
            WHERE admission_id = ?
              AND COALESCE(status, '') NOT IN ('deleted', 'cancelled')
            ORDER BY DATETIME(datetime) DESC, id DESC
            LIMIT 500
            """,
            (int(admission_id),),
        )
        orders = self._apply_preset_display_names_to_order_rows([_row_to_dict(row) for row in rows])
        if case:
            if timeline_rows is None:
                timeline_rows = self._fetch_operblock_timeline_event_rows(
                    int(admission_id),
                    operation_case_id=int(case["operation_case_id"]),
                )
            intervals = self._all_anesthesia_intervals(_stage_rows_from_timeline_rows(timeline_rows))
            if intervals:
                orders = [row for row in orders if self._order_row_in_any_interval(row, intervals)]
            else:
                orders = []
        orders = orders[:100]
        payload = {"admission_id": int(admission_id), "orders": orders}
        payload["content_hash"] = _hash_payload(payload)
        return payload

    def build_operblock_orders_snapshot(
        self,
        admission_id: int,
        *,
        operation_case_id: int | None = None,
    ) -> dict[str, Any]:
        case = self._get_case_row(operation_case_id) if operation_case_id else None
        return self._build_operblock_orders_snapshot_for_case(admission_id, case=case)

    @staticmethod
    def _order_row_in_any_interval(row: dict[str, Any], intervals: list[tuple[datetime, datetime | None]]) -> bool:
        order_dt = _parse_dt((row or {}).get("datetime"))
        if order_dt is None:
            return False
        order_minute = _minute_floor(order_dt)
        for start, end in intervals:
            if order_minute < start:
                continue
            if end is not None and order_minute > end:
                continue
            return True
        return False

    @staticmethod
    def _operblock_preset_display_names_by_id() -> dict[str, str]:
        try:
            presets = load_operblock_medication_presets(include_disabled=True)
        except Exception:
            return {}
        result: dict[str, str] = {}
        for preset in presets:
            preset_id = str((preset or {}).get("preset_id") or "").strip()
            display_name = operblock_medication_preset_display_name(preset or {})
            if preset_id and display_name:
                result[preset_id] = display_name
        return result

    def _apply_preset_display_names_to_order_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        display_names = self._operblock_preset_display_names_by_id()
        if not display_names:
            return rows
        for row in rows:
            preset_id = str((row or {}).get("drug_key") or "").strip()
            display_name = display_names.get(preset_id)
            if display_name:
                row["drug_display_name"] = display_name
        return rows

    def _build_operblock_timeline_snapshot_for_case(
        self,
        admission_id: int,
        *,
        case: dict[str, Any] | None = None,
        orders_snapshot: dict[str, Any] | None = None,
        timeline_rows: list[dict[str, Any]] | None = None,
    ) -> OperBlockTimelineSnapshot:
        if case and int(case.get("admission_id") or 0) != int(admission_id):
            raise OperBlockConflictError("Операция не принадлежит выбранной госпитализации.")
        if timeline_rows is None:
            timeline_rows = self._fetch_operblock_timeline_event_rows(
                admission_id,
                operation_case_id=int(case["operation_case_id"]) if case else None,
            )
        if orders_snapshot is None:
            orders_snapshot = self._build_operblock_orders_snapshot_for_case(
                admission_id,
                case=case,
                timeline_rows=timeline_rows,
            )
        snapshot = build_timeline_snapshot_from_legacy_orders(
            admission_id=int(admission_id),
            operation_case_id=int(case["operation_case_id"]) if case else None,
            table_code=str(case.get("table_code") or "") if case else None,
            order_rows=[_row_to_dict(row) for row in orders_snapshot.get("orders") or []],
            timeline_rows=timeline_rows,
            generated_at=_now_text(),
        )
        return self._apply_operation_end_to_timeline_snapshot(snapshot, _parse_dt(case.get("ended_at")) if case else None)

    def build_operblock_timeline_snapshot(
        self,
        admission_id: int,
        operation_case_id: int | None = None,
    ) -> OperBlockTimelineSnapshot:
        case = self._get_case_row(operation_case_id) if operation_case_id else self._get_latest_case_row_for_admission(admission_id)
        return self._build_operblock_timeline_snapshot_for_case(admission_id, case=case)

    @staticmethod
    def _apply_operation_end_to_timeline_snapshot(
        snapshot: OperBlockTimelineSnapshot,
        ended_at: datetime | None,
    ) -> OperBlockTimelineSnapshot:
        if ended_at is None:
            return snapshot
        end_minute = _minute_floor(ended_at)
        intervals = []
        changed = False
        for interval in snapshot.infusion_intervals:
            if interval.status == "active" and interval.end_time is None and interval.start_time <= end_minute:
                intervals.append(replace(interval, end_time=end_minute, status="stopped"))
                changed = True
            else:
                intervals.append(interval)
        if not changed:
            return snapshot
        return with_timeline_content_hash(
            OperBlockTimelineSnapshot(
                admission_id=snapshot.admission_id,
                operation_case_id=snapshot.operation_case_id,
                generated_at=snapshot.generated_at,
                bolus_events=snapshot.bolus_events,
                infusion_intervals=intervals,
                operation_events=snapshot.operation_events,
                source_versions=snapshot.source_versions,
            )
        )

    def build_operblock_protocol_snapshot(self, operation_case_id: int) -> dict[str, Any]:
        case = self._get_case_row(operation_case_id)
        admission_id = int(case["admission_id"])
        timeline_rows = self._fetch_operblock_timeline_event_rows(
            admission_id,
            operation_case_id=int(case["operation_case_id"]),
        )
        stage_state = self._build_operation_stage_state_from_case_rows(case, timeline_rows)
        header = self._build_operblock_patient_header_snapshot_from_case(
            case,
            latest=self.get_latest_vital_values(admission_id),
            stage_state=stage_state,
        )
        vitals = self._build_operblock_vitals_snapshot_for_case(admission_id, case=case)
        orders = self._build_operblock_orders_snapshot_for_case(
            admission_id,
            case=case,
            timeline_rows=timeline_rows,
        )
        timeline = self._build_operblock_timeline_snapshot_for_case(
            admission_id,
            case=case,
            orders_snapshot=orders,
            timeline_rows=timeline_rows,
        )
        chart_vitals = self._list_operation_vitals_for_case(case)
        timeline_payload = timeline.to_dict()
        payload = {
            "header": header,
            "vitals": vitals,
            "orders": orders,
            "timeline": timeline_payload,
            "chart_vitals": chart_vitals,
        }
        hash_payload = dict(payload)
        timeline_hash_payload = dict(timeline_payload)
        timeline_hash_payload.pop("generated_at", None)
        hash_payload["timeline"] = timeline_hash_payload
        hash_payload["chart_vitals"] = [
            {
                "id": vital.id,
                "admission_id": vital.admission_id,
                "timestamp": vital.timestamp.isoformat(timespec="seconds"),
                "sys": vital.sys,
                "dia": vital.dia,
                "pulse": vital.pulse,
                "temp": vital.temp,
                "spo2": vital.spo2,
                "rr": vital.rr,
                "cvp": vital.cvp,
                "revision": vital.revision,
            }
            for vital in chart_vitals
        ]
        payload["content_hash"] = _hash_payload(hash_payload)
        return payload
