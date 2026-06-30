from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.data.settings.settings_db import SettingsDatabase  # noqa: E402
from rem_card.data.settings.settings_import import (  # noqa: E402
    apply_settings_import,
    format_settings_import_change_side,
    preview_settings_import,
    resolve_settings_db_path,
)


class SettingsImportTest(unittest.TestCase):
    def test_preview_and_apply_selected_settings_from_baza_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "network_baza"
            source_db_path = source_root / "settings" / "remcard_settings.db"
            target_db_path = root / "dev_baza" / "settings" / "remcard_settings.db"
            source_db = SettingsDatabase(settings_db_path=str(source_db_path))
            target_db = SettingsDatabase(settings_db_path=str(target_db_path))
            source_db.ensure_ready()
            target_db.ensure_ready()

            self._write_app_setting(target_db, "shared", "display_settings", {"mode": "dev"})
            self._write_app_setting(target_db, "shared", "old_only", {"keep": True})
            self._write_app_setting(source_db, "shared", "display_settings", {"mode": "network"})
            self._write_app_setting(source_db, "doctor", "print_config", {"vitals": True})

            self.assertTrue(os.path.samefile(resolve_settings_db_path(str(source_root)), source_db_path))

            preview = preview_settings_import(target_db, str(source_root))
            changes_by_key = {(change.operation, change.key_label): change for change in preview.changes}
            self.assertIn(("update", "shared:display_settings"), changes_by_key)
            self.assertIn(("insert", "doctor:print_config"), changes_by_key)
            self.assertIn(("delete", "shared:old_only"), changes_by_key)

            bumped_catalogs: list[str] = []

            def bump_catalog(cursor, catalog_key, *_args, **_kwargs):
                bumped_catalogs.append(catalog_key)
                return 1, ""

            selected = [
                changes_by_key[("update", "shared:display_settings")].change_id,
                changes_by_key[("insert", "doctor:print_config")].change_id,
            ]
            report = apply_settings_import(
                target_db,
                str(source_root),
                selected,
                bump_catalog_version=bump_catalog,
            )

            self.assertTrue(report["applied"])
            self.assertEqual(report["counts"], {"insert": 1, "update": 1, "delete": 0})
            self.assertEqual(self._read_app_setting(target_db, "shared", "display_settings"), {"mode": "network"})
            self.assertEqual(self._read_app_setting(target_db, "doctor", "print_config"), {"vitals": True})
            self.assertEqual(self._read_app_setting(target_db, "shared", "old_only"), {"keep": True})
            self.assertEqual(sorted(bumped_catalogs), ["display_settings", "print_settings"])

    def test_preview_formatter_shows_nested_json_field_diff(self):
        before_payload = {
            "version": 1,
            "items": [
                {
                    "preset_id": "drug:fentanyl",
                    "display_name": "S. Fentanyli",
                    "solvent_volume_ml": "10",
                },
                {
                    "preset_id": "drug:fizrastvor",
                    "display_name": "Физраствор",
                    "solvent_volume_ml": "100",
                },
            ],
        }
        after_payload = {
            "version": 1,
            "items": [
                {
                    "preset_id": "drug:fentanyl",
                    "display_name": "S. Fentanyli",
                    "solvent_volume_ml": "10",
                },
                {
                    "preset_id": "drug:fizrastvor",
                    "display_name": "Физраствор",
                    "solvent_volume_ml": "250",
                },
            ],
        }
        before_row = {
            "scope": "operblock",
            "key": "medication_presets",
            "value_json": json.dumps(before_payload, ensure_ascii=False, sort_keys=True),
            "revision": 1,
        }
        after_row = {
            "scope": "operblock",
            "key": "medication_presets",
            "value_json": json.dumps(after_payload, ensure_ascii=False, sort_keys=True),
            "revision": 2,
        }

        before_text = format_settings_import_change_side(before_row, after_row)
        after_text = format_settings_import_change_side(after_row, before_row)

        self.assertIn("items[preset_id=drug:fizrastvor].solvent_volume_ml: 100", before_text)
        self.assertIn("items[preset_id=drug:fizrastvor].solvent_volume_ml: 250", after_text)
        self.assertNotIn("S. Fentanyli", before_text)
        self.assertNotIn("S. Fentanyli", after_text)
        self.assertNotIn("value_json", before_text)
        self.assertNotIn('"items"', after_text)

    @staticmethod
    def _write_app_setting(db: SettingsDatabase, scope: str, key: str, value: dict):
        with db.transaction("test_settings_import") as cursor:
            cursor.execute(
                """
                INSERT INTO app_settings (scope, key, value_json, revision, updated_at, updated_by_role, updated_by_user)
                VALUES (?, ?, ?, 1, '2026-06-17 00:00:00', 'system', NULL)
                ON CONFLICT(scope, key) DO UPDATE SET value_json = excluded.value_json
                """,
                (scope, key, json.dumps(value, ensure_ascii=False, sort_keys=True)),
            )

    @staticmethod
    def _read_app_setting(db: SettingsDatabase, scope: str, key: str):
        with db.read_connection() as conn:
            row = conn.execute(
                "SELECT value_json FROM app_settings WHERE scope = ? AND key = ?",
                (scope, key),
            ).fetchone()
        return None if row is None else json.loads(row["value_json"])


if __name__ == "__main__":
    unittest.main()
