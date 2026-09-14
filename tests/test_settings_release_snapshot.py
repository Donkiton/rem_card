from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.app.settings_db_paths import get_settings_backgrounds_dir, get_settings_icon_assets_dir  # noqa: E402
from rem_card.data.settings.settings_db import SettingsDatabase  # noqa: E402
from rem_card.data.settings import settings_release as settings_release_module  # noqa: E402
from rem_card.data.settings.settings_release import (  # noqa: E402
    BLOB_BASE64_MARKER,
    BLOB_FILE_MARKER,
    apply_settings_release_snapshot,
    export_settings_release_snapshot,
)
from rem_card.data.settings.settings_schema import now_text  # noqa: E402
from rem_card.services.settings.settings_service import SettingsService  # noqa: E402


class SettingsReleaseSnapshotTest(unittest.TestCase):
    def test_institution_identity_is_never_distributed_or_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_baza = root / "source_baza"
            target_baza = root / "target_baza"
            empty_target_baza = root / "empty_target_baza"
            source_service = SettingsService(SettingsDatabase(baza_dir=str(source_baza)))
            target_service = SettingsService(SettingsDatabase(baza_dir=str(target_baza)))
            empty_target_service = SettingsService(SettingsDatabase(baza_dir=str(empty_target_baza)))
            for service in (source_service, target_service, empty_target_service):
                service.ensure_ready()

            source_identity = {"full_name": "Тестовое учреждение", "short_name": "ТЕСТ"}
            target_identity = {"full_name": "Другая больница", "short_name": "ДБ"}
            source_service.set_app_setting(
                "institution",
                "identity",
                source_identity,
                catalog_key="institution",
                changed_by_role="system",
            )
            target_service.set_app_setting(
                "institution",
                "identity",
                target_identity,
                catalog_key="institution",
                changed_by_role="system",
            )

            snapshot_path = root / "settings_release_snapshot.json"
            export_report = export_settings_release_snapshot(
                str(source_baza),
                str(snapshot_path),
                release_version="identity-test",
            )
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            exported_app_settings = snapshot["tables"]["app_settings"]
            self.assertFalse(
                any(
                    row.get("scope") == "institution" and row.get("key") == "identity"
                    for row in exported_app_settings
                )
            )
            self.assertEqual(export_report["row_counts"]["app_settings"], len(exported_app_settings))

            # Simulate an older valid package that still contains the local
            # identity. Apply must preserve an existing institution and must
            # not create the row for an unconfigured institution.
            with source_service.db.read_connection() as conn:
                source_row = conn.execute(
                    "SELECT * FROM app_settings WHERE scope = 'institution' AND key = 'identity'"
                ).fetchone()
            legacy_row = {key: source_row[key] for key in source_row.keys() if key != "id"}
            exported_app_settings.append(legacy_row)
            snapshot["row_counts"]["app_settings"] = len(exported_app_settings)
            snapshot["content_hash"] = settings_release_module._content_hash(
                settings_release_module._snapshot_payload_for_hash(snapshot)
            )
            snapshot_path.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            (root / "settings_release_manifest.json").unlink()

            preserve_report = apply_settings_release_snapshot(
                target_service.db,
                str(snapshot_path),
                bump_catalog_version=target_service._bump_catalog_version,
            )
            target_service.invalidate_cache()
            self.assertEqual(
                target_service.get_app_setting("institution", "identity", default={}),
                target_identity,
            )
            self.assertGreaterEqual(preserve_report["tables"]["app_settings"]["preserved"], 1)

            skip_report = apply_settings_release_snapshot(
                empty_target_service.db,
                str(snapshot_path),
                bump_catalog_version=empty_target_service._bump_catalog_version,
            )
            empty_target_service.invalidate_cache()
            self.assertIsNone(
                empty_target_service.get_app_setting("institution", "identity", default=None)
            )
            self.assertGreaterEqual(skip_report["tables"]["app_settings"]["skipped"], 1)

    def test_snapshot_externalizes_media_and_uses_manifest_fastpath(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_baza = root / "source_baza"
            target_baza = root / "target_baza"
            source_db = SettingsDatabase(baza_dir=str(source_baza))
            source_db.ensure_ready()

            background_blob = b"release background bytes"
            icon_blob = b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"
            background_hash = hashlib.sha256(background_blob).hexdigest()
            icon_hash = hashlib.sha256(icon_blob).hexdigest()
            background_entry = {
                "id": "release_bg",
                "name": "Release background",
                "file": "release_bg.png",
                "start": "01-01",
                "end": "12-31",
            }
            background_payload = {"version": 1, "backgrounds": [background_entry]}

            with source_db.transaction("test_release_media_seed") as cursor:
                now = now_text()
                cursor.execute(
                    """
                    INSERT INTO app_settings (scope, key, value_json, revision, updated_at, updated_by_role, updated_by_user)
                    VALUES ('shared', 'background_settings', ?, 1, ?, 'system', NULL)
                    """,
                    (json.dumps(background_payload, ensure_ascii=False, sort_keys=True), now),
                )
                cursor.execute(
                    """
                    INSERT INTO ui_backgrounds (
                        background_key, name, scope, kind, value_json, image_blob,
                        image_mime, image_hash, enabled, active, revision, created_at, updated_at
                    )
                    VALUES (?, ?, 'shared', 'image', ?, ?, 'image/png', ?, 1, 1, 1, ?, ?)
                    """,
                    (
                        "release_bg",
                        "Release background",
                        json.dumps(background_entry, ensure_ascii=False, sort_keys=True),
                        background_blob,
                        background_hash,
                        now,
                        now,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO operblock_icons (
                        icon_key, category, target_key, name, default_file, value_json,
                        image_blob, image_mime, image_hash, enabled, sort_order,
                        revision, source, created_at, updated_at
                    )
                    VALUES (?, 'custom', ?, 'Release icon', 'release.svg', ?, ?, 'image/svg+xml', ?, 1, 999, 1, 'manual', ?, ?)
                    """,
                    (
                        "custom:release",
                        "release",
                        json.dumps({"source": "test"}, ensure_ascii=False, sort_keys=True),
                        icon_blob,
                        icon_hash,
                        now,
                        now,
                    ),
                )

            snapshot_path = root / "settings_release_snapshot.json"
            export_report = export_settings_release_snapshot(
                str(source_baza),
                str(snapshot_path),
                release_version="9.9.9",
                release_commit="test",
            )
            snapshot_text = snapshot_path.read_text(encoding="utf-8")
            self.assertGreaterEqual(int(export_report["media_files"]), 2)
            self.assertIn(BLOB_FILE_MARKER, snapshot_text)
            self.assertNotIn(BLOB_BASE64_MARKER, snapshot_text)

            target_service = SettingsService(SettingsDatabase(baza_dir=str(target_baza)))
            target_service.ensure_ready()
            apply_report = apply_settings_release_snapshot(
                target_service.db,
                str(snapshot_path),
                bump_catalog_version=target_service._bump_catalog_version,
            )
            self.assertTrue(apply_report["applied"])

            background_file = (
                Path(get_settings_backgrounds_dir(str(target_baza)))
                / f"bg_{background_hash}.png"
            )
            self.assertEqual(background_file.read_bytes(), background_blob)
            with target_service.db.read_connection() as conn:
                background_row = conn.execute(
                    "SELECT image_blob, image_hash FROM ui_backgrounds WHERE background_key = 'release_bg'"
                ).fetchone()
                icon_row = conn.execute(
                    "SELECT value_json, image_blob, image_hash FROM operblock_icons WHERE icon_key = 'custom:release'"
                ).fetchone()
                background_setting_row = conn.execute(
                    "SELECT value_json FROM app_settings "
                    "WHERE scope = 'shared' AND key = 'background_settings'"
                ).fetchone()
            self.assertIsNotNone(background_row)
            self.assertIsNone(background_row["image_blob"])
            self.assertEqual(background_row["image_hash"], background_hash)
            self.assertIsNotNone(icon_row)
            self.assertIsNone(icon_row["image_blob"])
            self.assertEqual(icon_row["image_hash"], icon_hash)
            applied_background_payload = json.loads(
                background_setting_row["value_json"]
            )
            self.assertEqual(
                applied_background_payload["backgrounds"][0]["file"],
                f"bg_{background_hash}.png",
            )
            icon_value = json.loads(icon_row["value_json"])
            icon_file = Path(get_settings_icon_assets_dir(str(target_baza))) / icon_value["asset_file"]
            self.assertEqual(icon_file.read_bytes(), icon_blob)

            second_report = apply_settings_release_snapshot(
                target_service.db,
                str(snapshot_path),
                bump_catalog_version=target_service._bump_catalog_version,
            )
            self.assertFalse(second_report["applied"])
            self.assertEqual(second_report["reason"], "already_applied")
            self.assertEqual(second_report.get("fast_path"), "manifest")

    def test_stale_manifest_cannot_hide_changed_snapshot_on_fastpath(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_service = SettingsService(SettingsDatabase(baza_dir=str(root / "source")))
            target_service = SettingsService(SettingsDatabase(baza_dir=str(root / "target")))
            source_service.ensure_ready()
            target_service.ensure_ready()
            snapshot_path = root / "settings_release_snapshot.json"
            export_settings_release_snapshot(str(root / "source"), str(snapshot_path))
            first_report = apply_settings_release_snapshot(
                target_service.db,
                str(snapshot_path),
                bump_catalog_version=target_service._bump_catalog_version,
            )
            applied_hash = first_report["snapshot_hash"]

            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snapshot["tables"]["app_settings"].append(
                {
                    "scope": "shared",
                    "key": "stale_manifest_probe",
                    "value_json": '{"value":2}',
                    "revision": 1,
                    "updated_at": now_text(),
                    "updated_by_role": "system",
                    "updated_by_user": None,
                }
            )
            snapshot["content_hash"] = settings_release_module._content_hash(
                settings_release_module._snapshot_payload_for_hash(snapshot)
            )
            self.assertNotEqual(snapshot["content_hash"], applied_hash)
            snapshot_path.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with target_service.db.read_connection() as conn:
                before_count = conn.execute("SELECT COUNT(*) FROM app_settings").fetchone()[0]
                before_hash = conn.execute(
                    "SELECT value FROM settings_meta WHERE key = ?",
                    (settings_release_module.SETTINGS_RELEASE_APPLIED_HASH_KEY,),
                ).fetchone()["value"]

            with self.assertRaisesRegex(ValueError, "manifest content_hash"):
                apply_settings_release_snapshot(
                    target_service.db,
                    str(snapshot_path),
                    bump_catalog_version=target_service._bump_catalog_version,
                )

            with target_service.db.read_connection() as conn:
                after_count = conn.execute("SELECT COUNT(*) FROM app_settings").fetchone()[0]
                after_hash = conn.execute(
                    "SELECT value FROM settings_meta WHERE key = ?",
                    (settings_release_module.SETTINGS_RELEASE_APPLIED_HASH_KEY,),
                ).fetchone()["value"]
                probe = conn.execute(
                    "SELECT 1 FROM app_settings WHERE scope = 'shared' AND key = 'stale_manifest_probe'"
                ).fetchone()
            self.assertEqual(after_count, before_count)
            self.assertEqual(after_hash, before_hash)
            self.assertIsNone(probe)


if __name__ == "__main__":
    unittest.main()
