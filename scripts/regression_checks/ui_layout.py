"""Safety-сценарии: ui_layout."""

from __future__ import annotations

from .common import PROJECT_ROOT
from pathlib import Path
from .common import _cached_source_segment
import ast
from datetime import datetime
import os
import subprocess
import sys
import textwrap
import time


def _class_methods_from_source(source: str, class_name: str):
    tree = ast.parse(source)
    class_defs = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name]
    if not class_defs:
        return None
    return {node.name: node for node in class_defs[0].body if isinstance(node, ast.FunctionDef)}


def _method_source(source: str, methods: dict, name: str) -> str:
    node = methods.get(name)
    return _cached_source_segment(source, node) if node is not None else ""


def _check_lazy_full_card_role_contract(
    role: str,
    source_path: Path,
    class_name: str,
    full_layout_name: str,
) -> tuple[bool, str]:
    source = source_path.read_text(encoding="utf-8")
    methods = _class_methods_from_source(source, class_name)
    if methods is None:
        return False, f"{role}: {class_name} not found"

    init_source = _method_source(source, methods, "init_ui")
    ensure_source = _method_source(source, methods, "_ensure_full_layout")
    load_source = _method_source(source, methods, "load_patient_card")
    if not init_source or not ensure_source or not load_source:
        return False, f"{role}: lazy layout methods missing"
    if "LightweightW1Shell" not in init_source:
        return False, f"{role}: startup must create LightweightW1Shell"
    if full_layout_name in init_source:
        return False, f"{role}: init_ui must not create {full_layout_name}"
    if full_layout_name not in ensure_source:
        return False, f"{role}: _ensure_full_layout must create {full_layout_name}"
    for marker in ("_full_layout_created", "_retire_w1_shell", "_wire_full_layout_signals"):
        if marker not in ensure_source:
            return False, f"{role}: _ensure_full_layout missing {marker}"
    if "_ensure_full_layout(reason=\"patient_open\")" not in load_source:
        return False, f"{role}: patient open must ensure full layout first"
    if "_patient_open_generation" not in load_source or "_set_nurse_orders_context_if_current" not in source:
        return False, f"{role}: deferred patient context must use generation guard"

    prewarm_source = _method_source(source, methods, "_schedule_card_ui_prewarm")
    if "_ensure_full_layout(reason=\"idle_prewarm\")" not in prewarm_source:
        return False, f"{role}: idle card UI prewarm must prepare the full layout before first patient open"
    selection_source = _method_source(source, methods, "_on_selection_mode_changed")
    if "ignored stale beds selection signal" not in selection_source:
        return False, f"{role}: stale beds selection signal guard missing"
    show_beds_source = _method_source(source, methods, "show_beds_mode")
    if role == "doctor" and "layout.bottom_row.show()" in show_beds_source and "_full_layout_created" not in show_beds_source:
        return False, "doctor: shell bottom_row must not be shown before full layout exists"
    return True, "ok"


def _check_lazy_full_card_main_window_contract(root: Path) -> tuple[bool, str]:
    main_source = (root / "ui" / "main_window.py").read_text(encoding="utf-8")
    forbidden = (
        "doctor_main.remcard_widget.layout_manager",
        "nurse_main.layout_manager",
        ".layout_manager.set_patient_selection_mode(\"beds\")",
    )
    for marker in forbidden:
        if marker in main_source:
            return False, f"MainWindow must use role-level API instead of {marker}"
    for marker in ("doctor_main.reset_to_beds()", "nurse_main.reset_to_beds()"):
        if marker not in main_source:
            return False, f"MainWindow missing role-level beds reset call: {marker}"
    return True, "ok"


def _check_lazy_full_card_layout_contract(temp_root: str) -> tuple[bool, str]:
    from .ui import _check_lazy_w1_shell_contract
    _ = temp_root
    root = PROJECT_ROOT

    ok, details = _check_lazy_w1_shell_contract(root)
    if not ok:
        return ok, details

    cases = [
        (
            "doctor",
            root / "ui" / "doctor_view" / "doctor_remcard_widget.py",
            "DoctorRemCardWidget",
            "RemCardLayoutManager",
        ),
        (
            "nurse",
            root / "ui" / "nurse_view" / "nurse_main_widget.py",
            "NurseMainWidget",
            "NurseRemCardLayoutManager",
        ),
    ]
    for role, source_path, class_name, full_layout_name in cases:
        ok, details = _check_lazy_full_card_role_contract(role, source_path, class_name, full_layout_name)
        if not ok:
            return ok, details

    return _check_lazy_full_card_main_window_contract(root)


