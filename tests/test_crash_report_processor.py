from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.scripts.process_crash_reports import process_crash_reports
from rem_card.services import crash_reports


def _capture_same_failure(role: str):
    try:
        raise ValueError("secret patient text")
    except ValueError:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        return crash_reports.capture_exception(
            "unhandled_python_exception",
            exc_type,
            exc_value,
            exc_traceback,
            role=role,
        )


def test_processor_groups_reports_quarantines_invalid_and_applies_retention(tmp_path, monkeypatch):
    spool = tmp_path / "spool"
    data_root = tmp_path / "Custom RemCard Root"
    monkeypatch.setenv("REMCARD_CRASH_OUTBOX_DIR", str(spool))
    _capture_same_failure("doctor")
    _capture_same_failure("nurse")
    assert crash_reports.flush_local_crash_outbox(data_root)["delivered"] == 2

    root = data_root / "logs" / "diagnostics" / "crashes"
    (root / "incoming" / "broken.json").write_text("not-json", encoding="utf-8")
    old_processed = root / "processed" / "2020" / "01" / "old.json"
    old_processed.parent.mkdir(parents=True, exist_ok=True)
    old_processed.write_text("{}", encoding="utf-8")
    old_time = (datetime.now() - timedelta(days=181)).timestamp()
    os.utime(old_processed, (old_time, old_time))

    result = process_crash_reports(data_root, retention_days=180)

    assert result["processed"] == 2
    assert result["invalid"] == 1
    assert result["removed_older_than_days"] >= 1
    assert not old_processed.exists()
    summary = Path(result["summary_path"]).read_text(encoding="utf-8")
    assert "Обработано событий: 2" in summary
    assert "Повторений: 2" in summary
    assert "doctor" in summary and "nurse" in summary
    assert len(list((root / "quarantine").glob("*.invalid.json"))) == 1
    assert list((root / "incoming").glob("*.json")) == []
