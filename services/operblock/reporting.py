from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping
from rem_card.app.operblock_offline_store import get_operblock_offline_root
from rem_card.services.operblock_medication_presets import (
    load_operblock_medication_presets,
    operblock_medication_preset_display_name,
    operblock_medication_preset_requires_narcotic_sheet,
)
from rem_card.services.operblock_anesthesia_types import normalize_operblock_anesthesia_type_label
from rem_card.services.operblock_timeline import OPERBLOCK_STAGE_KIND_LABELS, operation_stage_kind_from_payload
from .common import (
    _now_text,
    _parse_dt,
    _minute_floor,
    format_operblock_protocol_display,
    normalize_operblock_transfer_department,
    transfer_department_target_text,
    _hash_payload,
    _normalize_stage_text_list,
    _normalize_case_text,
    _surgeons_from_json,
)


class OperBlockReportingMixin:
    def build_operation_report_context(self, operation_case_id: int) -> dict[str, Any]:
        return self._run_report_read_operation(
            "operblock_report_context",
            lambda: self._build_operation_report_context(operation_case_id),
        )

    def _build_operation_report_context(self, operation_case_id: int) -> dict[str, Any]:
        snapshot = self.build_operblock_protocol_snapshot(operation_case_id)
        header = dict(snapshot.get("header") or {})
        stage_state = dict(header.get("stage_state") or {})
        timeline = dict(snapshot.get("timeline") or {})
        case = self._get_case_row(operation_case_id)
        vitals = [
            {
                "id": vital.id,
                "datetime": vital.timestamp.isoformat(timespec="seconds"),
                "sys": vital.sys,
                "dia": vital.dia,
                "pulse": vital.pulse,
                "spo2": vital.spo2,
                "temp": vital.temp,
                "rr": vital.rr,
                "cvp": vital.cvp,
            }
            for vital in self.list_operation_vitals(operation_case_id)
        ]
        stage_events = self._operation_report_stage_events(timeline)
        transfer_department = normalize_operblock_transfer_department(
            case.get("transfer_department")
            or header.get("transfer_department")
            or self._transfer_department_from_stage_events(stage_events)
        )
        anesthesia_interval = self._report_first_last_interval(stage_state.get("anesthesia_intervals"))
        surgery_interval = self._report_first_last_interval(stage_state.get("surgery_intervals"))
        controlled_medications = self._operation_report_controlled_medications(timeline)
        report = {
            "generated_at": _now_text(),
            "operation_case_id": int(operation_case_id),
            "patient": {
                "full_name": header.get("full_name") or "",
                "history_number": header.get("history_number") or "",
                "age": header.get("age") or "",
                "birth_date": header.get("birth_date") or "",
                "diagnosis_code": header.get("diagnosis_code") or "",
                "diagnosis_text": header.get("diagnosis_text") or "",
                "department_profile": header.get("department_profile") or "",
            },
            "case": {
                "table_code": header.get("table_code") or "",
                "table_display_name": header.get("table_display_name") or "",
                "admission_started_at": header.get("started_at"),
                "closed_at": header.get("ended_at"),
                "protocol_number": header.get("protocol_number"),
                "protocol_date": header.get("protocol_date"),
                "protocol_display": header.get("protocol_display") or "",
                "operation_name": self._report_operation_name(case, stage_state),
                "surgeons": self._report_surgeons(case, stage_state),
                "anesthesia_type": self._report_anesthesia_type(case, stage_state),
                "anesthesiologist": self._report_anesthesiologist(case, stage_state),
                "anesthetist": self._report_anesthetist(case, stage_state),
                "operating_nurse": self._report_operating_nurse(case, stage_state),
                "surgery_start": surgery_interval.get("start"),
                "surgery_end": surgery_interval.get("end"),
                "surgery_duration_minutes": self._duration_minutes(surgery_interval.get("start"), surgery_interval.get("end")),
                "anesthesia_start": anesthesia_interval.get("start"),
                "anesthesia_end": anesthesia_interval.get("end"),
                "anesthesia_duration_minutes": self._duration_minutes(anesthesia_interval.get("start"), anesthesia_interval.get("end")),
                "transfer_department": transfer_department,
                "transfer_department_target": transfer_department_target_text(transfer_department),
            },
            "stages": stage_events,
            "vitals": vitals,
            "timeline": timeline,
            "medications": self._operation_report_medications(timeline),
            "controlled_medications": controlled_medications,
        }
        report["content_hash"] = _hash_payload(report)
        return report

    @staticmethod
    def _transfer_department_from_stage_events(stage_events: list[dict[str, Any]]) -> str:
        for event in reversed(stage_events or []):
            payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
            department = normalize_operblock_transfer_department((payload or {}).get("transfer_department"))
            if department:
                return department
        return ""

    @staticmethod
    def _report_first_last_interval(intervals: Any) -> dict[str, Any]:
        rows = [dict(row or {}) for row in (intervals or []) if isinstance(row, Mapping)]
        rows = [row for row in rows if row.get("start")]
        rows.sort(key=lambda row: _parse_dt(row.get("start")) or datetime.max)
        if not rows:
            return {}
        first = rows[0]
        last_with_end = next((row for row in reversed(rows) if row.get("end")), rows[-1])
        return {"start": first.get("start"), "end": last_with_end.get("end")}

    @staticmethod
    def _duration_minutes(start_value: Any, end_value: Any) -> int | None:
        start_dt = _parse_dt(start_value)
        end_dt = _parse_dt(end_value)
        if start_dt is None or end_dt is None or end_dt < start_dt:
            return None
        return int(round((end_dt - start_dt).total_seconds() / 60.0))

    @staticmethod
    def _operation_report_stage_events(timeline: Mapping[str, Any] | dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for event in list((timeline or {}).get("operation_events") or []):
            data = dict(event or {})
            payload = data.get("payload") if isinstance(data.get("payload"), Mapping) else {}
            kind = operation_stage_kind_from_payload(payload)
            if not kind:
                continue
            label = _normalize_case_text(
                (payload or {}).get("label")
                or data.get("display_label")
                or data.get("raw_text")
                or OPERBLOCK_STAGE_KIND_LABELS.get(kind)
                or ""
            )
            rows.append(
                {
                    "kind": kind,
                    "label": label or OPERBLOCK_STAGE_KIND_LABELS.get(kind, "Этап операции"),
                    "event_time": data.get("event_time"),
                    "payload": dict(payload or {}),
                    "source_id": int(data.get("source_id") or 0),
                }
            )
        rows.sort(key=lambda item: (_parse_dt(item.get("event_time")) or datetime.min, int(item.get("source_id") or 0)))
        return rows

    @staticmethod
    def _report_operation_name(case: dict[str, Any], stage_state: dict[str, Any]) -> str:
        return _normalize_case_text(
            stage_state.get("last_operation_name")
            or stage_state.get("first_operation_name")
            or case.get("planned_operation_name")
            or ""
        )

    @staticmethod
    def _report_surgeons(case: dict[str, Any], stage_state: dict[str, Any]) -> list[str]:
        values = _normalize_stage_text_list(stage_state.get("last_surgeons") or stage_state.get("first_surgeons"))
        if values:
            return values
        return _surgeons_from_json(case.get("planned_surgeons_json"))

    @staticmethod
    def _report_anesthesia_type(case: dict[str, Any], stage_state: dict[str, Any]) -> str:
        return normalize_operblock_anesthesia_type_label(
            stage_state.get("last_anesthesia_assistance_type")
            or stage_state.get("first_anesthesia_assistance_type")
            or stage_state.get("current_anesthesia_assistance_type")
            or case.get("planned_anesthesia_assistance_type")
            or ""
        )

    @staticmethod
    def _report_anesthesiologist(case: dict[str, Any], stage_state: dict[str, Any]) -> str:
        return _normalize_case_text(
            stage_state.get("last_anesthesiologist")
            or stage_state.get("first_anesthesiologist")
            or case.get("planned_anesthesiologist")
            or ""
        )

    @staticmethod
    def _report_anesthetist(case: dict[str, Any], stage_state: dict[str, Any]) -> str:
        return _normalize_case_text(
            stage_state.get("last_anesthetist")
            or stage_state.get("first_anesthetist")
            or case.get("planned_anesthetist")
            or ""
        )

    @staticmethod
    def _report_operating_nurse(case: dict[str, Any], stage_state: dict[str, Any]) -> str:
        return _normalize_case_text(
            stage_state.get("last_operating_nurse")
            or stage_state.get("first_operating_nurse")
            or case.get("planned_operating_nurse")
            or ""
        )

    @staticmethod
    def _operation_report_medications(timeline: Mapping[str, Any] | dict[str, Any]) -> dict[str, Any]:
        boluses = []
        for event in list((timeline or {}).get("bolus_events") or []):
            payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
            boluses.append(
                {
                    "time": event.get("event_time"),
                    "name": _normalize_case_text(event.get("drug_label") or event.get("display_label") or event.get("raw_text")),
                    "display": _normalize_case_text(event.get("display_label") or event.get("raw_text") or event.get("drug_label")),
                    "dose_value": event.get("dose_value"),
                    "dose_unit": event.get("dose_unit"),
                    "volume_ml": event.get("volume_ml"),
                    "concentration_text": event.get("concentration_text"),
                    "route": event.get("route") or (payload or {}).get("route"),
                }
            )
        infusions = []
        for interval in list((timeline or {}).get("infusion_intervals") or []):
            payload = interval.get("payload") if isinstance(interval.get("payload"), Mapping) else {}
            infusions.append(
                {
                    "start": interval.get("start_time"),
                    "end": interval.get("end_time"),
                    "status": interval.get("status"),
                    "name": _normalize_case_text(
                        (payload or {}).get("display_name")
                        or (payload or {}).get("label")
                        or interval.get("drug_label")
                        or interval.get("display_label")
                    ),
                    "display": _normalize_case_text(interval.get("display_label") or interval.get("drug_label")),
                    "volume_ml": interval.get("volume_ml"),
                    "rate_value": interval.get("current_rate_value"),
                    "rate_unit": interval.get("current_rate_unit"),
                    "payload": dict(payload or {}),
                    "rate_history": list(interval.get("rate_history") or []),
                    "dose_history": list(interval.get("dose_history") or []),
                }
            )
        return {"boluses": boluses, "infusions": infusions}

    @staticmethod
    def _report_medication_identity_key(value: Any) -> str:
        text = _normalize_case_text(value).casefold().replace("ё", "е")
        text = re.sub(r"\s+\d+(?:[.,]\d+)?\s*%$", "", text).strip()
        return text

    @classmethod
    def _controlled_prescription_rules(cls, presets: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if presets is None:
            try:
                presets = load_operblock_medication_presets(include_disabled=True)
            except Exception:
                presets = []
        by_id: dict[str, bool] = {}
        name_keys: set[str] = set()
        for preset in presets or []:
            data = dict(preset or {})
            required = operblock_medication_preset_requires_narcotic_sheet(data)
            preset_id = str(data.get("preset_id") or "").strip()
            if preset_id:
                by_id[preset_id] = required
            if not required:
                continue
            for value in (
                operblock_medication_preset_display_name(data),
                data.get("label"),
                data.get("latin"),
                data.get("drug_name"),
                data.get("drug"),
            ):
                key = cls._report_medication_identity_key(value)
                if key:
                    name_keys.add(key)
        return {"by_id": by_id, "name_keys": name_keys}

    @classmethod
    def _controlled_prescription_required(cls, data: Mapping[str, Any], rules: Mapping[str, Any]) -> bool:
        payload = data.get("payload") if isinstance(data.get("payload"), Mapping) else {}
        preset_id = str((payload or {}).get("preset_id") or data.get("preset_id") or "").strip()
        by_id = rules.get("by_id") if isinstance(rules.get("by_id"), Mapping) else {}
        if preset_id and preset_id in by_id:
            return bool(by_id[preset_id])
        if preset_id and operblock_medication_preset_requires_narcotic_sheet(payload or {}):
            return True

        name_keys = rules.get("name_keys") if isinstance(rules.get("name_keys"), set) else set()
        if not name_keys:
            return False
        for value in (
            data.get("drug_label"),
            data.get("display_label"),
            data.get("name"),
            data.get("display"),
            data.get("raw_text"),
            (payload or {}).get("display_name"),
            (payload or {}).get("label"),
            (payload or {}).get("latin"),
        ):
            key = cls._report_medication_identity_key(value)
            if key and key in name_keys:
                return True
        return False

    @staticmethod
    def _controlled_decimal(value: Any) -> Decimal | None:
        text = str(value or "").strip().replace(",", ".")
        if not text:
            return None
        try:
            return Decimal(text)
        except (InvalidOperation, ValueError):
            return None

    @staticmethod
    def _controlled_format_decimal(value: Decimal) -> str:
        normalized = value.normalize()
        if normalized == normalized.to_integral():
            return str(normalized.quantize(Decimal("1")))
        return format(normalized, "f").rstrip("0").rstrip(".").replace(".", ",")

    @classmethod
    def _controlled_volume_decimal_ml(cls, value: Any) -> Decimal | None:
        text = _normalize_case_text(value)
        if not text:
            return None
        text = re.sub(r"\s*мл\s*$", "", text, flags=re.IGNORECASE).strip()
        return cls._controlled_decimal(text)

    @staticmethod
    def _controlled_dose_unit_key(unit: Any) -> str:
        raw = re.sub(r"\s+", "", str(unit or "").strip().casefold()).replace("ё", "е").replace("µ", "мк")
        raw = raw.rstrip(".")
        aliases = {
            "mg": "мг",
            "мг": "мг",
            "mkg": "мкг",
            "mcg": "мкг",
            "ug": "мкг",
            "мкг": "мкг",
            "мкгр": "мкг",
            "g": "г",
            "гр": "г",
            "г": "г",
            "ml": "мл",
            "мл": "мл",
        }
        return aliases.get(raw, raw)

    @classmethod
    def _controlled_dose_value_unit(
        cls,
        data: Mapping[str, Any],
        *,
        dose_text: Any = None,
        display_text: Any = None,
    ) -> tuple[Decimal | None, str]:
        value = cls._controlled_decimal(data.get("dose_value"))
        unit = _normalize_case_text(data.get("dose_unit"))
        if value is not None and unit:
            return value, unit

        source_text = " ".join(
            _normalize_case_text(value)
            for value in (dose_text, display_text, data.get("display_label"), data.get("raw_text"))
            if _normalize_case_text(value)
        )
        match = re.search(
            r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>мкгр|мкг|мг|мл|гр|г|mcg|mkg|mg|ml|ug|g)\b",
            source_text,
            flags=re.IGNORECASE,
        )
        if not match:
            return None, ""
        return cls._controlled_decimal(match.group("value")), _normalize_case_text(match.group("unit"))

    @classmethod
    def _controlled_concentration_mg_per_ml(cls, data: Mapping[str, Any], display_text: Any) -> Decimal | None:
        payload = data.get("payload") if isinstance(data.get("payload"), Mapping) else {}
        source_text = " ".join(
            _normalize_case_text(value)
            for value in (
                data.get("concentration_text"),
                (payload or {}).get("concentration"),
                (payload or {}).get("concentration_text"),
                display_text,
            )
            if _normalize_case_text(value)
        )
        ratio_match = re.search(
            r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>мкг|мкгр|mcg|mkg|ug|мг|mg|г|гр|g)\s*/\s*(?:мл|ml)\b",
            source_text,
            flags=re.IGNORECASE,
        )
        if ratio_match:
            value = cls._controlled_decimal(ratio_match.group("value"))
            if value is None or value <= 0:
                return None
            unit_key = cls._controlled_dose_unit_key(ratio_match.group("unit"))
            if unit_key == "мг":
                return value
            if unit_key == "г":
                return value * Decimal("1000")
            if unit_key == "мкг":
                return value / Decimal("1000")
            return None

        percent_match = re.search(r"(?P<percent>\d+(?:[.,]\d+)?)\s*%", source_text)
        if not percent_match:
            return None
        percent = cls._controlled_decimal(percent_match.group("percent"))
        if percent is None or percent <= 0:
            return None
        return percent * Decimal("10")

    @classmethod
    def _controlled_calculated_volume_ml(
        cls,
        data: Mapping[str, Any],
        *,
        dose_text: Any = None,
        display_text: Any = None,
    ) -> Decimal | None:
        payload = data.get("payload") if isinstance(data.get("payload"), Mapping) else {}
        for value in (
            data.get("volume_ml"),
            (payload or {}).get("calculated_volume_ml"),
            (payload or {}).get("volume_ml"),
        ):
            explicit = cls._controlled_volume_decimal_ml(value)
            if explicit is not None:
                return explicit

        dose_value, dose_unit = cls._controlled_dose_value_unit(
            data,
            dose_text=dose_text,
            display_text=display_text,
        )
        if dose_value is None:
            return None
        unit_key = cls._controlled_dose_unit_key(dose_unit)
        if unit_key == "мл":
            return dose_value
        dose_mg: Decimal | None = None
        if unit_key == "мг":
            dose_mg = dose_value
        elif unit_key == "г":
            dose_mg = dose_value * Decimal("1000")
        elif unit_key == "мкг":
            dose_mg = dose_value / Decimal("1000")
        if dose_mg is None:
            return None
        concentration = cls._controlled_concentration_mg_per_ml(data, display_text)
        if concentration is None or concentration <= 0:
            return None
        return dose_mg / concentration

    @classmethod
    def _controlled_prescription_name(cls, data: Mapping[str, Any], *, dose_text: Any = None) -> str:
        payload = data.get("payload") if isinstance(data.get("payload"), Mapping) else {}
        display = _normalize_case_text(
            data.get("display_label")
            or data.get("display")
            or data.get("raw_text")
            or data.get("name")
            or data.get("drug_label")
            or (payload or {}).get("display_name")
            or (payload or {}).get("label")
        )
        dose = _normalize_case_text(
            dose_text
            or (payload or {}).get("display_dose_text")
            or (payload or {}).get("dose_text")
        )
        if display and dose and dose.casefold() not in display.casefold():
            result = f"{display} {dose}".strip()
        else:
            result = display or dose or "Препарат"

        dose_value, dose_unit = cls._controlled_dose_value_unit(data, dose_text=dose, display_text=result)
        volume_ml = cls._controlled_calculated_volume_ml(data, dose_text=dose, display_text=result)
        volume_text = f"{cls._controlled_format_decimal(volume_ml)} мл" if volume_ml is not None else ""
        if (
            volume_ml is not None
            and cls._controlled_dose_unit_key(dose_unit) != "мл"
            and not re.search(r"\(\s*\d+(?:[.,]\d+)?\s*мл\s*\)", result, flags=re.IGNORECASE)
            and volume_text.casefold() not in result.casefold()
        ):
            result = f"{result} ({volume_text})"
        return result

    @classmethod
    def _operation_report_controlled_medications(
        cls,
        timeline: Mapping[str, Any] | dict[str, Any],
        *,
        presets: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        rules = cls._controlled_prescription_rules(presets)
        rows: list[dict[str, Any]] = []
        sequence = 0

        def add_row(time_value: Any, data: Mapping[str, Any], *, name: str | None = None) -> None:
            nonlocal sequence
            event_dt = _parse_dt(time_value)
            if event_dt is None:
                return
            sequence += 1
            rows.append(
                {
                    "datetime": _minute_floor(event_dt).isoformat(timespec="seconds"),
                    "name": _normalize_case_text(name) or cls._controlled_prescription_name(data),
                    "source": str(data.get("source") or data.get("id") or ""),
                    "source_id": int(data.get("source_id") or 0),
                    "sequence": sequence,
                }
            )

        for event in list((timeline or {}).get("bolus_events") or []):
            data = dict(event or {})
            if not cls._controlled_prescription_required(data, rules):
                continue
            add_row(data.get("event_time"), data)

        for interval in list((timeline or {}).get("infusion_intervals") or []):
            data = dict(interval or {})
            if not cls._controlled_prescription_required(data, rules):
                continue
            payload = data.get("payload") if isinstance(data.get("payload"), Mapping) else {}
            kind = str((payload or {}).get("kind") or "").strip().casefold()
            if kind == "gas":
                dose_history = [item for item in list(data.get("dose_history") or []) if isinstance(item, Mapping)]
                if dose_history:
                    for item in dose_history:
                        name = cls._controlled_prescription_name(data, dose_text=item.get("dose_text"))
                        add_row(item.get("event_time"), data, name=name)
                    continue
            add_row(data.get("start_time"), data)

        rows.sort(key=lambda item: (_parse_dt(item.get("datetime")) or datetime.max, int(item.get("sequence") or 0)))
        return rows

    def build_operation_report_pdf_path(self, operation_case_id: int) -> Path:
        return self._run_report_read_operation(
            "operblock_report_pdf_path",
            lambda: self._build_operation_report_pdf_path(operation_case_id),
        )

    def _build_operation_report_pdf_path(self, operation_case_id: int) -> Path:
        self.cleanup_operation_report_dir()
        case = self._get_case_row(operation_case_id)
        patient_name = self._safe_report_filename(case.get("full_name") or "patient")
        protocol_display = format_operblock_protocol_display(
            case.get("anesthesia_protocol_number"),
            case.get("anesthesia_protocol_date"),
        )
        protocol_slug = self._safe_report_filename(protocol_display or f"case_{int(operation_case_id)}")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_dir = Path(get_operblock_offline_root()) / "reports"
        return report_dir / f"{patient_name}_protocol_{protocol_slug}_{stamp}.pdf"

    def build_operation_report_pdf(self, operation_case_id: int, pdf_path) -> Path:
        from rem_card.services.operblock_reportlab_builder import OperBlockReportLabBuilder

        output_path = Path(pdf_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        context = self.build_operation_report_context(operation_case_id)
        OperBlockReportLabBuilder.build_pdf(context, output_path)
        return output_path

    @staticmethod
    def cleanup_operation_report_dir() -> None:
        report_dir = Path(get_operblock_offline_root()) / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        # PDFs may belong to cases still awaiting delivery. Age alone never
        # proves they are safe to delete; the local archive owns retention.

    @staticmethod
    def _safe_report_filename(value: Any) -> str:
        text = re.sub(r"\s+", "_", str(value or "").strip())
        text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
        text = re.sub(r"_+", "_", text).strip("._ ")
        return text[:80] or "report"
