#!/usr/bin/env python
"""
Startup benchmark for RemCard and patient-bed management entry paths.

Usage:
  python scripts/startup_benchmark.py --mode remcard --role doctor
  python scripts/startup_benchmark.py --mode patient-bed-management
  python scripts/startup_benchmark.py --role operblock --runs 5
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, List


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_PARENT = PROJECT_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from _local_rem_card_bootstrap import bootstrap_local_rem_card

    bootstrap_local_rem_card()
except Exception:
    if str(PROJECT_PARENT) not in sys.path:
        sys.path.insert(0, str(PROJECT_PARENT))


@dataclass
class StepResult:
    step: str
    ms: float


BENCHMARK_COLUMNS = [
    ("window_ms", "time_to_window_visible_ms"),
    ("board_ready_ms", "time_to_operblock_board_ready_ms"),
    ("bootstrap_ms", "bootstrap_ms"),
    ("opblock_import_ms", "opblock_import_ms"),
    ("role_activation_ms", "role_activation_ms"),
    ("widget_import_ms", "operblock_widget_import_ms"),
    ("widget_create_ms", "operblock_widget_create_ms"),
    ("init_ui_ms", "operblock_init_ui_ms"),
    ("auto_refresh_ms", "start_auto_refresh_ms"),
    ("first_refresh_ms", "first_refresh_board_ms"),
    ("snapshot_ms", "refresh_board_snapshot_ms"),
    ("apply_total_ms", "refresh_board_apply_total_ms"),
    ("clear_ms", "board_apply_clear_total_ms"),
    ("card_loop_ms", "board_apply_card_loop_total_ms"),
    ("empty_card_ms", "board_apply_make_empty_card_ms"),
    ("occupied_card_ms", "board_apply_make_occupied_card_ms"),
    ("photo_total_ms", "board_apply_card_photo_total_ms"),
    ("photo_load_ms", "board_apply_card_photo_pixmap_load_ms"),
    ("photo_scaled_ms", "board_apply_card_photo_scaled_ms"),
    ("layout_ms", "board_apply_layout_add_ms"),
    ("table_count", "board_apply_table_count"),
    ("occupied_count", "board_apply_occupied_count"),
    ("empty_count", "board_apply_empty_count"),
    ("recreated_count", "board_apply_card_recreated_count"),
    ("reused_count", "board_apply_card_reused_count"),
    ("skipped_unchanged_count", "board_apply_card_skipped_unchanged_count"),
    ("photo_cache_hit", "board_apply_photo_cache_hit_count"),
    ("photo_cache_miss", "board_apply_photo_cache_miss_count"),
    ("refresh_count_before_ready", "refresh_count_before_ready"),
    ("chart_import_before_ready", "chart_import_before_ready"),
    ("max_pause", "max_event_loop_pause_ms"),
]

TOP_PHASE_EXCLUDE = {
    "opblock_process_start_ts",
    "time_to_window_visible_ms",
    "time_to_operblock_board_ready_ms",
    "board_ready_after_paint_ms",
    "max_event_loop_pause_ms",
}

NESTED_PHASES = {
    "start_auto_refresh_ms": "wrapper -> first_refresh_board_ms",
    "first_refresh_board_ms": "child of start_auto_refresh_ms; includes snapshot/apply",
    "refresh_board_snapshot_ms": "child of first_refresh_board_ms",
    "build_operblock_board_snapshot_ms": "child of refresh_board_snapshot_ms",
    "refresh_board_apply_total_ms": "child of first_refresh_board_ms",
    "apply_board_snapshot_ms": "child of first_refresh_board_ms",
    "board_apply_clear_total_ms": "child of apply_board_snapshot_ms",
    "board_apply_card_loop_total_ms": "child of apply_board_snapshot_ms",
    "board_apply_make_empty_card_ms": "child of board_apply_card_loop_total_ms",
    "board_apply_make_occupied_card_ms": "child of board_apply_card_loop_total_ms",
    "board_apply_card_photo_total_ms": "child of card creation",
    "board_apply_layout_add_ms": "child of board_apply_card_loop_total_ms",
    "board_apply_order_relayout_ms": "child of board_apply_card_loop_total_ms",
}


def _now() -> float:
    return time.perf_counter()


def _ms(start: float) -> float:
    return round((_now() - start) * 1000.0, 2)


def _git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(PROJECT_ROOT),
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _db_counts(db_path: str) -> dict[str, int | str]:
    result: dict[str, int | str] = {}
    if not db_path:
        return result
    try:
        uri = "file:" + Path(db_path).as_posix() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            cursor = conn.cursor()
            for table in ("patients", "admissions", "operation_cases", "orders", "vitals"):
                try:
                    cursor.execute(f"SELECT COUNT(*) FROM {table}")
                    result[f"{table}_count"] = int(cursor.fetchone()[0])
                except Exception as exc:
                    result[f"{table}_count"] = f"error:{exc}"
        finally:
            conn.close()
    except Exception as exc:
        result["db_counts_error"] = str(exc)
    return result


def _runtime_metadata(container: Any, *, qt_version: str = "") -> dict[str, Any]:
    db_manager = getattr(container, "db_manager", None)
    db_path = str(getattr(db_manager, "db_path", "") or getattr(db_manager, "medical_db_path", "") or "")
    db_size_mb = None
    try:
        db_size_mb = round(os.path.getsize(db_path) / (1024 * 1024), 3) if db_path else None
    except Exception:
        db_size_mb = None
    metadata = {
        "runtime_mode": getattr(container, "runtime_mode", None),
        "db_path": db_path,
        "db_size_mb": db_size_mb,
        "local_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "timezone": datetime.now().astimezone().tzname(),
        "git_commit": _git_commit(),
        "python_version": sys.version.replace("\n", " "),
        "qt_version": qt_version,
    }
    metadata.update(_db_counts(db_path))
    return metadata


def _benchmark_remcard(role: str) -> dict:
    if role == "operblock":
        return _benchmark_operblock()

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    _ = app

    steps: List[StepResult] = []
    container = None
    window = None
    role_widget = None

    t = _now()
    from rem_card.app.main import _apply_app_theme

    _apply_app_theme(app, role)
    theme_ui_init_ms = _ms(t)
    steps.append(StepResult("theme_ui_init", theme_ui_init_ms))

    t = _now()
    from rem_card.ui.main_window import MainWindow

    steps.append(StepResult("import_ui_main_window", _ms(t)))

    t = _now()
    from rem_card.app.bootstrap import bootstrap

    steps.append(StepResult("import_bootstrap", _ms(t)))

    t = _now()
    container = bootstrap()
    bootstrap_ms = _ms(t)
    steps.append(StepResult("bootstrap", bootstrap_ms))
    startup_phases = dict(getattr(getattr(container, "db_manager", None), "startup_metrics", {}) or {})
    startup_phases["theme_ui_init_ms"] = theme_ui_init_ms
    startup_phases["total_bootstrap_ms"] = bootstrap_ms

    t = _now()
    window = MainWindow(container=None, role=role)
    steps.append(StepResult("construct_main_window", _ms(t)))

    if role == "doctor":
        t = _now()
        from rem_card.ui.doctor_view.doctor_main_widget import DoctorMainWidget

        role_widget = DoctorMainWidget(container.patient_service, container.remcard_service)
        steps.append(StepResult("construct_doctor_main_widget", _ms(t)))
    elif role == "nurse":
        t = _now()
        from rem_card.ui.nurse_view.nurse_main_widget import NurseMainWidget

        role_widget = NurseMainWidget(container.patient_service, container.remcard_service)
        steps.append(StepResult("construct_nurse_main_widget", _ms(t)))

    total = round(sum(step.ms for step in steps), 2)
    result = {
        "mode": "remcard",
        "role": role,
        "steps": [asdict(step) for step in steps],
        "startup_phases": startup_phases,
        "total_ms": total,
    }

    if role_widget is not None:
        role_widget.deleteLater()
    if window is not None:
        window.deleteLater()
    if container is not None:
        try:
            container.data_service.shutdown()
        except Exception:
            pass
        try:
            container.db_manager.close()
        except Exception:
            pass

    return result


def _benchmark_operblock() -> dict:
    process_started = _now()
    os.environ["REMCARD_OPERBLOCK_STARTUP_METRICS"] = "1"
    os.environ["REMCARD_UI_ROLE"] = "operblock"
    os.environ["REMCARD_LOCAL_FIRST_SYNC"] = "0"
    os.environ["REMCARD_LOCAL_OUTBOX_SYNC"] = "0"

    import_started = process_started
    from PySide6.QtCore import QCoreApplication, Qt, qVersion
    from PySide6.QtWidgets import QApplication
    from rem_card.app import operblock_startup_metrics

    operblock_startup_metrics.reset(started_at=process_started)

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    steps: List[StepResult] = []
    container = None
    window = None

    from rem_card.app.main import _apply_app_theme, _configure_operblock_startup_path
    from rem_card.app.bootstrap import bootstrap
    from rem_card.ui.main_window import MainWindow

    operblock_startup_metrics.record_since("opblock_import_ms", import_started, source="startup_benchmark")
    steps.append(StepResult("opblock_import", _ms(import_started)))

    path_setup = _configure_operblock_startup_path("operblock", False)
    if path_setup:
        raise RuntimeError("startup_benchmark does not support path setup mode")

    t = _now()
    _apply_app_theme(app, "operblock")
    theme_ui_init_ms = _ms(t)
    steps.append(StepResult("theme_ui_init", theme_ui_init_ms))

    t = _now()
    container = bootstrap(role="operblock")
    bootstrap_ms = _ms(t)
    operblock_startup_metrics.record_duration("bootstrap_ms", bootstrap_ms, source="startup_benchmark")
    steps.append(StepResult("bootstrap", bootstrap_ms))

    metadata = _runtime_metadata(container, qt_version=qVersion())
    operblock_startup_metrics.set_context(**metadata)

    startup_phases = dict(getattr(getattr(container, "db_manager", None), "startup_metrics", {}) or {})
    startup_phases["theme_ui_init_ms"] = theme_ui_init_ms
    startup_phases["total_bootstrap_ms"] = bootstrap_ms

    t = operblock_startup_metrics.timer_start()
    window = MainWindow(container=container, role="operblock")
    operblock_startup_metrics.record_since("main_window_create_ms", t, source="startup_benchmark")
    steps.append(StepResult("construct_main_window", _ms(t)))

    if hasattr(window, "prepare_initial_role_ui_for_startup"):
        t = _now()
        prepared = bool(window.prepare_initial_role_ui_for_startup())
        steps.append(StepResult("prepare_initial_role_ui", _ms(t)))
    else:
        prepared = False

    if prepared and hasattr(window, "start_initial_role_refresh"):
        window.start_initial_role_refresh()

    app.processEvents()
    window.show()
    operblock_startup_metrics.record_elapsed("time_to_window_visible_ms", source="startup_benchmark")
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, 0)
    app.processEvents()
    operblock_startup_metrics.record_elapsed("board_ready_after_paint_ms", source="startup_benchmark")
    operblock_startup_metrics.record_elapsed("time_to_operblock_board_ready_ms", source="startup_benchmark")
    pyqtgraph_before_ready = "pyqtgraph" in sys.modules
    chart_widget_before_ready = "rem_card.ui.shared.chart_widget" in sys.modules
    operblock_chart_before_ready = "rem_card.ui.operblock_view.operblock_chart_widget" in sys.modules
    operblock_startup_metrics.record_value(
        "pyqtgraph_import_before_ready",
        pyqtgraph_before_ready,
        source="startup_benchmark",
    )
    operblock_startup_metrics.record_value(
        "chart_widget_import_before_ready",
        chart_widget_before_ready,
        source="startup_benchmark",
    )
    operblock_startup_metrics.record_value(
        "operblock_chart_widget_import_before_ready",
        operblock_chart_before_ready,
        source="startup_benchmark",
    )
    operblock_startup_metrics.record_value(
        "chart_import_before_ready",
        bool(pyqtgraph_before_ready or chart_widget_before_ready or operblock_chart_before_ready),
        source="startup_benchmark",
    )

    metrics_snapshot = operblock_startup_metrics.snapshot()
    metrics = dict(metrics_snapshot.get("metrics") or {})
    steps.append(
        StepResult(
            "time_to_operblock_board_ready",
            round(float(metrics.get("time_to_operblock_board_ready_ms") or 0.0), 2),
        )
    )

    result = {
        "mode": "remcard",
        "role": "operblock",
        "metadata": metadata,
        "steps": [asdict(step) for step in steps],
        "startup_phases": startup_phases,
        "operblock_startup": metrics_snapshot,
        "total_ms": round(float(metrics.get("time_to_operblock_board_ready_ms") or 0.0), 2),
    }

    if window is not None:
        try:
            window.release_role_lock()
        except Exception:
            pass
        try:
            window.close()
            app.processEvents()
        except Exception:
            pass
        window.deleteLater()
    if container is not None:
        try:
            container.data_service.shutdown()
        except Exception:
            pass
        try:
            container.db_manager.close()
        except Exception:
            pass
    try:
        from rem_card.app.local_metrics import flush_metrics

        flush_metrics(timeout=1.0)
    except Exception:
        pass

    _ = Qt
    return result


def _benchmark_patient_bed_management() -> dict:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    _ = app

    steps: List[StepResult] = []
    container = None
    window = None

    t = _now()
    from rem_card.app.main import _apply_app_theme

    _apply_app_theme(app, "system")
    theme_ui_init_ms = _ms(t)
    steps.append(StepResult("theme_ui_init", theme_ui_init_ms))

    t = _now()
    from rem_card.ui.patient_bed_management.management_widget import PatientBedManagementWidget

    steps.append(StepResult("import_patient_bed_management_widget", _ms(t)))

    t = _now()
    from rem_card.app.bootstrap import bootstrap

    steps.append(StepResult("import_bootstrap", _ms(t)))

    t = _now()
    container = bootstrap()
    bootstrap_ms = _ms(t)
    steps.append(StepResult("bootstrap", bootstrap_ms))
    startup_phases = dict(getattr(getattr(container, "db_manager", None), "startup_metrics", {}) or {})
    startup_phases["theme_ui_init_ms"] = theme_ui_init_ms
    startup_phases["total_bootstrap_ms"] = bootstrap_ms

    t = _now()
    db_manager = container.remcard_service.orders_dao.db
    window = PatientBedManagementWidget(db_manager)
    steps.append(StepResult("construct_patient_bed_management_widget", _ms(t)))

    total = round(sum(step.ms for step in steps), 2)
    result = {
        "mode": "patient-bed-management",
        "steps": [asdict(step) for step in steps],
        "startup_phases": startup_phases,
        "total_ms": total,
    }

    if window is not None:
        window.deleteLater()
    if container is not None:
        try:
            container.data_service.shutdown()
        except Exception:
            pass
        try:
            container.db_manager.close()
        except Exception:
            pass

    return result


def _metric(result: dict, name: str) -> Any:
    return ((result.get("operblock_startup") or {}).get("metrics") or {}).get(name)


def _phase_counts(result: dict) -> dict[str, int]:
    return dict(((result.get("operblock_startup") or {}).get("counts") or {}))


def _fmt_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.1f}"
    try:
        return f"{float(value):.1f}"
    except Exception:
        return str(value)


def _phase_label(name: str) -> str:
    detail = NESTED_PHASES.get(name)
    return f"{name} ({detail})" if detail else name


def _table(headers: list[str], rows: Iterable[Iterable[Any]]) -> str:
    rendered_rows = [[str(cell) for cell in row] for row in rows]
    widths = [len(str(header)) for header in headers]
    for row in rendered_rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    header_line = " | ".join(str(header).ljust(widths[index]) for index, header in enumerate(headers))
    separator = "-+-".join("-" * width for width in widths)
    body = [" | ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)) for row in rendered_rows]
    return "\n".join([header_line, separator, *body])


def _top_phases(result: dict, limit: int = 5) -> list[tuple[str, float]]:
    durations = dict(((result.get("operblock_startup") or {}).get("durations") or {}))
    items = [
        (name, float(value))
        for name, value in durations.items()
        if name not in TOP_PHASE_EXCLUDE and isinstance(value, (int, float))
    ]
    items.sort(key=lambda item: item[1], reverse=True)
    return items[:limit]


def _print_single_summary(result: dict) -> None:
    if result.get("role") != "operblock":
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    metrics = (result.get("operblock_startup") or {}).get("metrics") or {}
    counts = _phase_counts(result)
    phase_rows = [
        (name, _fmt_value(value), counts.get(name, 1))
        for name, value in sorted(metrics.items())
        if name != "opblock_process_start_ts"
    ]
    print("Operblock startup phases")
    print(_table(["phase", "ms/value", "count"], phase_rows))
    print()
    print(f"time_to_operblock_board_ready_ms: {_fmt_value(metrics.get('time_to_operblock_board_ready_ms'))}")
    print(f"time_to_window_visible_ms: {_fmt_value(metrics.get('time_to_window_visible_ms'))}")
    print(f"max_event_loop_pause_ms: {_fmt_value(metrics.get('max_event_loop_pause_ms'))}")
    print()
    print("Top-5 phases")
    print(_table(["phase", "ms"], [(_phase_label(name), _fmt_value(value)) for name, value in _top_phases(result)]))
    print()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def _run_single(args: argparse.Namespace) -> dict:
    if args.mode == "remcard":
        return _benchmark_remcard(args.role)
    return _benchmark_patient_bed_management()


def _parse_child_json(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        text = line.strip()
        if text.startswith("{") and text.endswith("}"):
            return json.loads(text)
    return json.loads(stdout)


def _run_child(args: argparse.Namespace, run_index: int) -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_PARENT) + os.pathsep + env.get("PYTHONPATH", "")
    env["REMCARD_OPERBLOCK_STARTUP_METRICS"] = "1" if args.role == "operblock" else env.get(
        "REMCARD_OPERBLOCK_STARTUP_METRICS",
        "",
    )
    env["REMCARD_OPERBLOCK_STARTUP_RUN_ID"] = f"startup-benchmark-{run_index}"
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--mode",
        args.mode,
        "--role",
        args.role,
        "--runs",
        "1",
        "--json-only",
    ]
    completed = subprocess.run(
        command,
        cwd=str(PROJECT_ROOT),
        env=env,
        text=True,
        capture_output=True,
        timeout=max(30, int(args.child_timeout_sec)),
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"run {run_index} failed with code {completed.returncode}\n"
            f"STDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
        )
    return _parse_child_json(completed.stdout)


def _summarize_runs(results: list[dict]) -> dict:
    values = [
        float(_metric(result, "time_to_operblock_board_ready_ms") or 0.0)
        for result in results
        if result.get("role") == "operblock"
    ]
    summary = {
        "runs": len(results),
        "raw_time_to_operblock_board_ready_ms": values,
    }
    if values:
        summary.update(
            {
                "min_time_to_operblock_board_ready_ms": round(min(values), 3),
                "median_time_to_operblock_board_ready_ms": round(statistics.median(values), 3),
                "mean_time_to_operblock_board_ready_ms": round(statistics.mean(values), 3),
                "max_time_to_operblock_board_ready_ms": round(max(values), 3),
            }
        )
    return summary


def _print_multi_summary(results: list[dict]) -> None:
    if not results or results[0].get("role") != "operblock":
        print(json.dumps({"runs": results}, ensure_ascii=False, indent=2, default=str))
        return
    rows = []
    for index, result in enumerate(results, start=1):
        rows.append([index, *[_fmt_value(_metric(result, metric_name)) for _header, metric_name in BENCHMARK_COLUMNS]])
    print("Operblock startup benchmark")
    print(_table(["run", *[header for header, _metric_name in BENCHMARK_COLUMNS]], rows))
    print()
    summary = _summarize_runs(results)
    print("Board ready summary")
    print(
        _table(
            ["min", "median", "mean", "max", "raw"],
            [
                [
                    _fmt_value(summary.get("min_time_to_operblock_board_ready_ms")),
                    _fmt_value(summary.get("median_time_to_operblock_board_ready_ms")),
                    _fmt_value(summary.get("mean_time_to_operblock_board_ready_ms")),
                    _fmt_value(summary.get("max_time_to_operblock_board_ready_ms")),
                    ", ".join(_fmt_value(value) for value in summary.get("raw_time_to_operblock_board_ready_ms", [])),
                ]
            ],
        )
    )
    print()
    aggregate: dict[str, list[float]] = {}
    for result in results:
        for name, value in ((result.get("operblock_startup") or {}).get("durations") or {}).items():
            if name in TOP_PHASE_EXCLUDE:
                continue
            if isinstance(value, (int, float)):
                aggregate.setdefault(name, []).append(float(value))
    top_rows = []
    for name, phase_values in aggregate.items():
        top_rows.append((name, statistics.mean(phase_values), max(phase_values)))
    top_rows.sort(key=lambda item: item[1], reverse=True)
    print("Top-5 phases by mean")
    print(
        _table(
            ["phase", "mean_ms", "max_ms"],
            [(_phase_label(name), _fmt_value(mean), _fmt_value(maximum)) for name, mean, maximum in top_rows[:5]],
        )
    )
    print()
    print(json.dumps({"summary": summary, "runs": results}, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    parser = argparse.ArgumentParser(description="Startup benchmark for RemCard/patient-bed management")
    parser.add_argument("--mode", choices=["remcard", "patient-bed-management"], default="remcard")
    parser.add_argument("--role", choices=["doctor", "nurse", "operblock"], default="doctor", help="Role for remcard mode")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--json-only", action="store_true")
    parser.add_argument("--child-timeout-sec", type=int, default=120)
    args = parser.parse_args()
    args.runs = max(1, int(args.runs or 1))

    if args.runs > 1:
        results = [_run_child(args, index) for index in range(1, args.runs + 1)]
        if args.json_only:
            print(json.dumps({"summary": _summarize_runs(results), "runs": results}, ensure_ascii=False, default=str))
        else:
            _print_multi_summary(results)
        return

    result = _run_single(args)
    if args.json_only:
        print(json.dumps(result, ensure_ascii=False, default=str))
    else:
        _print_single_summary(result)


if __name__ == "__main__":
    main()