def _check_w1_days_label_scope_by_bed_type(temp_root: str) -> tuple[bool, str]:
    probe = textwrap.dedent(
        """
        import os
        import sys

        from _local_rem_card_bootstrap import bootstrap_local_rem_card

        bootstrap_local_rem_card()

        from scripts.regression_safety_checks import (
            _check_w1_days_label_scope_by_bed_type_runtime,
            _prepare_import_environment,
        )

        temp_root = sys.argv[1]
        _prepare_import_environment(temp_root)
        ok, details = _check_w1_days_label_scope_by_bed_type_runtime(temp_root)
        print(details)
        raise SystemExit(0 if ok else 1)
        """
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    result = subprocess.run(
        [sys.executable, "-c", probe, temp_root],
        cwd=str(PROJECT_ROOT),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=45,
    )
    if result.returncode != 0:
        return False, f"w1 days label runtime probe failed rc={result.returncode}: {(result.stderr or result.stdout)[-800:]}"
    return True, (result.stdout or "ok").strip().splitlines()[-1]


def _check_w1_days_label_scope_by_bed_type_runtime(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtWidgets import QApplication

    from rem_card.ui.rem_card_sectors.sector_4_sub import Sector4b

    class _Patient:
        def __init__(self, bed_number: int, admission_datetime: datetime):
            self.id = bed_number
            self.bed_number = bed_number
            self.history_number = f"REG-{bed_number}"
            self.admission_datetime = admission_datetime
            self.diagnosis_text = ""

        def get_display_name(self):
            return f"Пациент {self.bed_number}"

        def get_display_age(self, current_date):
            _ = current_date
            return "40 лет"

    app = QApplication.instance() or QApplication([])
    widget = Sector4b()
    try:
        current_date = datetime(2026, 5, 7, 9, 25)
        ordinary_patient = _Patient(1, datetime(2026, 5, 6, 9, 0))
        recovery_patient = _Patient(11, datetime(2026, 5, 7, 8, 3))

        widget.update_patient_info(ordinary_patient, current_date)
        if widget.lbl_days.text() != "Сутки: 2":
            return False, f"ordinary bed must use ICU day label, got: {widget.lbl_days.text()!r}"

        widget.update_patient_info(recovery_patient, current_date)
        if widget.lbl_days.text() != "Время в отделении: 1ч 20м":
            return False, f"recovery bed must use 10-minute department time, got: {widget.lbl_days.text()!r}"

        widget.update_patient_info(ordinary_patient, current_date)
        if widget.lbl_days.text() != "Сутки: 2":
            return False, f"ordinary bed label must recover after recovery patient, got: {widget.lbl_days.text()!r}"

        return True, "ok"
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def _check_w1a_display_settings_sleep_behavior(temp_root: str) -> tuple[bool, str]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtWidgets import QApplication

    from rem_card.ui.shared.display_settings_storage import DisplaySettingsStorage
    from rem_card.ui.rem_card_sectors.sector_w1a import SectorW1a
    from rem_card.ui.rem_card_sectors.sector_w1b import SectorW1b

    app = QApplication.instance() or QApplication([])
    display_settings_path = Path(temp_root) / "display_settings_w1a_regression.json"
    saved_display_settings_path = os.environ.get("REMCARD_DISPLAY_SETTINGS_PATH")
    os.environ["REMCARD_DISPLAY_SETTINGS_PATH"] = str(display_settings_path)

    class _CountingW1aService:
        def __init__(self):
            self.calls = 0

        def build_w1a_upcoming_orders_snapshot(self, *_args):
            self.calls += 1
            return {"content_hash": "disabled", "change_id": 1, "rows": []}

    disabled_widget = None
    disabled_w1b_widget = None
    try:
        storage = DisplaySettingsStorage()
        payload = storage.load()
        payload["active"]["doctor"]["w1a_upcoming_orders"]["enabled"] = False
        payload["active"]["nurse"]["w1a_upcoming_orders"]["enabled"] = True
        payload["active"]["doctor"]["w1b_lower_sector"]["enabled"] = False
        payload["active"]["nurse"]["w1b_lower_sector"]["enabled"] = True
        storage.save(payload)

        disabled_service = _CountingW1aService()
        disabled_widget = SectorW1a(service=disabled_service, role="doctor")
        disabled_widget.show()
        for _ in range(4):
            app.processEvents()
        disabled_widget.refresh_data(force=True)
        disabled_widget.handle_data_changes({"forced": True})
        for _ in range(4):
            app.processEvents()

        if disabled_service.calls != 0:
            return False, f"disabled doctor W1a must not call snapshot loader, got {disabled_service.calls}"
        if disabled_widget._refresh_worker is not None:
            return False, "disabled doctor W1a must not create refresh worker"
        if disabled_widget.main_container.isVisible():
            return False, "disabled doctor W1a must not render sector content"
        if disabled_widget._time_timer.isActive() or disabled_widget._refresh_timer.isActive():
            return False, "disabled doctor W1a must keep timers asleep"

        disabled_w1b_widget = SectorW1b(role="doctor")
        disabled_w1b_widget.show()
        app.processEvents()
        if disabled_w1b_widget.main_container.isVisible():
            return False, "disabled doctor W1b must not render lower sector content"
        if disabled_w1b_widget.maximumHeight() != 0:
            return False, "disabled doctor W1b must collapse to zero maximum height"
        if disabled_w1b_widget.sizeHint().height() != 0 or disabled_w1b_widget.minimumSizeHint().height() != 0:
            return False, "disabled doctor W1b must report zero layout hints"
    finally:
        if disabled_widget is not None:
            disabled_widget.close()
            disabled_widget.deleteLater()
            app.processEvents()
        if disabled_w1b_widget is not None:
            disabled_w1b_widget.close()
            disabled_w1b_widget.deleteLater()
            app.processEvents()
        if saved_display_settings_path is None:
            os.environ.pop("REMCARD_DISPLAY_SETTINGS_PATH", None)
        else:
            os.environ["REMCARD_DISPLAY_SETTINGS_PATH"] = saved_display_settings_path

    return True, "ok"


def _w1a_card_gaps(group: dict) -> list[int]:
    body_layout = group["layout"]
    card_gaps = []
    previous_geometry = None
    for index in range(body_layout.count()):
        item = body_layout.itemAt(index)
        card = item.widget() if item is not None else None
        if card is None:
            continue
        geometry = card.geometry()
        if previous_geometry is not None:
            card_gaps.append(geometry.y() - (previous_geometry.y() + previous_geometry.height()))
        previous_geometry = geometry
    return card_gaps


def _check_w1a_long_order_card(long_card) -> tuple[bool, str]:
    if long_card is None:
        return False, "W1a long multi-component card is missing"
    for label_name in ("lbl_line1", "lbl_line2", "lbl_method_dur"):
        label = getattr(long_card, label_name)
        if label.isVisible() and label.height() < label.heightForWidth(label.width()):
            return False, f"W1a long order clips {label_name}: {label.height()} < {label.heightForWidth(label.width())}"
    if long_card.lbl_line1.font().pixelSize() != 12 or long_card.lbl_method_dur.font().pixelSize() != 11:
        return False, "W1a long order must keep NurseOrderCard font sizes unchanged"
    return True, "ok"


def _check_w1a_rendered_layout(widget) -> tuple[bool, str]:
    ordered_groups = sorted(widget.groups.values(), key=lambda group: group["frame"].geometry().y())
    if len(ordered_groups) != 2:
        return False, f"W1a layout gap check expected 2 groups, got {len(ordered_groups)}"

    first_group = ordered_groups[0]
    card_gaps = _w1a_card_gaps(first_group)
    if card_gaps != [4, 4]:
        return False, f"W1a Ceftriaxoni card gaps must stay at body spacing 4px, got {card_gaps}"

    ok, details = _check_w1a_long_order_card(widget.cards.get(1))
    if not ok:
        return False, details

    group_gap = ordered_groups[1]["frame"].geometry().y() - (
        ordered_groups[0]["frame"].geometry().y() + ordered_groups[0]["frame"].geometry().height()
    )
    if group_gap != 3:
        return False, f"W1a patient group gap must stay at content spacing 3px, got {group_gap}"

    frame = first_group["frame"]
    header = first_group["header"]
    body = first_group["body"]
    expected_frame_height = header.height() + body.height() + frame.frameWidth() * 2
    if frame.height() != expected_frame_height:
        return False, f"W1a patient group frame has surplus height: {frame.height()} != {expected_frame_height}"
    return True, "ok"


def _check_w1c_placeholder_widget() -> tuple[bool, str]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtWidgets import QApplication

    from rem_card.ui.rem_card_sectors.sector_w1c import SectorW1c

    app = QApplication.instance() or QApplication([])
    widget = SectorW1c()
    try:
        widget.resize(250, 600)
        widget.show()
        app.processEvents()

        if not widget.main_container.isVisible():
            return False, "W1c placeholder frame must be visible"
        if widget.main_layout_v.count() != 0:
            return False, "W1c placeholder must not render inner content"
        margins = widget.layout().contentsMargins()
        if (margins.left(), margins.top(), margins.right(), margins.bottom()) != (3, 5, 5, 4):
            return False, "W1c placeholder must use W1a outer margins"
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()

    return True, "ok"


def _check_w1c_source_markers(root: Path, layout_cases: list[tuple[str, Path]]) -> tuple[bool, str]:
    w1c_source = (root / "ui" / "rem_card_sectors" / "sector_w1c.py").read_text(encoding="utf-8")
    missing_w1c_markers = [
        marker
        for marker in (
            "class SectorW1c",
            "setContentsMargins(3, 5, 5, 4)",
            "sector_w1c_main_container",
            "QWidget#sector_w1c_main_container",
            "setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)",
        )
        if marker not in w1c_source
    ]
    if missing_w1c_markers:
        return False, f"W1c placeholder sector missing marker: {missing_w1c_markers[0]}"

    for role, path in layout_cases:
        source = path.read_text(encoding="utf-8")
        missing_layout_markers = [
            marker
            for marker in (
                "SectorW1c",
                "def _ensure_sector_w1c",
                "self.sector_w1c = None",
                "self.sector_w1c = SectorW1c()",
                "self.sector_1a_stack.addWidget(self.sector_w1c)",
                "def _apply_w1_beds_sector_visibility",
                "use_w1c = not w1a_enabled and not w1b_enabled",
                "self.sector_1a_stack.setCurrentWidget(self._ensure_sector_w1c())",
            )
            if marker not in source
        ]
        if missing_layout_markers:
            return False, f"{role}: W1c layout routing missing marker: {missing_layout_markers[0]}"

    return True, "ok"


def _check_w1a_w1b_targeted_layout_and_read_model(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    root = PROJECT_ROOT

    layout_components = (root / "ui" / "shared" / "layout_components.py").read_text(encoding="utf-8")
    if "class CurrentPageStack(QStackedWidget)" not in layout_components:
        return False, "CurrentPageStack guard is missing"
    if "def sizeHint(self)" not in layout_components or "currentWidget()" not in layout_components:
        return False, "CurrentPageStack must size from current widget only"

    layout_cases = [
        ("doctor", root / "ui" / "shared" / "remcard_layout.py"),
        ("nurse", root / "ui" / "nurse_view" / "nurse_remcard_layout.py"),
    ]
    for role, path in layout_cases:
        source = path.read_text(encoding="utf-8")
        if "CurrentPageStack" not in source:
            return False, f"{role}: W1 stacks must use CurrentPageStack"
        if "self.sector_1b_stack = CurrentPageStack()" not in source:
            return False, f"{role}: sector_1b_stack still uses max-size QStackedWidget behavior"
        expected_w1a_ctor = (
            'SectorW1a(self.remcard_service, role="doctor", auto_initial_refresh=False)'
            if role == "doctor"
            else 'SectorW1a(self.remcard_service, role="nurse", auto_initial_refresh=False)'
        )
        if expected_w1a_ctor not in source:
            return False, f"{role}: W1a must receive remcard_service and role, and layout must not auto-start W1a"
        if "self.l_layout.setContentsMargins(3, 5, 5, 4)" in source:
            return False, f"{role}: W1 mode must not add column margins on top of W1a/1a sector margins"

    w1a_source = (root / "ui" / "rem_card_sectors" / "sector_w1a.py").read_text(encoding="utf-8")
    display_storage_source = (root / "ui" / "shared" / "display_settings_storage.py").read_text(encoding="utf-8")
    display_dialog_source = (root / "ui" / "admin_view" / "display_settings_dialog.py").read_text(encoding="utf-8")
    admin_main_source = (root / "ui" / "admin_view" / "admin_main_widget.py").read_text(encoding="utf-8")
    doctor_w1b_source = (root / "ui" / "rem_card_sectors" / "sector_w1b.py").read_text(encoding="utf-8")
    nurse_w1b_source = (root / "ui" / "rem_card_sectors" / "sector_w1b_nurse.py").read_text(encoding="utf-8")
    forbidden_w1a_markers = [
        "Статистика по препаратам",
        "open_statistics_requested",
        "build_full_card_snapshot",
        "build_card_snapshot",
        "get_nurse_orders_data(",
        "self.content_layout.addStretch(1)",
    ]
    for marker in forbidden_w1a_markers:
        if marker in w1a_source:
            return False, f"W1a contains forbidden legacy/full-card marker: {marker}"
    for marker in (
        "build_w1a_upcoming_orders_snapshot",
        "handle_data_changes",
        "apply_display_settings",
        "w1a_upcoming_orders_enabled",
        "not self._display_enabled",
        "_sleep_display_disabled",
        "_build_patient_groups",
        "w1a_patient_group_header",
        "card_data.pop(\"patient_name\", None)",
        "self.content_layout.setContentsMargins(2, 0, 2, 0)",
        "_bed_sort_key",
        "\"bed_number\": item.get(\"bed_number\")",
        "\"bed_number\": group_data.get(\"bed_number\")",
        "self.content_layout.setAlignment(Qt.AlignTop)",
        "self.scroll_layout.setContentsMargins(0, 3, 0, 0)",
        "self.scroll_layout.addWidget(self.cards_container, 0, Qt.AlignTop)",
        "self.scroll_layout.addStretch(1)",
        "def _pin_group_frame_height(self, group):",
        "frame.setFixedHeight(required_height)",
        "self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)",
        "header.setStyleSheet",
        "#d7eaf8",
        "#7f9fbd",
        "nurse_order_panel_mark:w1a:",
        "W1A_TIME_RECOMPUTE_MAX_MS = 60 * 1000",
    ):
        if marker not in w1a_source:
            return False, f"W1a missing targeted behavior marker: {marker}"
    for marker in (
        '"w1a_upcoming_orders"',
        "W1A_UPCOMING_ORDERS_DEFAULT_ENABLED = True",
        "def w1a_upcoming_orders_enabled",
        '"w1b_lower_sector"',
        "W1B_LOWER_SECTOR_DEFAULT_ENABLED = True",
        "def w1b_lower_sector_enabled",
    ):
        if marker not in display_storage_source:
            return False, f"display settings storage missing W1a marker: {marker}"
    for marker in (
        'super().__init__("Отображение", parent)',
        '"W1a - ближайшие назначения"',
        '"Показывать ближайшие назначения"',
        '"W1b - нижний сектор"',
        '"Показывать нижний сектор W1b"',
        '"W1a+W1b"',
        "DisplaySettingsOptionCard",
        '"zebra"',
    ):
        if marker not in display_dialog_source:
            return False, f"display settings dialog missing W1a/visual marker: {marker}"
    for marker in (
        'SectorW1b(role="doctor")',
        'SectorW1bNurse(role="nurse")',
    ):
        if marker not in layout_components:
            return False, f"W1b factory must create role-aware sector: {marker}"
    for role, source in (("doctor", doctor_w1b_source), ("nurse", nurse_w1b_source)):
        for marker in (
            "w1b_lower_sector_enabled",
            "apply_display_settings",
            "def sizeHint(self)",
            "QSize(0, 0)",
            "self.setMaximumHeight(0)",
        ):
            if marker not in source:
                return False, f"{role} W1b missing display toggle marker: {marker}"
    ok, details = _check_w1c_source_markers(root, layout_cases)
    if not ok:
        return False, details
    if 'QPushButton("Отображение")' not in admin_main_source:
        return False, "admin program settings button must be renamed to Отображение"

    service_source = (root / "services" / "order_domain_service.py").read_text(encoding="utf-8")
    if "def get_upcoming_orders_across_active_admissions" not in service_source:
        return False, "service read model for W1a is missing"
    for required_sql in (
        "JOIN beds b ON b.current_admission_id = adm.id AND b.status = 'OCCUPIED'",
        "JOIN patients p ON p.id = adm.patient_id",
        "b.bed_number AS bed_number",
        "ORDER BY CAST(b.bed_number AS INTEGER) ASC",
        "GROUP BY a2.order_id, DATETIME(a2.planned_time)",
    ):
        if required_sql not in service_source:
            return False, f"W1a read model must keep optimized active-admission SQL: {required_sql}"
    for visibility_sql in (
        "COALESCE(o.status, '') NOT IN ('deleted', 'cancelled')",
        "OR COALESCE(o.is_committed, 0) = 0",
    ):
        if service_source.count(visibility_sql) < 2:
            return False, f"current/W1a read models must hide committed deleted orders: {visibility_sql}"

    nurse_card_source = (root / "ui" / "shared" / "components" / "nurse_order_card.py").read_text(encoding="utf-8")
    for forbidden_marker in (
        "COMPACT_MAIN_FONT_PX",
        "def _apply_text_density",
        "def _method_text_for_width",
        "setHeightForWidth(True)",
    ):
        if forbidden_marker in nurse_card_source:
            return False, f"NurseOrderCard must not change inner typography/sizing for W1a gap fix: {forbidden_marker}"
    if "card_policy.setHeightForWidth(True)" in w1a_source:
        return False, "W1a must not change NurseOrderCard height-for-width policy for external gap fix"
    for required_marker in (
        "contentHeightChanged = Signal()",
        "required_height = max(ORDER_CARD_MIN_HEIGHT, self.heightForWidth(width))",
        "self.setFixedHeight(required_height)",
    ):
        if required_marker not in nurse_card_source:
            return False, f"NurseOrderCard must grow card height for wrapped multi-component orders: {required_marker}"

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from datetime import datetime, timedelta

    from PySide6.QtWidgets import QApplication

    from rem_card.ui.rem_card_sectors.sector_w1a import SectorW1a

    app = QApplication.instance() or QApplication([])
    now = datetime.now().replace(second=0, microsecond=0)

    def _w1a_row(row_id, admission_id, patient_name, bed_number, latin, dose, unit, comment, duration, offset_min):
        return {
            "id": row_id,
            "admission_id": admission_id,
            "patient_id": admission_id,
            "patient_name": patient_name,
            "bed_number": bed_number,
            "latin": latin,
            "dose_value": dose,
            "dose_unit": unit,
            "order_comment": comment,
            "duration_min": duration,
            "planned_time": (now + timedelta(minutes=offset_min)).isoformat(),
            "priority": 1,
            "comment": "",
        }

    ok, details = _check_w1a_display_settings_sleep_behavior(temp_root)
    if not ok:
        return False, details
    ok, details = _check_w1c_placeholder_widget()
    if not ok:
        return False, details

    widget = SectorW1a(service=None, role="nurse")
    try:
        widget.resize(250, 700)
        widget.show()
        app.processEvents()
        widget._apply_snapshot(
            {
                "content_hash": "layout-gap-check",
                "change_id": 1,
                "rows": [
                    _w1a_row(
                        1,
                        1,
                        "Иванов Иван Иванович",
                        "1",
                        "KCl 4% - 20 ml + S. MgSO4 25% - 10 ml + S. Insulini - 4 IU",
                        0,
                        "",
                        "S. Glucose 5% - 250 мл [ROUTE:В/в капельно] [DUR:120]",
                        120,
                        -10,
                    ),
                    _w1a_row(2, 1, "Иванов Иван Иванович", "1", "S. Ceftriaxoni", 1, "г", "S. NaCl 0.9% - 200мл [ROUTE:В/в капельно] [DUR:30]", 30, 0),
                    _w1a_row(3, 1, "Иванов Иван Иванович", "1", "S. Furosemidi", 20, "mg", "S. NaCl 0.9% - 10 мл [ROUTE:В/в струйно]", 0, 20),
                    _w1a_row(4, 2, "Петров Петр Петрович", "2", "S. Azithromycini", 500, "mg", "[ROUTE:Per os (внутрь)]", 0, 0),
                ],
            }
        )
        for _ in range(3):
            app.processEvents()

        ok, details = _check_w1a_rendered_layout(widget)
        if not ok:
            return False, details
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()

    return True, "ok"


def _check_w1_outcome_timer_ticks_without_beds_refresh(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from datetime import datetime

    from PySide6.QtWidgets import QApplication

    from rem_card.data.dto.remcard_dto import PatientStatus, PatientStatusEventDTO
    from rem_card.ui.rem_card_sectors.sector_4_sub import Sector4b

    app = QApplication.instance() or QApplication([])
    widget = Sector4b()
    status = PatientStatusEventDTO(
        admission_id=1,
        status=PatientStatus.TRANSFERRED,
        start_time=datetime.now(),
    )
    try:
        widget.show()
        app.processEvents()
        widget.update_status(status)
        widget.update_outcome_timer(status, delay_minutes=1)
        first_text = widget.lbl_outcome_timer.text()
        if widget.lbl_outcome_timer.isHidden():
            return False, "outcome timer label is hidden"
        if not widget._outcome_tick_timer.isActive():
            return False, "outcome timer QTimer is not active"

        deadline = time.monotonic() + 1.6
        changed = False
        while time.monotonic() < deadline:
            app.processEvents()
            if widget.lbl_outcome_timer.text() != first_text:
                changed = True
                break
            time.sleep(0.05)
        if not changed:
            return False, "outcome timer text did not tick without beds refresh"
        return True, "ok"
    finally:
        widget.close()


def _check_beds_mode_reentry_does_not_warn(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    source_path = PROJECT_ROOT / "ui" / "shared" / "remcard_layout.py"
    source_text = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source_text)
    methods = {
        node.name: _cached_source_segment(source_text, node) or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    }
    method = methods.get("set_patient_selection_mode", "")
    if "Skipping beds_selection_widget.refresh(): beds mode is already active" not in method:
        return False, "beds reentry must skip refresh without warning"
    if "if not already_beds and hasattr(self, 'beds_selection_widget')" in method:
        return False, "beds reentry warning guard still treats already_beds as uninitialized widget"
    if "elif hasattr(self, 'beds_selection_widget') and self.beds_selection_widget is not None" not in method:
        return False, "beds widget refresh must require an initialized widget"
    return True, "ok"


def _check_w1_outcome_release_runs_from_change_monitor(temp_root: str) -> tuple[bool, str]:
    _ = temp_root

    from rem_card.services.data_update_monitor import DataUpdateMonitor

    class FakeDataService:
        def __init__(self):
            self.calls = []
            self.current_change_id = 1

        def run_poll_maintenance_tasks(self):
            self.calls.append("maintenance")
            self.current_change_id = 2

        def get_latest_change_id(self):
            self.calls.append("latest")
            return self.current_change_id

        def fetch_changes_since(self, last_change_id):
            self.calls.append(("fetch", int(last_change_id)))
            return [
                {
                    "id": 2,
                    "entity_name": "beds",
                    "entity_id": 1,
                    "admission_id": 7,
                    "action": "update",
                    "changed_at": "2026-05-05 08:04:00.000",
                    "changed_by": "journal",
                    "version": 2,
                }
            ]

    service = FakeDataService()
    monitor = DataUpdateMonitor(service)
    monitor._last_seen_id = 1
    monitor._poll_once(force_emit=False, force_sources=[])
    if service.calls[:2] != ["maintenance", "latest"]:
        return False, f"maintenance must run before change cursor read, calls={service.calls}"
    if ("fetch", 1) not in service.calls:
        return False, f"change monitor did not fetch release changes after maintenance, calls={service.calls}"

    root = PROJECT_ROOT
    bootstrap_source = (root / "app" / "bootstrap.py").read_text(encoding="utf-8")
    if "add_poll_maintenance_task(self.remcard_service.maybe_release_due_outcome_beds)" not in bootstrap_source:
        return False, "bootstrap must register outcome auto-release as a data monitor maintenance task"
    facade_source = (root / "services" / "remcard_facade.py").read_text(encoding="utf-8")
    if "PatientService(patient_dao, data_service=data_service)" not in facade_source:
        return False, "RemCardService patient helper must receive DataService for coordinated releases"

    return True, "ok"


def _check_data_update_monitor_suppresses_shutdown_db_closed(temp_root: str) -> tuple[bool, str]:
    _ = temp_root

    from rem_card.app.db_availability import DatabaseClosedError
    from rem_card.services.data_update_monitor import DataUpdateMonitor

    class FakeDataService:
        _shutting_down = True

    monitor = DataUpdateMonitor(FakeDataService())
    if not monitor._should_suppress_poll_error(DatabaseClosedError("RemCard database connection is closed")):
        return False, "monitor must suppress DatabaseClosedError during shutdown"
    if not monitor._should_suppress_poll_error(RuntimeError("database connection is closed for remcard_read_one")):
        return False, "monitor must suppress textual closed-connection errors during shutdown"

    class RunningFakeDataService:
        _shutting_down = False

    running_monitor = DataUpdateMonitor(RunningFakeDataService())
    if running_monitor._should_suppress_poll_error(DatabaseClosedError("closed")):
        return False, "monitor must not suppress DatabaseClosedError while still running"

    class CursorFakeDataService:
        _shutting_down = False

        def __init__(self):
            self.current_change_id = 5

        def get_latest_change_id(self):
            return int(self.current_change_id)

    cursor_monitor = DataUpdateMonitor(CursorFakeDataService())
    cursor_monitor._poll_once(force_emit=False, force_sources=[])
    state = cursor_monitor.get_change_state() or {}
    if int(state.get("change_id") or 0) != 5:
        return False, f"monitor change state did not expose observed cursor: {state}"
    if int(state.get("refresh_request_seq", -1)) != int(state.get("refresh_observed_seq", -2)):
        return False, f"initial monitor state should have no pending refresh: {state}"
    cursor_monitor.request_refresh(source="regression_probe")
    pending_state = cursor_monitor.get_change_state() or {}
    if int(pending_state.get("refresh_request_seq") or 0) == int(pending_state.get("refresh_observed_seq") or 0):
        return False, f"requested refresh must be visible as pending: {pending_state}"
    cursor_monitor._poll_once(force_emit=False, force_sources=[])
    observed_state = cursor_monitor.get_change_state() or {}
    if int(observed_state.get("refresh_request_seq", -1)) != int(observed_state.get("refresh_observed_seq", -2)):
        return False, f"poll did not mark refresh request observed: {observed_state}"
    return True, "ok"


def _check_outcome_rollback_restores_released_w1_bed(temp_root: str) -> tuple[bool, str]:
    from datetime import datetime, timedelta

    from rem_card.data.dao.db_manager import DatabaseManager
    from rem_card.data.dao.patient_dao import PatientDAO
    from rem_card.data.dao.patient_status_dao import PatientStatusDAO
    from rem_card.data.dto.remcard_dto import PatientStatus
    from rem_card.services.patient_status_service import PatientStatusService

    saved_local_first = os.environ.get("REMCARD_LOCAL_FIRST_SYNC")
    os.environ["REMCARD_LOCAL_FIRST_SYNC"] = "0"
    db_path = os.path.join(temp_root, "outcome_rollback_w1.db")
    manager = DatabaseManager(db_path, db_path)
    try:
        admission_dt = datetime.now().replace(microsecond=0) - timedelta(hours=2)
        outcome_dt = datetime.now().replace(microsecond=0) - timedelta(minutes=5)
        with manager.remcard_transaction(source="regression_seed_outcome_rollback_w1") as cursor:
            cursor.execute("INSERT INTO beds(bed_number, status, current_admission_id) VALUES (1, 'FREE', NULL)")
            cursor.execute("INSERT INTO patients(full_name) VALUES (?)", ("Rollback Outcome Patient",))
            patient_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO admissions(patient_id, bed_number, history_number, admission_datetime)
                VALUES (?, 1, 'REG-OUTCOME-ROLLBACK', ?)
                """,
                (patient_id, admission_dt.isoformat()),
            )
            admission_id = int(cursor.lastrowid)
            cursor.execute(
                """
                UPDATE beds
                SET status = 'OCCUPIED',
                    current_admission_id = ?,
                    revision = COALESCE(revision, 0) + 1
                WHERE bed_number = 1
                """,
                (admission_id,),
            )
            cursor.execute(
                """
                INSERT INTO patient_status_events(
                    admission_id, status, start_time, end_time, created_by, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, 'REGRESSION', ?, ?)
                """,
                (
                    admission_id,
                    PatientStatus.ACTIVE.value,
                    admission_dt.isoformat(),
                    outcome_dt.isoformat(),
                    admission_dt.isoformat(),
                    admission_dt.isoformat(),
                ),
            )
            cursor.execute(
                """
                INSERT INTO patient_status_events(
                    admission_id, status, start_time, created_by, created_at, updated_at
                )
                VALUES (?, ?, ?, 'REGRESSION', ?, ?)
                """,
                (
                    admission_id,
                    PatientStatus.DEAD.value,
                    outcome_dt.isoformat(),
                    outcome_dt.isoformat(),
                    outcome_dt.isoformat(),
                ),
            )

        patient_dao = PatientDAO(manager)
        status_service = PatientStatusService(PatientStatusDAO(manager))
        released = patient_dao.release_due_outcome_beds(delay_minutes=0)
        if released != 1:
            return False, f"expected one released bed, got {released}"
        if patient_dao.get_active_patients():
            return False, "patient remained in W1 after outcome release"

        if not status_service.rollback_last_status(admission_id):
            return False, "rollback returned False"

        bed = manager.fetch_one_remcard(
            "SELECT status, current_admission_id FROM beds WHERE bed_number = 1"
        )
        if not bed or bed["status"] != "OCCUPIED" or int(bed["current_admission_id"]) != admission_id:
            return False, f"bed was not restored after rollback: {dict(bed) if bed else None}"

        admission = manager.fetch_one_remcard(
            "SELECT is_active, outcome, death_datetime, transfer_datetime FROM admissions WHERE id = ?",
            (admission_id,),
        )
        if not admission or int(admission["is_active"]) != 1:
            return False, f"admission was not reactivated: {dict(admission) if admission else None}"
        if admission["outcome"] or admission["death_datetime"] or admission["transfer_datetime"]:
            return False, f"outcome fields were not cleared: {dict(admission)}"

        active_patients = patient_dao.get_active_patients()
        if [p.id for p in active_patients] != [admission_id]:
            return False, f"W1 active patients mismatch after rollback: {[p.id for p in active_patients]}"

        current_status = status_service.get_current_status(admission_id)
        if not current_status or current_status.status != PatientStatus.ACTIVE:
            return False, f"unexpected current status after rollback: {current_status}"
        return True, "ok"
    finally:
        manager.close()
        if saved_local_first is None:
            os.environ.pop("REMCARD_LOCAL_FIRST_SYNC", None)
        else:
            os.environ["REMCARD_LOCAL_FIRST_SYNC"] = saved_local_first


def _check_build_release_uses_published_version(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    root = PROJECT_ROOT
    source = (root / "scripts" / "build_release.py").read_text(encoding="utf-8")
    required = [
        "validate_release_source_identity(",
        "expected_version=args.expected_version",
        "expected_commit=args.expected_commit",
        "find_changelog_entry(root, version)",
        'root / "app" / "release_info.json"',
        "finish_release(root, version, args)",
    ]
    missing = [item for item in required if item not in source]
    if missing:
        return False, f"build_release published-version flow missing {missing}"
    return True, "ok"


def _check_pyinstaller_settings_release_snapshot_source(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    root = PROJECT_ROOT
    source = (root / "RemCard.spec").read_text(encoding="utf-8")
    if "REMCARD_SETTINGS_RELEASE_SOURCE_BAZA" not in source:
        return False, "RemCard.spec must keep explicit settings release source override"
    if "from rem_card.app.runtime_paths import get_dev_baza_dir" in source:
        return False, "RemCard.spec must not resolve release settings source through alias runtime_paths"
    if "REMCARD_SETTINGS_RELEASE_SOURCE_BAZA must point" not in source:
        return False, "RemCard.spec must require the explicit settings release source root"
    return True, "ok"


def _check_patient_card_cache_lru_10(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from datetime import datetime

    from rem_card.services import persistent_snapshot_cache
    from rem_card.services.read_coordinator import READ_CACHE_MAX_PATIENTS, ReadCoordinator

    if READ_CACHE_MAX_PATIENTS != 10:
        return False, f"expected default card cache size 10, got {READ_CACHE_MAX_PATIENTS}"

    class FakeRemCardService:
        def __init__(self):
            self.build_calls = 0
            self.versions = {}

        def get_latest_change_id(self, admission_id=None, include_global=True):
            _ = include_global
            return int(self.versions.get(int(admission_id or 0), 1))

        def build_full_card_snapshot(self, admission_id, shift_date, **kwargs):
            self.build_calls += 1
            _ = kwargs
            return {
                "admission_id": int(admission_id),
                "shift_date": shift_date,
                "start_dt": shift_date,
                "end_dt": shift_date,
                "vitals": [],
                "vitals_extended": [],
                "fluids": [],
                "effective_bounds": (shift_date, shift_date),
                "balance_runtime": {"orders": [], "start_dt": shift_date, "end_dt": shift_date},
                "change_id": int(self.versions.get(int(admission_id), 1)),
            }

        def build_vitals_snapshot(self, admission_id, shift_date, **kwargs):
            self.build_calls += 1
            _ = kwargs
            return {
                "admission_id": int(admission_id),
                "shift_date": shift_date,
                "start_dt": shift_date,
                "end_dt": shift_date,
                "vitals": [{"pulse": int(admission_id)}],
                "vitals_extended": [],
                "latest_values": {"pulse": int(admission_id)},
                "effective_bounds": (shift_date, shift_date),
                "change_id": int(self.versions.get(int(admission_id), 1)),
            }

    service = FakeRemCardService()
    coordinator = ReadCoordinator(service)
    shift_date = datetime(2026, 5, 3, 8, 0, 0)

    def card_key(admission_id: int):
        context = coordinator.make_patient_snapshot_context(
            source_db="live",
            admission_id=admission_id,
            shift_date=shift_date,
            role="doctor",
            mode="live",
            variant="card_full",
        )
        return context.cache_key()

    def card_key_at(admission_id: int, dt: datetime):
        context = coordinator.make_patient_snapshot_context(
            source_db="live",
            admission_id=admission_id,
            shift_date=dt,
            role="doctor",
            mode="live",
            variant="card_full",
        )
        return context.cache_key()

    def vitals_key(admission_id: int):
        context = coordinator.make_patient_snapshot_context(
            source_db="live",
            admission_id=admission_id,
            shift_date=shift_date,
            role="doctor",
            mode="live",
            variant="vitals",
        )
        return context.cache_key()

    for admission_id in range(1, 11):
        coordinator.load_patient_card_snapshot(
            admission_id,
            shift_date,
            role="doctor",
            force_refresh=False,
        )

    if len(coordinator._patient_card_cache) != 10:
        return False, f"card cache should hold 10 entries, got {len(coordinator._patient_card_cache)}"
    if coordinator.get_cached_card(card_key(1)) is None:
        return False, "patient 1 card cache missing before LRU overflow"

    coordinator.load_patient_card_snapshot(11, shift_date, role="doctor", force_refresh=False)
    if coordinator.get_cached_card(card_key(1)) is None:
        return False, "recently used patient 1 was evicted instead of oldest entry"
    if card_key(2) in coordinator._patient_card_cache:
        return False, "oldest patient 2 memory cache survived after 11th context"
    if not persistent_snapshot_cache.flush(timeout_sec=5.0):
        return False, "patient card persistent cache writer did not flush"

    same_shift_times = [
        datetime(2026, 5, 3, 8, 0, 0),
        datetime(2026, 5, 3, 9, 15, 30),
        datetime(2026, 5, 3, 13, 40, 10),
        datetime(2026, 5, 3, 23, 59, 59),
        datetime(2026, 5, 4, 2, 30, 0),
        datetime(2026, 5, 4, 7, 59, 59),
    ]
    same_shift_keys = {card_key_at(1, dt) for dt in same_shift_times}
    if len(same_shift_keys) != 1:
        return False, f"same medical shift produced time-dependent card cache keys: {same_shift_keys}"
    if card_key_at(1, datetime(2026, 5, 4, 8, 0, 0)) in same_shift_keys:
        return False, "next medical shift reused previous card cache key"

    restarted_coordinator = ReadCoordinator(service)
    persisted_after_restart = restarted_coordinator.get_cached_card(card_key(2))
    if persisted_after_restart is None:
        return False, "patient card persistent cache was not restored after coordinator restart"
    if int(persisted_after_restart.get("admission_id") or 0) != 2:
        return False, f"unexpected restored admission_id: {persisted_after_restart.get('admission_id')}"

    coordinator.load_patient_vitals_snapshot(3, shift_date, role="doctor", force_refresh=False)
    if not persistent_snapshot_cache.flush(timeout_sec=5.0):
        return False, "patient vitals persistent cache writer did not flush"
    restarted_vitals = ReadCoordinator(service)
    persisted_vitals = restarted_vitals.get_cached_vitals(vitals_key(3))
    if persisted_vitals is None:
        return False, "patient vitals persistent cache was not restored after coordinator restart"
    if persisted_vitals.get("latest_values", {}).get("pulse") != 3:
        return False, f"unexpected restored vitals snapshot: {persisted_vitals}"

    service.versions[1] = 2
    if coordinator.get_current_cached_card(card_key(1)) is not None:
        return False, "stale patient 1 card cache was treated as current"
    if coordinator.get_cached_card(card_key(1)) is None:
        return False, "stale patient 1 card cache was removed instead of preserved for SWR"
    refreshed = coordinator.load_patient_card_snapshot(1, shift_date, role="doctor", force_refresh=False)
    if int(refreshed.get("version") or 0) != 2:
        return False, f"patient 1 card cache did not refresh to version 2: {refreshed.get('version')}"

    return True, "ok"


def _check_patient_open_cached_card_always_rehydrates(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from datetime import datetime
    from types import SimpleNamespace

    from rem_card.ui.doctor_view.doctor_remcard_widget import DoctorRemCardWidget
    from rem_card.ui.nurse_view.nurse_main_widget import NurseMainWidget

    shift_date = datetime(2026, 5, 15, 8, 0, 0)

    def context_key(*, admission_id=20, shift_date=shift_date, load_scope="patient_open_card"):
        return (int(admission_id or 0), shift_date, str(load_scope or "full"))

    doctor_calls = []
    doctor = SimpleNamespace(
        admission_id=20,
        _current_date=shift_date,
        _card_snapshot_cache={
            "scope": "patient_card",
            "version": 95066,
            "balance_runtime": {"orders": []},
            "fluids": [],
        },
        _current_snapshot_context_key=context_key,
        _request_card_snapshot=lambda **kwargs: doctor_calls.append(dict(kwargs)),
    )
    DoctorRemCardWidget._request_card_hydration_if_current(
        doctor,
        20,
        shift_date,
        context_key(),
        ensure_initial_status=True,
    )
    if len(doctor_calls) != 1:
        return False, "doctor cached full patient_card skipped freshness hydration"
    if doctor_calls[0].get("load_scope") != "patient_open_card":
        return False, f"doctor hydration used wrong scope: {doctor_calls[0]}"

    nurse_calls = []
    nurse = SimpleNamespace(
        layout_manager=SimpleNamespace(current_admission_id=20),
        _current_date=shift_date,
        _card_snapshot_cache={
            "scope": "patient_card",
            "version": 95066,
            "balance_runtime": {"orders": []},
            "fluids": [],
        },
        _current_snapshot_context_key=context_key,
        _request_card_snapshot=lambda **kwargs: nurse_calls.append(dict(kwargs)),
    )
    NurseMainWidget._request_card_hydration_if_current(
        nurse,
        20,
        shift_date,
        context_key(),
        ensure_initial_status=False,
    )
    if len(nurse_calls) != 1:
        return False, "nurse cached full patient_card skipped freshness hydration"
    if nurse_calls[0].get("load_scope") != "patient_open_card":
        return False, f"nurse hydration used wrong scope: {nurse_calls[0]}"

    stale_context_calls = []
    stale_context_doctor = SimpleNamespace(
        admission_id=21,
        _current_date=shift_date,
        _card_snapshot_cache={"scope": "patient_card", "balance_runtime": {}},
        _current_snapshot_context_key=context_key,
        _request_card_snapshot=lambda **kwargs: stale_context_calls.append(dict(kwargs)),
    )
    DoctorRemCardWidget._request_card_hydration_if_current(
        stale_context_doctor,
        20,
        shift_date,
        context_key(),
        ensure_initial_status=True,
    )
    if stale_context_calls:
        return False, "doctor stale hydration context should not request a card snapshot"

    return True, "ok"


def _check_patient_snapshot_cache_invalidates_on_vitals_change(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from types import SimpleNamespace

    from rem_card.ui.doctor_view.doctor_remcard_widget import DoctorRemCardWidget
    from rem_card.ui.nurse_view.nurse_main_widget import NurseMainWidget

    class FakeCoordinator:
        def __init__(self):
            self.vitals_calls = []
            self.card_calls = []

        def invalidate_patient_vitals_for_admission(self, admission_id, *, reason=""):
            self.vitals_calls.append((int(admission_id), reason))
            return 1

        def invalidate_patient_card_for_admission(self, admission_id, *, reason=""):
            self.card_calls.append((int(admission_id), reason))
            return 1

    payload = {
        "admission_ids": [20],
        "changes": [
            {
                "entity_name": "vitals",
                "admission_id": 20,
            }
        ],
    }
    changed_entities = {"vitals"}

    doctor_coordinator = FakeCoordinator()
    doctor = SimpleNamespace(
        _archive_read_only_mode=False,
        admission_id=20,
        _payload_force_sources=DoctorRemCardWidget._payload_force_sources,
        _get_read_coordinator=lambda: doctor_coordinator,
    )
    DoctorRemCardWidget._invalidate_vitals_cache_from_payload(doctor, payload, changed_entities)
    if doctor_coordinator.vitals_calls != [(20, "data_changes:vitals")]:
        return False, f"doctor vitals cache invalidation mismatch: {doctor_coordinator.vitals_calls}"
    if doctor_coordinator.card_calls != [(20, "data_changes:vitals")]:
        return False, f"doctor card cache invalidation mismatch: {doctor_coordinator.card_calls}"

    nurse_coordinator = FakeCoordinator()
    nurse = SimpleNamespace(
        layout_manager=SimpleNamespace(current_admission_id=20),
        _payload_force_sources=NurseMainWidget._payload_force_sources,
        _get_read_coordinator=lambda: nurse_coordinator,
    )
    NurseMainWidget._invalidate_vitals_cache_from_payload(nurse, payload, changed_entities)
    if nurse_coordinator.vitals_calls != [(20, "data_changes:vitals")]:
        return False, f"nurse vitals cache invalidation mismatch: {nurse_coordinator.vitals_calls}"
    if nurse_coordinator.card_calls != [(20, "data_changes:vitals")]:
        return False, f"nurse card cache invalidation mismatch: {nurse_coordinator.card_calls}"

    return True, "ok"


def _check_vital_settings_cache_invalidates_on_sync(temp_root: str) -> tuple[bool, str]:
    _ = temp_root

    from rem_card.services.remcard_facade import RemCardService
    from rem_card.services.vital_service import VitalService

    class FakeVitalsDAO:
        def __init__(self):
            self.settings = {"ad": 1, "pulse": 1, "temp": 1, "spo2": 1, "rr": 0, "cvp": 0}
            self.reads = 0

        def get_vital_settings(self, admission_id, date):
            _ = admission_id, date
            self.reads += 1
            return dict(self.settings)

    service = RemCardService.__new__(RemCardService)
    fake_dao = FakeVitalsDAO()
    service._vitals = VitalService(fake_dao, patient_dao=None)

    from datetime import datetime

    shift_date = datetime(2026, 5, 22, 8, 0)
    first = service._vitals.get_vital_settings_cached(29, shift_date)
    if first.get("cvp") != 0:
        return False, f"initial fake settings mismatch: {first}"

    fake_dao.settings["cvp"] = 1
    still_cached = service._vitals.get_vital_settings_cached(29, shift_date)
    if still_cached.get("cvp") != 0:
        return False, "test setup failed: settings cache did not hold the old cvp value"

    RemCardService._handle_data_changes_for_cache(
        service,
        {
            "changed_entities": ["vital_settings"],
            "sync_actions": {"full_refresh_required": False},
        },
    )
    after_vital_settings_change = service._vitals.get_vital_settings_cached(29, shift_date)
    if after_vital_settings_change.get("cvp") != 1:
        return False, "vital_settings change did not refresh VitalService settings cache"

    fake_dao.settings["cvp"] = 0
    RemCardService._handle_data_changes_for_cache(
        service,
        {
            "changes": [{"entity_name": "orders", "admission_id": 1}],
            "sync_actions": {"full_refresh_required": False},
        },
    )
    after_orders_change = service._vitals.get_vital_settings_cached(29, shift_date)
    if after_orders_change.get("cvp") != 1:
        return False, "unrelated orders change should not invalidate vital settings cache"

    RemCardService._handle_data_changes_for_cache(
        service,
        {
            "changed_entities": [],
            "sync_actions": {"full_refresh_required": True},
        },
    )
    after_full_refresh = service._vitals.get_vital_settings_cached(29, shift_date)
    if after_full_refresh.get("cvp") != 0:
        return False, "full refresh did not invalidate vital settings cache"

    return True, "ok"


def _check_patient_snapshot_persistent_cache_invalidation(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from datetime import datetime

    from rem_card.services import persistent_snapshot_cache
    from rem_card.services.read_coordinator import PATIENT_SNAPSHOT_CACHE_FORMAT_VERSION, ReadCoordinator

    shift_key = datetime(2026, 5, 22, 8, 0, 0).isoformat(timespec="seconds")
    card_key = ("live", 29, shift_key, "nurse", "live", "card_committed", "card-hash")
    vitals_key = ("live", 29, shift_key, "nurse", "live", "vitals", "vitals-hash")

    persistent_snapshot_cache.store_snapshot(
        "patient_card",
        card_key,
        {
            "admission_id": 29,
            "version": 97701,
            "settings": {"cvp": 0},
        },
    )

    coordinator = object.__new__(ReadCoordinator)
    coordinator._patient_card_cache = {}
    coordinator._patient_card_cache_index = {}
    coordinator._cache_version_validation = {}
    stale_card = ReadCoordinator.get_cached_card(coordinator, card_key)
    if stale_card is not None:
        return False, "old-format patient_card persistent snapshot was accepted"
    if persistent_snapshot_cache.load_snapshot("patient_card", card_key) is not None:
        return False, "old-format patient_card persistent snapshot was not deleted"

    current_snapshot = {
        "admission_id": 29,
        "version": 97702,
        "settings": {"cvp": 1},
        "snapshot_cache_format_version": PATIENT_SNAPSHOT_CACHE_FORMAT_VERSION,
    }
    persistent_snapshot_cache.store_snapshot("patient_card", card_key, current_snapshot)
    persistent_snapshot_cache.store_snapshot("patient_vitals", vitals_key, current_snapshot)

    removed = ReadCoordinator.invalidate_patient_card_for_admission(coordinator, 29, reason="test")
    if removed < 1:
        return False, "patient_card persistent snapshot was not counted as invalidated"
    if persistent_snapshot_cache.load_snapshot("patient_card", card_key) is not None:
        return False, "patient_card persistent snapshot survived admission invalidation"

    coordinator._patient_vitals_cache = {}
    coordinator._patient_cache_index = {}
    removed_vitals = ReadCoordinator.invalidate_patient_vitals_for_admission(coordinator, 29, reason="test")
    if removed_vitals < 1:
        return False, "patient_vitals persistent snapshot was not counted as invalidated"
    if persistent_snapshot_cache.load_snapshot("patient_vitals", vitals_key) is not None:
        return False, "patient_vitals persistent snapshot survived admission invalidation"

    return True, "ok"


def _check_read_coordinator_partial_snapshots(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from datetime import datetime

    from rem_card.services.read_coordinator import ReadCoordinator

    class FakeRemCardService:
        def __init__(self):
            self.versions = {}
            self.calls = []
            self.full_calls = 0

        def get_latest_change_id(self, admission_id=None, include_global=True):
            _ = include_global
            return int(self.versions.get(int(admission_id or 0), 1))

        def build_full_card_snapshot(self, *args, **kwargs):
            _ = args, kwargs
            self.full_calls += 1
            raise AssertionError("partial snapshots must not call full card snapshot")

        def _base(self, scope, admission_id, shift_date):
            self.calls.append(scope)
            return {
                "admission_id": int(admission_id),
                "shift_date": shift_date,
                "change_id": int(self.versions.get(int(admission_id or 0), 1)),
            }

        def build_balance_snapshot(self, admission_id, shift_date, **kwargs):
            _ = kwargs
            snapshot = self._base("balance", admission_id, shift_date)
            snapshot["fluids"] = [{"amount": 10}]
            snapshot["balance_runtime"] = {"orders": [], "start_dt": shift_date, "end_dt": shift_date}
            return snapshot

        def build_diet_snapshot(self, admission_id, shift_date, **kwargs):
            _ = kwargs
            snapshot = self._base("diet", admission_id, shift_date)
            snapshot["events"] = [{"amount_ml": 150}]
            snapshot["totals"] = {"daily": 150}
            return snapshot

        def build_patient_header_snapshot(self, admission_id, shift_date, **kwargs):
            _ = kwargs
            snapshot = self._base("patient_header", admission_id, shift_date)
            snapshot["patient"] = {"name": "test"}
            snapshot["status"] = {"status": "active"}
            return snapshot

        def build_status_snapshot(self, admission_id, shift_date, **kwargs):
            _ = kwargs
            snapshot = self._base("status", admission_id, shift_date)
            snapshot["status"] = {"status": "active"}
            snapshot["active_intervals"] = []
            return snapshot

        def build_ivl_snapshot(self, admission_id, shift_date, **kwargs):
            _ = kwargs
            snapshot = self._base("ivl", admission_id, shift_date)
            snapshot["summary"] = {"active_case": None}
            snapshot["timeline"] = []
            return snapshot

        def build_beds_snapshot(self, reference_dt=None, **kwargs):
            _ = kwargs
            dt = reference_dt or datetime(2026, 5, 3, 8)
            snapshot = self._base("beds", 0, dt)
            snapshot["patients"] = [{"id": 1}]
            snapshot["runtime_snapshot"] = {1: {"card_exists": True}}
            return snapshot

    service = FakeRemCardService()
    coordinator = ReadCoordinator(service)
    shift_date = datetime(2026, 5, 3, 9, 15)

    balance = coordinator.load_balance_snapshot(1, shift_date, role="doctor", force_refresh=True)
    if balance.get("scope") != "balance" or balance.get("tab_name") != "balance":
        return False, f"balance snapshot scope mismatch: {balance}"
    if balance.get("dedup_signature", (None,))[0:3] != (1, "balance", 1):
        return False, f"balance dedup signature mismatch: {balance.get('dedup_signature')}"
    if not balance.get("content_hash") or balance.get("dedup_signature")[3] != balance.get("content_hash"):
        return False, "balance snapshot content_hash is not part of dedup signature"

    same_balance = coordinator.load_balance_snapshot(1, shift_date, role="doctor", force_refresh=True)
    if same_balance.get("dedup_signature") != balance.get("dedup_signature"):
        return False, "same partial content produced different dedup signature"
    if same_balance.get("load_trace_id") == balance.get("load_trace_id"):
        return False, "trace ids should stay diagnostic, not dedup keys"

    balance_context = coordinator.make_patient_snapshot_context(
        source_db="live",
        admission_id=1,
        shift_date=shift_date,
        role="doctor",
        mode="live",
        variant="balance_full",
    )
    if coordinator.get_current_cached_patient_scope(balance_context.cache_key()) is None:
        return False, "fresh patient scope cache was not treated as current"
    service.versions[1] = 2
    if coordinator.get_current_cached_patient_scope(balance_context.cache_key()) is not None:
        return False, "stale patient scope cache was treated as current"
    if coordinator.get_cached_patient_scope(balance_context.cache_key()) is None:
        return False, "stale patient scope cache was not preserved for SWR"
    refreshed = coordinator.load_balance_snapshot(1, shift_date, role="doctor", force_refresh=False)
    if int(refreshed.get("version") or 0) != 2:
        return False, f"stale partial snapshot did not refresh to version 2: {refreshed.get('version')}"

    coordinator.load_diet_snapshot(1, shift_date, role="doctor", force_refresh=True)
    coordinator.load_patient_header_snapshot(1, shift_date, role="doctor", force_refresh=True)
    coordinator.load_status_snapshot(1, shift_date, role="doctor", force_refresh=True)
    coordinator.load_ivl_snapshot(1, shift_date, role="doctor", force_refresh=True)
    beds = coordinator.load_beds_snapshot(shift_date, role="nurse", force_refresh=True)
    required_calls = {"balance", "diet", "patient_header", "status", "ivl", "beds"}
    if not required_calls.issubset(set(service.calls)):
        return False, f"missing partial snapshot builders: calls={service.calls}"
    if service.full_calls:
        return False, f"partial snapshots called full snapshot {service.full_calls} times"
    if beds.get("dedup_signature", (None,))[0:3] != (0, "beds", 1):
        return False, f"beds dedup signature mismatch: {beds.get('dedup_signature')}"

    source = (PROJECT_ROOT / "services" / "read_coordinator.py").read_text(encoding="utf-8")
    if "id(snapshot)" in source:
        return False, "snapshot identity must not be used for dedup"
    if "dedup_signature" not in source or "content_hash" not in source:
        return False, "read coordinator missing content-based dedup fields"

    for widget_path in (
        PROJECT_ROOT / "ui" / "doctor_view" / "doctor_remcard_widget.py",
        PROJECT_ROOT / "ui" / "nurse_view" / "nurse_main_widget.py",
    ):
        widget_source = widget_path.read_text(encoding="utf-8")
        tree = ast.parse(widget_source)
        apply_snapshot = ""
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_apply_card_snapshot":
                apply_snapshot = _cached_source_segment(widget_source, node) or ""
                break
        if "context_key" not in apply_snapshot or "_current_snapshot_context_key" not in apply_snapshot:
            return False, f"{widget_path.name}: snapshot stale guard does not use context key"

    return True, "ok"


def _check_read_coordinator_monitor_validated_cache_hits(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    import time
    from datetime import datetime

    from rem_card.services.read_coordinator import ReadCoordinator

    class FakeDataService:
        def __init__(self):
            self.state = {
                "change_id": 10,
                "observed_monotonic": time.monotonic(),
                "refresh_request_seq": 1,
                "refresh_observed_seq": 1,
                "state_epoch": 1,
            }

        def get_observed_change_state(self):
            return dict(self.state)

    class FakeRemCardService:
        def __init__(self):
            self.version = 10
            self.latest_calls = []
            self.data_service = FakeDataService()

        def get_observed_change_state(self):
            return self.data_service.get_observed_change_state()

        def get_latest_change_id(self, admission_id=None, include_global=True):
            self.latest_calls.append((admission_id, include_global))
            return int(self.version)

        def build_full_card_snapshot(self, admission_id, shift_date, **kwargs):
            _ = kwargs
            return {
                "admission_id": int(admission_id),
                "shift_date": shift_date,
                "start_dt": shift_date,
                "end_dt": shift_date,
                "vitals": [],
                "vitals_extended": [],
                "fluids": [],
                "effective_bounds": (shift_date, shift_date),
                "balance_runtime": {"orders": [], "start_dt": shift_date, "end_dt": shift_date},
                "change_id": int(self.version),
            }

        def build_vitals_snapshot(self, admission_id, shift_date, **kwargs):
            _ = kwargs
            return {
                "admission_id": int(admission_id),
                "shift_date": shift_date,
                "start_dt": shift_date,
                "end_dt": shift_date,
                "vitals": [],
                "vitals_extended": [],
                "latest_values": {},
                "effective_bounds": (shift_date, shift_date),
                "change_id": int(self.version),
            }

        def build_balance_snapshot(self, admission_id, shift_date, **kwargs):
            _ = kwargs
            return {
                "admission_id": int(admission_id),
                "shift_date": shift_date,
                "fluids": [],
                "balance_runtime": {"orders": [], "start_dt": shift_date, "end_dt": shift_date},
                "change_id": int(self.version),
            }

    shift_date = datetime(2026, 5, 3, 8, 0, 0)

    scenarios = [
        (
            "card",
            "card_full",
            lambda coordinator: coordinator.load_patient_card_snapshot(
                31,
                shift_date,
                role="doctor",
                force_refresh=True,
            ),
            lambda coordinator, key: coordinator.get_current_cached_card(key),
        ),
        (
            "vitals",
            "vitals",
            lambda coordinator: coordinator.load_patient_vitals_snapshot(
                31,
                shift_date,
                role="doctor",
                force_refresh=True,
            ),
            lambda coordinator, key: coordinator.get_current_cached_vitals(key),
        ),
        (
            "balance",
            "balance_full",
            lambda coordinator: coordinator.load_balance_snapshot(
                31,
                shift_date,
                role="doctor",
                force_refresh=True,
            ),
            lambda coordinator, key: coordinator.get_current_cached_patient_scope(key),
        ),
    ]

    for scope, variant, warm_cache, current_get in scenarios:
        service = FakeRemCardService()
        coordinator = ReadCoordinator(service)
        context = coordinator.make_patient_snapshot_context(
            source_db="live",
            admission_id=31,
            shift_date=shift_date,
            role="doctor",
            mode="live",
            variant=variant,
        )
        cache_key = context.cache_key()

        warm_cache(coordinator)
        service.latest_calls.clear()
        for _ in range(5):
            if current_get(coordinator, cache_key) is None:
                return False, f"{scope}: monitor-validated cache hit returned stale"
        if service.latest_calls:
            return False, f"{scope}: monitor-validated cache hit still read DB: {service.latest_calls}"

        service.data_service.state["refresh_request_seq"] = 2
        if current_get(coordinator, cache_key) is None:
            return False, f"{scope}: pending refresh should still allow DB-verified cache hit"
        if len(service.latest_calls) != 1:
            return False, f"{scope}: pending refresh must bypass monitor fast path, calls={service.latest_calls}"

        service.latest_calls.clear()
        service.data_service.state["refresh_observed_seq"] = 2
        if current_get(coordinator, cache_key) is None:
            return False, f"{scope}: observed refresh cache hit returned stale"
        if service.latest_calls:
            return False, f"{scope}: observed refresh should reuse validation, calls={service.latest_calls}"

        service.version = 11
        service.data_service.state["change_id"] = 11
        if current_get(coordinator, cache_key) is not None:
            return False, f"{scope}: newer monitor cursor must not hide stale cache"
        if len(service.latest_calls) != 1:
            return False, f"{scope}: newer monitor cursor must force one DB check, calls={service.latest_calls}"

    return True, "ok"


def _check_visible_section_cache_keys_use_shift_context(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from datetime import datetime
    from collections import OrderedDict

    from rem_card.services import persistent_snapshot_cache
    from rem_card.ui.shared.components.current_orders_widget import CurrentNurseOrdersWidget
    from rem_card.ui.shared.components.diet_intake_widget import DietIntakeWidget

    same_shift_times = [
        datetime(2026, 5, 3, 8, 0, 0),
        datetime(2026, 5, 3, 9, 15, 30),
        datetime(2026, 5, 3, 13, 40, 10),
        datetime(2026, 5, 3, 23, 59, 59),
        datetime(2026, 5, 4, 2, 30, 0),
        datetime(2026, 5, 4, 7, 59, 59),
    ]
    next_shift = datetime(2026, 5, 4, 8, 0, 0)

    orders_keys = {CurrentNurseOrdersWidget._cache_key_for(7, dt) for dt in same_shift_times}
    orders_key_next = CurrentNurseOrdersWidget._cache_key_for(7, next_shift)
    if len(orders_keys) != 1:
        return False, f"orders visible cache key still depends on open time: {orders_keys}"
    if orders_key_next in orders_keys:
        return False, "orders visible cache key does not separate different medical shifts"

    diet = DietIntakeWidget.__new__(DietIntakeWidget)
    diet.admission_id = 7
    diet.role = "doctor"
    diet.read_only = False
    diet_keys = set()
    for dt in same_shift_times:
        diet.shift_date = dt
        diet_keys.add(diet._cache_key())
    diet.shift_date = next_shift
    diet_key_next = diet._cache_key()
    if len(diet_keys) != 1:
        return False, f"diet cache key still depends on open time: {diet_keys}"
    if diet_key_next in diet_keys:
        return False, "diet cache key does not separate different medical shifts"

    class FakeService:
        def get_latest_change_id(self, admission_id=None, include_global=True):
            _ = admission_id, include_global
            return 5

    orders_widget = CurrentNurseOrdersWidget.__new__(CurrentNurseOrdersWidget)
    orders_widget.service = FakeService()
    orders_widget.admission_id = 7
    orders_widget.shift_date = same_shift_times[0]
    orders_widget._snapshot_cache = OrderedDict()
    orders_widget._store_snapshot_cache([{"id": 1, "planned_time": "2026-05-03T09:00:00"}])
    if not persistent_snapshot_cache.flush(timeout_sec=5.0):
        return False, "current orders persistent cache writer did not flush"
    orders_persisted = persistent_snapshot_cache.load_snapshot("current_orders", orders_widget._cache_key())
    if not orders_persisted or orders_persisted.get("data", [{}])[0].get("id") != 1:
        return False, f"current orders persistent cache was not stored: {orders_persisted}"
    if not CurrentNurseOrdersWidget._is_cache_snapshot_compatible(orders_persisted):
        return False, f"current orders persistent cache format is stale: {orders_persisted}"
    if CurrentNurseOrdersWidget._is_cache_snapshot_compatible({"version": 5, "data": []}):
        return False, "current orders cache accepted a pre-fix snapshot without format version"

    diet.service = FakeService()
    diet._snapshot_cache = OrderedDict()
    diet._templates = []
    diet._plan = None
    diet._events = []
    diet.shift_date = same_shift_times[0]
    diet._store_snapshot_cache()
    if not persistent_snapshot_cache.flush(timeout_sec=5.0):
        return False, "diet persistent cache writer did not flush"
    diet_persisted = persistent_snapshot_cache.load_snapshot("diet", diet._cache_key())
    if diet_persisted is None or "events" not in diet_persisted:
        return False, f"diet persistent cache was not stored: {diet_persisted}"

    return True, "ok"


def _check_balance_loading_state_uses_placeholders(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtWidgets import QApplication

    from rem_card.ui.rem_card_sectors.balance.sector_2b_g import Sector2b_g
    from rem_card.ui.rem_card_sectors.balance.sector_2b_v import Sector2b_v
    from rem_card.ui.rem_card_sectors.sector_3a import Sector3a
    from rem_card.ui.rem_card_sectors.sector_3b import Sector3b
    from rem_card.ui.rem_card_sectors.sector_4a import Sector4a

    app = QApplication.instance() or QApplication([])
    _ = app

    widgets = [Sector2b_g(), Sector2b_v(), Sector3a(), Sector3b(), Sector4a()]
    try:
        for widget in widgets:
            if not hasattr(widget, "set_loading_state"):
                return False, f"{widget.__class__.__name__} has no set_loading_state"
            widget.set_loading_state()

        checks = [
            widgets[0].total_in_val.text(),
            widgets[1].total_out_val.text(),
            widgets[1].balance_val.text(),
            widgets[2].total_in_val.text(),
            widgets[3].total_out_val.text(),
            widgets[4].balance_val.text(),
        ]
        bad = [text for text in checks if text.strip().startswith("0")]
        if bad:
            return False, f"loading state still shows zero-like values: {bad}"
        if not all("—" in text for text in checks):
            return False, f"loading state should use placeholders, got {checks}"
        return True, "ok"
    finally:
        for widget in widgets:
            widget.close()


def _check_lazy_section_snapshot_caches(temp_root: str) -> tuple[bool, str]:
    from .orders import _wait_for_movement_snapshot
    _ = temp_root
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from datetime import datetime, timedelta

    from PySide6.QtWidgets import QApplication

    from rem_card.ui.rem_card_sectors.sector_events import SectorEvents
    from rem_card.ui.rem_card_sectors.sector_ivl import SectorIvl

    app = QApplication.instance() or QApplication([])
    _ = app

    class FakeStatusService:
        def __init__(self):
            self.version = {}
            self.calls = []

        def get_latest_change_id(self, admission_id=None, include_global=False):
            _ = include_global
            return int(self.version.get(int(admission_id or 0), 1))

        def get_observed_change_state(self):
            return {"change_id": max(self.version.values(), default=1)}

        def get_movement_snapshot(self, admission_id, shift_start, shift_end):
            events = self.get_events_in_range(admission_id, shift_start, shift_end)
            return {
                "admission_id": admission_id, "events": events,
                "version": self.get_latest_change_id(admission_id),
                "is_archive": shift_end < datetime.now(), "late_state": {},
                "total_events": len(events), "current_status": None,
            }

        def get_events_in_range(self, admission_id, shift_start, shift_end):
            self.calls.append(("range", int(admission_id), shift_start.isoformat(), shift_end.isoformat()))
            return []

        def get_events(self, admission_id):
            self.calls.append(("all", int(admission_id)))
            return []

    class FakeRemCardService:
        def __init__(self):
            self.version = {}
            self.calls = []

        def get_latest_change_id(self, admission_id=None, include_global=False):
            _ = include_global
            return int(self.version.get(int(admission_id or 0), 1))

        def get_ventilation_summary(self, admission_id):
            self.calls.append(("summary", int(admission_id)))
            return {"active_case": None, "total_duration_seconds": 0.0}

        def get_ventilation_timeline(self, admission_id):
            self.calls.append(("timeline", int(admission_id)))
            return []

        def get_latest_ventilation_case(self, admission_id):
            self.calls.append(("latest", int(admission_id)))
            return None

        def get_patient(self, admission_id):
            _ = admission_id
            return None

    shift_start = datetime(2026, 5, 3, 8, 0)
    shift_end = shift_start + timedelta(hours=12)

    events_service = FakeStatusService()
    events_widget = SectorEvents()
    ivl_service = FakeRemCardService()
    ivl_widget = SectorIvl()
    try:
        events_widget.role = "Врач"
        events_widget.shift_start = shift_start
        events_widget.shift_end = shift_end
        events_widget.set_patient(1, events_service)
        _wait_for_movement_snapshot(events_widget, app)
        events_widget.set_patient(2, events_service)
        _wait_for_movement_snapshot(events_widget, app)
        events_widget.set_patient(1, events_service)
        _wait_for_movement_snapshot(events_widget, app)
        event_patient_calls = [call[1] for call in events_service.calls]
        if event_patient_calls != [1, 2]:
            return False, f"events hot-cache should avoid repeated DB load, calls={events_service.calls}"

        for admission_id in range(3, 11):
            events_widget.set_patient(admission_id, events_service)
            _wait_for_movement_snapshot(events_widget, app)
        events_widget.set_patient(1, events_service)
        _wait_for_movement_snapshot(events_widget, app)
        events_widget.set_patient(11, events_service)
        _wait_for_movement_snapshot(events_widget, app)
        event_keys = list(events_widget._snapshot_cache.keys())
        if len(event_keys) != 10 or not any(key[0] == 1 for key in event_keys) or any(key[0] == 2 for key in event_keys):
            return False, f"events LRU cache mismatch: {event_keys}"

        ivl_widget.set_runtime_context(ivl_service, 1)
        ivl_widget.set_runtime_context(ivl_service, 2)
        ivl_widget.set_runtime_context(ivl_service, 1)
        ivl_patient_calls = [call[1] for call in ivl_service.calls if call[0] == "summary"]
        if ivl_patient_calls != [1, 2]:
            return False, f"ivl hot-cache should avoid repeated DB load, calls={ivl_service.calls}"

        for admission_id in range(3, 11):
            ivl_widget.set_runtime_context(ivl_service, admission_id)
        ivl_widget.set_runtime_context(ivl_service, 1)
        ivl_widget.set_runtime_context(ivl_service, 11)
        ivl_keys = list(ivl_widget._snapshot_cache.keys())
        if len(ivl_keys) != 10 or (1, "ivl") not in ivl_keys or (2, "ivl") in ivl_keys:
            return False, f"ivl LRU cache mismatch: {ivl_keys}"
        return True, "ok"
    finally:
        events_widget.close()
        ivl_widget.close()


def _check_sync_entity_classifications(actions) -> tuple[bool, str]:
    orders = actions({
        "changed_entities": ["orders"],
        "changes": [{"entity_name": "orders", "admission_id": 1}],
    })
    if orders["full_refresh_required"] or orders["card_snapshot_required"]:
        return False, f"orders should not require full card snapshot: {orders}"
    if not orders["orders_refresh"] or orders["vitals_snapshot_required"]:
        return False, f"orders classification mismatch: {orders}"
    if not orders["balance_refresh"]:
        return False, f"orders should refresh balance sectors: {orders}"

    administrations = actions({
        "changed_entities": ["administrations"],
        "changes": [{"entity_name": "administrations", "admission_id": 1}],
    })
    if administrations["full_refresh_required"] or administrations["card_snapshot_required"]:
        return False, f"administrations should not require full card snapshot: {administrations}"
    if not (administrations["orders_refresh"] and administrations["balance_refresh"]):
        return False, f"administrations should refresh orders and balance: {administrations}"

    vitals = actions({
        "changed_entities": ["vitals"],
        "changes": [{"entity_name": "vitals", "admission_id": 1}],
    })
    if vitals["full_refresh_required"] or vitals["card_snapshot_required"]:
        return False, f"vitals should be partial snapshot only: {vitals}"
    if not vitals["vitals_snapshot_required"]:
        return False, f"vitals snapshot was not requested: {vitals}"

    fluids = actions({
        "changed_entities": ["fluids"],
        "changes": [{"entity_name": "fluids", "admission_id": 1}],
    })
    if fluids["full_refresh_required"] or fluids["card_snapshot_required"] or fluids["vitals_snapshot_required"]:
        return False, f"fluids should stay balance-only: {fluids}"
    if not fluids["balance_refresh"]:
        return False, f"balance refresh was not requested: {fluids}"

    diet = actions({
        "changed_entities": ["diet_plan", "oral_intake_events"],
        "changes": [
            {"entity_name": "diet_plan", "admission_id": 1},
            {"entity_name": "oral_intake_events", "admission_id": 1},
        ],
    })
    if diet["full_refresh_required"] or diet["card_snapshot_required"]:
        return False, f"diet/oral changes should not require full card snapshot: {diet}"
    if not diet["diet_refresh"] or not diet["balance_refresh"]:
        return False, f"diet/oral classification mismatch: {diet}"

    status = actions({
        "changed_entities": ["patient_status_events"],
        "changes": [{"entity_name": "patient_status_events", "admission_id": 1}],
    })
    if status["full_refresh_required"] or status["card_snapshot_required"]:
        return False, f"status should not require full card snapshot: {status}"
    if not (status["status_refresh"] and status["vitals_snapshot_required"] and status["balance_refresh"]):
        return False, f"status classification mismatch: {status}"
    return True, "ok"


def _check_sync_forced_classifications(actions) -> tuple[bool, str]:
    local_force = actions({
        "forced": True,
        "force_source": "orders_left_click:1",
        "changed_entities": ["orders"],
    })
    if local_force["full_refresh_required"] or local_force["card_snapshot_required"]:
        return False, f"local orders force should not require full refresh: {local_force}"
    if not local_force["balance_refresh"]:
        return False, f"local orders force should refresh balance: {local_force}"

    doctor_mark_force = actions({
        "forced": True,
        "force_source": "doctor_order_mark:5",
        "changed_entities": ["administrations"],
    })
    if doctor_mark_force["full_refresh_required"] or doctor_mark_force["card_snapshot_required"]:
        return False, f"doctor order mark should not require full refresh: {doctor_mark_force}"
    if not (doctor_mark_force["orders_refresh"] and doctor_mark_force["balance_refresh"]):
        return False, f"doctor order mark should refresh orders and balance: {doctor_mark_force}"

    nurse_panel_force = actions({
        "forced": True,
        "force_source": "nurse_order_panel_mark:5",
        "changed_entities": ["administrations"],
    })
    if nurse_panel_force["full_refresh_required"] or nurse_panel_force["card_snapshot_required"]:
        return False, f"nurse panel mark should not require full refresh: {nurse_panel_force}"
    if not (nurse_panel_force["orders_refresh"] and nurse_panel_force["balance_refresh"]):
        return False, f"nurse panel mark should refresh orders and balance: {nurse_panel_force}"

    gap = actions({
        "gap_detected": True,
        "reason": "gap_detected",
        "changed_entities": ["orders"],
    })
    if not (gap["full_refresh_required"] and gap["card_snapshot_required"]):
        return False, f"gap must require full refresh: {gap}"

    empty_forced = actions({"forced": True, "force_source": "unknown_source"})
    if empty_forced["full_refresh_required"] or empty_forced["card_snapshot_required"]:
        return False, f"unknown forced refresh must stay targeted: {empty_forced}"

    manual_refresh = actions({
        "forced": True,
        "force_source": "manual_refresh:doctor",
    })
    if not (
        manual_refresh["full_refresh_required"]
        and manual_refresh["card_snapshot_required"]
    ):
        return False, f"explicit manual refresh must stay full: {manual_refresh}"

    rotation_refresh = actions({
        "forced": True,
        "force_source": "database_rotation:admin",
    })
    if not (
        rotation_refresh["full_refresh_required"]
        and rotation_refresh["card_snapshot_required"]
    ):
        return False, f"database rotation must require full refresh: {rotation_refresh}"
    return True, "ok"


def _check_sync_coordinator_classifies_targeted_refresh(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from rem_card.services.sync_coordinator import SyncCoordinator

    def actions(payload):
        return SyncCoordinator.classify(payload)["sync_actions"]

    for check in (_check_sync_entity_classifications, _check_sync_forced_classifications):
        ok, details = check(actions)
        if not ok:
            return False, details
    return True, "ok"


def _check_orders_delta_expected_fallbacks_are_info(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    import logging

    from rem_card.services.read_coordinator import ReadCoordinator

    expected_info = (
        "empty_change_rows",
        "empty_delta_rows",
        "delta_no_effect",
        "unsupported_entities:orders",
    )
    for reason in expected_info:
        if ReadCoordinator._orders_delta_fallback_log_level(reason) != logging.INFO:
            return False, f"expected orders delta fallback must log at INFO: {reason}"
    expected_warning = (
        "delta_unknown_order:12",
        "version_violation_after_delta",
    )
    for reason in expected_warning:
        if ReadCoordinator._orders_delta_fallback_log_level(reason) != logging.WARNING:
            return False, f"unsafe orders delta fallback must stay WARNING: {reason}"
    source_text = (PROJECT_ROOT / "services" / "read_coordinator.py").read_text(encoding="utf-8")
    if "logger.log(" not in source_text or "_orders_delta_fallback_log_level(delta_failure_reason)" not in source_text:
        return False, "orders delta fallback path must use reason-aware log level"
    return True, "ok"


def _check_orders_balance_adapter_uses_local_state(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    from datetime import datetime, timedelta

    from rem_card.data.dto.remcard_dto import AdministrationDTO, OrderDTO, OrderStatus, OrderType
    from rem_card.services.balance_calculator import BalanceCalculator
    from rem_card.services.diet_service import normalize_schedule
    from rem_card.ui.shared.orders_balance_adapter import (
        apply_current_order_mark_overrides,
        build_balance_orders_from_orders_widget,
        oral_totals_from_runtime,
    )

    shift_start = datetime(2026, 5, 3, 8, 0)

    class FakeService:
        def get_day_period(self, value):
            _ = value
            return shift_start, shift_start + timedelta(days=1)

    class FakeModel:
        def __init__(self, *, pending_mark: bool):
            self.service = FakeService()
            self.admission_id = 7
            self.shift_date = shift_start
            self.orders = [
                OrderDTO(
                    id=11,
                    admission_id=7,
                    latin="NaCl",
                    type=OrderType.INFUSION_INTERMITTENT,
                    status=OrderStatus.ACTIVE,
                    volume_total=100.0,
                    duration_min=60,
                    is_committed=1,
                )
            ]
            admin = AdministrationDTO(
                id=21,
                order_id=11,
                planned_time=shift_start + timedelta(hours=1),
                status="planned",
                is_committed=1,
                comment="",
                volume_ml=100.0,
            )
            if pending_mark:
                setattr(admin, "_pending_mark", "nurse_executed")
            self.admin_map = {(11, admin.planned_time.isoformat()): admin}

    class FakeWidget:
        def __init__(self, pending: int = 0, has_drafts: bool = False, pending_mark: bool = False):
            self.model = FakeModel(pending_mark=pending_mark)
            self._pending_admin_write_count = pending
            self._has_drafts = has_drafts

        def has_drafts(self):
            return self._has_drafts

    inactive_widget = FakeWidget()
    if build_balance_orders_from_orders_widget(inactive_widget, 7, shift_start) is not None:
        return False, "inactive orders widget without local state should not override balance runtime"

    active_widget = FakeWidget()
    active_widget.model.admin_map[(11, (shift_start + timedelta(hours=1)).isoformat())].comment = "nurse_not_executed"
    active_orders = build_balance_orders_from_orders_widget(active_widget, 7, shift_start, tab_active=True)
    if not active_orders:
        return False, "active orders tab should use visible local model for balance"
    active_admins = getattr(active_orders[0], "administrations", None) or []
    if active_admins[0].comment != "nurse_not_executed":
        return False, "active orders tab lost committed nurse mark from local model"

    widget = FakeWidget(pending=1, pending_mark=True)
    balance_orders = build_balance_orders_from_orders_widget(widget, 7, shift_start)
    if not balance_orders or balance_orders[0] is widget.model.orders[0]:
        return False, "local balance adapter did not return copied orders"
    admins = getattr(balance_orders[0], "administrations", None) or []
    if len(admins) != 1:
        return False, f"local balance adapter did not attach administrations: {admins}"
    if admins[0].comment != "nurse_executed" or admins[0].actual_time is None:
        return False, "pending nurse mark was not applied to local balance administration"

    setattr(widget.model.orders[0], "_pending_delete", True)
    deleted_orders = build_balance_orders_from_orders_widget(widget, 7, shift_start)
    if deleted_orders != []:
        return False, f"pending deleted order should be excluded from local balance: {deleted_orders}"

    if build_balance_orders_from_orders_widget(widget, 8, shift_start) is not None:
        return False, "different admission should not use local orders"

    runtime_orders = []
    for order_id, admin_id, hour in ((101, 201, 1), (102, 202, 2)):
        order = OrderDTO(
            id=order_id,
            admission_id=7,
            drug_key="manual_balance_test",
            latin="Manual balance test",
            type=OrderType.INFUSION_INTERMITTENT,
            status=OrderStatus.ACTIVE,
            dose_value=0,
            dose_unit="ml",
            duration_min=0,
            is_committed=1,
            comment="S. NaCl - 250 ml",
        )
        order.administrations = [
            AdministrationDTO(
                id=admin_id,
                order_id=order_id,
                planned_time=shift_start + timedelta(hours=hour),
                status="planned",
                is_committed=1,
                comment="",
            )
        ]
        runtime_orders.append(order)

    class FakeCurrentOrders:
        def __init__(self, mark: str):
            self.service = FakeService()
            self.admission_id = 7
            self.shift_date = shift_start
            self._pending_marks = {
                201: {
                    "mark": mark,
                    "actual_time": (shift_start + timedelta(hours=1)).isoformat(),
                    "started_mono": 0.0,
                }
            }

        def _get_pending_mark(self, admin_id: int):
            return self._pending_marks.get(int(admin_id))

    patched_not_done = apply_current_order_mark_overrides(
        runtime_orders,
        FakeCurrentOrders("nurse_not_executed"),
        7,
        shift_start,
    )
    if patched_not_done is None or patched_not_done[0].administrations[0].comment != "nurse_not_executed":
        return False, "sector 1a pending not-done mark was not applied to balance orders"
    if runtime_orders[0].administrations[0].comment:
        return False, "sector 1a balance override mutated runtime orders"
    base_calc = BalanceCalculator.calculate(runtime_orders, shift_start + timedelta(hours=3), shift_start + timedelta(days=1))
    not_done_calc = BalanceCalculator.calculate(patched_not_done, shift_start + timedelta(hours=3), shift_start + timedelta(days=1))
    if base_calc["daily"]["total"] != 500 or not_done_calc["daily"]["total"] != 250:
        return False, f"sector 1a not-done daily balance mismatch: base={base_calc} not_done={not_done_calc}"

    patched_done = apply_current_order_mark_overrides(
        runtime_orders,
        FakeCurrentOrders("nurse_executed"),
        7,
        shift_start,
    )
    done_calc = BalanceCalculator.calculate(patched_done, shift_start + timedelta(hours=3), shift_start + timedelta(days=1))
    if done_calc["current"]["total"] != 250 or done_calc["daily"]["total"] != 500:
        return False, f"sector 1a executed balance mismatch: {done_calc}"

    patched_cancel = apply_current_order_mark_overrides(
        patched_not_done,
        FakeCurrentOrders(""),
        7,
        shift_start,
    )
    if patched_cancel is None or patched_cancel[0].administrations[0].comment:
        return False, "sector 1a pending cancel mark did not clear balance order mark"
    cancel_calc = BalanceCalculator.calculate(patched_cancel, shift_start + timedelta(hours=3), shift_start + timedelta(days=1))
    if cancel_calc["daily"]["total"] != 500 or cancel_calc["current"]["total"] != 0:
        return False, f"sector 1a cancel balance mismatch: {cancel_calc}"

    class FakeOralEvent:
        def __init__(self, event_time, amount_ml):
            self.event_time = event_time
            self.amount_ml = amount_ml

    oral_current, oral_daily = oral_totals_from_runtime(
        {
            "oral_events": [
                FakeOralEvent(shift_start + timedelta(hours=1), 100),
                FakeOralEvent(shift_start + timedelta(hours=5), 200),
            ]
        },
        shift_start + timedelta(hours=2),
    )
    if (oral_current, oral_daily) != (300.0, 0.0):
        return False, f"cached oral totals mismatch: {(oral_current, oral_daily)}"

    oral_plan_schedule = [
        {"time": "08:00", "amount": 200},
        {"time": "12:00", "amount": 300},
    ]
    planned_current, planned_daily = oral_totals_from_runtime(
        {
            "oral_events": [],
            "oral_plan_schedule": oral_plan_schedule,
            "oral_shift_date": shift_start,
            "oral_start_dt": shift_start,
            "oral_end_dt": shift_start + timedelta(days=1),
        },
        shift_start + timedelta(hours=2),
    )
    if (planned_current, planned_daily) != (0.0, 500.0):
        return False, f"planned oral totals mismatch without facts: {(planned_current, planned_daily)}"

    planned_fact_current, planned_fact_daily = oral_totals_from_runtime(
        {
            "oral_events": [FakeOralEvent(shift_start, 200)],
            "oral_plan_schedule": oral_plan_schedule,
            "oral_shift_date": shift_start,
            "oral_start_dt": shift_start,
            "oral_end_dt": shift_start + timedelta(days=1),
        },
        shift_start + timedelta(hours=2),
    )
    if (planned_fact_current, planned_fact_daily) != (200.0, 500.0):
        return False, f"planned oral totals mismatch with planned fact: {(planned_fact_current, planned_fact_daily)}"

    unplanned_current, unplanned_daily = oral_totals_from_runtime(
        {
            "oral_events": [FakeOralEvent(shift_start + timedelta(hours=2), 100)],
            "oral_plan_schedule": oral_plan_schedule,
            "oral_shift_date": shift_start,
            "oral_start_dt": shift_start,
            "oral_end_dt": shift_start + timedelta(days=1),
        },
        shift_start + timedelta(hours=2),
    )
    if (unplanned_current, unplanned_daily) != (100.0, 500.0):
        return False, f"planned oral totals mismatch with unplanned fact: {(unplanned_current, unplanned_daily)}"

    explicit_local_plan_current, explicit_local_plan_daily = oral_totals_from_runtime(
        {
            "oral_events": [],
            "oral_plan_schedule": oral_plan_schedule,
            "oral_shift_date": shift_start,
            "oral_start_dt": shift_start,
            "oral_end_dt": shift_start + timedelta(days=1),
        },
        shift_start + timedelta(hours=2),
        oral_events=[],
        oral_plan={"schedule_json": normalize_schedule([{"time": "10:00", "amount": 150}])},
    )
    if (explicit_local_plan_current, explicit_local_plan_daily) != (0.0, 150.0):
        return False, f"local oral plan did not override runtime plan: {(explicit_local_plan_current, explicit_local_plan_daily)}"

    deleted_plan_current, deleted_plan_daily = oral_totals_from_runtime(
        {
            "oral_events": [],
            "oral_plan_schedule": oral_plan_schedule,
            "oral_shift_date": shift_start,
            "oral_start_dt": shift_start,
            "oral_end_dt": shift_start + timedelta(days=1),
        },
        shift_start + timedelta(hours=2),
        oral_events=[],
        oral_plan=None,
    )
    if (deleted_plan_current, deleted_plan_daily) != (0.0, 0.0):
        return False, f"deleted local oral plan still used runtime plan: {(deleted_plan_current, deleted_plan_daily)}"

    fallback_current, fallback_daily = oral_totals_from_runtime(
        {"oral_totals": {"current": 10, "daily": 20}},
        shift_start + timedelta(hours=2),
    )
    if (fallback_current, fallback_daily) != (10.0, 20.0):
        return False, f"fallback oral totals mismatch: {(fallback_current, fallback_daily)}"

    return True, "ok"


def _check_card_widgets_use_sync_actions_for_partial_refresh(temp_root: str) -> tuple[bool, str]:
    _ = temp_root
    widget_paths = [
        PROJECT_ROOT / "ui" / "doctor_view" / "doctor_remcard_widget.py",
        PROJECT_ROOT / "ui" / "nurse_view" / "nurse_main_widget.py",
    ]
    for path in widget_paths:
        source_text = path.read_text(encoding="utf-8")
        tree = ast.parse(source_text)
        methods = {
            node.name: _cached_source_segment(source_text, node) or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        on_changes = methods.get("_on_data_changes", "")
        if not on_changes:
            return False, f"{path.name}: _on_data_changes not found"
        if "sync_actions" not in on_changes or "vitals_snapshot_required" not in on_changes:
            return False, f"{path.name}: SyncCoordinator actions are not used"
        if "if card_snapshot_required:" in on_changes:
            return False, f"{path.name}: card_snapshot_required must not be an unconditional full-card path"
        if 'load_scope="patient_open_vitals"' not in on_changes:
            return False, f"{path.name}: vitals changes must use partial vitals snapshot"
        if "_current_status_is_outcome()" not in on_changes:
            return False, f"{path.name}: outcome refresh must skip redundant vitals snapshot"
        if "skipped vitals snapshot after outcome" not in on_changes:
            return False, f"{path.name}: outcome vitals-snapshot skip should be logged"
        local_force_pos = on_changes.find("_is_local_orders_force_payload")
        diet_pos = on_changes.find("_handle_diet_sync", local_force_pos)
        if local_force_pos < 0 or diet_pos < 0:
            return False, f"{path.name}: local orders force branch not found"
        local_force_block = on_changes[local_force_pos:diet_pos]
        if "_refresh_balance_from_db()" in local_force_block:
            return False, f"{path.name}: local order force must not synchronously reload balance from DB"
        if "_schedule_balance_update()" not in local_force_block:
            return False, f"{path.name}: local order force must schedule local balance update"
        if '_balance_snapshot_sync.schedule(payload.get("last_change_id", 0))' not in local_force_block:
            return False, f"{path.name}: local order force must schedule cursor-guarded balance read"
        if "_refresh_current_orders_from_payload(payload)" not in local_force_block:
            return False, f"{path.name}: local order force must refresh sector 1a current orders"
        refresh_orders_method = methods.get("_refresh_orders_from_payload", "")
        if "_refresh_current_orders_from_payload(payload)" not in refresh_orders_method:
            return False, f"{path.name}: external order changes must refresh sector 1a current orders"
        current_orders_helper = methods.get("_refresh_current_orders_from_payload", "")
        if "handle_data_changes(payload)" not in current_orders_helper:
            return False, f"{path.name}: sector 1a current orders helper must delegate change payloads"
        partial_actions = methods.get("_apply_partial_sync_actions", "")
        if "_apply_partial_sync_actions(" not in on_changes or not partial_actions:
            return False, f"{path.name}: partial sync action dispatcher missing"
        for helper in (
            "_refresh_balance_from_db",
            "_refresh_status_from_db",
            "_refresh_ivl_from_db",
        ):
            if helper not in methods:
                return False, f"{path.name}: {helper} helper missing"
            if f"{helper}()" not in partial_actions:
                return False, f"{path.name}: {helper} is not called from partial sync dispatcher"
        balance_method = methods.get("update_balance_data") or methods.get("_update_balance_calculations") or ""
        if "get_oral_intake_totals" in balance_method:
            return False, f"{path.name}: balance UI update must not synchronously read oral totals from DB"
        if "oral_totals_from_runtime" not in balance_method:
            return False, f"{path.name}: balance UI update must use cached oral runtime"
        if "project_balance_orders(" not in balance_method or "apply_orders_widget_mark_overrides(" not in balance_method:
            return False, f"{path.name}: balance must project drafts and execution over authoritative state"
        if "_balance_snapshot_sync.schedule()" not in methods.get("_refresh_balance_from_db", ""):
            return False, f"{path.name}: balance refresh must be asynchronous"
    return True, "ok"
