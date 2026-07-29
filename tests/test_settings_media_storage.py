from __future__ import annotations

import hashlib
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

from rem_card.app.settings_db_paths import (  # noqa: E402
    get_settings_backgrounds_dir,
    get_settings_backgrounds_dir_from_db_path,
    get_settings_icon_assets_dir,
)
from rem_card.app.settings_media_cache import materialize_media_cache  # noqa: E402
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
            background_payload = {
                "version": 1,
                "backgrounds": [
                    {
                        "id": "default",
                        "name": "Стандартный фон",
                        "file": "fon.png",
                        "start": "01-01",
                        "end": "12-31",
                        "locked": True,
                    },
                    {
                        **background_value,
                        "name": "Test background",
                        "start": "01-01",
                        "end": "01-02",
                    },
                ],
            }
            icon_value = {"source": "test"}
            now = now_text()
            with service.db.transaction("test_media_migration_seed") as cursor:
                cursor.execute(
                    """
                    UPDATE app_settings
                    SET value_json = ?, revision = revision + 1, updated_at = ?
                    WHERE scope = 'shared' AND key = 'background_settings'
                    """,
                    (json.dumps(background_payload), now),
                )
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
            backgrounds_dir = Path(get_settings_backgrounds_dir(baza_dir))
            backgrounds_dir.mkdir(parents=True, exist_ok=True)
            (backgrounds_dir / "old_unused.png").write_bytes(b"unused")

            preview = service.migrate_media_blobs_to_files()
            self.assertFalse(preview["applied"])
            self.assertEqual(preview["backgrounds_pending"], 1)
            self.assertEqual(preview["icons_pending"], 1)
            self.assertIn("old_unused.png", preview["background_files_to_remove_names"])

            report = service.migrate_media_blobs_to_files(apply=True, compact=True)
            self.assertTrue(report["applied"])
            self.assertEqual(report["backgrounds_migrated"], 1)
            self.assertEqual(report["icons_migrated"], 1)
            self.assertLessEqual(report["compaction"]["size_after"], report["compaction"]["size_before"])

            background_name = f"bg_{hashlib.sha256(background_blob).hexdigest()}.png"
            background_path = Path(get_settings_backgrounds_dir(baza_dir)) / background_name
            self.assertEqual(background_path.read_bytes(), background_blob)
            self.assertFalse((backgrounds_dir / "old_unused.png").exists())
            with service.db.read_connection() as conn:
                background_row = conn.execute(
                    "SELECT value_json, image_blob, image_hash, image_size_bytes "
                    "FROM ui_backgrounds WHERE background_key = 'test_bg'"
                ).fetchone()
                icon_row = conn.execute(
                    "SELECT value_json, image_blob FROM operblock_icons WHERE icon_key = 'custom:test_media'"
                ).fetchone()
            self.assertIsNone(background_row["image_blob"])
            self.assertEqual(background_row["image_hash"], hashlib.sha256(background_blob).hexdigest())
            self.assertEqual(background_row["image_size_bytes"], len(background_blob))
            self.assertEqual(json.loads(background_row["value_json"])["file"], background_name)
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

    def test_media_directories_follow_the_actual_settings_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "custom_settings" / "renamed.db"
            self.assertEqual(
                Path(get_settings_backgrounds_dir_from_db_path(str(db_path))),
                db_path.parent / "backgrounds",
            )

    def test_local_media_cache_is_hash_addressed_and_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "shared" / "background.png"
            source.parent.mkdir(parents=True)
            content = b"cached background"
            source.write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            cache_dir = root / "cache"
            previous = os.environ.get("REMCARD_MEDIA_CACHE_DIR")
            os.environ["REMCARD_MEDIA_CACHE_DIR"] = str(cache_dir)
            try:
                first = materialize_media_cache(
                    source_path=str(source),
                    settings_db_path=str(root / "settings" / "remcard_settings.db"),
                    kind="backgrounds",
                    image_hash=digest,
                    expected_size=len(content),
                )
                source.unlink()
                second = materialize_media_cache(
                    source_path=str(source),
                    settings_db_path=str(root / "settings" / "remcard_settings.db"),
                    kind="backgrounds",
                    image_hash=digest,
                    expected_size=len(content),
                )
            finally:
                if previous is None:
                    os.environ.pop("REMCARD_MEDIA_CACHE_DIR", None)
                else:
                    os.environ["REMCARD_MEDIA_CACHE_DIR"] = previous
            self.assertEqual(first, second)
            self.assertEqual(Path(second).read_bytes(), content)


if __name__ == "__main__":
    unittest.main()
