from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from types import MethodType, SimpleNamespace


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from rem_card.ui.nurse_view import nurse_main_widget as nurse_module  # noqa: E402
from rem_card.ui.nurse_view.nurse_main_widget import NurseMainWidget  # noqa: E402
from rem_card.ui.shared.async_patient_navigation import resolve_card_date  # noqa: E402


def _bind_card_navigation(widget, monkeypatch):
    for name in (
        "_show_resolved_card_date",
        "on_show_card_clicked",
    ):
        setattr(widget, name, MethodType(getattr(NurseMainWidget, name), widget))

    def request_card(service, admission_id, now, exclude_future, _source, deliver):
        deliver(resolve_card_date(service, admission_id, now, exclude_future=exclude_future))

    monkeypatch.setattr(nurse_module, "patient_navigation", lambda _owner: SimpleNamespace(request_card=request_card))


def _freeze_nurse_datetime(now: datetime):
    original_datetime = nurse_module.datetime

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls):
            return now

    nurse_module.datetime = FrozenDateTime
    return original_datetime


def test_show_card_from_historical_nurse_card_opens_latest_when_current_is_missing(monkeypatch):
    now = datetime(2026, 8, 9, 12, 0)
    latest_created = datetime(2026, 8, 8, 8, 0)
    loaded = []
    service = SimpleNamespace(
        has_card=lambda _admission_id, _date: False,
        get_all_card_dates=lambda _admission_id: [latest_created],
    )
    widget = SimpleNamespace(
        remcard_service=service,
        layout_manager=SimpleNamespace(current_admission_id=1),
        load_patient_card=lambda admission_id, target_date: loaded.append((admission_id, target_date)),
    )
    _bind_card_navigation(widget, monkeypatch)
    original_datetime = _freeze_nurse_datetime(now)
    try:
        widget.on_show_card_clicked()
    finally:
        nurse_module.datetime = original_datetime

    assert loaded == [(1, latest_created)]


def test_show_card_from_historical_nurse_card_prefers_existing_current_card(monkeypatch):
    now = datetime(2026, 8, 9, 12, 0)
    latest_created = datetime(2026, 8, 8, 8, 0)
    loaded = []
    service = SimpleNamespace(
        has_card=lambda _admission_id, _date: True,
        get_all_card_dates=lambda _admission_id: [latest_created],
    )
    widget = SimpleNamespace(
        remcard_service=service,
        layout_manager=SimpleNamespace(current_admission_id=1),
        load_patient_card=lambda admission_id, target_date: loaded.append((admission_id, target_date)),
    )
    _bind_card_navigation(widget, monkeypatch)
    original_datetime = _freeze_nurse_datetime(now)
    try:
        widget.on_show_card_clicked()
    finally:
        nurse_module.datetime = original_datetime

    assert loaded == [(1, now)]
