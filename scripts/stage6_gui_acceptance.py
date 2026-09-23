#!/usr/bin/env python
"""Live Qt acceptance for Stage 6.5 on a previously-created synthetic SMB run.

Source usage configures isolation itself.  A frozen test entry may configure the
same environment first and execute this file with ``runpy.run_path``.  The
runner never creates a database and refuses roots without successful synthetic
network-harness evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AUTHORIZED_ROOT = Path(
    r"\\fs.acrb-amursk.ru\common\РАО\Пациенты\remcard\test\_etap6"
)
RUN_PREFIX = "stage6_"


def _normalized(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(os.fspath(path)))).rstrip("\\/")


def _is_within(path: str | os.PathLike[str], root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(temporary, path)


def _validate_synthetic_run(run_root: Path) -> dict[str, Any]:
    if _normalized(run_root.parent) != _normalized(AUTHORIZED_ROOT):
        raise ValueError(f"GUI acceptance root must be a direct child of {AUTHORIZED_ROOT}")
    if not run_root.name.startswith(RUN_PREFIX) or not run_root.is_dir():
        raise ValueError(f"Invalid or unavailable synthetic run root: {run_root}")
    evidence = run_root / "evidence" / "stage6_network_acceptance.json"
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.get("ok") is not True
        or payload.get("synthetic_only") is not True
        or _normalized(payload.get("run_root", "")) != _normalized(run_root)
    ):
        raise RuntimeError(f"Synthetic network acceptance evidence is missing or unsuccessful: {evidence}")
    db_path = run_root / "archiv" / "rao_journal.db"
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    return {"network_evidence": str(evidence), "db_path": str(db_path)}


def build_source_environment(
    run_root: Path,
    local_root: Path,
    *,
    platform: str = "windows",
) -> dict[str, str]:
    """Build the source-run profile without importing RemCard application code."""
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from scripts.stage6_network_acceptance import build_isolated_child_environment

    env = build_isolated_child_environment(run_root, local_root, allow_setup=False)
    env.update(
        {
            "QT_QPA_PLATFORM": platform,
            "REMCARD_LOCAL_METRICS_ENABLED": "1",
            "REMCARD_LOCAL_METRICS_SYNC": "1",
            "REMCARD_LOCAL_FIRST_SYNC": "1",
            "REMCARD_LOCAL_SYNC_INTERVAL_SEC": "2",
            "REMCARD_PERSISTENT_SNAPSHOT_CACHE": "1",
            "REMCARD_BACKGROUND_INTEGRITY_ENABLED": "0",
            "REMCARD_STARTUP_QUICKCHECK_BACKGROUND_ENABLED": "0",
            "REMCARD_UI_WATCHDOG_ENABLED": "1",
        }
    )
    return env


def apply_source_environment(env: dict[str, str]) -> None:
    """Apply source isolation and bootstrap the local package before Qt imports."""
    for key in list(os.environ):
        if key.startswith("REMCARD_"):
            os.environ.pop(key, None)
    os.environ.update(env)
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from _local_rem_card_bootstrap import bootstrap_local_rem_card

    bootstrap_local_rem_card()
    from rem_card.app.isolated_test_runtime import isolate_qsettings

    isolate_qsettings(env["REMCARD_CI_SETTINGS_DIR"])


def validate_preconfigured_environment(
    run_root: Path,
    local_root: Path,
    *,
    require_config: bool = False,
) -> dict[str, Any]:
    """Fail closed when a frozen test bootstrap did not redirect every state path."""
    network_keys = ("REMCARD_BAZA_DIR", "REMCARD_DEV_BAZA_DIR")
    local_keys = (
        "REMCARD_DEV_DATABASE_CONFIG", "REMCARD_DATA_PATH_CONFIG",
        "REMCARD_LOCAL_LOGS_DIR", "REMCARD_CRASH_OUTBOX_DIR",
        "REMCARD_STYLE_SETTINGS_PATH", "REMCARD_DISPLAY_SETTINGS_PATH",
        "REMCARD_BACKGROUND_SETTINGS_PATH", "REMCARD_LAB_COLUMNS_SETTINGS_PATH",
        "REMCARD_CI_SETTINGS_DIR", "REMCARD_EMERGENCY_DB_ROOT",
        "REMCARD_MEDIA_CACHE_DIR", "REMCARD_OUTBOX_PATH", "REMCARD_REPLICA_PATH",
        "LOCALAPPDATA", "APPDATA", "ProgramData", "USERPROFILE", "HOME", "TEMP", "TMP",
    )
    mismatches = {
        key: os.environ.get(key, "")
        for key in network_keys
        if _normalized(os.environ.get(key, "")) != _normalized(run_root)
    }
    mismatches.update(
        {
            key: os.environ.get(key, "")
            for key in local_keys
            if not _is_within(os.environ.get(key, ""), local_root)
        }
    )
    required_values = {
        "REMCARD_LOCAL_METRICS_ENABLED": "1",
        "REMCARD_LOCAL_METRICS_SYNC": "1",
        "REMCARD_LOCAL_FIRST_SYNC": "1",
        "REMCARD_DEV_EXISTING_BAZA_ONLY": "1",
        "REMCARD_PATH_SETUP_MODE": "0",
    }
    for key, expected in required_values.items():
        if os.environ.get(key) != expected:
            mismatches[key] = os.environ.get(key, "")
    config_path = Path(os.environ.get("REMCARD_DATA_PATH_CONFIG", ""))
    configured_root = ""
    if require_config:
        try:
            config_payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
            configured_root = str(config_payload.get("baza_dir") or config_payload.get("path") or "")
        except (OSError, TypeError, ValueError):
            mismatches["REMCARD_DATA_PATH_CONFIG_CONTENT"] = "unreadable"
        else:
            if _normalized(configured_root) != _normalized(run_root):
                mismatches["REMCARD_DATA_PATH_CONFIG_CONTENT"] = configured_root
    if mismatches:
        raise RuntimeError(f"Stage 6 GUI isolation is incomplete: {mismatches}")
    return {
        "ok": True,
        "network_keys": list(network_keys),
        "local_keys": list(local_keys),
        "local_first_sync": os.environ["REMCARD_LOCAL_FIRST_SYNC"],
        "qt_platform": os.environ.get("QT_QPA_PLATFORM", ""),
        "configured_root": configured_root,
    }


def _collect_startup_metrics(local_root: Path) -> dict[str, Any]:
    """Summarize local startup diagnostics without copying clinical payloads."""
    events: list[dict[str, Any]] = []
    configured_log_root = Path(os.environ.get("REMCARD_LOCAL_LOGS_DIR", local_root / "Logs"))
    if not _is_within(configured_log_root, local_root):
        return {
            "count": 0,
            "events": [],
            "logs_root": "",
            "error": "metrics log root is outside local state root",
        }
    log_root = configured_log_root
    for path in sorted(log_root.glob("metrics*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                payload = json.loads(line)
            except (TypeError, ValueError):
                continue
            if payload.get("metric") != "startup_diagnostic":
                continue
            events.append(
                {
                    "pid": payload.get("pid"),
                    "stage": payload.get("stage"),
                    "phase": payload.get("phase"),
                    "outcome": payload.get("outcome"),
                    "role": payload.get("role"),
                    "session_id": payload.get("session_id"),
                    "target": payload.get("target"),
                    "duration_ms": payload.get("elapsed_ms"),
                }
            )
    return {
        "count": len(events),
        "events": events,
        "logs_root": str(log_root),
    }


def _collect_prewarm_metrics(local_root: Path) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    log_root = Path(os.environ.get("REMCARD_LOCAL_LOGS_DIR", local_root / "Logs"))
    if not _is_within(log_root, local_root):
        return {"count": 0, "events": [], "logs_root": "", "error": "outside local state root"}
    accepted = {"card_ui_prewarm_step_ms", "card_ui_prewarm_total_ms"}
    for path in sorted(log_root.glob("metrics*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                payload = json.loads(line)
            except (TypeError, ValueError):
                continue
            if payload.get("metric") not in accepted:
                continue
            events.append(
                {
                    "metric": payload.get("metric"),
                    "pid": payload.get("pid"),
                    "role": payload.get("role"),
                    "step": payload.get("step"),
                    "mode": payload.get("mode"),
                    "result": payload.get("result"),
                    "duration_ms": payload.get("value"),
                    "work_ms": payload.get("work_ms"),
                    "steps": payload.get("steps"),
                }
            )
    return {"count": len(events), "events": events, "logs_root": str(log_root)}


class _Heartbeat:
    def __init__(self, app: Any, window: Any, local_path: Path):
        from PySide6.QtCore import QTimer

        self.app = app
        self.window = window
        self.local_path = local_path
        self.timer = QTimer(window)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._tick)
        self.phase = "startup"
        self.ticks = 0
        self.max_gap_ms = 0.0
        self.gaps_over_1000_ms: list[float] = []
        self.modals: list[dict[str, str]] = []
        self._last = time.perf_counter()
        self._last_json = 0.0

    def start(self) -> None:
        self._last = time.perf_counter()
        self.timer.start()

    def stop(self) -> None:
        self.timer.stop()
        self._write()

    def set_phase(self, value: str) -> None:
        self.phase = str(value)

    def _snapshot(self) -> dict[str, Any]:
        return {
            "pid": os.getpid(),
            "phase": self.phase,
            "ticks": self.ticks,
            "max_gap_ms": round(self.max_gap_ms, 3),
            "gaps_over_1000_ms": [round(value, 3) for value in self.gaps_over_1000_ms],
            "role": str(getattr(self.window, "role", "") or ""),
            "busy": bool(getattr(self.window, "_busy", False)),
            "modals": list(self.modals),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def _write(self) -> None:
        _atomic_json(self.local_path, self._snapshot())

    def _tick(self) -> None:
        now = time.perf_counter()
        gap_ms = (now - self._last) * 1000.0
        self._last = now
        self.ticks += 1
        self.max_gap_ms = max(self.max_gap_ms, gap_ms)
        if gap_ms > 1000.0:
            self.gaps_over_1000_ms.append(gap_ms)
        modal = self.app.activeModalWidget()
        if modal is not None:
            event = {
                "class": type(modal).__name__,
                "title": str(getattr(modal, "windowTitle", lambda: "")() or ""),
                "phase": self.phase,
            }
            self.modals.append(event)
            if hasattr(modal, "reject"):
                modal.reject()
            else:
                modal.close()
        if self.ticks % 10 == 0:
            from rem_card.app.local_metrics import record_metric

            record_metric(
                "stage6_gui_heartbeat",
                gap_ms,
                force_flush=True,
                phase=self.phase,
                role=str(getattr(self.window, "role", "") or ""),
                busy=int(bool(getattr(self.window, "_busy", False))),
            )
            self._write()

    def summary(self) -> dict[str, Any]:
        return self._snapshot()


def _wait_until(
    app: Any,
    predicate: Callable[[], bool],
    *,
    timeout: float,
    label: str,
) -> float:
    from PySide6.QtCore import QEventLoop

    started = time.perf_counter()
    deadline = started + timeout
    while time.perf_counter() < deadline:
        app.processEvents(QEventLoop.AllEvents, 50)
        if predicate():
            return time.perf_counter() - started
        time.sleep(0.01)
    raise TimeoutError(f"Timed out waiting for {label} after {timeout:.1f}s")


def _entry_ready(window: Any, role: str) -> bool:
    return bool(
        window.role == role
        and window.container is not None
        and window.role_window is not None
        and window.stack.currentWidget() is window.role_window
        and not window._busy
        and window._entry_cancel is None
    )


def _click_role(window: Any, role: str) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    button = window.welcome.role_buttons[role]
    QTest.mouseClick(button, Qt.LeftButton)


def _cancel_stress(app: Any, window: Any, heartbeat: _Heartbeat, timeout: float) -> dict[str, Any]:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    heartbeat.set_phase("cancel_stress")
    _click_role(window, "doctor")
    for index in range(30):
        _click_role(window, "nurse" if index % 2 else "doctor")
        app.processEvents()
    cancel_button = window.welcome.cancel_entry_button
    if cancel_button.isEnabled():
        QTest.mouseClick(cancel_button, Qt.LeftButton)
    else:
        window.cancel_role_entry()
    elapsed = _wait_until(
        app,
        lambda: (
            not window._busy
            and window.container is None
            and window.lease is None
            and window._entry_cancel is None
            and not window.role
        ),
        timeout=timeout,
        label="cancelled role admission cleanup",
    )
    sessions_dir = Path(window.root) / "session_locks" / "unified_access" / "sessions"
    metadata = [str(path) for path in sessions_dir.glob("*.json")] if sessions_dir.is_dir() else []
    return {
        "ok": not metadata,
        "mode": "early_admission_stress",
        "elapsed_sec": round(elapsed, 3),
        "entry_rejections": int(window._entry_rejections),
        "session_metadata_left": metadata,
    }


def _cancel_integrity_scan(
    app: Any,
    window: Any,
    heartbeat: _Heartbeat,
    timeout: float,
) -> dict[str, Any]:
    """Cancel once the frozen UI reports that its real integrity scan started."""
    import multiprocessing

    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtTest import QTest
    from rem_card.ui.shared.async_call import AsyncCallThread

    expected = "Проверка целостности базы…"
    observed: dict[str, Any] = {"message": "", "clicked": False, "click_enabled": False}
    watch = QTimer(window)
    watch.setInterval(5)

    def cancel_when_scanning() -> None:
        message = str(getattr(window.welcome, "_preparing_message", "") or "")
        if message != expected or observed["clicked"]:
            return
        observed["message"] = message
        observed["click_enabled"] = bool(window.welcome.cancel_entry_button.isEnabled())
        if observed["click_enabled"]:
            observed["clicked"] = True
            QTest.mouseClick(window.welcome.cancel_entry_button, Qt.LeftButton)

    heartbeat.set_phase("integrity_scan_cancel")
    watch.timeout.connect(cancel_when_scanning)
    watch.start()
    started = time.perf_counter()
    _click_role(window, "doctor")
    try:
        _wait_until(
            app,
            lambda: bool(observed["clicked"]),
            timeout=timeout,
            label="integrity scan cancellation click",
        )
        elapsed = _wait_until(
            app,
            lambda: (
                not window._busy
                and window.container is None
                and window.lease is None
                and window._entry_cancel is None
                and not window.role
                and not window._workers
                and not any(worker.isRunning() for worker in AsyncCallThread._keepalive_threads)
                and not multiprocessing.active_children()
            ),
            timeout=timeout,
            label="cancelled integrity scan cleanup",
        )
    finally:
        watch.stop()
        watch.deleteLater()

    sessions_dir = Path(window.root) / "session_locks" / "unified_access" / "sessions"
    metadata = [str(path) for path in sessions_dir.glob("*.json")] if sessions_dir.is_dir() else []
    active_children = [child.pid for child in multiprocessing.active_children()]
    return {
        "ok": observed["clicked"] and not metadata and not active_children,
        "mode": "frozen_integrity_scan",
        "expected_message": expected,
        "observed_message": observed["message"],
        "cancel_button_enabled": observed["click_enabled"],
        "clicked_once": observed["clicked"],
        "elapsed_sec": round(time.perf_counter() - started, 3),
        "cleanup_wait_sec": round(elapsed, 3),
        "session_metadata_left": metadata,
        "active_child_pids": active_children,
    }


def _replica_snapshot(window: Any) -> dict[str, Any]:
    snapshot = window.container.db_manager.get_sync_health_snapshot().get("replica", {})
    return {
        "enabled": bool(snapshot.get("enabled")),
        "ready": bool(snapshot.get("ready")),
        "last_sync_ok_ts": float(snapshot.get("last_sync_ok_ts") or 0.0),
        "last_sync_error": str(snapshot.get("last_sync_error") or ""),
        "local_db_path": str(snapshot.get("local_db_path") or ""),
    }


def _role_card_widget(window: Any, role: str) -> Any:
    if role == "doctor":
        return window.role_window.doctor_main.remcard_widget
    return window.role_window.nurse_main


def _wait_idle_prewarm(app: Any, window: Any, role: str, timeout: float) -> dict[str, Any]:
    from rem_card.app.local_metrics import record_metric

    widget = _role_card_widget(window, role)
    started = time.perf_counter()
    _wait_until(
        app,
        lambda: bool(
            getattr(widget, "_card_ui_prewarm_done", False)
            or getattr(widget, "_card_ui_prewarm_failed", False)
        ),
        timeout=timeout,
        label=f"{role} idle card UI prewarm",
    )
    prewarmer = getattr(widget, "_card_ui_prewarmer", None)
    elapsed = time.perf_counter() - started
    done = bool(
        getattr(widget, "_card_ui_prewarm_done", False)
        and prewarmer is not None
        and getattr(prewarmer, "done", False)
    )
    failed = bool(
        getattr(widget, "_card_ui_prewarm_failed", False)
        or (prewarmer is not None and getattr(prewarmer, "failed", False))
    )
    record_metric(
        "stage6_gui_idle_prewarm_wait",
        round(elapsed * 1000.0, 3),
        force_flush=True,
        role=role,
        done=int(done),
        failed=int(failed),
    )
    if not done or failed:
        error = getattr(prewarmer, "error", None) if prewarmer is not None else None
        raise RuntimeError(
            f"{role} idle card UI prewarm failed: "
            f"done={done} failed={failed} error={type(error).__name__ if error else ''}"
        )
    return {
        "ok": True,
        "elapsed_sec": round(elapsed, 3),
        "completed_steps": int(getattr(prewarmer, "completed_steps", 0)),
        "total_steps": int(getattr(prewarmer, "total_steps", 0)),
        "mode": str(getattr(prewarmer, "_mode", "")),
    }


def _open_card(app: Any, window: Any, role: str, admission_id: int, timeout: float) -> dict[str, Any]:
    started = time.perf_counter()
    widget = _role_card_widget(window, role)
    current = datetime.now()
    if role == "doctor":
        widget.load_patient_card(admission_id, current, request_snapshot=True)
        widget.layout_manager.set_patient_selection_mode("card")
        predicate = lambda: (
            int(getattr(widget, "admission_id", 0) or 0) == admission_id
            and str(getattr(widget, "_selection_mode", "")) == "card"
        )
        selected = lambda: int(getattr(widget, "admission_id", 0) or 0)
    else:
        widget.load_patient_card(admission_id, current)
        widget.layout_manager.set_patient_selection_mode("card")
        predicate = lambda: (
            int(getattr(widget.layout_manager, "current_admission_id", 0) or 0) == admission_id
            and str(getattr(widget, "_selection_mode", "")) == "card"
        )
        selected = lambda: int(getattr(widget.layout_manager, "current_admission_id", 0) or 0)
    _wait_until(app, predicate, timeout=timeout, label=f"{role} patient card")
    # Let queued hydration/UI work run under heartbeat observation.
    _wait_until(app, lambda: (time.perf_counter() - started) >= 1.0, timeout=3.0, label="card heartbeat window")
    return {
        "ok": selected() == admission_id,
        "admission_id": selected(),
        "elapsed_sec": round(time.perf_counter() - started, 3),
        "snapshot_loaded": bool(getattr(widget, "_card_snapshot_cache", None)),
        "widget_class": type(widget).__name__,
    }


def _exercise_role(
    app: Any,
    window: Any,
    heartbeat: _Heartbeat,
    role: str,
    admission_id: int,
    timeout: float,
    idle_prewarm: bool,
) -> dict[str, Any]:
    from rem_card.app.local_metrics import record_metric

    heartbeat.set_phase(f"{role}_entry")
    chooser_wait_sec = _wait_until(
        app,
        lambda: (
            window.stack.currentWidget() is window.welcome
            and not window._transition.running
            and window.welcome.role_buttons[role].isEnabled()
            and not window._workers
        ),
        timeout=timeout,
        label=f"{role} chooser ready",
    )
    entry_started = time.perf_counter()
    _click_role(window, role)
    _wait_until(app, lambda: _entry_ready(window, role), timeout=timeout, label=f"{role} role entry")
    entry_sec = time.perf_counter() - entry_started
    heartbeat.set_phase(f"{role}_replica")
    _wait_until(
        app,
        lambda: _replica_snapshot(window).get("ready", False),
        timeout=min(timeout, 45.0),
        label=f"{role} local replica",
    )
    replica = _replica_snapshot(window)
    if not _is_within(replica["local_db_path"], Path(os.environ["LOCALAPPDATA"])):
        raise RuntimeError(f"{role} replica escaped LOCALAPPDATA: {replica['local_db_path']}")

    prewarm = None
    if idle_prewarm:
        heartbeat.set_phase(f"{role}_idle_prewarm")
        prewarm = _wait_idle_prewarm(app, window, role, timeout)

    heartbeat.set_phase(f"{role}_card")
    card = _open_card(app, window, role, admission_id, timeout)
    record_metric(
        "stage6_gui_role_acceptance",
        round(entry_sec * 1000.0, 3),
        force_flush=True,
        role=role,
        card_open=int(card["ok"]),
        replica_ready=int(replica["ready"]),
    )

    heartbeat.set_phase(f"{role}_exit")
    exit_started = time.perf_counter()
    window.request_role_exit(force=True)
    _wait_until(
        app,
        lambda: (
            window.container is None
            and window.lease is None
            and window.role_window is None
            and not window.role
            and not window._busy
            and not window._leaving
            and window.stack.currentWidget() is window.welcome
            and not window._transition.running
        ),
        timeout=timeout,
        label=f"{role} role shutdown",
    )
    return {
        "ok": card["ok"] and replica["ready"],
        "role": role,
        "chooser_wait_sec": round(chooser_wait_sec, 3),
        "entry_sec": round(entry_sec, 3),
        "exit_sec": round(time.perf_counter() - exit_started, 3),
        "idle_prewarm": prewarm,
        "card": card,
        "replica": replica,
    }


def run_acceptance(args: argparse.Namespace) -> dict[str, Any]:
    run_root = Path(args.run_root)
    safety = _validate_synthetic_run(run_root)
    local_root = Path(args.local_state).resolve()
    local_root.mkdir(parents=True, exist_ok=True)

    frozen = bool(getattr(sys, "frozen", False))
    if not frozen:
        env = build_source_environment(run_root, local_root, platform=args.platform)
        apply_source_environment(env)
    isolation = validate_preconfigured_environment(
        run_root,
        local_root,
        require_config=frozen,
    )

    from PySide6.QtWidgets import QApplication
    from rem_card.app.local_metrics import flush_metrics, record_metric
    from rem_card.ui.unified_window import UnifiedWindow

    app = QApplication.instance() or QApplication(["RemCard Stage6 GUI acceptance"])
    app.setQuitOnLastWindowClosed(False)
    window = UnifiedWindow()
    qt_settings_file = Path(window.settings.fileName()).resolve()
    if not _is_within(qt_settings_file, local_root):
        raise RuntimeError(f"QSettings escaped local state root: {qt_settings_file}")
    isolation["qt_settings_file"] = str(qt_settings_file)
    window._check_updates = lambda: None
    window._exit_update_checked = True
    window.show()
    heartbeat_path = local_root / "State" / "stage6_gui_heartbeat.json"
    heartbeat = _Heartbeat(app, window, heartbeat_path)
    heartbeat.start()
    roles: list[dict[str, Any]] = []
    cancel_result: dict[str, Any] = {}
    started = time.perf_counter()
    try:
        record_metric("stage6_gui_acceptance", 1, force_flush=True, phase="begin")
        window.initialize()
        heartbeat.set_phase("chooser_initialization")
        chooser_sec = _wait_until(
            app,
            lambda: (
                window.root
                and window.stack.currentWidget() is window.welcome
                and not window._busy
                and not window._workers
                and not window._transition.running
                and window.welcome.role_buttons["doctor"].isEnabled()
            ),
            timeout=args.timeout,
            label="unified role chooser",
        )
        if _normalized(window.root) != _normalized(run_root):
            raise RuntimeError(f"UnifiedWindow resolved unexpected root: {window.root}")

        cancel_result = (
            _cancel_integrity_scan(app, window, heartbeat, args.timeout)
            if frozen
            else _cancel_stress(app, window, heartbeat, args.timeout)
        )
        if not cancel_result["ok"]:
            raise RuntimeError(f"cancel stress left session metadata: {cancel_result}")
        for role in args.roles:
            roles.append(
                _exercise_role(
                    app,
                    window,
                    heartbeat,
                    role,
                    args.admission_id,
                    args.timeout,
                    bool(getattr(args, "idle_prewarm", False)),
                )
            )
        ok = all(item.get("ok") for item in roles) and not heartbeat.modals
        record_metric("stage6_gui_acceptance", int(ok), force_flush=True, phase="end")
        return {
            "schema_version": 1,
            "ok": ok,
            "frozen": frozen,
            "pid": os.getpid(),
            "run_root": str(run_root),
            "local_state_root": str(local_root),
            "admission_id": args.admission_id,
            "safety": safety,
            "isolation": isolation,
            "chooser_sec": round(chooser_sec, 3),
            "cancel_stress": cancel_result,
            "roles": roles,
            "heartbeat": heartbeat.summary(),
            "elapsed_sec": round(time.perf_counter() - started, 3),
        }
    finally:
        heartbeat.stop()
        try:
            if window.container is not None:
                window.request_role_exit(force=True)
                _wait_until(
                    app,
                    lambda: window.container is None and window.lease is None,
                    timeout=min(args.timeout, 30.0),
                    label="final runtime cleanup",
                )
        finally:
            window._exit_update_checked = True
            window.close()
            app.processEvents()
            flush_metrics(timeout=2.0)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live Stage 6.5 UnifiedWindow acceptance")
    parser.add_argument("--stage6-acceptance", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--local-state")
    parser.add_argument("--admission-id", type=int, default=1)
    parser.add_argument("--roles", default="doctor,nurse")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--platform", choices=("windows", "offscreen"), default="windows")
    parser.add_argument(
        "--idle-prewarm",
        action="store_true",
        help="wait for each role's staged card UI prewarm before opening the patient",
    )
    args = parser.parse_args(argv)
    args.roles = [role.strip() for role in args.roles.split(",") if role.strip()]
    if not args.roles or any(role not in {"doctor", "nurse"} for role in args.roles):
        parser.error("--roles accepts only doctor,nurse")
    if args.admission_id <= 0:
        parser.error("--admission-id must be positive")
    if not args.local_state:
        args.local_state = str(
            Path(tempfile.gettempdir()) / f"remcard-stage6-gui-{uuid.uuid4().hex}"
        )
    return args


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    args = _parse_args(argv)
    started_at = datetime.now(timezone.utc)
    report: dict[str, Any]
    try:
        report = run_acceptance(args)
        code = 0 if report.get("ok") else 1
    except BaseException as exc:
        report = {
            "schema_version": 1,
            "ok": False,
            "frozen": bool(getattr(sys, "frozen", False)),
            "pid": os.getpid(),
            "run_root": args.run_root,
            "local_state_root": args.local_state,
            "error": f"{type(exc).__name__}: {exc}",
        }
        code = 2
    report["startup_metrics"] = _collect_startup_metrics(Path(args.local_state))
    report["started_at"] = started_at.isoformat()
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["idle_prewarm_requested"] = bool(args.idle_prewarm)
    report["prewarm_metrics"] = _collect_prewarm_metrics(Path(args.local_state))
    stamp = started_at.strftime("%Y%m%dT%H%M%SZ")
    local_evidence = Path(args.local_state) / "State" / f"stage6_gui_acceptance_{stamp}_{os.getpid()}.json"
    report["local_evidence_path"] = str(local_evidence)
    _atomic_json(local_evidence, report)
    try:
        run_root = Path(args.run_root)
        if _normalized(run_root.parent) == _normalized(AUTHORIZED_ROOT):
            smb_evidence = run_root / "evidence" / local_evidence.name
            report["evidence_path"] = str(smb_evidence)
            _atomic_json(smb_evidence, report)
            _atomic_json(local_evidence, report)
    except Exception as exc:
        report["evidence_write_error"] = f"{type(exc).__name__}: {exc}"
        _atomic_json(local_evidence, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
