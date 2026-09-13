"""Снимки реальной карточки пациента в светлой и тёмной теме на временной БД.

Запуск:
    python scripts/theme_card_preview.py --output tmp/theme-card-preview

Сценарий намеренно не использует рабочую БД, пользовательские QSettings или
сетевую папку. Он создаёт схему и единственную синтетическую карту в каталоге
tempfile, после чего рендерит настоящий ``DoctorRemCardWidget`` без ``show()``.
В результатах отдельно указано синхронное время, ожидание снимка и захват PNG.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from _local_rem_card_bootstrap import bootstrap_local_rem_card

bootstrap_local_rem_card()


def _configure_isolated_environment(root: Path, platform: str) -> None:
    """Redirect every mutable runtime location before app modules are imported."""
    state = root / "state"
    environment = {
        "QT_QPA_PLATFORM": platform,
        "REMCARD_BAZA_DIR": str(root / "baza"),
        "REMCARD_STYLE_SETTINGS_PATH": str(state / "style_settings.json"),
        "REMCARD_LOCAL_LOGS_DIR": str(state / "logs"),
        "REMCARD_LOCAL_CACHE_SUFFIX": "theme-card-preview",
        "REMCARD_LOCAL_FIRST_SYNC": "0",
        "REMCARD_LOCAL_OUTBOX_SYNC": "0",
        "REMCARD_CHANGELOG_LIVE_TRIM_ENABLED": "0",
        "REMCARD_CARD_UI_PREWARM": "0",
        "REMCARD_JOURNAL_PREWARM": "0",
        "REMCARD_JOURNAL_WIDGET_PREWARM": "0",
        "REMCARD_PATIENT_PREVIEW_ICON_PREWARM_DELAY_MS": "3600000",
        "REMCARD_FULL_RUNTIME_THEME": "1",
        "LOCALAPPDATA": str(state / "localappdata"),
        "APPDATA": str(state / "appdata"),
        "ProgramData": str(state / "programdata"),
        "USERPROFILE": str(state / "profile"),
        "TEMP": str(state / "temp"),
        "TMP": str(state / "temp"),
    }
    for key in (
        "REMCARD_BAZA_DIR", "LOCALAPPDATA", "APPDATA", "ProgramData",
        "USERPROFILE", "TEMP", "TMP",
    ):
        Path(environment[key]).mkdir(parents=True, exist_ok=True)
    os.environ.update(environment)


def _install_isolated_qsettings(settings_dir: Path) -> None:
    """Use the explicit INI overload even for QSettings(org, app)."""
    from PySide6 import QtCore

    original = QtCore.QSettings
    original.setDefaultFormat(original.IniFormat)
    for scope in (original.UserScope, original.SystemScope):
        original.setPath(original.IniFormat, scope, str(settings_dir))

    class PreviewSettings(original):
        def __init__(self, *args, **kwargs):
            if len(args) >= 2 and isinstance(args[0], str) and isinstance(args[1], str):
                super().__init__(original.IniFormat, original.UserScope, *args, **kwargs)
            else:
                super().__init__(*args, **kwargs)

    QtCore.QSettings = PreviewSettings


def _seed_synthetic_card(db_manager) -> int:
    """Create only demonstration data in the just-created temporary database."""
    from datetime import datetime, timedelta

    now = datetime.now().replace(second=0, microsecond=0)
    start = now.replace(hour=8, minute=0) - (timedelta(days=1) if now.hour < 8 else timedelta())

    def operation(cursor):
        cursor.execute(
            """INSERT INTO patients (full_name, admission_uid, birth_date, last_name, first_name, middle_name)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("Демонстрационный Пациент РАО", "THEME-PREVIEW-ONLY", "1982-05-12", "Демонстрационный", "Пациент", "РАО"),
        )
        patient_id = int(cursor.lastrowid)
        cursor.execute(
            """INSERT INTO admissions (
                   patient_id, bed_number, history_number, admission_datetime, patient_age,
                   patient_age_unit, patient_gender, diagnosis_code, diagnosis_text, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                patient_id, 1, "ТЕМА-001", now.isoformat(), 44, "л", "Мужской", "J18.9",
                "Демонстрационная пневмония. Данные созданы только для проверки оформления.",
                now.isoformat(), now.isoformat(),
            ),
        )
        admission_id = int(cursor.lastrowid)
        cursor.execute(
            "INSERT INTO beds (bed_number, status, current_admission_id) VALUES (?, ?, ?)",
            (1, "OCCUPIED", admission_id),
        )
        cursor.execute(
            """INSERT INTO patient_status_events (admission_id, status, start_time, created_by)
               VALUES (?, 'ACTIVE', ?, 'theme-card-preview')""",
            (admission_id, start.isoformat()),
        )
        for index, hours in enumerate((0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22)):
            stamp = start + timedelta(hours=hours)
            cursor.execute(
                """INSERT INTO vitals (
                       admission_id, datetime, sys, dia, pulse, temp, spo2, rr, cvp,
                       last_modified_by, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    admission_id, stamp.isoformat(), 112 + (index % 4) * 4,
                    68 + (index % 3) * 3, 72 + (index % 5) * 2,
                    36.4 + (index % 3) * 0.1, 96 + (index % 3), 16 + (index % 3),
                    5 + (index % 2), "theme-card-preview", stamp.isoformat(),
                ),
            )
        return admission_id

    return int(db_manager.run_write_operation(operation, source="theme_card_preview_seed"))


