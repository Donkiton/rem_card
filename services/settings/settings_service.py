from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from rem_card.app.db_runtime_context import DbRuntimeContext
from rem_card.app import operblock_startup_metrics
from rem_card.app.local_metrics import record_metric
from rem_card.app.logger import logger
from rem_card.app.sqlite_shared import run_integrity_check, run_quick_check
from rem_card.data.dto.lab_orders_dto import LAB_MATERIAL_LABELS, LabMaterial
from rem_card.data.dto.remcard_dto import DietTemplateDTO
from rem_card.data.settings.settings_db import SettingsDatabase, get_settings_database, reset_settings_database
from rem_card.data.settings.settings_schema import SEED_IMPORT_VERSION, now_text
from rem_card.services.operblock_icon_defaults import (
    MAX_OPERBLOCK_ICON_BLOB_BYTES,
    SEEDED_CUSTOM_ICON_DEFINITIONS,
    SUPPORTED_OPERBLOCK_ICON_EXTENSIONS,
)
from rem_card.services.remcard_icon_defaults import REMCARD_ICON_DEFINITIONS
from rem_card.services.shift_service import ShiftService


DRUG_CATALOG_KEY = "drug_catalog"
ORDER_TEMPLATES_KEY = "order_templates"
LAB_ANALYSIS_KEY = "lab_analysis"
DIET_TEMPLATES_KEY = "diet_templates"
DOCTORS_KEY = "doctors"
PRINT_SETTINGS_KEY = "print_settings"
DISPLAY_SETTINGS_KEY = "display_settings"
BACKGROUND_SETTINGS_KEY = "background_settings"
STYLE_SETTINGS_KEY = "style_settings"
EMERGENCY_PASSWORD_KEY = "emergency_password"
EMERGENCY_PASSWORD_CATALOG_KEY = "emergency_password"
OPERBLOCK_ICONS_KEY = "operblock_icons"
OPERBLOCK_SETTINGS_KEY = "operblock_settings"
OPERBLOCK_SETTINGS_SCOPE = "operblock"
OPERBLOCK_GROUP_ROUTES_APP_KEY = "group_routes"
OPERBLOCK_TEAM_APP_KEY = "team"
OPERBLOCK_ANESTHESIA_TYPES_APP_KEY = "anesthesia_types"
OPERBLOCK_QUICK_ORDER_BUTTONS_APP_KEY = "quick_order_buttons"
OPERBLOCK_QUICK_ORDERS_APP_KEY = "quick_orders"
OPERBLOCK_MEDICATION_PRESETS_APP_KEY = "medication_presets"
OPERBLOCK_APP_SETTING_KEYS = (
    OPERBLOCK_GROUP_ROUTES_APP_KEY,
    OPERBLOCK_TEAM_APP_KEY,
    OPERBLOCK_ANESTHESIA_TYPES_APP_KEY,
    OPERBLOCK_QUICK_ORDER_BUTTONS_APP_KEY,
    OPERBLOCK_QUICK_ORDERS_APP_KEY,
    OPERBLOCK_MEDICATION_PRESETS_APP_KEY,
)
OPERBLOCK_LEGACY_IMPORT_APP_SETTING_KEYS = (
    OPERBLOCK_TEAM_APP_KEY,
    OPERBLOCK_ANESTHESIA_TYPES_APP_KEY,
    OPERBLOCK_QUICK_ORDER_BUTTONS_APP_KEY,
    OPERBLOCK_QUICK_ORDERS_APP_KEY,
    OPERBLOCK_MEDICATION_PRESETS_APP_KEY,
)
DEFAULT_EMERGENCY_PASSWORD = "2u1x8dxgeD"
MIN_EMERGENCY_PASSWORD_LENGTH = 6
MAX_BACKGROUND_IMAGE_BLOB_BYTES = 32 * 1024 * 1024
PROCESS_SOURCE_CLIENT_ID = f"settings:{os.getpid()}:{uuid.uuid4().hex}"
LEGACY_PRESCRIPTION_OVERRIDE_IMPORT_META_KEY = "prescription_legacy_override_import_hash"
OPERBLOCK_ICONS_SEED_META_VERSION_KEY = "operblock_icons_seed_version"
OPERBLOCK_ICONS_SEED_META_HASH_KEY = "operblock_icons_seed_hash"
OPERBLOCK_ICONS_SEED_VERSION = "seeded_custom_icons_v2"
SEEDED_ICON_DEFINITIONS = (
    *SEEDED_CUSTOM_ICON_DEFINITIONS,
    *REMCARD_ICON_DEFINITIONS,
)


