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
                    VALUES (?, ?, 'shared', 'image', ?, NULL, 'image/png', ?, 1, 1, 1, ?, ?)
                    """,
                    (
                        "test_bg",
                        "Test background",
                        json.dumps(background_entry, ensure_ascii=False, sort_keys=True),
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


if __name__ == "__main__":
    unittest.main()
