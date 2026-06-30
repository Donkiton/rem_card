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

from rem_card.app.settings_db_paths import get_settings_backgrounds_dir  # noqa: E402
from rem_card.data.settings.settings_db import SettingsDatabase  # noqa: E402
from rem_card.data.settings.settings_release import (  # noqa: E402
    BLOB_BASE64_MARKER,
    BLOB_FILE_MARKER,
    apply_settings_release_snapshot,
    export_settings_release_snapshot,
)
from rem_card.data.settings.settings_schema import now_text  # noqa: E402
from rem_card.services.settings.settings_service import SettingsService  # noqa: E402


class SettingsReleaseSnapshotTest(unittest.TestCase):
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

            background_file = Path(get_settings_backgrounds_dir(str(target_baza))) / "release_bg.png"
            self.assertEqual(background_file.read_bytes(), background_blob)
            with target_service.db.read_connection() as conn:
                background_row = conn.execute(
                    "SELECT image_blob, image_hash FROM ui_backgrounds WHERE background_key = 'release_bg'"
                ).fetchone()
                icon_row = conn.execute(
                    "SELECT image_blob, image_hash FROM operblock_icons WHERE icon_key = 'custom:release'"
                ).fetchone()
            self.assertIsNotNone(background_row)
            self.assertIsNone(background_row["image_blob"])
            self.assertEqual(background_row["image_hash"], background_hash)
            self.assertIsNotNone(icon_row)
            self.assertEqual(icon_row["image_blob"], icon_blob)
            self.assertEqual(icon_row["image_hash"], icon_hash)

            snapshot_path.write_text("{ broken json", encoding="utf-8")
            second_report = apply_settings_release_snapshot(
                target_service.db,
                str(snapshot_path),
                bump_catalog_version=target_service._bump_catalog_version,
            )
            self.assertFalse(second_report["applied"])
            self.assertEqual(second_report["reason"], "already_applied")
            self.assertEqual(second_report.get("fast_path"), "manifest")


if __name__ == "__main__":
    unittest.main()
