#!/usr/bin/env python
"""
Startup benchmark for RemCard and patient-bed management entry paths.

Usage:
  set PYTHONPATH=C:\Project
  python %REMCARD_PROJECT_ROOT%\scripts\startup_benchmark.py --mode remcard
  python %REMCARD_PROJECT_ROOT%\scripts\startup_benchmark.py --mode patient-bed-management
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, asdict
from typing import List


@dataclass
class StepResult:
    step: str
    ms: float


def _now() -> float:
    return time.perf_counter()


def _ms(start: float) -> float:
    return round((_now() - start) * 1000.0, 2)


def _benchmark_remcard(role: str) -> dict:
    import sys
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


def _benchmark_patient_bed_management() -> dict:
    import sys
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


def main():
    parser = argparse.ArgumentParser(description="Startup benchmark for RemCard/patient-bed management")
    parser.add_argument("--mode", choices=["remcard", "patient-bed-management"], required=True)
    parser.add_argument("--role", choices=["doctor", "nurse"], default="doctor", help="Role for remcard mode")
    args = parser.parse_args()

    if args.mode == "remcard":
        result = _benchmark_remcard(args.role)
    else:
        result = _benchmark_patient_bed_management()

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
