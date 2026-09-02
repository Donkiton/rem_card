from datetime import datetime
from types import MethodType, SimpleNamespace

from rem_card.ui.doctor_view.doctor_remcard_widget import DoctorRemCardWidget


def test_current_archive_card_defers_initial_status_write_to_snapshot_worker():
    direct_status_writes = []
    reload_requests = []
    selected_date = datetime(2026, 9, 2, 9, 47, 3)

    status_service = SimpleNamespace(
        ensure_initial_status=lambda *args: direct_status_writes.append(args),
    )
    widget = SimpleNamespace(
        _is_loading=False,
        _archive_read_only_mode=False,
        admission_id=37,
        service=SimpleNamespace(status_service=status_service),
        layout_manager=SimpleNamespace(),
        _ensure_card_widgets_initialized=lambda: None,
        _should_ensure_initial_status_for_date=lambda _date: True,
        blockSignals=lambda _blocked: None,
        force_reload_all=lambda **kwargs: reload_requests.append(kwargs),
        _update_yesterday_button_state=lambda: None,
        _apply_archive_read_only_state=lambda: None,
        update=lambda: None,
    )
    widget.safe_load_archived_card = MethodType(
        DoctorRemCardWidget.safe_load_archived_card,
        widget,
    )

    widget.safe_load_archived_card(selected_date)

    assert direct_status_writes == []
    assert reload_requests == [{"ensure_initial_status": True}]
    assert widget.current_date == selected_date
    assert widget._is_loading is False