@dataclass(frozen=True)
class SettingsSnapshot:
    catalog_key: str
    version: int
    content_hash: str
    generated_at: str
    items: tuple[Any, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class DrugCatalogSnapshot(SettingsSnapshot):
    groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    forms: dict[str, dict[str, Any]] = field(default_factory=dict)
    routes: dict[str, dict[str, Any]] = field(default_factory=dict)
    solvents: dict[str, dict[str, Any]] = field(default_factory=dict)
    drugs: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class OrderTemplatesSnapshot(SettingsSnapshot):
    templates: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class LabAnalysisCatalogSnapshot(SettingsSnapshot):
    materials: tuple[dict[str, Any], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class DietTemplatesSnapshot(SettingsSnapshot):
    pass


@dataclass(frozen=True)
class DoctorsCatalogSnapshot(SettingsSnapshot):
    pass


@dataclass(frozen=True)
class PrintSettingsSnapshot(SettingsSnapshot):
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DisplaySettingsSnapshot(SettingsSnapshot):
    payload: dict[str, Any] = field(default_factory=dict)


CATALOG_TABLES: dict[str, tuple[tuple[str, str], ...]] = {
    DRUG_CATALOG_KEY: (
        ("drug_groups", "code"),
        ("dosage_forms", "code"),
        ("administration_routes", "code"),
        ("solvents", "code"),
        ("drugs", "code"),
    ),
    ORDER_TEMPLATES_KEY: (("order_templates", "template_key"),),
    LAB_ANALYSIS_KEY: (("lab_analysis_templates", "analysis_code"), ("app_settings", "key")),
    DIET_TEMPLATES_KEY: (("diet_templates", "template_key"),),
    DOCTORS_KEY: (("doctors", "id"),),
    PRINT_SETTINGS_KEY: (("print_templates", "template_key"), ("app_settings", "key")),
    DISPLAY_SETTINGS_KEY: (("app_settings", "key"),),
    BACKGROUND_SETTINGS_KEY: (("ui_backgrounds", "background_key"), ("app_settings", "key")),
    STYLE_SETTINGS_KEY: (("app_settings", "key"),),
    EMERGENCY_PASSWORD_CATALOG_KEY: (("app_settings", "key"),),
    OPERBLOCK_ICONS_KEY: (("operblock_icons", "icon_key"),),
    OPERBLOCK_SETTINGS_KEY: (("app_settings", "key"),),
}

APP_SETTINGS_HASH_KEYS: dict[str, tuple[str, ...]] = {
    LAB_ANALYSIS_KEY: ("lab_materials",),
    PRINT_SETTINGS_KEY: ("print_config",),
    DISPLAY_SETTINGS_KEY: ("display_settings", "lab_orders_columns"),
    BACKGROUND_SETTINGS_KEY: ("background_settings",),
    STYLE_SETTINGS_KEY: ("style_settings",),
    EMERGENCY_PASSWORD_CATALOG_KEY: (EMERGENCY_PASSWORD_KEY,),
    OPERBLOCK_SETTINGS_KEY: OPERBLOCK_APP_SETTING_KEYS,
}

PRESCRIPTION_DATASET_TABLES = {
    "drugs": "drugs",
    "groups": "drug_groups",
    "forms": "dosage_forms",
    "admin_types": "administration_routes",
    "diluents": "solvents",
    "templates": "order_templates",
}
PRESCRIPTION_SEED_SOURCES = {
    "groups": "groups.seed.json",
    "forms": "forms.seed.json",
    "admin_types": "admin_types.seed.json",
    "diluents": "diluents.seed.json",
    "drugs": "drugs.seed.json",
    "templates": "templates.seed.json",
}


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash_value(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _load_json_file(path: str) -> Any:
    if not path or not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _read_json_dict(path: str, *, warnings: list[str]) -> dict[str, Any]:
    try:
        payload = _load_json_file(path)
    except json.JSONDecodeError as exc:
        warnings.append(f"{path}: JSON поврежден ({exc})")
        return {}
    except OSError as exc:
        warnings.append(f"{path}: недоступен ({exc})")
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_optional_json_dict(path: str, *, warnings: list[str]) -> dict[str, Any]:
    if not path or not os.path.exists(path):
        return {}
    return _read_json_dict(path, warnings=warnings)


def _read_json_any(path: str, *, warnings: list[str]) -> Any:
    try:
        return _load_json_file(path)
    except json.JSONDecodeError as exc:
        warnings.append(f"{path}: JSON поврежден ({exc})")
        return None
    except OSError as exc:
        warnings.append(f"{path}: недоступен ({exc})")
        return None


def _first_existing_path(*paths: str) -> str:
    for path in paths:
        if path and os.path.exists(path):
            return path
    return next((path for path in paths if path), "")


def _payload_dict(raw: Any) -> dict[str, Any]:
    return dict(raw) if isinstance(raw, dict) else {}


def _enabled_from_int(value: Any) -> int:
    try:
        return 1 if int(value) else 0
    except Exception:
        return 1 if bool(value) else 0


def _normalize_bool_int(value: Any) -> int:
    if isinstance(value, str):
        return 1 if value.strip().lower() in {"1", "true", "yes", "y", "да"} else 0
    return 1 if bool(value) else 0


def _slug(value: Any, fallback: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9_]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text or fallback


def _normalize_lab_times(values: Any) -> list[str]:
    if values is None or values == "":
        raw_values: list[Any] = []
    elif isinstance(values, str):
        raw_values = [item.strip() for item in values.split(",")]
    else:
        raw_values = list(values or [])
    result: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        time_text = str(raw or "").strip()
        if not time_text:
            continue
        if not ShiftService.is_time_input_valid(time_text):
            raise ValueError("Время анализа должно быть в формате HH:mm")
        normalized = ShiftService.normalize_time(time_text)
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    result.sort(key=lambda item: ((int(item[:2]) - 8) % 24, int(item[3:5])))
    return result


def _normalize_lab_material(value: Any, labels: dict[str, str] | None = None) -> str:
    text = str(value or "").strip()
    raw = text.lower()
    if not raw:
        return LabMaterial.VENOUS_BLOOD.value
    material_labels = dict(LAB_MATERIAL_LABELS)
    material_labels.update(labels or {})
    aliases = {
        LabMaterial.VENOUS_BLOOD.value: LabMaterial.VENOUS_BLOOD.value,
        "venous": LabMaterial.VENOUS_BLOOD.value,
        "кровь венозная": LabMaterial.VENOUS_BLOOD.value,
        "венозная кровь": LabMaterial.VENOUS_BLOOD.value,
        LabMaterial.ARTERIAL_BLOOD.value: LabMaterial.ARTERIAL_BLOOD.value,
        "arterial": LabMaterial.ARTERIAL_BLOOD.value,
        "кровь артериальная": LabMaterial.ARTERIAL_BLOOD.value,
        "артериальная кровь": LabMaterial.ARTERIAL_BLOOD.value,
        LabMaterial.URINE.value: LabMaterial.URINE.value,
        "моча": LabMaterial.URINE.value,
        LabMaterial.LIQUOR.value: LabMaterial.LIQUOR.value,
        "csf": LabMaterial.LIQUOR.value,
        "ликвор": LabMaterial.LIQUOR.value,
    }
    for code, label in material_labels.items():
        aliases[str(code or "").strip().lower()] = str(code)
        aliases[str(label or "").strip().lower()] = str(code)
    return aliases.get(raw, text)


def _normalize_diet_schedule(schedule: Any) -> str:
    if schedule is None or schedule == "":
        raw_items = []
    elif isinstance(schedule, str):
        raw_items = json.loads(schedule)
    else:
        raw_items = schedule
    if not isinstance(raw_items, list):
        raise ValueError("Расписание питания должно быть списком")
    normalized = []
    seen_times = set()
    for item in raw_items:
        if not isinstance(item, dict):
            raise ValueError("Строка расписания питания должна быть объектом")
        time_text = str(item.get("time") or "").strip()
        if not ShiftService.is_time_input_valid(time_text):
            raise ValueError("Время питания должно быть в формате HH:mm")
        normalized_time = ShiftService.normalize_time(time_text)
        if normalized_time in seen_times:
            raise ValueError("В расписании питания не должно быть повторяющихся времен")
        seen_times.add(normalized_time)
        amount = int(float(item.get("amount") or 0))
        if amount <= 0:
            raise ValueError("Объем питания должен быть больше 0 мл")
        normalized.append({"time": normalized_time, "amount": amount})
    normalized.sort(key=lambda item: ((int(item["time"][:2]) - 8) % 24, int(item["time"][3:5])))
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


class SettingsService:
    def __init__(
        self,
        db: SettingsDatabase | None = None,
        *,
        context: DbRuntimeContext | None = None,
        runtime_context: DbRuntimeContext | None = None,
        settings_db_path: str | None = None,
        settings_db_lock_path: str | None = None,
        settings_backups_dir: str | None = None,
        settings_backup_health_dir: str | None = None,
        readonly: bool | None = None,
    ):
        self.db = db or get_settings_database(
            context=context,
            runtime_context=runtime_context,
            settings_db_path=settings_db_path,
            settings_db_lock_path=settings_db_lock_path,
            settings_backups_dir=settings_backups_dir,
            settings_backup_health_dir=settings_backup_health_dir,
            readonly=readonly,
        )
        self._ready_lock = threading.RLock()
        self._ready = False
        self._ready_info: dict[str, Any] = {}
        self._import_report: dict[str, Any] = {}
        self._snapshot_cache: dict[str, tuple[int, str, Any]] = {}
        self._last_seen_settings_change_id = 0
        self.source_client_id = PROCESS_SOURCE_CLIENT_ID

    def ensure_ready(self) -> dict[str, Any]:
        with self._ready_lock:
            if self._ready:
                return dict(self._ready_info)
            info = self.db.ensure_ready()
            if self.db.settings_readonly:
                self._ready = True
                self._ready_info = dict(info)
                return dict(info)
            self._ensure_legacy_import()
            self._ensure_operblock_settings_imported()
            release_report = self._apply_bundled_release_snapshot_if_needed()
            if release_report:
                info = {**info, "settings_release_snapshot": release_report}
            background_repair_report = self._repair_background_settings_from_rows()
            if background_repair_report:
                info = {**info, "background_settings_repair": background_repair_report}
            self._ensure_default_operblock_icons()
            self._ready = True
            self._ready_info = dict(info)
            return dict(info)

    def invalidate_cache(self, catalog_key: str | None = None) -> None:
        if catalog_key:
            self._snapshot_cache.pop(catalog_key, None)
            return
        self._snapshot_cache.clear()

    def get_import_report(self) -> dict[str, Any]:
        self.ensure_ready()
        if self._import_report:
            return dict(self._import_report)
        return self.get_app_setting("shared", "settings_import_report", default={}) or {}

    def _ensure_legacy_import(self) -> None:
        seed_already_imported = False
        with self.db.read_connection() as conn:
            row = conn.execute(
                "SELECT value FROM settings_meta WHERE key = 'seed_import_version'"
            ).fetchone()
            if row and str(row[0]) == SEED_IMPORT_VERSION:
                seed_already_imported = True
            if seed_already_imported:
                existing_rows = 0
            else:
                existing_rows = 0
                for table, _key in (
                    ("drugs", "code"),
                    ("drug_groups", "code"),
                    ("lab_analysis_templates", "analysis_code"),
                    ("diet_templates", "template_key"),
                ):
                    existing_rows += int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] or 0)

        if seed_already_imported:
            self._ensure_legacy_prescription_overrides_imported()
            return

        with self.db.transaction("settings_legacy_import") as cursor:
            report = self._import_legacy_sources(cursor) if existing_rows == 0 else {
                "skipped": True,
                "reason": "settings DB уже содержит данные",
            }
            self._set_meta(cursor, "seed_import_version", SEED_IMPORT_VERSION)
            self._set_meta(cursor, "last_import_report_json", _stable_json(report))
            self._write_app_setting_in_tx(
                cursor,
                "shared",
                "settings_import_report",
                report,
                changed_by_role="system",
                catalog_key=DISPLAY_SETTINGS_KEY,
                log_change=False,
            )
            self._write_app_setting_in_tx(
                cursor,
                "shared",
                EMERGENCY_PASSWORD_KEY,
                DEFAULT_EMERGENCY_PASSWORD,
                changed_by_role="system",
                catalog_key=EMERGENCY_PASSWORD_CATALOG_KEY,
                log_change=False,
            )
            for catalog_key in (
                DRUG_CATALOG_KEY,
                ORDER_TEMPLATES_KEY,
                LAB_ANALYSIS_KEY,
                DIET_TEMPLATES_KEY,
                DOCTORS_KEY,
                PRINT_SETTINGS_KEY,
                DISPLAY_SETTINGS_KEY,
                BACKGROUND_SETTINGS_KEY,
                OPERBLOCK_ICONS_KEY,
                OPERBLOCK_SETTINGS_KEY,
                STYLE_SETTINGS_KEY,
                EMERGENCY_PASSWORD_CATALOG_KEY,
            ):
                self._bump_catalog_version(
                    cursor,
                    catalog_key,
                    entity_type=catalog_key,
                    entity_id=None,
                    operation="import",
                    changed_by_role="system",
                    before=None,
                    after=report if catalog_key == DRUG_CATALOG_KEY else None,
                )
            self._import_report = report

        with self.db.read_connection() as conn:
            ok, reason = run_quick_check(conn)
            if not ok:
                raise RuntimeError(f"settings DB quick_check after import failed: {reason}")
            ok, reason = run_integrity_check(conn)
            if not ok:
                raise RuntimeError(f"settings DB integrity_check after import failed: {reason}")
        self._ensure_legacy_prescription_overrides_imported()

    def _legacy_operblock_settings_payloads(self) -> dict[str, Any]:
        from rem_card.app import paths as app_paths
        from rem_card.services.operblock_anesthesia_types import (
            OPERBLOCK_ANESTHESIA_TYPES_VERSION,
            load_operblock_anesthesia_types,
            normalize_operblock_anesthesia_types_payload,
        )
        from rem_card.services.operblock_medication_presets import (
            OPERBLOCK_MEDICATION_PRESETS_VERSION,
            load_operblock_medication_presets,
            normalize_operblock_medication_presets_payload,
        )
        from rem_card.services.operblock_quick_order_buttons import (
            OPERBLOCK_QUICK_ORDER_BUTTONS_VERSION,
            load_operblock_quick_order_buttons,
            normalize_operblock_quick_order_buttons_payload,
        )
        from rem_card.services.operblock_quick_orders import (
            OPERBLOCK_QUICK_ORDERS_VERSION,
            load_operblock_quick_orders,
            normalize_operblock_quick_orders_payload,
        )
        from rem_card.services.operblock_team import (
            OPERBLOCK_TEAM_VERSION,
            load_operblock_team,
            normalize_operblock_team_payload,
        )

        seed_dir = str(getattr(app_paths, "SEED_DIR", "") or "")
        user_dir = self._legacy_user_dict_dir(app_paths)

        def safe_payload(key: str, builder, fallback: dict[str, Any]) -> dict[str, Any]:
            try:
                payload = builder()
                return payload if isinstance(payload, dict) else dict(fallback)
            except Exception as exc:
                logger.warning("Legacy operblock settings import skipped for %s: %s", key, exc)
                return dict(fallback)

        return {
            OPERBLOCK_TEAM_APP_KEY: safe_payload(
                OPERBLOCK_TEAM_APP_KEY,
                lambda: normalize_operblock_team_payload(
                    {
                        "version": OPERBLOCK_TEAM_VERSION,
                        "items": load_operblock_team(user_dict_dir=user_dir),
                    }
                ),
                {"version": OPERBLOCK_TEAM_VERSION, "items": []},
            ),
            OPERBLOCK_ANESTHESIA_TYPES_APP_KEY: safe_payload(
                OPERBLOCK_ANESTHESIA_TYPES_APP_KEY,
                lambda: normalize_operblock_anesthesia_types_payload(
                    {
                        "version": OPERBLOCK_ANESTHESIA_TYPES_VERSION,
                        "items": load_operblock_anesthesia_types(user_dict_dir=user_dir),
                    }
                ),
                {"version": OPERBLOCK_ANESTHESIA_TYPES_VERSION, "items": []},
            ),
            OPERBLOCK_QUICK_ORDER_BUTTONS_APP_KEY: safe_payload(
                OPERBLOCK_QUICK_ORDER_BUTTONS_APP_KEY,
                lambda: normalize_operblock_quick_order_buttons_payload(
                    {
                        "version": OPERBLOCK_QUICK_ORDER_BUTTONS_VERSION,
                        "items": load_operblock_quick_order_buttons(user_dict_dir=user_dir),
                    }
                ),
                {"version": OPERBLOCK_QUICK_ORDER_BUTTONS_VERSION, "items": []},
            ),
            OPERBLOCK_QUICK_ORDERS_APP_KEY: safe_payload(
                OPERBLOCK_QUICK_ORDERS_APP_KEY,
                lambda: normalize_operblock_quick_orders_payload(
                    {
                        "version": OPERBLOCK_QUICK_ORDERS_VERSION,
                        "items": load_operblock_quick_orders(seed_dir=seed_dir, user_dict_dir=user_dir),
                    }
                ),
                {"version": OPERBLOCK_QUICK_ORDERS_VERSION, "items": []},
            ),
            OPERBLOCK_MEDICATION_PRESETS_APP_KEY: safe_payload(
                OPERBLOCK_MEDICATION_PRESETS_APP_KEY,
                lambda: normalize_operblock_medication_presets_payload(
                    {
                        "version": OPERBLOCK_MEDICATION_PRESETS_VERSION,
                        "items": load_operblock_medication_presets(
                            seed_dir=seed_dir,
                            user_dict_dir=user_dir,
                            include_disabled=True,
                        ),
                    }
                ),
                {"version": OPERBLOCK_MEDICATION_PRESETS_VERSION, "items": []},
            ),
        }

    def _ensure_operblock_settings_imported(self) -> None:
        with self.db.read_connection() as conn:
            placeholders = ",".join("?" for _ in OPERBLOCK_LEGACY_IMPORT_APP_SETTING_KEYS)
            rows = conn.execute(
                f"""
                SELECT key
                FROM app_settings
                WHERE scope = ? AND key IN ({placeholders})
                """,
                (OPERBLOCK_SETTINGS_SCOPE, *OPERBLOCK_LEGACY_IMPORT_APP_SETTING_KEYS),
            ).fetchall()
            existing_keys = {str(row["key"]) for row in rows}
        missing_keys = [key for key in OPERBLOCK_LEGACY_IMPORT_APP_SETTING_KEYS if key not in existing_keys]
        if not missing_keys:
            return
        payloads = self._legacy_operblock_settings_payloads()
        if not payloads:
            return

        changed: dict[str, Any] = {}
        with self.db.transaction("settings_operblock_legacy_import") as cursor:
            for key in missing_keys:
                if self._select_app_setting(cursor, OPERBLOCK_SETTINGS_SCOPE, key) is not None:
                    continue
                payload = payloads[key]
                self._write_app_setting_in_tx(
                    cursor,
                    OPERBLOCK_SETTINGS_SCOPE,
                    key,
                    payload,
                    changed_by_role="system",
                    catalog_key=OPERBLOCK_SETTINGS_KEY,
                    log_change=False,
                )
                changed[key] = payload
            if changed:
                self._bump_catalog_version(
                    cursor,
                    OPERBLOCK_SETTINGS_KEY,
                    "operblock_settings",
                    None,
                    "import",
                    changed_by_role="system",
                    before=None,
                    after=changed,
                )

    def _apply_bundled_release_snapshot_if_needed(self) -> dict[str, Any]:
        from rem_card.app.runtime_paths import is_compiled
        from rem_card.data.settings.settings_release import (
            apply_settings_release_snapshot,
            find_release_snapshot_path,
        )

        should_apply = is_compiled() or os.environ.get("REMCARD_APPLY_SETTINGS_RELEASE_SNAPSHOT") == "1"
        if not should_apply:
            return {}
        snapshot_path = find_release_snapshot_path()
        if not snapshot_path:
            return {}
        try:
            report = apply_settings_release_snapshot(
                self.db,
                snapshot_path,
                bump_catalog_version=self._bump_catalog_version,
            )
        except Exception as exc:
            raise RuntimeError(f"Не удалось применить пакет обновления настроек: {exc}") from exc
        if report.get("applied"):
            self.invalidate_cache()
        logger.info("Settings release snapshot result: %s", report)
        return report

    def _repair_background_settings_from_rows(self) -> dict[str, Any] | None:
        try:
            from rem_card.ui.shared.background_settings import normalize_background_settings_payload
        except Exception:
            return None

        with self.db.read_connection() as conn:
            app_row = conn.execute(
                "SELECT * FROM app_settings WHERE scope = 'shared' AND key = 'background_settings'"
            ).fetchone()
            background_rows = conn.execute(
                """
                SELECT background_key, value_json
                FROM ui_backgrounds
                WHERE enabled = 1
                ORDER BY active DESC, background_key ASC
                """
            ).fetchall()

        current_payload: dict[str, Any] = {}
        if app_row and app_row["value_json"]:
            try:
                raw_payload = json.loads(app_row["value_json"])
                current_payload = raw_payload if isinstance(raw_payload, dict) else {}
            except Exception:
                current_payload = {}
        current_normalized = normalize_background_settings_payload(current_payload)
        current_backgrounds = list(current_normalized.get("backgrounds") or [])
        current_ids = {str(item.get("id") or "") for item in current_backgrounds if isinstance(item, dict)}

        restored_entries: list[dict[str, Any]] = []
        for row in background_rows:
            background_key = str(row["background_key"] or "").strip()
            if not background_key or background_key in current_ids:
                continue
            try:
                value = json.loads(row["value_json"] or "{}")
            except Exception:
                value = {}
            if not isinstance(value, dict):
                continue
            entry = dict(value)
            entry["id"] = str(entry.get("id") or background_key)
            if entry["id"] in current_ids:
                continue
            restored_entries.append(entry)
            current_ids.add(entry["id"])

        if not restored_entries:
            return None

        repaired_payload = normalize_background_settings_payload({"backgrounds": [*current_backgrounds, *restored_entries]})
        with self.db.transaction("settings_background_settings_repair") as cursor:
            before = self._select_app_setting(cursor, "shared", "background_settings")
            self._write_app_setting_in_tx(
                cursor,
                "shared",
                "background_settings",
                repaired_payload,
                changed_by_role="repair",
                catalog_key=BACKGROUND_SETTINGS_KEY,
                log_change=False,
            )
            self._sync_background_rows_in_tx(cursor, repaired_payload)
            self._bump_catalog_version(
                cursor,
                BACKGROUND_SETTINGS_KEY,
                "background_settings",
                "shared:background_settings",
                "repair_missing_rows",
                changed_by_role="repair",
                before=before,
                after=repaired_payload,
            )
        return {
            "repaired": True,
            "restored_rows": len(restored_entries),
            "restored_ids": [str(item.get("id") or "") for item in restored_entries],
        }

    def _import_legacy_sources(self, cursor) -> dict[str, Any]:
        started = time.perf_counter()
        warnings: list[str] = []
        from rem_card.app import paths as app_paths

        seed_dir = app_paths.SEED_DIR
        user_dir = self._legacy_user_dict_dir(app_paths)
        package_root = os.path.abspath(os.path.join(os.path.dirname(app_paths.__file__), ".."))
        bundled_settings_dir = os.path.join(app_paths.get_resources_dir(), "rem_card", "settings")
        package_settings_dir = os.path.join(package_root, "settings")

        def legacy_dict(file_name: str) -> dict[str, Any]:
            return _read_json_dict(
                _first_existing_path(os.path.join(user_dir, file_name), os.path.join(seed_dir, file_name)),
                warnings=warnings,
            )

        def legacy_any(file_name: str) -> Any:
            return _read_json_any(
                _first_existing_path(os.path.join(user_dir, file_name), os.path.join(seed_dir, file_name)),
                warnings=warnings,
            )

        def settings_path(*parts: str) -> str:
            return _first_existing_path(
                os.path.join(bundled_settings_dir, *parts),
                os.path.join(package_settings_dir, *parts),
            )

        overrides_path = self._legacy_user_overrides_path(app_paths)
        overrides = _read_optional_json_dict(overrides_path, warnings=warnings)

        counts: dict[str, int] = {}
        merged_by_name: dict[str, dict[str, tuple[dict[str, Any], int, str]]] = {}
        for name, file_name in PRESCRIPTION_SEED_SOURCES.items():
            seed = _read_json_dict(os.path.join(seed_dir, file_name), warnings=warnings)
            merged_by_name[name] = self._merged_legacy_catalog(seed, overrides.get(name), source_name=file_name)

        counts["groups"] = self._import_groups(cursor, merged_by_name["groups"])
        counts["dosage_forms"] = self._import_forms(cursor, merged_by_name["forms"])
        counts["routes"] = self._import_routes(cursor, merged_by_name["admin_types"])
        counts["solvents"] = self._import_solvents(cursor, merged_by_name["diluents"])
        counts["drugs"] = self._import_drugs(cursor, merged_by_name["drugs"])
        counts["order_templates"] = self._import_order_templates(
            cursor,
            merged_by_name["templates"],
            overrides.get("template_order"),
        )
        counts["doctors"] = self._import_doctors(
            cursor,
            legacy_any("death_protocol_doctors.json"),
        )
        counts["diet_templates"] = self._import_diet_templates(
            cursor,
            legacy_dict("diet_templates.json"),
        )
        counts["lab_templates"] = self._import_lab_templates(
            cursor,
            legacy_dict("lab_analysis_templates.json"),
        )
        counts["display_settings"] = self._import_json_app_setting(
            cursor,
            "shared",
            "display_settings",
            settings_path("display_settings", "display_settings.json"),
            warnings=warnings,
            catalog_key=DISPLAY_SETTINGS_KEY,
        )
        counts["background_settings"] = self._import_background_settings(
            cursor,
            settings_path("display_settings", "background_settings.json"),
            warnings=warnings,
        )
        counts["lab_columns_settings"] = self._import_json_app_setting(
            cursor,
            "shared",
            "lab_orders_columns",
            settings_path("display_settings", "lab_orders_columns.json"),
            warnings=warnings,
            catalog_key=DISPLAY_SETTINGS_KEY,
        )
        counts["style_settings"] = self._import_json_app_setting(
            cursor,
            "shared",
            "style_settings",
            settings_path("color_scheme", "style_settings.json"),
            warnings=warnings,
            catalog_key=STYLE_SETTINGS_KEY,
        )
        counts["print_settings"] = self._import_default_print_settings(cursor)
        report = {
            "source_seed_dir": seed_dir,
            "source_user_dir": user_dir,
            "source_user_overrides_path": overrides_path,
            "counts": counts,
            "warnings": warnings,
            "imported_at": now_text(),
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
        }
        if overrides:
            self._set_meta(cursor, LEGACY_PRESCRIPTION_OVERRIDE_IMPORT_META_KEY, _hash_value(overrides))
        record_metric("settings_import_ms", report["elapsed_ms"], force_flush=True)
        logger.info("Settings legacy import report: %s", report)
        return report

    @staticmethod
    def _is_external_dictionary_dir(app_paths: Any, path: str) -> bool:
        try:
            external_dir = os.path.join(app_paths.get_executable_dir(), "rem_card", "data", "dictionaries")
        except Exception:
            return False
        try:
            return os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(external_dir))
        except Exception:
            return False

    def _legacy_user_dict_dir(self, app_paths: Any) -> str:
        seed_dir = str(getattr(app_paths, "SEED_DIR", "") or "")
        user_dir = str(getattr(app_paths, "USER_DICT_DIR", "") or "")
        if user_dir and not self._is_external_dictionary_dir(app_paths, user_dir):
            return user_dir
        return seed_dir

    @staticmethod
    def _legacy_user_overrides_path(app_paths: Any) -> str:
        seed_dir = str(getattr(app_paths, "SEED_DIR", "") or "")
        user_dir = str(getattr(app_paths, "USER_DICT_DIR", "") or "")
        if SettingsService._is_external_dictionary_dir(app_paths, user_dir):
            user_dir = seed_dir
        candidates = [
            os.path.join(user_dir, "user_overrides.json"),
            os.path.join(seed_dir, "user_overrides.json"),
        ]
        baza_dir = str(getattr(app_paths, "BAZA_DIR", "") or "")
        if baza_dir:
            candidates.append(os.path.join(baza_dir, "rem_card", "data", "dictionaries", "user_overrides.json"))
        for candidate in candidates:
            if candidate and os.path.exists(candidate):
                return candidate
        return ""

    def _ensure_legacy_prescription_overrides_imported(self) -> None:
        warnings: list[str] = []
        from rem_card.app import paths as app_paths

        overrides_path = self._legacy_user_overrides_path(app_paths)
        overrides = _read_optional_json_dict(overrides_path, warnings=warnings)
        if not overrides:
            return
        overrides_hash = _hash_value(overrides)
        with self.db.read_connection() as conn:
            row = conn.execute(
                "SELECT value FROM settings_meta WHERE key = ?",
                (LEGACY_PRESCRIPTION_OVERRIDE_IMPORT_META_KEY,),
            ).fetchone()
            if row:
                return
            if not self._legacy_override_repair_needed(conn):
                return

        with self.db.transaction("settings_legacy_prescription_overrides") as cursor:
            changed = self._apply_legacy_prescription_overrides(
                cursor,
                overrides,
                seed_dir=app_paths.SEED_DIR,
                warnings=warnings,
            )
            self._set_meta(cursor, LEGACY_PRESCRIPTION_OVERRIDE_IMPORT_META_KEY, overrides_hash)
            self._set_meta(cursor, "last_legacy_prescription_overrides_path", overrides_path)
            if changed["drug_catalog"]:
                self._bump_catalog_version(
                    cursor,
                    DRUG_CATALOG_KEY,
                    entity_type="drug_catalog",
                    entity_id=None,
                    operation="legacy_override_import",
                    changed_by_role="system",
                    before=None,
                    after={
                        "source": overrides_path,
                        "changed_rows": changed["drug_catalog"],
                        "warnings": warnings,
                    },
                )
            if changed["order_templates"]:
                self._bump_catalog_version(
                    cursor,
                    ORDER_TEMPLATES_KEY,
                    entity_type="order_templates",
                    entity_id=None,
                    operation="legacy_override_import",
                    changed_by_role="system",
                    before=None,
                    after={
                        "source": overrides_path,
                        "changed_rows": changed["order_templates"],
                        "warnings": warnings,
                    },
                )

    @staticmethod
    def _legacy_override_repair_needed(conn) -> bool:
        for table in (
            "drug_groups",
            "dosage_forms",
            "administration_routes",
            "solvents",
            "drugs",
            "order_templates",
        ):
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS cnt
                FROM {table}
                WHERE COALESCE(source, '') IN ('override', 'manual')
                """
            ).fetchone()
            if int(row["cnt"] if row else 0) > 0:
                return False
        return True

    def _apply_legacy_prescription_overrides(
        self,
        cursor,
        overrides: dict[str, Any],
        *,
        seed_dir: str,
        warnings: list[str],
    ) -> dict[str, int]:
        changed = {"drug_catalog": 0, "order_templates": 0}
        merged_by_name: dict[str, dict[str, tuple[dict[str, Any], int, str]]] = {}
        for name, file_name in PRESCRIPTION_SEED_SOURCES.items():
            seed = _read_json_dict(os.path.join(seed_dir, file_name), warnings=warnings)
            merged_by_name[name] = self._merged_legacy_catalog(seed, overrides.get(name), source_name=file_name)

        changed["drug_catalog"] += self._apply_legacy_override_items(
            cursor,
            "drug_groups",
            "code",
            merged_by_name["groups"],
            self._upsert_group,
        )
        changed["drug_catalog"] += self._apply_legacy_override_items(
            cursor,
            "dosage_forms",
            "code",
            merged_by_name["forms"],
            self._upsert_form,
        )
        changed["drug_catalog"] += self._apply_legacy_override_items(
            cursor,
            "administration_routes",
            "code",
            merged_by_name["admin_types"],
            self._upsert_route,
        )
        changed["drug_catalog"] += self._apply_legacy_override_items(
            cursor,
            "solvents",
            "code",
            merged_by_name["diluents"],
            self._upsert_solvent,
        )
        changed["drug_catalog"] += self._apply_legacy_override_items(
            cursor,
            "drugs",
            "code",
            merged_by_name["drugs"],
            self._upsert_drug,
        )

        template_order = list(overrides.get("template_order")) if isinstance(overrides.get("template_order"), list) else []
        order_index = {str(key): idx + 1 for idx, key in enumerate(template_order)}
        ordered_templates: dict[str, tuple[dict[str, Any], int, str]] = {}
        for fallback_sort, (code, (payload, enabled, source)) in enumerate(merged_by_name["templates"].items(), start=1):
            if source != "override":
                continue
            sort_order = order_index.get(str(code), len(order_index) + fallback_sort)
            ordered_templates[code] = (payload, enabled, source)
            payload["_legacy_sort_order"] = sort_order
        changed["order_templates"] += self._apply_legacy_override_items(
            cursor,
            "order_templates",
            "template_key",
            ordered_templates,
            self._upsert_order_template,
            sort_order_from_payload="_legacy_sort_order",
        )
        return changed

    def _apply_legacy_override_items(
        self,
        cursor,
        table: str,
        key_column: str,
        items: dict[str, tuple[dict[str, Any], int, str]],
        upsert,
        *,
        sort_order_from_payload: str | None = None,
    ) -> int:
        changed = 0
        for fallback_sort_order, (code, (payload, enabled, source)) in enumerate(items.items(), start=1):
            if source != "override":
                continue
            row = self._select_by_key(cursor, table, key_column, code)
            if row and str(row.get("source") or "") == "manual":
                continue
            sort_order = int(payload.pop(sort_order_from_payload, fallback_sort_order)) if sort_order_from_payload else fallback_sort_order
            payload_json = _stable_json(payload)
            if row:
                row_payload = str(row.get("payload_json") or "")
                row_enabled = int(row.get("enabled") if row.get("enabled") is not None else 1)
                row_sort_order = int(row.get("sort_order") if row.get("sort_order") is not None else 0)
                row_source = str(row.get("source") or "")
                if (
                    row_payload == payload_json
                    and row_enabled == int(enabled)
                    and row_sort_order == int(sort_order)
                    and row_source == "override"
                ):
                    continue
            upsert(cursor, code, payload, enabled=enabled, sort_order=sort_order, source="override", bump=False)
            changed += 1
        return changed

    @staticmethod
    def _set_meta(cursor, key: str, value: str) -> None:
        cursor.execute(
            """
            INSERT INTO settings_meta (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, str(value), now_text()),
        )

    @staticmethod
    def _merged_legacy_catalog(
        seed: dict[str, Any],
        overrides_raw: Any,
        *,
        source_name: str,
    ) -> dict[str, tuple[dict[str, Any], int, str]]:
        overrides = overrides_raw if isinstance(overrides_raw, dict) else {}
        ordered_keys = list(seed.keys())
        for key in overrides:
            if key not in seed:
                ordered_keys.append(key)
        result: dict[str, tuple[dict[str, Any], int, str]] = {}
        for key in ordered_keys:
            seed_payload = _payload_dict(seed.get(key))
            override_payload = _payload_dict(overrides.get(key))
            if override_payload.get("_deleted"):
                payload = dict(seed_payload)
                payload["_deleted"] = True
                result[str(key)] = (payload, 0, "override")
                continue
            if key in overrides and override_payload:
                result[str(key)] = (dict(override_payload), 1, "override")
            else:
                result[str(key)] = (dict(seed_payload), 1, f"seed:{source_name}")
        return result

    def _import_groups(self, cursor, items: dict[str, tuple[dict[str, Any], int, str]]) -> int:
        count = 0
        for sort_order, (code, (payload, enabled, source)) in enumerate(items.items(), start=1):
            self._upsert_group(cursor, code, payload, enabled=enabled, sort_order=sort_order, source=source, bump=False)
            count += 1
        return count

    def _import_forms(self, cursor, items: dict[str, tuple[dict[str, Any], int, str]]) -> int:
        count = 0
        for sort_order, (code, (payload, enabled, source)) in enumerate(items.items(), start=1):
            self._upsert_form(cursor, code, payload, enabled=enabled, sort_order=sort_order, source=source, bump=False)
            count += 1
        return count

    def _import_routes(self, cursor, items: dict[str, tuple[dict[str, Any], int, str]]) -> int:
        count = 0
        for sort_order, (code, (payload, enabled, source)) in enumerate(items.items(), start=1):
            self._upsert_route(cursor, code, payload, enabled=enabled, sort_order=sort_order, source=source, bump=False)
            count += 1
        return count

    def _import_solvents(self, cursor, items: dict[str, tuple[dict[str, Any], int, str]]) -> int:
        count = 0
        for sort_order, (code, (payload, enabled, source)) in enumerate(items.items(), start=1):
            self._upsert_solvent(cursor, code, payload, enabled=enabled, sort_order=sort_order, source=source, bump=False)
            count += 1
        return count

    def _import_drugs(self, cursor, items: dict[str, tuple[dict[str, Any], int, str]]) -> int:
        count = 0
        for sort_order, (code, (payload, enabled, source)) in enumerate(items.items(), start=1):
            self._upsert_drug(cursor, code, payload, enabled=enabled, sort_order=sort_order, source=source, bump=False)
            count += 1
        return count

    def _import_order_templates(
        self,
        cursor,
        items: dict[str, tuple[dict[str, Any], int, str]],
        order_keys: Any,
    ) -> int:
        ordered = list(order_keys) if isinstance(order_keys, list) else []
        order_index = {str(key): idx + 1 for idx, key in enumerate(ordered)}
        count = 0
        for fallback_sort, (code, (payload, enabled, source)) in enumerate(items.items(), start=1):
            sort_order = order_index.get(str(code), len(order_index) + fallback_sort)
            self._upsert_order_template(
                cursor,
                code,
                payload,
                enabled=enabled,
                sort_order=sort_order,
                source=source,
                bump=False,
            )
            count += 1
        return count

    def _import_doctors(self, cursor, payload: Any) -> int:
        raw = payload.get("doctors", payload.get("items", [])) if isinstance(payload, dict) else payload
        if not isinstance(raw, list):
            raw = []
        seen = set()
        count = 0
        for sort_order, item in enumerate(raw, start=1):
            if isinstance(item, dict):
                full_name = " ".join(str(
                    item.get("full_name")
                    or item.get("name")
                    or item.get("doctor")
                    or item.get("fio")
                    or ""
                ).split())
                position = " ".join(str(item.get("position") or "").split())
            else:
                full_name = " ".join(str(item or "").split())
                position = ""
            key = full_name.lower()
            if not full_name or key in seen:
                continue
            seen.add(key)
            code = _slug(full_name, f"doctor_{sort_order}")
            cursor.execute(
                """
                INSERT INTO doctors (
                    code, full_name, short_name, position, department, signature_text,
                    enabled, sort_order, revision, payload_json, source, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, '', ?, 1, ?, 1, ?, 'legacy_json', ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    full_name = excluded.full_name,
                    position = excluded.position,
                    signature_text = excluded.signature_text,
                    sort_order = excluded.sort_order,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    code,
                    full_name,
                    full_name,
                    position,
                    full_name,
                    sort_order,
                    _stable_json({"full_name": full_name, "position": position}),
                    now_text(),
                    now_text(),
                ),
            )
            count += 1
        return count

    def _import_diet_templates(self, cursor, payload: dict[str, Any]) -> int:
        raw_templates = payload.get("templates", []) if isinstance(payload, dict) else []
        items: list[tuple[Any, dict[str, Any]]] = []
        if isinstance(raw_templates, dict):
            items = [(key, dict(value)) for key, value in raw_templates.items() if isinstance(value, dict)]
        elif isinstance(raw_templates, list):
            items = [(idx, dict(value)) for idx, value in enumerate(raw_templates, start=1) if isinstance(value, dict)]
        count = 0
        for sort_order, (fallback_key, raw) in enumerate(items, start=1):
            if raw.get("_deleted"):
                enabled = 0
            else:
                enabled = 1
            name = str(raw.get("name") or "").strip()
            if not name:
                continue
            schedule_json = _normalize_diet_schedule(raw.get("schedule", raw.get("schedule_json", [])))
            full_payload = dict(raw)
            full_payload["schedule_json"] = schedule_json
            key = str(raw.get("template_key") or raw.get("code") or raw.get("id") or fallback_key)
            template_key = _slug(key or name, f"diet_{sort_order}")
            cursor.execute(
                """
                INSERT INTO diet_templates (
                    template_key, name, display_name, description, payload_json,
                    enabled, sort_order, revision, source, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'legacy_json', ?, ?)
                ON CONFLICT(template_key) DO UPDATE SET
                    name = excluded.name,
                    display_name = excluded.display_name,
                    description = excluded.description,
                    payload_json = excluded.payload_json,
                    enabled = excluded.enabled,
                    sort_order = excluded.sort_order,
                    updated_at = excluded.updated_at
                """,
                (
                    template_key,
                    name,
                    str(raw.get("display_name") or name),
                    str(raw.get("diet_text") or raw.get("description") or ""),
                    _stable_json(full_payload),
                    enabled,
                    sort_order,
                    str(raw.get("created_at") or now_text()),
                    str(raw.get("updated_at") or now_text()),
                ),
            )
            count += 1
        return count

    def _import_lab_templates(self, cursor, payload: dict[str, Any]) -> int:
        raw_materials = payload.get("materials", []) if isinstance(payload, dict) else []
        materials = [
            {"code": str(code), "label": str(label), "built_in": True, "version": 1}
            for code, label in LAB_MATERIAL_LABELS.items()
        ]
        used_codes = {item["code"] for item in materials}
        used_labels = {item["label"].lower() for item in materials}
        if isinstance(raw_materials, list):
            for raw in raw_materials:
                if not isinstance(raw, dict) or raw.get("_deleted"):
                    continue
                label = str(raw.get("label") or raw.get("name") or "").strip()
                if not label:
                    continue
                raw_code = str(raw.get("code") or "").strip()
                if raw_code in LAB_MATERIAL_LABELS or label.lower() in used_labels:
                    continue
                code = _slug(raw_code or label, f"material_{len(materials) + 1}")
                while code in used_codes:
                    code = f"{code}_{len(used_codes) + 1}"
                used_codes.add(code)
                used_labels.add(label.lower())
                materials.append({"code": code, "label": label, "built_in": False, "version": int(raw.get("version") or 1)})
        self._write_app_setting_in_tx(
            cursor,
            "shared",
            "lab_materials",
            materials,
            changed_by_role="system",
            catalog_key=LAB_ANALYSIS_KEY,
            log_change=False,
        )
        labels = {item["code"]: item["label"] for item in materials}

        raw_templates = payload.get("templates", []) if isinstance(payload, dict) else []
        if isinstance(raw_templates, dict):
            raw_templates = list(raw_templates.values())
        if not isinstance(raw_templates, list):
            raw_templates = []
        count = 0
        used_codes_templates: set[str] = set()
        for sort_order, raw in enumerate(raw_templates, start=1):
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            if not name:
                continue
            code = _slug(raw.get("code") or name, f"analysis_{sort_order}")
            suffix = 2
            base = code
            while code in used_codes_templates:
                code = f"{base}_{suffix}"
                suffix += 1
            used_codes_templates.add(code)
            default_times = _normalize_lab_times(raw.get("default_times"))
            full_payload = dict(raw)
            full_payload["default_times"] = default_times
            cursor.execute(
                """
                INSERT INTO lab_analysis_templates (
                    analysis_code, name, display_name, category, material, aliases_json,
                    default_priority, fasting_required, enabled, frequent, sort_order,
                    payload_json, revision, source, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'legacy_json', ?, ?)
                ON CONFLICT(analysis_code) DO UPDATE SET
                    name = excluded.name,
                    display_name = excluded.display_name,
                    category = excluded.category,
                    material = excluded.material,
                    aliases_json = excluded.aliases_json,
                    enabled = excluded.enabled,
                    frequent = excluded.frequent,
                    sort_order = excluded.sort_order,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    code,
                    name,
                    str(raw.get("display_name") or name),
                    str(raw.get("category") or ""),
                    _normalize_lab_material(raw.get("material"), labels),
                    _stable_json(raw.get("aliases") or raw.get("aliases_json") or []),
                    str(raw.get("default_priority") or ""),
                    _normalize_bool_int(raw.get("fasting_required", False)),
                    0 if raw.get("_deleted") else _enabled_from_int(raw.get("enabled", 1)),
                    _normalize_bool_int(raw.get("frequent", False)),
                    sort_order,
                    _stable_json(full_payload),
                    int(raw.get("version") or 1),
                    str(raw.get("created_at") or now_text()),
                    str(raw.get("updated_at") or now_text()),
                ),
            )
            count += 1
        return count

    def _import_json_app_setting(
        self,
        cursor,
        scope: str,
        key: str,
        path: str,
        *,
        warnings: list[str],
        catalog_key: str,
        fallback_path: str | None = None,
    ) -> int:
        source = path
        if not os.path.exists(source) and fallback_path:
            source = fallback_path
        payload = _read_json_dict(source, warnings=warnings)
        if not payload:
            return 0
        self._write_app_setting_in_tx(
            cursor,
            scope,
            key,
            payload,
            changed_by_role="system",
            catalog_key=catalog_key,
            log_change=False,
        )
        return 1

    def _import_background_settings(self, cursor, path: str, *, warnings: list[str]) -> int:
        payload = _read_json_dict(path, warnings=warnings)
        if not payload:
            return 0
        self._write_app_setting_in_tx(
            cursor,
            "shared",
            "background_settings",
            payload,
            changed_by_role="system",
            catalog_key=BACKGROUND_SETTINGS_KEY,
            log_change=False,
        )
        self._sync_background_rows_in_tx(cursor, payload)
        return 1

    def _import_default_print_settings(self, cursor) -> int:
        payload = {
            "vitals": True,
            "balance": True,
            "prescriptions": True,
            "events": True,
            "ventilation": False,
            "labs": False,
            "procedures": False,
            "death_outcome": True,
            "death_protocol": True,
            "transfusion_registration": True,
            "outcome_report_reminder": False,
        }
        self._write_app_setting_in_tx(
            cursor,
            "doctor",
            "print_config",
            payload,
            changed_by_role="system",
            catalog_key=PRINT_SETTINGS_KEY,
            log_change=False,
        )
        self._write_app_setting_in_tx(
            cursor,
            "nurse",
            "print_config",
            {key: payload[key] for key in ("vitals", "balance", "prescriptions", "events")},
            changed_by_role="system",
            catalog_key=PRINT_SETTINGS_KEY,
            log_change=False,
        )
        cursor.execute(
            """
            INSERT INTO print_templates (
                template_key, name, scope, template_type, params_json,
                enabled, active, revision, created_at, updated_at
            )
            VALUES ('default_report_sections', 'Разделы печати', 'shared', 'settings', ?, 1, 1, 1, ?, ?)
            ON CONFLICT(template_key) DO UPDATE SET params_json = excluded.params_json, updated_at = excluded.updated_at
            """,
            (_stable_json(payload), now_text(), now_text()),
        )
        return 2

    def _read_row_json(self, row: Any, column: str = "payload_json") -> dict[str, Any]:
        if not row or column not in row.keys() or not row[column]:
            return {}
        try:
            payload = json.loads(row[column])
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _select_by_key(self, cursor, table: str, key_column: str, key: str) -> dict[str, Any] | None:
        row = cursor.execute(f"SELECT * FROM {table} WHERE {key_column} = ?", (key,)).fetchone()
        return dict(row) if row else None

    def _upsert_group(self, cursor, code: str, payload: dict[str, Any], *, enabled: int = 1, sort_order: int = 0, source: str = "manual", bump: bool = True) -> None:
        before = self._select_by_key(cursor, "drug_groups", "code", code)
        name = str(payload.get("name_ru") or payload.get("name") or payload.get("display_name") or code)
        cursor.execute(
            """
            INSERT INTO drug_groups (
                code, name, display_name, sort_order, enabled, color, icon, revision,
                payload_json, source, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name = excluded.name,
                display_name = excluded.display_name,
                sort_order = excluded.sort_order,
                enabled = excluded.enabled,
                color = excluded.color,
                icon = excluded.icon,
                revision = drug_groups.revision + 1,
                payload_json = excluded.payload_json,
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            (
                code,
                name,
                str(payload.get("display_name") or name),
                int(sort_order or 0),
                int(enabled),
                payload.get("color"),
                payload.get("icon"),
                _stable_json(payload),
                source,
                now_text(),
                now_text(),
            ),
        )
        if bump:
            self._bump_catalog_version(cursor, DRUG_CATALOG_KEY, "drug_groups", code, "upsert", before=before, after=payload)

    def _upsert_form(self, cursor, code: str, payload: dict[str, Any], *, enabled: int = 1, sort_order: int = 0, source: str = "manual", bump: bool = True) -> None:
        before = self._select_by_key(cursor, "dosage_forms", "code", code)
        name = str(payload.get("name_ru") or payload.get("name") or payload.get("display_name") or code)
        cursor.execute(
            """
            INSERT INTO dosage_forms (
                code, name, display_name, enabled, sort_order, revision,
                payload_json, source, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name = excluded.name,
                display_name = excluded.display_name,
                enabled = excluded.enabled,
                sort_order = excluded.sort_order,
                revision = dosage_forms.revision + 1,
                payload_json = excluded.payload_json,
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            (code, name, str(payload.get("display_name") or name), int(enabled), int(sort_order or 0), _stable_json(payload), source, now_text(), now_text()),
        )
        if bump:
            self._bump_catalog_version(cursor, DRUG_CATALOG_KEY, "dosage_forms", code, "upsert", before=before, after=payload)

    def _upsert_route(self, cursor, code: str, payload: dict[str, Any], *, enabled: int = 1, sort_order: int = 0, source: str = "manual", bump: bool = True) -> None:
        before = self._select_by_key(cursor, "administration_routes", "code", code)
        name = str(payload.get("name_ru") or payload.get("name") or payload.get("display_name") or code)
        cursor.execute(
            """
            INSERT INTO administration_routes (
                code, name, display_name, enabled, sort_order, revision,
                payload_json, source, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name = excluded.name,
                display_name = excluded.display_name,
                enabled = excluded.enabled,
                sort_order = excluded.sort_order,
                revision = administration_routes.revision + 1,
                payload_json = excluded.payload_json,
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            (code, name, str(payload.get("display_name") or name), int(enabled), int(sort_order or 0), _stable_json(payload), source, now_text(), now_text()),
        )
        if bump:
            self._bump_catalog_version(cursor, DRUG_CATALOG_KEY, "administration_routes", code, "upsert", before=before, after=payload)

    def _upsert_solvent(self, cursor, code: str, payload: dict[str, Any], *, enabled: int = 1, sort_order: int = 0, source: str = "manual", bump: bool = True) -> None:
        before = self._select_by_key(cursor, "solvents", "code", code)
        name = str(payload.get("display") or payload.get("name") or payload.get("latin") or code)
        default_volumes = payload.get("default_volumes")
        default_volume = None
        if isinstance(default_volumes, list) and default_volumes:
            try:
                default_volume = float(default_volumes[0])
            except Exception:
                default_volume = None
        cursor.execute(
            """
            INSERT INTO solvents (
                code, name, display_name, default_volume_ml, unit, enabled, sort_order,
                revision, payload_json, source, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 'ml', ?, ?, 1, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name = excluded.name,
                display_name = excluded.display_name,
                default_volume_ml = excluded.default_volume_ml,
                enabled = excluded.enabled,
                sort_order = excluded.sort_order,
                revision = solvents.revision + 1,
                payload_json = excluded.payload_json,
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            (code, name, str(payload.get("display") or name), default_volume, int(enabled), int(sort_order or 0), _stable_json(payload), source, now_text(), now_text()),
        )
        if bump:
            self._bump_catalog_version(cursor, DRUG_CATALOG_KEY, "solvents", code, "upsert", before=before, after=payload)

    def _upsert_drug(self, cursor, code: str, payload: dict[str, Any], *, enabled: int = 1, sort_order: int = 0, source: str = "manual", bump: bool = True) -> None:
        before = self._select_by_key(cursor, "drugs", "code", code)
        name = str(payload.get("name") or payload.get("latin") or code)
        aliases = payload.get("aliases") or []
        cursor.execute(
            """
            INSERT INTO drugs (
                code, latin, name, display_name, aliases_json, group_code, form_code,
                route_code, unit, default_dose, default_dilution_json, admin_type,
                duration_min, uses_line, enabled, sort_order, source, revision,
                payload_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                latin = excluded.latin,
                name = excluded.name,
                display_name = excluded.display_name,
                aliases_json = excluded.aliases_json,
                group_code = excluded.group_code,
                form_code = excluded.form_code,
                route_code = excluded.route_code,
                unit = excluded.unit,
                default_dose = excluded.default_dose,
                default_dilution_json = excluded.default_dilution_json,
                admin_type = excluded.admin_type,
                duration_min = excluded.duration_min,
                uses_line = excluded.uses_line,
                enabled = excluded.enabled,
                sort_order = excluded.sort_order,
                source = excluded.source,
                revision = drugs.revision + 1,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (
                code,
                payload.get("latin"),
                name,
                str(payload.get("display_name") or payload.get("latin") or name),
                _stable_json(aliases),
                payload.get("group"),
                payload.get("form_key"),
                payload.get("route_code") or payload.get("admin_type"),
                payload.get("unit"),
                str(payload.get("default_dose", "")),
                _stable_json(payload.get("default_dilution")),
                payload.get("admin_type"),
                int(payload.get("duration_min") or 0),
                _normalize_bool_int(payload.get("uses_line", False)),
                int(enabled),
                int(sort_order or 0),
                source,
                _stable_json(payload),
                now_text(),
                now_text(),
            ),
        )
        if bump:
            self._bump_catalog_version(cursor, DRUG_CATALOG_KEY, "drugs", code, "upsert", before=before, after=payload)

    def _upsert_order_template(self, cursor, key: str, payload: dict[str, Any], *, enabled: int = 1, sort_order: int = 0, source: str = "manual", bump: bool = True) -> None:
        before = self._select_by_key(cursor, "order_templates", "template_key", key)
        name = str(payload.get("name") or payload.get("display_name") or key)
        template_type = str(payload.get("template_type") or "medication")
        cursor.execute(
            """
            INSERT INTO order_templates (
                template_key, name, display_name, scope, drug_code, group_code,
                route_code, form_code, solvent_code, template_type, text_template,
                params_json, enabled, sort_order, revision, source, created_at, updated_at
            )
            VALUES (?, ?, ?, 'doctor', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(template_key) DO UPDATE SET
                name = excluded.name,
                display_name = excluded.display_name,
                drug_code = excluded.drug_code,
                group_code = excluded.group_code,
                route_code = excluded.route_code,
                form_code = excluded.form_code,
                solvent_code = excluded.solvent_code,
                template_type = excluded.template_type,
                text_template = excluded.text_template,
                params_json = excluded.params_json,
                enabled = excluded.enabled,
                sort_order = excluded.sort_order,
                revision = order_templates.revision + 1,
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            (
                key,
                name,
                str(payload.get("display_name") or name),
                payload.get("drug_code"),
                payload.get("group_code"),
                payload.get("route_code"),
                payload.get("form_key"),
                payload.get("solvent_code"),
                template_type,
                str(payload.get("text_template") or ""),
                _stable_json(payload),
                int(enabled),
                int(sort_order or 0),
                source,
                now_text(),
                now_text(),
            ),
        )
        if bump:
            self._bump_catalog_version(cursor, ORDER_TEMPLATES_KEY, "order_templates", key, "upsert", before=before, after=payload)

    def _compute_catalog_hash(self, cursor, catalog_key: str) -> str:
        payload: dict[str, Any] = {"catalog_key": catalog_key, "tables": {}}
        for table, key_column in CATALOG_TABLES.get(catalog_key, ()):
            if table == "app_settings":
                keys = APP_SETTINGS_HASH_KEYS.get(catalog_key, ())
                rows = cursor.execute(
                    """
                    SELECT scope, key, value_json, revision, updated_at
                    FROM app_settings
                    WHERE key IN ({})
                    ORDER BY scope, key
                    """.format(",".join("?" for _ in keys) or "''"),
                    tuple(keys),
                ).fetchall()
            elif table == "ui_backgrounds":
                rows = cursor.execute(
                    "SELECT * FROM ui_backgrounds ORDER BY active DESC, background_key ASC"
                ).fetchall()
            elif table == "print_templates":
                rows = cursor.execute(
                    "SELECT * FROM print_templates ORDER BY active DESC, template_key ASC"
                ).fetchall()
            elif table == "operblock_icons":
                rows = cursor.execute(
                    "SELECT * FROM operblock_icons ORDER BY enabled DESC, sort_order ASC, icon_key ASC"
                ).fetchall()
            else:
                rows = cursor.execute(
                    f"SELECT * FROM {table} ORDER BY enabled DESC, sort_order ASC, {key_column} ASC"
                ).fetchall()
            payload["tables"][table] = [dict(row) for row in rows]
        return _hash_value(payload)

    def _bump_catalog_version(
        self,
        cursor,
        catalog_key: str,
        entity_type: str,
        entity_id: str | None,
        operation: str,
        *,
        changed_by_role: str | None = "doctor",
        changed_by_user: str | None = None,
        source_client_id: str | None = None,
        before: Any = None,
        after: Any = None,
    ) -> tuple[int, str]:
        current_row = cursor.execute(
            "SELECT version FROM settings_catalog_versions WHERE catalog_key = ?",
            (catalog_key,),
        ).fetchone()
        current_version = int(current_row[0]) if current_row else 0
        version = current_version + 1
        content_hash = self._compute_catalog_hash(cursor, catalog_key)
        changed_at = now_text()
        effective_source_client_id = str(source_client_id or self.source_client_id).strip()
        if not effective_source_client_id:
            effective_source_client_id = PROCESS_SOURCE_CLIENT_ID
        cursor.execute(
            """
            INSERT INTO settings_catalog_versions (
                catalog_key, version, content_hash, updated_at, updated_by_role, updated_by_user
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(catalog_key) DO UPDATE SET
                version = excluded.version,
                content_hash = excluded.content_hash,
                updated_at = excluded.updated_at,
                updated_by_role = excluded.updated_by_role,
                updated_by_user = excluded.updated_by_user
            """,
            (catalog_key, version, content_hash, changed_at, changed_by_role, changed_by_user),
        )
        cursor.execute(
            """
            INSERT INTO settings_change_log (
                entity_type, entity_id, operation, scope, version, changed_at,
                changed_by_role, changed_by_user, source_client_id, content_hash
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entity_type,
                entity_id,
                operation,
                catalog_key,
                version,
                changed_at,
                changed_by_role,
                changed_by_user,
                effective_source_client_id,
                content_hash,
            ),
        )
        cursor.execute(
            """
            INSERT INTO settings_audit_log (
                entity_type, entity_id, operation, before_json, after_json,
                changed_at, changed_by_role, changed_by_user, source_client_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entity_type,
                entity_id,
                operation,
                None if before is None else _stable_json(before),
                None if after is None else _stable_json(after),
                changed_at,
                changed_by_role,
                changed_by_user,
                effective_source_client_id,
            ),
        )
        self.invalidate_cache(catalog_key)
        return version, content_hash

    def get_catalog_version(self, catalog_key: str) -> tuple[int, str]:
        self.ensure_ready()
        with self.db.read_connection() as conn:
            row = conn.execute(
                "SELECT version, content_hash FROM settings_catalog_versions WHERE catalog_key = ?",
                (catalog_key,),
            ).fetchone()
        if not row:
            return 0, ""
        return int(row["version"] or 0), str(row["content_hash"] or "")

    def latest_change_id(self) -> int:
        self.ensure_ready()
        with self.db.read_connection() as conn:
            row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM settings_change_log").fetchone()
        return int(row[0] or 0) if row else 0

    def fetch_changes_since(self, last_change_id: int) -> list[dict[str, Any]]:
        self.ensure_ready()
        with self.db.read_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, entity_type, entity_id, operation, scope, version,
                       changed_at, changed_by_role, changed_by_user, source_client_id, content_hash
                FROM settings_change_log
                WHERE id > ?
                ORDER BY id ASC
                """,
                (int(last_change_id or 0),),
            ).fetchall()
        return [dict(row) for row in rows]

    def load_prescription_datasets(self) -> dict[str, dict[str, Any]]:
        self.ensure_ready()
        snapshot = self.drug_catalog_snapshot()
        templates_snapshot = self.order_templates_snapshot()
        return {
            "drugs": dict(snapshot.drugs),
            "groups": dict(snapshot.groups),
            "dilutions": dict(snapshot.solvents),
            "templates": dict(templates_snapshot.templates),
            "forms": dict(snapshot.forms),
            "admin_types": dict(snapshot.routes),
        }

    def drug_catalog_snapshot(self) -> DrugCatalogSnapshot:
        catalog_key = DRUG_CATALOG_KEY
        version, content_hash = self.get_catalog_version(catalog_key)
        cached = self._snapshot_cache.get(catalog_key)
        if cached and cached[0] == version and cached[1] == content_hash:
            record_metric("settings_cache_hit", 1, catalog_key=catalog_key)
            return cached[2]
        record_metric("settings_cache_miss", 1, catalog_key=catalog_key)
        with self.db.read_connection() as conn:
            groups = self._dict_payload_rows(conn, "drug_groups", "code")
            forms = self._dict_payload_rows(conn, "dosage_forms", "code")
            routes = self._dict_payload_rows(conn, "administration_routes", "code")
            solvents = self._dict_payload_rows(conn, "solvents", "code")
            drugs = self._dict_payload_rows(conn, "drugs", "code")
        snapshot = DrugCatalogSnapshot(
            catalog_key=catalog_key,
            version=version,
            content_hash=content_hash,
            generated_at=now_text(),
            items=tuple(drugs.items()),
            groups=groups,
            forms=forms,
            routes=routes,
            solvents=solvents,
            drugs=drugs,
        )
        self._snapshot_cache[catalog_key] = (version, content_hash, snapshot)
        return snapshot

    def order_templates_snapshot(self) -> OrderTemplatesSnapshot:
        catalog_key = ORDER_TEMPLATES_KEY
        version, content_hash = self.get_catalog_version(catalog_key)
        cached = self._snapshot_cache.get(catalog_key)
        if cached and cached[0] == version and cached[1] == content_hash:
            record_metric("settings_cache_hit", 1, catalog_key=catalog_key)
            return cached[2]
        record_metric("settings_cache_miss", 1, catalog_key=catalog_key)
        with self.db.read_connection() as conn:
            rows = conn.execute(
                """
                SELECT template_key, params_json
                FROM order_templates
                WHERE enabled = 1
                ORDER BY sort_order ASC, template_key ASC
                """
            ).fetchall()
        templates: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                payload = json.loads(row["params_json"] or "{}")
            except Exception:
                payload = {}
            if isinstance(payload, dict):
                templates[str(row["template_key"])] = payload
        snapshot = OrderTemplatesSnapshot(
            catalog_key=catalog_key,
            version=version,
            content_hash=content_hash,
            generated_at=now_text(),
            items=tuple(templates.items()),
            templates=templates,
        )
        self._snapshot_cache[catalog_key] = (version, content_hash, snapshot)
        return snapshot

    def _dict_payload_rows(self, conn, table: str, key_column: str) -> dict[str, dict[str, Any]]:
        rows = conn.execute(
            f"""
            SELECT *
            FROM {table}
            WHERE enabled = 1
            ORDER BY sort_order ASC, {key_column} ASC
            """
        ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = str(row[key_column])
            payload = {}
            payload_json = row["payload_json"] if "payload_json" in row.keys() else None
            if payload_json:
                try:
                    decoded = json.loads(payload_json)
                    if isinstance(decoded, dict):
                        payload = decoded
                except Exception:
                    payload = {}
            if not payload:
                payload = {k: row[k] for k in row.keys() if k not in {"id", "payload_json"}}
            result[key] = payload
        return result

    def save_prescription_item(self, dict_name: str, key: str, data: dict[str, Any]) -> None:
        self.ensure_ready()
        normalized = str(dict_name)
        if normalized not in PRESCRIPTION_DATASET_TABLES:
            raise ValueError(f"Неизвестный справочник: {dict_name}")
        code = str(key or "").strip()
        if not code:
            raise ValueError("Код справочника не указан")
        payload = dict(data or {})
        with self.db.transaction(f"settings_prescription_{normalized}_save") as cursor:
            if normalized == "drugs":
                self._upsert_drug(cursor, code, payload)
            elif normalized == "groups":
                self._upsert_group(cursor, code, payload)
            elif normalized == "forms":
                self._upsert_form(cursor, code, payload)
            elif normalized == "admin_types":
                self._upsert_route(cursor, code, payload)
            elif normalized == "diluents":
                self._upsert_solvent(cursor, code, payload)
            elif normalized == "templates":
                self._upsert_order_template(cursor, code, payload)

    def delete_prescription_item(self, dict_name: str, key: str) -> None:
        self.ensure_ready()
        mapping = {
            "drugs": ("drugs", "code", DRUG_CATALOG_KEY),
            "groups": ("drug_groups", "code", DRUG_CATALOG_KEY),
            "forms": ("dosage_forms", "code", DRUG_CATALOG_KEY),
            "admin_types": ("administration_routes", "code", DRUG_CATALOG_KEY),
            "diluents": ("solvents", "code", DRUG_CATALOG_KEY),
            "templates": ("order_templates", "template_key", ORDER_TEMPLATES_KEY),
        }
        if dict_name not in mapping:
            raise ValueError(f"Неизвестный справочник: {dict_name}")
        table, key_column, catalog_key = mapping[dict_name]
        code = str(key or "").strip()
        with self.db.transaction(f"settings_prescription_{dict_name}_delete") as cursor:
            before = self._select_by_key(cursor, table, key_column, code)
            cursor.execute(
                f"""
                UPDATE {table}
                SET enabled = 0,
                    revision = COALESCE(revision, 0) + 1,
                    updated_at = ?
                WHERE {key_column} = ?
                """,
                (now_text(), code),
            )
            self._bump_catalog_version(cursor, catalog_key, table, code, "delete", before=before, after={"enabled": False})

    def save_template_order(self, order_keys: list[str]) -> None:
        self.ensure_ready()
        with self.db.transaction("settings_order_templates_reorder") as cursor:
            for sort_order, key in enumerate(order_keys or [], start=1):
                cursor.execute(
                    "UPDATE order_templates SET sort_order = ?, updated_at = ? WHERE template_key = ?",
                    (sort_order, now_text(), str(key)),
                )
            self._bump_catalog_version(
                cursor,
                ORDER_TEMPLATES_KEY,
                "order_templates",
                None,
                "reorder",
                before=None,
                after={"order": list(order_keys or [])},
            )

    def list_lab_materials(self) -> list[dict[str, Any]]:
        default = [
            {"code": str(code), "label": str(label), "built_in": True, "version": 1}
            for code, label in LAB_MATERIAL_LABELS.items()
        ]
        materials = self.get_app_setting("shared", "lab_materials", default=default)
        return list(materials or default) if isinstance(materials, list) else default

    def material_labels(self) -> dict[str, str]:
        labels = dict(LAB_MATERIAL_LABELS)
        for material in self.list_lab_materials():
            code = str(material.get("code") or "").strip()
            label = str(material.get("label") or "").strip()
            if code and label:
                labels[code] = label
        return labels

    def list_lab_templates(self) -> list[dict[str, Any]]:
        snapshot = self.lab_analysis_snapshot()
        return [dict(item) for item in snapshot.items]

    def lab_analysis_snapshot(self) -> LabAnalysisCatalogSnapshot:
        catalog_key = LAB_ANALYSIS_KEY
        version, content_hash = self.get_catalog_version(catalog_key)
        cached = self._snapshot_cache.get(catalog_key)
        if cached and cached[0] == version and cached[1] == content_hash:
            record_metric("settings_cache_hit", 1, catalog_key=catalog_key)
            return cached[2]
        record_metric("settings_cache_miss", 1, catalog_key=catalog_key)
        labels = self.material_labels()
        with self.db.read_connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM lab_analysis_templates
                WHERE enabled = 1
                ORDER BY sort_order ASC, name ASC
                """
            ).fetchall()
        items = []
        for row in rows:
            payload = self._row_payload_or_empty(row)
            default_times = payload.get("default_times")
            if not isinstance(default_times, list):
                default_times = []
            material = str(row["material"] or payload.get("material") or LabMaterial.VENOUS_BLOOD.value)
            items.append(
                {
                    "id": int(row["id"]),
                    "code": str(row["analysis_code"]),
                    "name": str(row["name"]),
                    "display_name": str(row["display_name"] or row["name"]),
                    "category": str(row["category"] or ""),
                    "material": material,
                    "material_label": labels.get(material, material),
                    "comment": str(payload.get("comment") or ""),
                    "default_times": list(default_times),
                    "version": int(row["revision"] or 1),
                    "created_at": str(row["created_at"] or ""),
                    "updated_at": str(row["updated_at"] or ""),
                    "last_modified_by": str(payload.get("last_modified_by") or "doctor"),
                    "frequent": bool(row["frequent"]),
                }
            )
        snapshot = LabAnalysisCatalogSnapshot(
            catalog_key=catalog_key,
            version=version,
            content_hash=content_hash,
            generated_at=now_text(),
            items=tuple(items),
            materials=tuple(self.list_lab_materials()),
        )
        self._snapshot_cache[catalog_key] = (version, content_hash, snapshot)
        return snapshot

    def _row_payload_or_empty(self, row) -> dict[str, Any]:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def create_lab_material(self, *, name: str) -> str:
        self.ensure_ready()
        label = str(name or "").strip()
        if not label:
            raise ValueError("Укажите название материала")
        materials = self.list_lab_materials()
        used_codes = {str(item.get("code") or "") for item in materials}
        used_labels = {str(item.get("label") or "").strip().lower() for item in materials}
        if label.lower() in used_labels:
            raise ValueError("Такой материал уже есть в справочнике")
        code = _slug(label, f"material_{len(materials) + 1}")
        suffix = 2
        base = code
        while code in used_codes:
            code = f"{base}_{suffix}"
            suffix += 1
        materials.append({"code": code, "label": label, "built_in": False, "version": 1})
        self.set_app_setting("shared", "lab_materials", materials, catalog_key=LAB_ANALYSIS_KEY, entity_type="lab_materials", operation="insert")
        return code

    def save_lab_materials(self, materials: list[dict[str, Any]]) -> None:
        self.ensure_ready()
        existing_materials = self.list_lab_materials()
        normalized = self._normalize_lab_material_rows(materials)
        if not normalized:
            raise ValueError("Список материалов не должен быть пустым")
        new_codes = {str(item.get("code") or "") for item in normalized}
        with self.db.transaction("settings_lab_materials_replace") as cursor:
            current_row = self._select_app_setting(cursor, "shared", "lab_materials")
            try:
                current = json.loads((current_row or {}).get("value_json") or "[]")
            except Exception:
                current = []
            current_codes = {
                str(item.get("code") or "")
                for item in (current if isinstance(current, list) else [])
                if isinstance(item, dict)
            }
            if not current_codes:
                current_codes = {
                    str(item.get("code") or "")
                    for item in existing_materials
                    if isinstance(item, dict)
                }
            removed_codes = {code for code in current_codes - new_codes if code}
            used_rows = cursor.execute(
                """
                SELECT name, material
                FROM lab_analysis_templates
                WHERE enabled = 1
                """
            ).fetchall()
            used_removed = [
                f"{row['name']} ({row['material']})"
                for row in used_rows
                if str(row["material"] or "") in removed_codes
            ]
            if used_removed:
                raise ValueError(
                    "Материал используется в анализах: " + ", ".join(used_removed[:5])
                )
            before = self._select_app_setting(cursor, "shared", "lab_materials")
            self._write_app_setting_in_tx(
                cursor,
                "shared",
                "lab_materials",
                normalized,
                changed_by_role="doctor",
                catalog_key=LAB_ANALYSIS_KEY,
                log_change=False,
            )
            self._bump_catalog_version(
                cursor,
                LAB_ANALYSIS_KEY,
                "lab_materials",
                None,
                "replace",
                before=before,
                after={"materials": normalized},
            )

    def _normalize_lab_material_rows(self, materials: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        used_codes: set[str] = set()
        used_labels: set[str] = set()
        for index, raw in enumerate(materials or [], start=1):
            if not isinstance(raw, dict):
                continue
            label = " ".join(str(raw.get("label") or raw.get("name") or "").split())
            if not label:
                raise ValueError("Укажите название материала")
            label_key = label.lower()
            if label_key in used_labels:
                raise ValueError(f"Материал уже есть в списке: {label}")
            code = str(raw.get("code") or "").strip()
            if not code:
                code = _slug(label, f"material_{index}")
            base = code
            suffix = 2
            while code in used_codes:
                code = f"{base}_{suffix}"
                suffix += 1
            used_codes.add(code)
            used_labels.add(label_key)
            item = dict(raw)
            item["code"] = code
            item["label"] = label
            item["built_in"] = bool(item.get("built_in", code in LAB_MATERIAL_LABELS))
            item["version"] = int(item.get("version") or 1)
            item["sort_order"] = index
            result.append(item)
        return result

    def create_lab_template(self, *, name: str, material: str = LabMaterial.VENOUS_BLOOD.value, comment: str = "", default_times: Any = None, code: str = "") -> int:
        self.ensure_ready()
        labels = self.material_labels()
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise ValueError("Укажите название анализа")
        with self.db.transaction("settings_lab_template_create") as cursor:
            used_codes = {
                str(row[0])
                for row in cursor.execute("SELECT analysis_code FROM lab_analysis_templates").fetchall()
            }
            new_code = _slug(code or normalized_name, "analysis")
            base = new_code
            suffix = 2
            while new_code in used_codes:
                new_code = f"{base}_{suffix}"
                suffix += 1
            row = cursor.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM lab_analysis_templates").fetchone()
            sort_order = int(row[0] or 1)
            payload = {
                "name": normalized_name,
                "material": _normalize_lab_material(material, labels),
                "comment": str(comment or "").strip(),
                "default_times": _normalize_lab_times(default_times),
                "last_modified_by": "doctor",
            }
            cursor.execute(
                """
                INSERT INTO lab_analysis_templates (
                    analysis_code, name, display_name, category, material, aliases_json,
                    enabled, frequent, sort_order, payload_json, revision, source,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, '', ?, '[]', 1, 0, ?, ?, 1, 'manual', ?, ?)
                """,
                (new_code, normalized_name, normalized_name, payload["material"], sort_order, _stable_json(payload), now_text(), now_text()),
            )
            new_id = int(cursor.lastrowid)
            self._bump_catalog_version(cursor, LAB_ANALYSIS_KEY, "lab_analysis_templates", str(new_id), "insert", after=payload)
            return new_id

    def update_lab_template(self, template_id: int, *, name: str, material: str, comment: str = "", default_times: Any = None, expected_version: Optional[int] = None) -> None:
        self.ensure_ready()
        labels = self.material_labels()
        with self.db.transaction("settings_lab_template_update") as cursor:
            current = cursor.execute("SELECT * FROM lab_analysis_templates WHERE id = ?", (int(template_id),)).fetchone()
            if not current:
                raise ValueError("Шаблон анализа не найден")
            if expected_version is not None and int(current["revision"] or 0) != int(expected_version):
                raise ValueError("Шаблон анализа был изменен другим пользователем")
            payload = self._row_payload_or_empty(current)
            payload.update(
                {
                    "name": str(name or "").strip(),
                    "material": _normalize_lab_material(material, labels),
                    "comment": str(comment or "").strip(),
                    "default_times": _normalize_lab_times(default_times),
                    "last_modified_by": "doctor",
                }
            )
            if not payload["name"]:
                raise ValueError("Укажите название анализа")
            before = dict(current)
            cursor.execute(
                """
                UPDATE lab_analysis_templates
                SET name = ?,
                    display_name = ?,
                    material = ?,
                    payload_json = ?,
                    revision = revision + 1,
                    updated_at = ?
                WHERE id = ? AND revision = ?
                """,
                (payload["name"], payload["name"], payload["material"], _stable_json(payload), now_text(), int(template_id), int(current["revision"] or 1)),
            )
            if cursor.rowcount != 1:
                raise ValueError("Шаблон анализа был изменен другим пользователем")
            self._bump_catalog_version(cursor, LAB_ANALYSIS_KEY, "lab_analysis_templates", str(template_id), "update", before=before, after=payload)

    def delete_lab_template(self, template_id: int, *, expected_version: Optional[int] = None) -> None:
        self.ensure_ready()
        with self.db.transaction("settings_lab_template_delete") as cursor:
            current = cursor.execute("SELECT * FROM lab_analysis_templates WHERE id = ?", (int(template_id),)).fetchone()
            if not current:
                raise ValueError("Шаблон анализа не найден")
            if expected_version is not None and int(current["revision"] or 0) != int(expected_version):
                raise ValueError("Шаблон анализа был изменен другим пользователем")
            cursor.execute(
                "UPDATE lab_analysis_templates SET enabled = 0, revision = revision + 1, updated_at = ? WHERE id = ?",
                (now_text(), int(template_id)),
            )
            self._bump_catalog_version(cursor, LAB_ANALYSIS_KEY, "lab_analysis_templates", str(template_id), "delete", before=dict(current), after={"enabled": False})

    def reorder_lab_templates(self, ordered_template_ids: list[int]) -> None:
        self.ensure_ready()
        with self.db.transaction("settings_lab_template_reorder") as cursor:
            for sort_order, raw_id in enumerate(ordered_template_ids or [], start=1):
                cursor.execute(
                    "UPDATE lab_analysis_templates SET sort_order = ?, updated_at = ? WHERE id = ?",
                    (sort_order, now_text(), int(raw_id)),
                )
            self._bump_catalog_version(cursor, LAB_ANALYSIS_KEY, "lab_analysis_templates", None, "reorder", after={"order": list(ordered_template_ids or [])})

    def list_diet_templates(self) -> list[DietTemplateDTO]:
        self.ensure_ready()
        with self.db.read_connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM diet_templates
                WHERE enabled = 1
                ORDER BY sort_order ASC, name ASC
                """
            ).fetchall()
        result: list[DietTemplateDTO] = []
        for row in rows:
            payload = self._row_payload_or_empty(row)
            result.append(
                DietTemplateDTO(
                    id=int(row["id"]),
                    name=str(row["name"] or ""),
                    diet_text=str(row["description"] or payload.get("diet_text") or ""),
                    schedule_json=str(payload.get("schedule_json") or "[]"),
                    is_default=_normalize_bool_int(payload.get("is_default", False)),
                    version=int(row["revision"] or 1),
                    created_at=str(row["created_at"] or ""),
                    updated_at=str(row["updated_at"] or ""),
                    last_modified_by=str(payload.get("last_modified_by") or "doctor"),
                )
            )
        return result

    def get_diet_template(self, template_id: int) -> DietTemplateDTO:
        for template in self.list_diet_templates():
            if int(template.id or 0) == int(template_id):
                return template
        raise ValueError("Шаблон питания не найден")

    def create_diet_template(self, name: str, diet_text: str = "", schedule_json: Any = None, is_default: bool = False) -> int:
        self.ensure_ready()
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise ValueError("Название шаблона питания обязательно")
        with self.db.transaction("settings_diet_template_create") as cursor:
            row = cursor.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM diet_templates").fetchone()
            sort_order = int(row[0] or 1)
            key = _slug(normalized_name, f"diet_{sort_order}")
            existing = cursor.execute("SELECT 1 FROM diet_templates WHERE template_key = ?", (key,)).fetchone()
            if existing:
                key = f"{key}_{int(time.time())}"
            payload = {
                "name": normalized_name,
                "diet_text": str(diet_text or ""),
                "schedule_json": _normalize_diet_schedule(schedule_json),
                "is_default": bool(is_default),
                "last_modified_by": "doctor",
            }
            cursor.execute(
                """
                INSERT INTO diet_templates (
                    template_key, name, display_name, description, payload_json,
                    enabled, sort_order, revision, source, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 1, ?, 1, 'manual', ?, ?)
                """,
                (key, normalized_name, normalized_name, payload["diet_text"], _stable_json(payload), sort_order, now_text(), now_text()),
            )
            new_id = int(cursor.lastrowid)
            self._bump_catalog_version(cursor, DIET_TEMPLATES_KEY, "diet_templates", str(new_id), "insert", after=payload)
            return new_id

    def update_diet_template(self, template_id: int, name: str, diet_text: str = "", schedule_json: Any = None, is_default: bool = False, expected_version: Optional[int] = None) -> None:
        self.ensure_ready()
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise ValueError("Название шаблона питания обязательно")
        with self.db.transaction("settings_diet_template_update") as cursor:
            current = cursor.execute("SELECT * FROM diet_templates WHERE id = ?", (int(template_id),)).fetchone()
            if not current:
                raise ValueError("Шаблон питания не найден")
            if expected_version is not None and int(expected_version) > 0 and int(current["revision"] or 0) != int(expected_version):
                raise ValueError("Шаблон питания был изменен другим пользователем")
            payload = self._row_payload_or_empty(current)
            payload.update(
                {
                    "name": normalized_name,
                    "diet_text": str(diet_text or ""),
                    "schedule_json": _normalize_diet_schedule(schedule_json),
                    "is_default": bool(is_default),
                    "last_modified_by": "doctor",
                }
            )
            cursor.execute(
                """
                UPDATE diet_templates
                SET name = ?,
                    display_name = ?,
                    description = ?,
                    payload_json = ?,
                    revision = revision + 1,
                    updated_at = ?
                WHERE id = ? AND revision = ?
                """,
                (normalized_name, normalized_name, payload["diet_text"], _stable_json(payload), now_text(), int(template_id), int(current["revision"] or 1)),
            )
            if cursor.rowcount != 1:
                raise ValueError("Шаблон питания был изменен другим пользователем")
            self._bump_catalog_version(cursor, DIET_TEMPLATES_KEY, "diet_templates", str(template_id), "update", before=dict(current), after=payload)

    def delete_diet_template(self, template_id: int, expected_version: Optional[int] = None) -> None:
        self.ensure_ready()
        with self.db.transaction("settings_diet_template_delete") as cursor:
            current = cursor.execute("SELECT * FROM diet_templates WHERE id = ?", (int(template_id),)).fetchone()
            if not current:
                raise ValueError("Шаблон питания не найден")
            if expected_version is not None and int(expected_version) > 0 and int(current["revision"] or 0) != int(expected_version):
                raise ValueError("Шаблон питания был изменен другим пользователем")
            cursor.execute(
                "UPDATE diet_templates SET enabled = 0, revision = revision + 1, updated_at = ? WHERE id = ?",
                (now_text(), int(template_id)),
            )
            self._bump_catalog_version(cursor, DIET_TEMPLATES_KEY, "diet_templates", str(template_id), "delete", before=dict(current), after={"enabled": False})

    def reorder_diet_templates(self, ordered_template_ids: list[int]) -> None:
        self.ensure_ready()
        with self.db.transaction("settings_diet_template_reorder") as cursor:
            for sort_order, raw_id in enumerate(ordered_template_ids or [], start=1):
                cursor.execute(
                    "UPDATE diet_templates SET sort_order = ?, updated_at = ? WHERE id = ?",
                    (sort_order, now_text(), int(raw_id)),
                )
            self._bump_catalog_version(cursor, DIET_TEMPLATES_KEY, "diet_templates", None, "reorder", after={"order": list(ordered_template_ids or [])})

    def load_doctors(self) -> list[str]:
        return [item["full_name"] for item in self.load_doctor_records()]

    def load_doctor_records(self) -> list[dict[str, str]]:
        self.ensure_ready()
        with self.db.read_connection() as conn:
            rows = conn.execute(
                """
                SELECT full_name, position
                FROM doctors
                WHERE enabled = 1
                ORDER BY sort_order ASC, full_name ASC
                """
            ).fetchall()
        result: list[dict[str, str]] = []
        seen = set()
        for row in rows:
            full_name = " ".join(str(row["full_name"] or "").split())
            position = " ".join(str(row["position"] or "").split())
            key = full_name.lower()
            if not full_name or key in seen:
                continue
            seen.add(key)
            result.append({"full_name": full_name, "position": position})
        return result

    def save_doctors(self, doctors: list[str]) -> None:
        self.save_doctor_records([{"full_name": item, "position": ""} for item in doctors or []])

    def save_doctor_records(self, doctors: list[dict[str, str]]) -> None:
        self.ensure_ready()
        normalized: list[dict[str, str]] = []
        seen = set()
        for item in doctors or []:
            if isinstance(item, dict):
                full_name = " ".join(str(
                    item.get("full_name")
                    or item.get("name")
                    or item.get("doctor")
                    or item.get("fio")
                    or ""
                ).split())
                position = " ".join(str(item.get("position") or "").split())
            else:
                full_name = " ".join(str(item or "").split())
                position = ""
            key = full_name.lower()
            if not full_name or key in seen:
                continue
            seen.add(key)
            normalized.append({"full_name": full_name, "position": position})
        with self.db.transaction("settings_doctors_save") as cursor:
            before_rows = [dict(row) for row in cursor.execute("SELECT * FROM doctors ORDER BY sort_order").fetchall()]
            cursor.execute("UPDATE doctors SET enabled = 0, updated_at = ?", (now_text(),))
            for sort_order, item in enumerate(normalized, start=1):
                full_name = item["full_name"]
                position = item["position"]
                code = _slug(full_name, f"doctor_{sort_order}")
                payload = {"full_name": full_name, "position": position}
                cursor.execute(
                    """
                    INSERT INTO doctors (
                        code, full_name, short_name, position, signature_text, enabled, sort_order,
                        revision, payload_json, source, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, 1, ?, 1, ?, 'manual', ?, ?)
                    ON CONFLICT(code) DO UPDATE SET
                        full_name = excluded.full_name,
                        short_name = excluded.short_name,
                        position = excluded.position,
                        signature_text = excluded.signature_text,
                        enabled = 1,
                        sort_order = excluded.sort_order,
                        revision = doctors.revision + 1,
                        payload_json = excluded.payload_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        code,
                        full_name,
                        full_name,
                        position,
                        full_name,
                        sort_order,
                        _stable_json(payload),
                        now_text(),
                        now_text(),
                    ),
                )
            self._bump_catalog_version(cursor, DOCTORS_KEY, "doctors", None, "replace", before=before_rows, after={"doctors": normalized})

    def get_app_setting(self, scope: str, key: str, *, default: Any = None) -> Any:
        self.ensure_ready()
        with self.db.read_connection() as conn:
            row = conn.execute(
                "SELECT value_json FROM app_settings WHERE scope = ? AND key = ?",
                (str(scope), str(key)),
            ).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value_json"])
        except Exception:
            return default

    def set_app_setting(
        self,
        scope: str,
        key: str,
        value: Any,
        *,
        catalog_key: str = DISPLAY_SETTINGS_KEY,
        entity_type: str = "app_settings",
        operation: str = "update",
        changed_by_role: str | None = "doctor",
        changed_by_user: str | None = None,
    ) -> None:
        self.ensure_ready()
        with self.db.transaction(f"settings_app_setting_{key}") as cursor:
            before = self._select_app_setting(cursor, scope, key)
            self._write_app_setting_in_tx(
                cursor,
                scope,
                key,
                value,
                changed_by_role=changed_by_role,
                changed_by_user=changed_by_user,
                catalog_key=catalog_key,
                log_change=False,
            )
            if key == "background_settings":
                self._sync_background_rows_in_tx(cursor, value)
            self._bump_catalog_version(
                cursor,
                catalog_key,
                entity_type,
                f"{scope}:{key}",
                operation,
                changed_by_role=changed_by_role,
                changed_by_user=changed_by_user,
                before=before,
                after=value,
            )

    def _select_app_setting(self, cursor, scope: str, key: str) -> dict[str, Any] | None:
        row = cursor.execute(
            "SELECT * FROM app_settings WHERE scope = ? AND key = ?",
            (str(scope), str(key)),
        ).fetchone()
        return dict(row) if row else None

    def _write_app_setting_in_tx(
        self,
        cursor,
        scope: str,
        key: str,
        value: Any,
        *,
        changed_by_role: str | None,
        catalog_key: str,
        changed_by_user: str | None = None,
        log_change: bool = False,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO app_settings (scope, key, value_json, revision, updated_at, updated_by_role, updated_by_user)
            VALUES (?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(scope, key) DO UPDATE SET
                value_json = excluded.value_json,
                revision = app_settings.revision + 1,
                updated_at = excluded.updated_at,
                updated_by_role = excluded.updated_by_role,
                updated_by_user = excluded.updated_by_user
            """,
            (str(scope), str(key), _stable_json(value), now_text(), changed_by_role, changed_by_user),
        )
        if log_change:
            self._bump_catalog_version(cursor, catalog_key, "app_settings", f"{scope}:{key}", "update", after=value)

    def _sync_background_rows_in_tx(self, cursor, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        backgrounds = payload.get("backgrounds")
        if not isinstance(backgrounds, list):
            return
        active_key = ""
        try:
            from rem_card.ui.shared.background_settings import active_background_entry

            active_key = str(active_background_entry(payload, require_file=False).get("id") or "")
        except Exception:
            active_key = ""
        for index, raw in enumerate(backgrounds, start=1):
            if not isinstance(raw, dict):
                continue
            key = str(raw.get("id") or f"background_{index}")
            file_name = str(raw.get("file") or "")
            image_blob = None
            image_mime = None
            image_hash = None
            image_path = ""
            if file_name:
                try:
                    from rem_card.ui.shared.background_settings import background_file_path

                    image_path = background_file_path(file_name)
                except Exception:
                    image_path = ""
            if image_path and os.path.isfile(image_path):
                try:
                    size = os.path.getsize(image_path)
                    if size <= MAX_BACKGROUND_IMAGE_BLOB_BYTES:
                        with open(image_path, "rb") as fh:
                            image_blob = fh.read()
                        image_hash = hashlib.sha256(image_blob).hexdigest()
                        image_mime = mimetypes.guess_type(image_path)[0] or "application/octet-stream"
                    else:
                        logger.warning(
                            "Background image is too large for settings DB blob: %s (%s bytes, max %s)",
                            image_path,
                            size,
                            MAX_BACKGROUND_IMAGE_BLOB_BYTES,
                        )
                except Exception:
                    image_blob = None
            cursor.execute(
                """
                INSERT INTO ui_backgrounds (
                    background_key, name, scope, kind, value_json, image_blob,
                    image_mime, image_hash, enabled, active, revision, created_at, updated_at
                )
                VALUES (?, ?, 'shared', ?, ?, ?, ?, ?, 1, ?, 1, ?, ?)
                ON CONFLICT(background_key) DO UPDATE SET
                    name = excluded.name,
                    kind = excluded.kind,
                    value_json = excluded.value_json,
                    image_blob = COALESCE(excluded.image_blob, ui_backgrounds.image_blob),
                    image_mime = COALESCE(excluded.image_mime, ui_backgrounds.image_mime),
                    image_hash = COALESCE(excluded.image_hash, ui_backgrounds.image_hash),
                    active = excluded.active,
                    revision = ui_backgrounds.revision + 1,
                    updated_at = excluded.updated_at
                """,
                (
                    key,
                    str(raw.get("name") or key),
                    "image" if file_name else "color",
                    _stable_json(raw),
                    image_blob,
                    image_mime,
                    image_hash,
                    1 if key == active_key else 0,
                    now_text(),
                    now_text(),
                ),
            )

    def materialize_background_image(self, background_key: str, target_path: str) -> bool:
        self.ensure_ready()
        with self.db.read_connection() as conn:
            row = conn.execute(
                "SELECT image_blob FROM ui_backgrounds WHERE background_key = ?",
                (str(background_key),),
            ).fetchone()
        if not row or row["image_blob"] is None:
            return False
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        tmp_path = f"{target_path}.{os.getpid()}.tmp"
        with open(tmp_path, "wb") as fh:
            fh.write(row["image_blob"])
        os.replace(tmp_path, target_path)
        return True

    @staticmethod
    def _read_operblock_icon_image(image_path: str) -> tuple[bytes, str, str]:
        path = os.path.abspath(os.path.normpath(str(image_path or "").strip().strip('"')))
        if not os.path.isfile(path):
            raise FileNotFoundError("Файл иконки не найден.")
        extension = os.path.splitext(path)[1].lower()
        if extension not in SUPPORTED_OPERBLOCK_ICON_EXTENSIONS:
            raise ValueError("Поддерживаются только изображения PNG, JPG, JPEG, BMP, GIF, WEBP или SVG.")
        size = os.path.getsize(path)
        if size > MAX_OPERBLOCK_ICON_BLOB_BYTES:
            raise ValueError(
                "Файл иконки слишком большой: "
                f"{size} байт, максимум {MAX_OPERBLOCK_ICON_BLOB_BYTES} байт."
            )
        with open(path, "rb") as fh:
            image_blob = fh.read()
        image_mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        image_hash = hashlib.sha256(image_blob).hexdigest()
        return image_blob, image_mime, image_hash

    @staticmethod
    def _operblock_icons_seed_hash() -> str:
        return _hash_value(
            [
                {
                    "icon_key": definition.icon_key,
                    "category": definition.category,
                    "target_key": definition.target_key,
                    "name": definition.name,
                    "default_file": definition.default_file,
                    "sort_order": int(definition.sort_order or 0),
                    "source_file": definition.source_file or definition.default_file,
                }
                for definition in SEEDED_ICON_DEFINITIONS
            ]
        )

    def _operblock_icons_seed_fastpath_state(self) -> dict[str, Any]:
        expected_keys = tuple(definition.icon_key for definition in SEEDED_ICON_DEFINITIONS)
        expected_hash = self._operblock_icons_seed_hash()
        if not expected_keys:
            return {
                "ready": True,
                "reason": "no_seed_definitions",
                "expected_hash": expected_hash,
                "missing_keys": [],
            }

        with self.db.read_connection() as conn:
            meta_rows = conn.execute(
                """
                SELECT key, value
                FROM settings_meta
                WHERE key IN (?, ?)
                """,
                (OPERBLOCK_ICONS_SEED_META_VERSION_KEY, OPERBLOCK_ICONS_SEED_META_HASH_KEY),
            ).fetchall()
            meta = {str(row["key"]): str(row["value"] or "") for row in meta_rows}
            placeholders = ",".join("?" for _ in expected_keys)
            icon_rows = conn.execute(
                f"SELECT icon_key FROM operblock_icons WHERE icon_key IN ({placeholders})",
                expected_keys,
            ).fetchall()

        existing_keys = {str(row["icon_key"] or "") for row in icon_rows}
        missing_keys = [key for key in expected_keys if key not in existing_keys]
        version_ok = meta.get(OPERBLOCK_ICONS_SEED_META_VERSION_KEY) == OPERBLOCK_ICONS_SEED_VERSION
        hash_ok = meta.get(OPERBLOCK_ICONS_SEED_META_HASH_KEY) == expected_hash
        if missing_keys:
            reason = "missing_icons"
        elif not version_ok:
            reason = "marker_version_mismatch"
        elif not hash_ok:
            reason = "marker_hash_mismatch"
        else:
            reason = "fast_path"
        return {
            "ready": not missing_keys and version_ok and hash_ok,
            "reason": reason,
            "expected_hash": expected_hash,
            "missing_keys": missing_keys,
        }

    @staticmethod
    def _missing_operblock_seed_icon_keys(cursor) -> list[str]:
        expected_keys = tuple(definition.icon_key for definition in SEEDED_ICON_DEFINITIONS)
        if not expected_keys:
            return []
        placeholders = ",".join("?" for _ in expected_keys)
        rows = cursor.execute(
            f"SELECT icon_key FROM operblock_icons WHERE icon_key IN ({placeholders})",
            expected_keys,
        ).fetchall()
        existing_keys = {str(row["icon_key"] or "") for row in rows}
        return [key for key in expected_keys if key not in existing_keys]

    def _ensure_default_operblock_icons(self) -> None:
        inserted: list[str] = []
        if getattr(self.db, "settings_readonly", False):
            operblock_startup_metrics.record_value(
                "settings_operblock_icons_seed_skipped",
                "readonly",
                source="settings_service",
            )
            operblock_startup_metrics.record_value(
                "settings_operblock_icons_seed_reason",
                "readonly",
                source="settings_service",
            )
            return

        fastpath_started = operblock_startup_metrics.timer_start()
        fastpath_state: dict[str, Any]
        try:
            fastpath_state = self._operblock_icons_seed_fastpath_state()
        except Exception as exc:
            fastpath_state = {
                "ready": False,
                "reason": f"fastpath_read_error:{type(exc).__name__}",
                "expected_hash": self._operblock_icons_seed_hash(),
                "missing_keys": [],
            }
        operblock_startup_metrics.record_since(
            "settings_operblock_icons_seed_fastpath_ms",
            fastpath_started,
            source="settings_service",
            reason=fastpath_state.get("reason"),
        )
        if fastpath_state.get("ready"):
            operblock_startup_metrics.record_value(
                "settings_operblock_icons_seed_skipped",
                "fast_path",
                source="settings_service",
            )
            operblock_startup_metrics.record_value(
                "settings_operblock_icons_seed_reason",
                str(fastpath_state.get("reason") or "fast_path"),
                source="settings_service",
            )
            return

        metric_started = operblock_startup_metrics.timer_start()
        seed_status = "error"
        try:
            from rem_card.app.paths import get_icon_dir

            icon_dir = get_icon_dir()
            expected_hash = str(fastpath_state.get("expected_hash") or self._operblock_icons_seed_hash())
            with self.db.transaction("settings_operblock_icons_seed") as cursor:
                for definition in SEEDED_ICON_DEFINITIONS:
                    existing = cursor.execute(
                        "SELECT 1 FROM operblock_icons WHERE icon_key = ?",
                        (definition.icon_key,),
                    ).fetchone()
                    if existing:
                        continue
                    source_file = definition.source_file or definition.default_file
                    source_path = os.path.join(icon_dir, source_file)
                    if not os.path.isfile(source_path):
                        logger.warning("Operblock default icon seed file missing: %s", source_path)
                        continue
                    image_blob, image_mime, image_hash = self._read_operblock_icon_image(source_path)
                    value = {
                        "source": "default_seed",
                        "source_file": source_file,
                        "default_file": definition.default_file,
                    }
                    now = now_text()
                    cursor.execute(
                        """
                        INSERT INTO operblock_icons (
                            icon_key, category, target_key, name, default_file, value_json,
                            image_blob, image_mime, image_hash, enabled, sort_order,
                            revision, source, created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 1, 'seed', ?, ?)
                        """,
                        (
                            definition.icon_key,
                            definition.category,
                            definition.target_key,
                            definition.name,
                            definition.default_file,
                            _stable_json(value),
                            image_blob,
                            image_mime,
                            image_hash,
                            int(definition.sort_order or 0),
                            now,
                            now,
                        ),
                    )
                    inserted.append(definition.icon_key)
                missing_after_seed = self._missing_operblock_seed_icon_keys(cursor)
                if inserted:
                    self._bump_catalog_version(
                        cursor,
                        OPERBLOCK_ICONS_KEY,
                        "operblock_icons",
                        None,
                        "seed_defaults",
                        changed_by_role="system",
                        after={"inserted": inserted},
                    )
                if not missing_after_seed:
                    self._set_meta(cursor, OPERBLOCK_ICONS_SEED_META_VERSION_KEY, OPERBLOCK_ICONS_SEED_VERSION)
                    self._set_meta(cursor, OPERBLOCK_ICONS_SEED_META_HASH_KEY, expected_hash)
                seed_status = "ok"
            operblock_startup_metrics.record_value(
                "settings_operblock_icons_seed_skipped",
                "no",
                source="settings_service",
            )
            operblock_startup_metrics.record_value(
                "settings_operblock_icons_seed_reason",
                str(fastpath_state.get("reason") or "seed_required"),
                source="settings_service",
            )
        except Exception as exc:
            logger.warning("Не удалось подготовить стандартные иконки оперблока: %s", exc)
            operblock_startup_metrics.record_value(
                "settings_operblock_icons_seed_skipped",
                "error",
                source="settings_service",
            )
            operblock_startup_metrics.record_value(
                "settings_operblock_icons_seed_reason",
                f"error:{type(exc).__name__}",
                source="settings_service",
            )
        finally:
            operblock_startup_metrics.record_since(
                "settings_operblock_icons_seed_ms",
                metric_started,
                source="settings_service",
                inserted_count=len(inserted),
                status=seed_status,
                reason=fastpath_state.get("reason"),
            )

    def list_operblock_icons(self) -> dict[str, dict[str, Any]]:
        self.ensure_ready()
        with self.db.read_connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM operblock_icons
                WHERE enabled = 1
                ORDER BY sort_order ASC, icon_key ASC
                """
            ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            value_json = item.get("value_json")
            value = {}
            if value_json:
                try:
                    decoded = json.loads(value_json)
                    if isinstance(decoded, dict):
                        value = decoded
                except Exception:
                    value = {}
            item["value"] = value
            result[str(item.get("icon_key") or "")] = item
        return result

    def list_remcard_icons(self) -> dict[str, dict[str, Any]]:
        records = self.list_operblock_icons()
        remcard_keys = {definition.icon_key for definition in REMCARD_ICON_DEFINITIONS}
        return {
            key: record
            for key, record in records.items()
            if key in remcard_keys or str(record.get("category") or "").startswith("remcard")
        }

    def save_operblock_icon(
        self,
        *,
        icon_key: str,
        category: str,
        target_key: str,
        name: str,
        default_file: str,
        image_path: str,
        sort_order: int = 0,
        changed_by_role: str | None = "doctor",
    ) -> None:
        self.ensure_ready()
        clean_key = str(icon_key or "").strip()
        if not clean_key:
            raise ValueError("Ключ иконки не указан.")
        image_blob, image_mime, image_hash = self._read_operblock_icon_image(image_path)
        value = {
            "source": "user_upload",
            "source_file": os.path.basename(str(image_path or "")),
            "default_file": str(default_file or ""),
        }
        with self.db.transaction("settings_operblock_icon_save") as cursor:
            before = self._select_by_key(cursor, "operblock_icons", "icon_key", clean_key)
            now = now_text()
            cursor.execute(
                """
                INSERT INTO operblock_icons (
                    icon_key, category, target_key, name, default_file, value_json,
                    image_blob, image_mime, image_hash, enabled, sort_order,
                    revision, source, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 1, 'manual', ?, ?)
                ON CONFLICT(icon_key) DO UPDATE SET
                    category = excluded.category,
                    target_key = excluded.target_key,
                    name = excluded.name,
                    default_file = excluded.default_file,
                    value_json = excluded.value_json,
                    image_blob = excluded.image_blob,
                    image_mime = excluded.image_mime,
                    image_hash = excluded.image_hash,
                    enabled = 1,
                    sort_order = excluded.sort_order,
                    revision = operblock_icons.revision + 1,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (
                    clean_key,
                    str(category or "custom"),
                    str(target_key or clean_key),
                    str(name or clean_key),
                    str(default_file or ""),
                    _stable_json(value),
                    image_blob,
                    image_mime,
                    image_hash,
                    int(sort_order or 0),
                    now,
                    now,
                ),
            )
            self._bump_catalog_version(
                cursor,
                OPERBLOCK_ICONS_KEY,
                "operblock_icons",
                clean_key,
                "update",
                changed_by_role=changed_by_role,
                before=before,
                after={
                    "icon_key": clean_key,
                    "category": str(category or "custom"),
                    "target_key": str(target_key or clean_key),
                    "name": str(name or clean_key),
                    "default_file": str(default_file or ""),
                    "image_hash": image_hash,
                },
            )

    def save_remcard_icon(
        self,
        *,
        icon_key: str,
        category: str,
        target_key: str,
        name: str,
        default_file: str,
        image_path: str,
        sort_order: int = 0,
        changed_by_role: str | None = "doctor",
    ) -> None:
        self.save_operblock_icon(
            icon_key=icon_key,
            category=category or "remcard",
            target_key=target_key,
            name=name,
            default_file=default_file,
            image_path=image_path,
            sort_order=sort_order,
            changed_by_role=changed_by_role,
        )

    def _compute_app_settings_hash(self, scope: str, key: str) -> str:
        value = self.get_app_setting(scope, key, default=None)
        return _hash_value(value)


_DEFAULT_SERVICE: SettingsService | None = None


def get_settings_service(
    context: DbRuntimeContext | None = None,
    *,
    runtime_context: DbRuntimeContext | None = None,
    settings_db_path: str | None = None,
    settings_db_lock_path: str | None = None,
    settings_backups_dir: str | None = None,
    settings_backup_health_dir: str | None = None,
    readonly: bool | None = None,
) -> SettingsService:
    global _DEFAULT_SERVICE
    if (
        context is not None
        or runtime_context is not None
        or settings_db_path is not None
        or settings_db_lock_path is not None
        or settings_backups_dir is not None
        or settings_backup_health_dir is not None
        or readonly is not None
    ):
        return SettingsService(
            context=context,
            runtime_context=runtime_context,
            settings_db_path=settings_db_path,
            settings_db_lock_path=settings_db_lock_path,
            settings_backups_dir=settings_backups_dir,
            settings_backup_health_dir=settings_backup_health_dir,
            readonly=readonly,
        )
    if _DEFAULT_SERVICE is None:
        _DEFAULT_SERVICE = SettingsService()
    return _DEFAULT_SERVICE


def configure_settings_service(
    context: DbRuntimeContext | None = None,
    *,
    runtime_context: DbRuntimeContext | None = None,
    settings_db_path: str | None = None,
    settings_db_lock_path: str | None = None,
    settings_backups_dir: str | None = None,
    settings_backup_health_dir: str | None = None,
    readonly: bool | None = None,
) -> SettingsService:
    global _DEFAULT_SERVICE
    reset_settings_database()
    _DEFAULT_SERVICE = SettingsService(
        context=context,
        runtime_context=runtime_context,
        settings_db_path=settings_db_path,
        settings_db_lock_path=settings_db_lock_path,
        settings_backups_dir=settings_backups_dir,
        settings_backup_health_dir=settings_backup_health_dir,
        readonly=readonly,
    )
    return _DEFAULT_SERVICE


def reset_settings_service() -> None:
    global _DEFAULT_SERVICE
    _DEFAULT_SERVICE = None
    reset_settings_database()