def _process_events(app, duration_ms: int = 0) -> None:
    from PySide6.QtCore import QEventLoop

    deadline = time.perf_counter() + duration_ms / 1000.0
    while True:
        app.processEvents(QEventLoop.AllEvents, 50)
        if time.perf_counter() >= deadline:
            return
        time.sleep(0.01)


def _dispose_widget(app, widget) -> None:
    """Destroy a card before the next sample without entering its closeEvent."""
    from PySide6.QtCore import QCoreApplication, QEvent
    import shiboken6

    widget.shutdown()
    widget.deleteLater()
    for _ in range(40):
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        _process_events(app)
        if not shiboken6.isValid(widget):
            return
        time.sleep(0.005)
    raise RuntimeError("DoctorRemCardWidget was not deleted before the next preview run")


def _wait_for_snapshot(app, widget, timeout_ms: int = 3500) -> dict[str, float | bool]:
    """Wait only for the async patient snapshot that makes this card usable."""
    started = time.perf_counter()
    deadline = started + timeout_ms / 1000.0
    while time.perf_counter() < deadline:
        _process_events(app)
        chart = getattr(widget, "chart", None)
        if getattr(widget, "_card_snapshot_cache", None) and chart is not None and getattr(chart, "vitals_data", None):
            return {"ready": True, "wait_ms": (time.perf_counter() - started) * 1000}
        time.sleep(0.01)
    return {"ready": False, "wait_ms": (time.perf_counter() - started) * 1000}


def _snapshot(widget, destination: Path) -> dict[str, float]:
    """Measure rendering grab separately from PNG encoding and disk write."""
    widget.layout().activate()
    grab_started = time.perf_counter()
    pixmap = widget.grab()
    grab_ms = (time.perf_counter() - grab_started) * 1000
    save_started = time.perf_counter()
    if pixmap.isNull() or not pixmap.save(str(destination), "PNG"):
        raise RuntimeError(f"Не удалось сохранить снимок {destination}")
    return {
        "grab_ms": grab_ms,
        "png_save_ms": (time.perf_counter() - save_started) * 1000,
    }


TAB_NAMES = {
    "vitals": "Витальные функции",
    "appointments": "Назначения",
    "balance": "Баланс жидкости",
    "ivl": "ИВЛ",
    "analyses": "Анализы",
    "diet": "Диета",
}


def _p95(samples: list[float]) -> float:
    return sorted(samples)[max(0, int((len(samples) * 0.95) + 0.999999) - 1)]


def _summary(rows: list[dict], key: str) -> dict[str, float]:
    values = [float(row[key]) for row in rows]
    return {"median_ms": round(statistics.median(values), 2), "p95_ms": round(_p95(values), 2)}


