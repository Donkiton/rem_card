from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.data.settings.settings_db import SettingsDatabase, SettingsDbError  # noqa: E402
from rem_card.data.settings.settings_schema import now_text  # noqa: E402
from rem_card.services.settings.settings_service import (  # noqa: E402
    BACKGROUND_SETTINGS_KEY,
    SETTINGS_STARTUP_WRITE_BUSY_REASON,
    SettingsService,
)


def _write_settings_lock(lock_path: str, *, source: str = "settings_test_holder") -> None:
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "timestamp": time.time(),
                "pid": os.getpid() + 100000,
                "host": socket.gethostname(),
                "user_id": "test-holder",
                "source": source,
                "thread_id": 1,
            },
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )


class SettingsStartupWritesTest(unittest.TestCase):
    def test_app_settings_are_served_from_memory_after_startup_prime(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = SettingsDatabase(baza_dir=str(Path(tmp) / "baza"))
            service = SettingsService(db)
            service.ensure_ready()
            expected = service.get_app_setting(
                "shared",
                "background_settings",
                default={},
            )

            with patch.object(
                db,
                "read_connection",
                side_effect=AssertionError("unexpected synchronous settings read"),
            ):
                cached = service.get_app_setting(
                    "shared",
                    "background_settings",
                    default={},
                )

            self.assertEqual(cached, expected)

    def test_operblock_startup_never_repairs_backgrounds(self):
        for role in ("operblock", "operblock_planned", "operblock_emergency"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as tmp:
                db = SettingsDatabase(baza_dir=str(Path(tmp) / "baza"))
                db.ensure_ready()
                service = SettingsService(db)

                with (
                    patch.dict(os.environ, {"REMCARD_UI_ROLE": role}),
                    patch.object(service, "_ensure_legacy_import"),
                    patch.object(service, "_ensure_operblock_settings_imported", return_value=None),
                    patch.object(service, "_apply_bundled_release_snapshot_if_needed", return_value=None),
                    patch.object(service, "_repair_background_settings_from_rows") as background_repair,
                    patch.object(service, "_ensure_default_operblock_icons", return_value=None),
                ):
                    info = service.ensure_ready()

                background_repair.assert_not_called()
                self.assertEqual(
                    info["background_settings_startup"],
                    {"skipped": True, "reason": "role_has_no_background"},
                )

    def test_release_snapshot_write_is_skipped_when_settings_lock_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = SettingsDatabase(baza_dir=str(root / "baza"))
            db.ensure_ready()
            service = SettingsService(db)
            snapshot_path = root / "settings_release_snapshot.json"
            snapshot_path.write_text("{}", encoding="utf-8")
            _write_settings_lock(db.lock_path, source="settings_background_settings_repair")

            with (
                patch("rem_card.app.runtime_paths.is_compiled", return_value=True),
                patch("rem_card.data.settings.settings_release.find_release_snapshot_path", return_value=str(snapshot_path)),
                patch("rem_card.data.settings.settings_release.apply_settings_release_snapshot") as apply_mock,
            ):
                report = service._apply_bundled_release_snapshot_if_needed()

            apply_mock.assert_not_called()
            self.assertTrue(report["skipped"])
            self.assertEqual(report["reason"], SETTINGS_STARTUP_WRITE_BUSY_REASON)
            self.assertEqual(report["source"], "settings_release_snapshot_apply")
            self.assertEqual(report["holder"]["holder_source"], "settings_background_settings_repair")

    def test_release_snapshot_busy_transaction_returns_startup_warning_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = SettingsDatabase(baza_dir=str(root / "baza"))
            db.ensure_ready()
            service = SettingsService(db)
            snapshot_path = root / "settings_release_snapshot.json"
            snapshot_path.write_text("{}", encoding="utf-8")

            with (
                patch("rem_card.app.runtime_paths.is_compiled", return_value=True),
                patch("rem_card.data.settings.settings_release.find_release_snapshot_path", return_value=str(snapshot_path)),
                patch(
                    "rem_card.data.settings.settings_release.apply_settings_release_snapshot",
                    side_effect=SettingsDbError("БД настроек временно занята другим рабочим местом."),
                ),
            ):
                report = service._apply_bundled_release_snapshot_if_needed()

            self.assertTrue(report["skipped"])
            self.assertEqual(report["reason"], SETTINGS_STARTUP_WRITE_BUSY_REASON)
            self.assertIn("warning", report)

    def test_background_repair_write_is_skipped_when_settings_lock_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = SettingsDatabase(baza_dir=str(root / "baza"))
            db.ensure_ready()
            service = SettingsService(db)
            now = now_text()
            background_entry = {
                "id": "test_bg",
                "name": "Test background",
                "file": "test_bg.png",
                "start": "01-01",
                "end": "12-31",
            }
            with db.transaction("test_background_seed") as cursor:
                cursor.execute(
                    """
                    INSERT INTO ui_backgrounds (
                        background_key, name, scope, kind, value_json, image_blob,
                        image_mime, image_hash, enabled, active, revision, created_at, updated_at
                    )
                    VALUES (?, ?, 'shared', 'image', ?, ?, 'image/png', ?, 1, 1, 1, ?, ?)
                    """,
                    (
                        "test_bg",
                        "Test background",
                        json.dumps(background_entry, ensure_ascii=False, sort_keys=True),
                        b"test image bytes",
                        "hash",
                        now,
                        now,
                    ),
                )
            _write_settings_lock(db.lock_path, source="settings_release_snapshot_apply")

            report = service._repair_background_settings_from_rows()

            self.assertIsNotNone(report)
            self.assertTrue(report["skipped"])
            self.assertEqual(report["reason"], SETTINGS_STARTUP_WRITE_BUSY_REASON)
            with db.read_connection() as conn:
                row = conn.execute(
                    "SELECT 1 FROM app_settings WHERE scope = 'shared' AND key = 'background_settings'"
                ).fetchone()
            self.assertIsNone(row)

    def test_background_save_disables_removed_rows_so_repair_does_not_restore_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = SettingsDatabase(baza_dir=str(root / "baza"))
            db.ensure_ready()
            service = SettingsService(db)
            now = now_text()
            keep_entry = {
                "id": "keep_bg",
                "name": "Keep background",
                "file": "keep.png",
                "start": "01-01",
                "end": "01-02",
            }
            removed_entry = {
                "id": "removed_bg",
                "name": "Removed background",
                "file": "removed.png",
                "start": "02-01",
                "end": "02-02",
            }
            with db.transaction("test_background_seed") as cursor:
                for entry in (keep_entry, removed_entry):
                    cursor.execute(
                        """
                        INSERT INTO ui_backgrounds (
                            background_key, name, scope, kind, value_json, image_blob,
                            image_mime, image_hash, enabled, active, revision, created_at, updated_at
                        )
                        VALUES (?, ?, 'shared', 'image', ?, ?, 'image/png', ?, 1, 0, 1, ?, ?)
                        """,
                        (
                            entry["id"],
                            entry["name"],
                            json.dumps(entry, ensure_ascii=False, sort_keys=True),
                            b"test image bytes",
                            f"hash-{entry['id']}",
                            now,
                            now,
                        ),
                    )

            payload = {
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
                    keep_entry,
                ],
            }
            with db.transaction("test_background_save") as cursor:
                service._write_app_setting_in_tx(
                    cursor,
                    "shared",
                    "background_settings",
                    payload,
                    changed_by_role="doctor",
                    catalog_key=BACKGROUND_SETTINGS_KEY,
                    log_change=False,
                )
                service._sync_background_rows_in_tx(cursor, payload)

            with db.read_connection() as conn:
                removed_row = conn.execute(
                    "SELECT enabled, active FROM ui_backgrounds WHERE background_key = 'removed_bg'"
                ).fetchone()
                keep_row = conn.execute(
                    "SELECT enabled FROM ui_backgrounds WHERE background_key = 'keep_bg'"
                ).fetchone()
            self.assertIsNotNone(removed_row)
            self.assertEqual(int(removed_row["enabled"]), 0)
            self.assertEqual(int(removed_row["active"]), 0)
            self.assertEqual(int(keep_row["enabled"]), 1)

            repair_report = service._repair_background_settings_from_rows()

            self.assertIsNone(repair_report)
            with db.read_connection() as conn:
                saved_row = conn.execute(
                    "SELECT value_json FROM app_settings WHERE scope = 'shared' AND key = 'background_settings'"
                ).fetchone()
            self.assertIsNotNone(saved_row)
            saved = json.loads(saved_row["value_json"])
            saved_ids = {
                str(item.get("id") or "")
                for item in saved.get("backgrounds", [])
                if isinstance(item, dict)
            }
            self.assertIn("keep_bg", saved_ids)
            self.assertNotIn("removed_bg", saved_ids)

    def test_background_repair_skips_rows_without_file_or_blob(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = SettingsDatabase(baza_dir=str(root / "baza"))
            db.ensure_ready()
            service = SettingsService(db)
            now = now_text()
            missing_entry = {
                "id": "missing_bg",
                "name": "Missing background",
                "file": "missing.png",
                "start": "01-01",
                "end": "01-02",
            }
            with db.transaction("test_background_missing_seed") as cursor:
                cursor.execute(
                    """
                    INSERT INTO ui_backgrounds (
                        background_key, name, scope, kind, value_json, image_blob,
                        image_mime, image_hash, enabled, active, revision, created_at, updated_at
                    )
                    VALUES (?, ?, 'shared', 'image', ?, NULL, NULL, NULL, 1, 0, 1, ?, ?)
                    """,
                    (
                        "missing_bg",
                        "Missing background",
                        json.dumps(missing_entry, ensure_ascii=False, sort_keys=True),
                        now,
                        now,
                    ),
                )

            report = service._repair_background_settings_from_rows()

            self.assertIsNone(report)
            with db.read_connection() as conn:
                row = conn.execute(
                    "SELECT 1 FROM app_settings WHERE scope = 'shared' AND key = 'background_settings'"
                ).fetchone()
            self.assertIsNone(row)

    def test_background_repair_prunes_missing_app_entries_without_blob(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = SettingsDatabase(baza_dir=str(root / "baza"))
            db.ensure_ready()
            service = SettingsService(db)
            now = now_text()
            missing_entry = {
                "id": "missing_bg",
                "name": "Missing background",
                "file": "missing.png",
                "start": "01-01",
                "end": "01-02",
            }
            payload = {
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
                    missing_entry,
                ],
            }
            with db.transaction("test_background_missing_app_seed") as cursor:
                cursor.execute(
                    """
                    INSERT INTO app_settings (scope, key, value_json, revision, updated_at, updated_by_role, updated_by_user)
                    VALUES ('shared', 'background_settings', ?, 1, ?, 'repair', NULL)
                    """,
                    (json.dumps(payload, ensure_ascii=False, sort_keys=True), now),
                )
                cursor.execute(
                    """
                    INSERT INTO ui_backgrounds (
                        background_key, name, scope, kind, value_json, image_blob,
                        image_mime, image_hash, enabled, active, revision, created_at, updated_at
                    )
                    VALUES (?, ?, 'shared', 'image', ?, NULL, NULL, NULL, 1, 0, 1, ?, ?)
                    """,
                    (
                        "missing_bg",
                        "Missing background",
                        json.dumps(missing_entry, ensure_ascii=False, sort_keys=True),
                        now,
                        now,
                    ),
                )

            report = service._repair_background_settings_from_rows()

            self.assertIsNotNone(report)
            self.assertTrue(report["repaired"])
            self.assertEqual(report["restored_ids"], [])
            self.assertEqual(report["pruned_ids"], ["missing_bg"])
            with db.read_connection() as conn:
                row = conn.execute(
                    "SELECT value_json FROM app_settings WHERE scope = 'shared' AND key = 'background_settings'"
                ).fetchone()
            self.assertIsNotNone(row)
            saved = json.loads(row["value_json"])
            saved_ids = [
                str(item.get("id") or "")
                for item in saved.get("backgrounds", [])
                if isinstance(item, dict)
            ]
            self.assertIn("default", saved_ids)
            self.assertNotIn("missing_bg", saved_ids)

    def test_background_repair_restores_rows_with_blob_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = SettingsDatabase(baza_dir=str(root / "baza"))
            db.ensure_ready()
            service = SettingsService(db)
            now = now_text()
            blob_entry = {
                "id": "blob_bg",
                "name": "Blob background",
                "file": "blob.png",
                "start": "01-01",
                "end": "01-02",
            }
            with db.transaction("test_background_blob_seed") as cursor:
                cursor.execute(
                    """
                    INSERT INTO ui_backgrounds (
                        background_key, name, scope, kind, value_json, image_blob,
                        image_mime, image_hash, enabled, active, revision, created_at, updated_at
                    )
                    VALUES (?, ?, 'shared', 'image', ?, ?, 'image/png', ?, 1, 0, 1, ?, ?)
                    """,
                    (
                        "blob_bg",
                        "Blob background",
                        json.dumps(blob_entry, ensure_ascii=False, sort_keys=True),
                        b"test image bytes",
                        "hash",
                        now,
                        now,
                    ),
                )

            report = service._repair_background_settings_from_rows()

            self.assertIsNotNone(report)
            self.assertTrue(report["repaired"])
            self.assertEqual(report["restored_ids"], ["blob_bg"])
            with db.read_connection() as conn:
                saved_row = conn.execute(
                    "SELECT value_json FROM app_settings WHERE scope = 'shared' AND key = 'background_settings'"
                ).fetchone()
            self.assertIsNotNone(saved_row)
            saved = json.loads(saved_row["value_json"])
            saved_ids = [
                str(item.get("id") or "")
                for item in saved.get("backgrounds", [])
                if isinstance(item, dict)
            ]
            self.assertIn("blob_bg", saved_ids)


if __name__ == "__main__":
    unittest.main()
