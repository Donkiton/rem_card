from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping
from rem_card.app import operblock_startup_metrics
from rem_card.app.logger import logger
from rem_card.services.operblock_timeline import OPERBLOCK_STAGE_KIND_LABELS, operation_stage_kind_from_payload
from .common import (
    OPERBLOCK_ROLE,
    OPERBLOCK_TABLES,
    validate_operblock_runtime_path,
    _parse_dt,
    _hash_payload,
    _normalize_case_text,
    _surgeons_from_json,
    _age_text,
    _row_to_dict,
)


class OperBlockBoardMixin:
    @staticmethod
    def _board_operation_events_from_timeline(timeline: Mapping[str, Any] | dict[str, Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for event in list((timeline or {}).get("operation_events") or []):
            payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
            kind = operation_stage_kind_from_payload(payload)
            if not kind:
                continue
            label = _normalize_case_text(
                (payload or {}).get("label")
                or event.get("display_label")
                or event.get("raw_text")
                or OPERBLOCK_STAGE_KIND_LABELS.get(kind)
                or ""
            )
            result.append(
                {
                    "id": event.get("id") or "",
                    "source_id": int(event.get("source_id") or 0),
                    "kind": kind,
                    "label": label or OPERBLOCK_STAGE_KIND_LABELS.get(kind, "Этап операции"),
                    "event_time": event.get("event_time"),
                    "revision": int(event.get("revision") or 0),
                }
            )
        result.sort(key=lambda item: (_parse_dt(item.get("event_time")) or datetime.min, int(item.get("source_id") or 0)))
        return result

    @staticmethod
    def _board_medication_history_from_timeline(timeline: Mapping[str, Any] | dict[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for event in list((timeline or {}).get("bolus_events") or []):
            label = _normalize_case_text(event.get("display_label") or event.get("raw_text") or event.get("drug_label"))
            if not label:
                continue
            items.append(
                {
                    "time": event.get("event_time"),
                    "label": label,
                    "kind": "bolus",
                    "kind_label": "Болюс",
                    "source_id": int(event.get("source_id") or 0),
                }
            )
        for interval in list((timeline or {}).get("infusion_intervals") or []):
            payload = interval.get("payload") if isinstance(interval.get("payload"), Mapping) else {}
            label = _normalize_case_text(
                interval.get("display_label")
                or (payload or {}).get("display_name")
                or (payload or {}).get("label")
                or interval.get("drug_label")
            )
            if not label:
                continue
            is_gas = str((payload or {}).get("kind") or "").strip().casefold() == "gas"
            dose_text = _normalize_case_text((payload or {}).get("display_dose_text") or (payload or {}).get("dose_text"))
            if is_gas and dose_text and dose_text.casefold() not in label.casefold():
                label = f"{label} {dose_text}".strip()
            try:
                source_id = int(str(interval.get("interval_id") or "0").rsplit(":", 1)[-1] or 0)
            except (TypeError, ValueError):
                source_id = 0
            items.append(
                {
                    "time": interval.get("start_time"),
                    "label": label,
                    "kind": "gas" if is_gas else "infusion",
                    "kind_label": "Газ" if is_gas else "Инфузия",
                    "source_id": source_id,
                }
            )
        items.sort(key=lambda item: (_parse_dt(item.get("time")) or datetime.min, int(item.get("source_id") or 0)))
        return items

    def build_operblock_board_snapshot(self, table_code: str | None = None) -> dict[str, Any]:
        metric_started = operblock_startup_metrics.timer_start()
        validate_operblock_runtime_path(self.db)
        clean_table_code = self._validate_table_code(table_code) if table_code else None
        table_filter_clause = ""
        params: tuple[Any, ...] = ()
        if clean_table_code:
            table_filter_clause = "AND t.code = ?"
            params = (clean_table_code,)
        rows = self.db.fetch_all_remcard(
            f"""
            SELECT
                t.code AS table_code,
                t.display_name AS table_display_name,
                t.sort_order,
                oc.id AS operation_case_id,
                oc.patient_id,
                oc.admission_id,
                oc.status AS case_status,
                oc.started_at,
                oc.ended_at,
                COALESCE(oc.revision, 0) AS case_revision,
                oc.planned_operation_name,
                oc.planned_surgeons_json,
                oc.planned_operating_nurse,
                oc.planned_anesthesiologist,
                oc.planned_anesthetist,
                oc.height_cm,
                oc.weight_kg,
                oc.allergies,
                oc.blood_group,
                oc.blood_rh,
                oc.preop_sys,
                oc.preop_dia,
                oc.preop_pulse,
                oc.preop_spo2,
                COALESCE(oc.preop_save_initial_vitals, 1) AS preop_save_initial_vitals,
                p.full_name,
                p.birth_date,
                a.history_number,
                a.patient_gender,
                a.patient_age,
                a.patient_months,
                a.patient_age_unit,
                a.diagnosis_code,
                a.diagnosis_text,
                a.department_profile,
                (
                    SELECT v.id FROM vitals v
                    WHERE v.admission_id = oc.admission_id
                       AND DATETIME(v.datetime) >= DATETIME(oc.started_at)
                      AND (v.sys IS NOT NULL OR v.dia IS NOT NULL OR v.pulse IS NOT NULL OR v.spo2 IS NOT NULL)
                     ORDER BY DATETIME(v.datetime) ASC, v.id ASC
                    LIMIT 1
                ) AS first_vitals_id,
                (
                    SELECT v.id FROM vitals v
                    WHERE v.admission_id = oc.admission_id
                       AND DATETIME(v.datetime) >= DATETIME(oc.started_at)
                      AND (v.sys IS NOT NULL OR v.dia IS NOT NULL OR v.pulse IS NOT NULL OR v.spo2 IS NOT NULL)
                     ORDER BY DATETIME(v.datetime) DESC, v.id DESC
                    LIMIT 1
                ) AS latest_vitals_id,
                (
                    SELECT v.sys FROM vitals v
                    WHERE v.admission_id = oc.admission_id
                       AND DATETIME(v.datetime) >= DATETIME(oc.started_at)
                      AND (v.sys IS NOT NULL OR v.dia IS NOT NULL OR v.pulse IS NOT NULL OR v.spo2 IS NOT NULL)
                     ORDER BY DATETIME(v.datetime) DESC, v.id DESC
                    LIMIT 1
                ) AS latest_sys,
                (
                    SELECT v.dia FROM vitals v
                    WHERE v.admission_id = oc.admission_id
                       AND DATETIME(v.datetime) >= DATETIME(oc.started_at)
                      AND (v.sys IS NOT NULL OR v.dia IS NOT NULL OR v.pulse IS NOT NULL OR v.spo2 IS NOT NULL)
                     ORDER BY DATETIME(v.datetime) DESC, v.id DESC
                    LIMIT 1
                ) AS latest_dia,
                (
                    SELECT v.pulse FROM vitals v
                    WHERE v.admission_id = oc.admission_id
                       AND DATETIME(v.datetime) >= DATETIME(oc.started_at)
                      AND (v.sys IS NOT NULL OR v.dia IS NOT NULL OR v.pulse IS NOT NULL OR v.spo2 IS NOT NULL)
                     ORDER BY DATETIME(v.datetime) DESC, v.id DESC
                    LIMIT 1
                ) AS latest_pulse,
                (
                    SELECT v.spo2 FROM vitals v
                    WHERE v.admission_id = oc.admission_id
                       AND DATETIME(v.datetime) >= DATETIME(oc.started_at)
                      AND (v.sys IS NOT NULL OR v.dia IS NOT NULL OR v.pulse IS NOT NULL OR v.spo2 IS NOT NULL)
                     ORDER BY DATETIME(v.datetime) DESC, v.id DESC
                    LIMIT 1
                ) AS latest_spo2,
                (
                    SELECT v.datetime FROM vitals v
                    WHERE v.admission_id = oc.admission_id
                       AND DATETIME(v.datetime) >= DATETIME(oc.started_at)
                      AND (v.sys IS NOT NULL OR v.dia IS NOT NULL OR v.pulse IS NOT NULL OR v.spo2 IS NOT NULL)
                     ORDER BY DATETIME(v.datetime) DESC, v.id DESC
                    LIMIT 1
                ) AS latest_vitals_time
            FROM operating_tables t
            LEFT JOIN operation_cases oc
                ON oc.table_code = t.code
               AND oc.status = 'active'
            LEFT JOIN admissions a ON a.id = oc.admission_id
            LEFT JOIN patients p ON p.id = oc.patient_id
            WHERE t.code IN ('emergency', 'planned')
              {table_filter_clause}
            ORDER BY t.sort_order
            """,
            params,
        )

        by_code = {str(row["table_code"]): _row_to_dict(row) for row in rows}
        tables = []
        visible_tables = [
            table for table in OPERBLOCK_TABLES if not clean_table_code or str(table.get("code") or "") == clean_table_code
        ]
        for table in visible_tables:
            row = by_code.get(table["code"], {})
            occupied = row.get("operation_case_id") is not None
            patient = None
            if occupied:
                latest_sys = row.get("latest_sys")
                latest_dia = row.get("latest_dia")
                latest_pulse = row.get("latest_pulse")
                latest_spo2 = row.get("latest_spo2")
                latest_time = row.get("latest_vitals_time")
                latest_source = "current" if latest_time else ""
                if (
                    latest_time
                    and row.get("latest_vitals_id") is not None
                    and row.get("latest_vitals_id") == row.get("first_vitals_id")
                    and any(
                        value is not None
                        for value in (row.get("preop_sys"), row.get("preop_dia"), row.get("preop_pulse"), row.get("preop_spo2"))
                    )
                ):
                    latest_source = "initial"
                if latest_time in (None, "") and any(
                    value is not None
                    for value in (row.get("preop_sys"), row.get("preop_dia"), row.get("preop_pulse"), row.get("preop_spo2"))
                ):
                    latest_sys = row.get("preop_sys")
                    latest_dia = row.get("preop_dia")
                    latest_pulse = row.get("preop_pulse")
                    latest_spo2 = row.get("preop_spo2")
                    latest_source = "initial"
                operation_events: list[dict[str, Any]] = []
                medication_history: list[dict[str, Any]] = []
                try:
                    timeline = self.build_operblock_timeline_snapshot(
                        int(row.get("admission_id") or 0),
                        operation_case_id=int(row.get("operation_case_id") or 0),
                    ).to_dict()
                    operation_events = self._board_operation_events_from_timeline(timeline)
                    medication_history = self._board_medication_history_from_timeline(timeline)
                except Exception as exc:
                    logger.warning(
                        "operblock board timeline summary failed case_id=%s: %s",
                        row.get("operation_case_id"),
                        exc,
                        exc_info=True,
                    )
                patient = {
                    "patient_id": row.get("patient_id"),
                    "admission_id": row.get("admission_id"),
                    "operation_case_id": row.get("operation_case_id"),
                    "full_name": row.get("full_name") or "Неизвестно",
                    "history_number": row.get("history_number") or "",
                    "age": _age_text(row),
                    "gender": row.get("patient_gender") or "",
                    "diagnosis_code": row.get("diagnosis_code") or "",
                    "diagnosis_text": row.get("diagnosis_text") or "",
                    "department_profile": row.get("department_profile") or "",
                    "operation_name": row.get("planned_operation_name") or "",
                    "surgeons": _surgeons_from_json(row.get("planned_surgeons_json")),
                    "operating_nurse": row.get("planned_operating_nurse") or "",
                    "anesthesiologist": row.get("planned_anesthesiologist") or "",
                    "anesthetist": row.get("planned_anesthetist") or "",
                    "height_cm": row.get("height_cm"),
                    "weight_kg": row.get("weight_kg"),
                    "allergies": row.get("allergies") or "",
                    "blood_group": row.get("blood_group") or "",
                    "blood_rh": row.get("blood_rh") or "",
                    "status_text": "В операционной",
                    "started_at": row.get("started_at"),
                    "revision": int(row.get("case_revision") or 0),
                    "latest": {
                        "ad": self._format_ad(latest_sys, latest_dia),
                        "sys": latest_sys,
                        "dia": latest_dia,
                        "pulse": latest_pulse,
                        "spo2": latest_spo2,
                        "datetime": latest_time,
                        "source": latest_source,
                    },
                    "operation_events": operation_events,
                    "medication_history": medication_history,
                }
            tables.append(
                {
                    "code": table["code"],
                    "display_name": row.get("table_display_name") or table["display_name"],
                    "sort_order": table["sort_order"],
                    "occupied": occupied,
                    "patient": patient,
                }
            )

        payload = {
            "role": OPERBLOCK_ROLE,
            "tables": tables,
        }
        payload["content_hash"] = _hash_payload(payload)
        operblock_startup_metrics.record_since(
            "build_operblock_board_snapshot_ms",
            metric_started,
            source="operblock_service",
            table_count=len(tables),
        )
        return payload