def _prepare_mode(manager, app, mode: str, *, baseline: bool) -> None:
    if baseline:
        # Compatibility path for a source tree predating ThemeManager.
        from rem_card.app.main import _apply_basic_app_theme

        _apply_basic_app_theme(app)
        return
    manager.set_mode(mode, save=False)
    # set_mode("light") is a no-op on a fresh manager. Apply the global
    # palette/QSS explicitly so a light initial open uses the real theme too.
    manager.apply_to_app(app, role="doctor")


def _open_and_capture(
    *,
    app,
    manager,
    container,
    admission_id: int,
    mode: str,
    tab: str,
    output: Path,
    run_index: int,
    cache_state: str,
    baseline: bool,
    capture: bool = True,
) -> tuple[dict, object]:
    _prepare_mode(manager, app, mode, baseline=baseline)
    from datetime import datetime
    from rem_card.ui.doctor_view.doctor_remcard_widget import DoctorRemCardWidget

    started = time.perf_counter()
    widget = DoctorRemCardWidget(
        container.remcard_service,
        admission_id,
        patient_service=container.patient_service,
        operblock_service=container.operblock_service,
    )
    widget.resize(1440, 940)
    widget.load_patient_card(admission_id, datetime.now(), request_snapshot=True)
    widget.layout_manager.set_patient_selection_mode("card")
    active_open_ms = (time.perf_counter() - started) * 1000
    readiness = _wait_for_snapshot(app, widget)
    end_to_end_ready_ms = active_open_ms + float(readiness["wait_ms"])

    tab_started = time.perf_counter()
    resolved_tab = widget.layout_manager.set_active_tab(TAB_NAMES[tab], source="cache")
    tab_switch_sync_ms = (time.perf_counter() - tab_started) * 1000
    drain_started = time.perf_counter()
    _process_events(app)
    tab_event_drain_ms = (time.perf_counter() - drain_started) * 1000

    image_name = f"doctor-card-{tab}-{mode}.png" if capture else None
    snapshot = _snapshot(widget, output / image_name) if image_name is not None else {
        "grab_ms": 0.0, "png_save_ms": 0.0,
    }
    end_to_end_first_frame_ms = (
        end_to_end_ready_ms + tab_switch_sync_ms + tab_event_drain_ms + snapshot["grab_ms"]
    )
    return (
        {
            "run": run_index,
            "mode": mode,
            "tab": tab,
            "resolved_tab": resolved_tab,
            "cache_state": cache_state,
            "snapshot_ready": bool(readiness["ready"]),
            "active_open_ms": round(active_open_ms, 2),
            "readiness_wait_ms": round(float(readiness["wait_ms"]), 2),
            "end_to_end_ready_ms": round(end_to_end_ready_ms, 2),
            "end_to_end_first_frame_ms": round(end_to_end_first_frame_ms, 2),
            "tab_switch_sync_ms": round(tab_switch_sync_ms, 2),
            "tab_event_drain_ms": round(tab_event_drain_ms, 2),
            "sync_grab_ms": round(snapshot["grab_ms"], 2),
            "png_save_ms": round(snapshot["png_save_ms"], 2),
            "screenshot": image_name,
        },
        widget,
    )


