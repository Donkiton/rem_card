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

from rem_card.app.settings_db_paths import (  # noqa: E402
    get_settings_backgrounds_dir,
    get_settings_icon_assets_dir,
)
from rem_card.data.settings.settings_db import SettingsDatabase  # noqa: E402
from rem_card.data.settings.settings_release import export_settings_release_snapshot  # noqa: E402
from rem_card.data.settings.settings_schema import now_text  # noqa: E402
from rem_card.services.settings.settings_service import SettingsService  # noqa: E402


class SettingsMediaStorageTest(unittest.TestCase):
    def test_blob_migration_has_dry_run_and_moves_media_to_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            baza_dir = str(Path(tmp) / "baza")
            service = SettingsService(SettingsDatabase(baza_dir=baza_dir))
            service.ensure_ready()
            background_blob = b"background image"
            icon_blob = b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"
            background_value = {"id": "test_bg", "file": "test_bg.png"}
            icon_value = {"source": "test"}
            now = now_text()
            with service.db.transaction("test_media_migration_seed") as cursor:
                cursor.execute(
                    """
                    INSERT INTO ui_backgrounds (
                        background_key, name, scope, kind, value_json, image_blob,
                        image_mime, image_hash, enabled, active, revision, created_at, updated_at
                    ) VALUES (?, ?, 'shared', 'image', ?, ?, 'image/png', ?, 1, 0, 1, ?, ?)
                    """,
                    (
                        "test_bg",
                        "Test background",
                        json.dumps(background_value),
                        background_blob,
                        hashlib.sha256(background_blob).hexdigest(),
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
                    ) VALUES (?, 'custom', ?, ?, 'test.svg', ?, ?, 'image/svg+xml', ?, 1, 999, 1, 'manual', ?, ?)
                    """,
                    (
                        "custom:test_media",
                        "test_media",
                        "Test icon",
                        json.dumps(icon_value),
                        icon_blob,
                        hashlib.sha256(icon_blob).hexdigest(),
                        now,
                        now,
                    ),
                )

            preview = service.migrate_media_blobs_to_files()
            self.assertFalse(preview["applied"])
            self.assertEqual(preview["backgrounds_pending"], 1)
            self.assertEqual(preview["icons_pending"], 1)

            report = service.migrate_media_blobs_to_files(apply=True, compact=True)
            self.assertTrue(report["applied"])
            self.assertEqual(report["backgrounds_migrated"], 1)
            self.assertEqual(report["icons_migrated"], 1)
            self.assertLessEqual(report["compaction"]["size_after"], report["compaction"]["size_before"])

            background_path = Path(get_settings_backgrounds_dir(baza_dir)) / "test_bg.png"
            self.assertEqual(background_path.read_bytes(), background_blob)
            with service.db.read_connection() as conn:
                background_row = conn.execute(
                    "SELECT image_blob FROM ui_backgrounds WHERE background_key = 'test_bg'"
                ).fetchone()
                icon_row = conn.execute(
                    "SELECT value_json, image_blob FROM operblock_icons WHERE icon_key = 'custom:test_media'"
                ).fetchone()
            self.assertIsNone(background_row["image_blob"])
            self.assertIsNone(icon_row["image_blob"])
            asset_file = json.loads(icon_row["value_json"])["asset_file"]
            asset_path = Path(get_settings_icon_assets_dir(baza_dir)) / asset_file
            self.assertEqual(asset_path.read_bytes(), icon_blob)

            _version, loaded = service.get_operblock_icon_records(
                ["custom:test_media"],
                include_blob=True,
                ensure_defaults=False,
            )
            self.assertEqual(loaded["custom:test_media"]["image_blob"], icon_blob)

            snapshot_path = str(Path(tmp) / "settings_release_snapshot.json")
            export_report = export_settings_release_snapshot(baza_dir, snapshot_path)
            self.assertGreaterEqual(int(export_report["media_files"]), 2)


if __name__ == "__main__":
    unittest.main()
