from __future__ import annotations

from typing import Any


class SyncCoordinator:
    FULL_REFRESH_REASONS = {
        "gap_detected",
        "cursor_moved_backwards",
        "empty_change_rows",
        "recovery",
        "manual_refresh",
    }

    ENTITY_SCOPES = {
        "orders": {"orders", "balance"},
        "administrations": {"orders", "administrations", "balance"},
        "vitals": {"vitals"},
        "vital_settings": {"vitals"},
        "fluids": {"balance"},
        "diet_plan": {"diet"},
        "oral_intake_events": {"diet", "balance"},
        "diet_templates": {"diet"},
        "beds": {"beds", "patient_header"},
        "admissions": {"beds", "patient_header"},
        "patients": {"beds", "patient_header"},
        "patient_status_events": {"patient_status", "patient_header", "vitals", "balance"},
        "ivl_episodes": {"ivl"},
        "clinical_events": {"ivl"},
        "devices": {"ivl"},
        "respiratory_support": {"ivl"},
        "operations": {"patient_header"},
        "transfusions": {"balance"},
    }

    SOURCE_PREFIX_SCOPES = {
        "orders_": {"orders", "balance"},
        "doctor_order_mark:": {"orders", "administrations", "balance"},
        "nurse_order_mark:": {"orders", "administrations", "balance"},
        "nurse_order_panel_mark:": {"orders", "administrations", "balance"},
        "fluid": {"balance"},
        "balance": {"balance"},
        "diet": {"diet"},
        "oral_intake": {"diet", "balance"},
        "status_": {"patient_status", "patient_header", "vitals", "balance"},
        "doctor_create_empty_card:": {"vitals", "patient_header"},
        "ivl": {"ivl"},
        "vent": {"ivl"},
        "patient_bed": {"beds", "patient_header"},
        "archive_": {"beds", "patient_header"},
    }

    VITALS_SNAPSHOT_SCOPES = {
        "vitals",
        "patient_header",
        "patient_status",
        "beds",
    }

    @classmethod
    def classify(cls, payload: dict[str, Any]) -> dict[str, Any]:
        result = dict(payload or {})
        changed_entities = cls._changed_entities(result)
        force_sources = cls._force_sources(result)
        scopes = cls._scopes_for_entities(changed_entities)
        scopes.update(cls._scopes_for_sources(force_sources))

        reason = str(result.get("reason") or "")
        gap_detected = bool(result.get("gap_detected"))
        forced = bool(result.get("forced"))
        has_changes = bool(result.get("changes"))
        has_scope = bool(scopes)

        full_refresh_required = (
            gap_detected
            or reason in cls.FULL_REFRESH_REASONS
            or (forced and not has_changes and not has_scope)
        )
        vitals_snapshot_required = full_refresh_required or bool(scopes.intersection(cls.VITALS_SNAPSHOT_SCOPES))
        card_snapshot_required = full_refresh_required

        result["sync_actions"] = {
            "changed_entities": sorted(changed_entities),
            "scopes": sorted(scopes),
            "full_refresh_required": bool(full_refresh_required),
            "card_snapshot_required": bool(card_snapshot_required),
            "vitals_snapshot_required": bool(vitals_snapshot_required),
            "orders_refresh": bool(full_refresh_required or scopes.intersection({"orders", "administrations"})),
            "diet_refresh": bool(full_refresh_required or "diet" in scopes),
            "balance_refresh": bool(full_refresh_required or "balance" in scopes),
            "beds_refresh": bool(full_refresh_required or "beds" in scopes),
            "patient_header_refresh": bool(full_refresh_required or "patient_header" in scopes),
            "status_refresh": bool(full_refresh_required or "patient_status" in scopes),
            "ivl_refresh": bool(full_refresh_required or "ivl" in scopes),
        }
        return result

    @staticmethod
    def _changed_entities(payload: dict[str, Any]) -> set[str]:
        entities = {
            str(entity)
            for entity in (payload.get("changed_entities") or [])
            if entity is not None and str(entity)
        }
        if entities:
            return entities
        return {
            str(change.get("entity_name") or "")
            for change in (payload.get("changes") or [])
            if change.get("entity_name")
        }

    @staticmethod
    def _force_sources(payload: dict[str, Any]) -> list[str]:
        sources: list[str] = []
        raw_many = payload.get("force_sources") or []
        if isinstance(raw_many, (list, tuple, set)):
            sources.extend(str(item) for item in raw_many if item)
        raw_one = payload.get("force_source")
        if raw_one:
            sources.append(str(raw_one))
        return list(dict.fromkeys(sources))

    @classmethod
    def _scopes_for_entities(cls, entities: set[str]) -> set[str]:
        scopes: set[str] = set()
        for entity in entities:
            scopes.update(cls.ENTITY_SCOPES.get(entity, set()))
        return scopes

    @classmethod
    def _scopes_for_sources(cls, sources: list[str]) -> set[str]:
        scopes: set[str] = set()
        for source in sources:
            for prefix, mapped_scopes in cls.SOURCE_PREFIX_SCOPES.items():
                if source.startswith(prefix):
                    scopes.update(mapped_scopes)
        return scopes