def _run(
    output: Path,
    platform: str,
    modes: list[str],
    tab: str,
    runs: int,
    *,
    baseline: bool,
) -> dict:
    with tempfile.TemporaryDirectory(prefix="remcard-theme-card-preview-") as temp_dir:
        root = Path(temp_dir)
        _configure_isolated_environment(root, platform)
        _install_isolated_qsettings(root / "state" / "qtsettings")

        from PySide6.QtGui import QFont, QFontDatabase
        from PySide6.QtWidgets import QApplication
        from rem_card.app.bootstrap import bootstrap

        app = QApplication.instance() or QApplication([])
        segoe = Path(os.environ.get("WINDIR", r"C:\\Windows")) / "Fonts" / "segoeui.ttf"
        if segoe.is_file():
            QFontDatabase.addApplicationFont(str(segoe))
        app.setFont(QFont("Segoe UI", 10))

        manager = None
        if baseline:
            _prepare_mode(None, app, "baseline", baseline=True)
        else:
            from rem_card.ui.styles.theme_manager import get_theme_manager

            manager = get_theme_manager()
            manager.storage.save = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("theme_card_preview must never save preferences")
            )
            manager.load("doctor")
            _prepare_mode(manager, app, "light", baseline=False)

        bootstrap_started = time.perf_counter()
        container = bootstrap(role="doctor")
        bootstrap_ms = (time.perf_counter() - bootstrap_started) * 1000
        admission_id = _seed_synthetic_card(container.db_manager)

        rows_by_mode: dict[str, list[dict]] = {mode: [] for mode in modes}
        for mode_index, mode in enumerate(modes):
            for run_index in range(1, runs + 1):
                row, widget = _open_and_capture(
                    app=app, manager=manager, container=container,
                    admission_id=admission_id, mode=mode, tab=tab,
                    output=output, run_index=run_index,
                    cache_state="cold" if mode_index == 0 and run_index == 1 else "warm",
                    baseline=baseline,
                )
                rows_by_mode[mode].append(row)
                _dispose_widget(app, widget)

        toggle_result = None
        if "light" in modes and not baseline:
            _toggle_row, toggle = _open_and_capture(
                app=app, manager=manager, container=container,
                admission_id=admission_id, mode="light", tab=tab,
                output=output, run_index=0, cache_state="warm",
                baseline=False, capture=False,
            )
            switch_started = time.perf_counter()
            manager.set_mode("dark", save=False)
            switch_call_ms = (time.perf_counter() - switch_started) * 1000
            toggle_image = f"doctor-card-{tab}-existing-dark.png"
            toggle_snapshot = _snapshot(toggle, output / toggle_image)
            toggle_result = {
                "set_mode_sync_ms": round(switch_call_ms, 2),
                "sync_grab_ms": round(toggle_snapshot["grab_ms"], 2),
                "png_save_ms": round(toggle_snapshot["png_save_ms"], 2),
                "total_sync_ms": round(switch_call_ms + toggle_snapshot["grab_ms"], 2),
                "screenshot": toggle_image,
            }
            _dispose_widget(app, toggle)

        result = {
            "scope": "real DoctorRemCardWidget and full patient-card layout; synthetic temporary database",
            "theme_path": "baseline _apply_basic_app_theme" if baseline else "runtime ThemeManager",
            "database_root": "temporary directory removed after run",
            "settings": "isolated temporary QSettings and disabled theme persistence",
            "deferred_delete_verified": True,
            "tab": tab,
            "runs_per_initial_mode": runs,
            "initial_open_runs": rows_by_mode,
            "initial_open_summary_ms": {
                mode: {
                    "active_open": _summary(rows, "active_open_ms"),
                    "readiness_wait": _summary(rows, "readiness_wait_ms"),
                    "end_to_end_ready": _summary(rows, "end_to_end_ready_ms"),
                    "end_to_end_first_frame": _summary(rows, "end_to_end_first_frame_ms"),
                    "sync_grab": _summary(rows, "sync_grab_ms"),
                }
                for mode, rows in rows_by_mode.items()
            },
            "existing_card_light_to_dark_sync": toggle_result,
            "bootstrap_and_schema_ms": {
                "bootstrap_and_schema": round(bootstrap_ms, 2),
            },
            "limitations": [
                "This invocation records one source tree; compare equal cold or warm rows from separate baseline/new runs.",
                "--baseline is a raw comparison only when this script is run from a source tree without ThemeManager.",
                "The p95 is the highest sample when fewer than 20 runs are requested.",
                "A tab's async content after its first event drain is not included in synchronous tab or grab timings.",
            ],
        }
        container.data_service.shutdown()
        container.db_manager.close()
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Снимки реальной RemCard на синтетических данных")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--platform", default="offscreen", choices=("offscreen", "windows"))
    parser.add_argument("--mode", default="both", choices=("light", "dark", "both"))
    parser.add_argument("--tab", default="vitals", choices=tuple(TAB_NAMES))
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--baseline", action="store_true", help="Старая базовая тема без ThemeManager")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.baseline and args.mode != "light":
        parser.error("--baseline допускает только --mode light")
    modes = ["baseline"] if args.baseline else (["light", "dark"] if args.mode == "both" else [args.mode])
    result = _run(args.output, args.platform, modes, args.tab, max(1, args.runs), baseline=args.baseline)
    (args.output / "timings.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
