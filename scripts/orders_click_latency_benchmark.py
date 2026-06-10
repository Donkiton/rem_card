#!/usr/bin/env python
"""
Benchmark for doctor orders sheet click latency.

Measures:
1) click -> model.dataChanged for target cell (UI update intent)
2) click -> DB commit (new latest administration row for that cell)

Usage:
  set PYTHONPATH=C:\Project
  python %REMCARD_PROJECT_ROOT%\scripts\orders_click_latency_benchmark.py --clicks 40
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import statistics
import sys
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication

PROJECT_ROOT = Path(__file__).resolve().parents[1]
try:
    from _local_rem_card_bootstrap import bootstrap_local_rem_card

    bootstrap_local_rem_card()
except Exception:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

from rem_card.app.bootstrap import bootstrap
from rem_card.data.dto.remcard_dto import OrderDTO, OrderStatus, OrderType
from rem_card.ui.doctor_view.doctor_main_widget import DoctorMainWidget


def _configure_benchmark_env():
    # Бенчмарк должен измерять клики/коммиты, а не фоновые реплики/аутбокс.
    os.environ.setdefault("REMCARD_LOCAL_OUTBOX_SYNC", "0")
    os.environ.setdefault("REMCARD_LOCAL_FIRST_SYNC", "0")


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    arr = sorted(values)
    k = (len(arr) - 1) * p
    f = int(k)
    c = min(f + 1, len(arr) - 1)
    if f == c:
        return arr[f]
    return arr[f] + (arr[c] - arr[f]) * (k - f)


def _wait_for(predicate, app: QApplication, timeout_sec: float = 2.0) -> float | None:
    started = time.perf_counter()
    while (time.perf_counter() - started) < timeout_sec:
        app.processEvents()
        if predicate():
            return (time.perf_counter() - started) * 1000.0
        time.sleep(0.002)
    return None


@dataclass
class Samples:
    ui: list[float]
    db: list[float]


class DataChangedProbe(QObject):
    def __init__(self, model):
        super().__init__()
        self._model = model
        self._armed_at: float | None = None
        self._target_row = -1
        self._target_col = -1
        self.first_change_ms: float | None = None
        self._model.dataChanged.connect(self._on_data_changed)

    def arm(self, row: int, col: int):
        self._armed_at = time.perf_counter()
        self._target_row = row
        self._target_col = col
        self.first_change_ms = None

    def _on_data_changed(self, top_left, bottom_right, _roles):
        if self._armed_at is None or self.first_change_ms is not None:
            return
        if top_left.row() <= self._target_row <= bottom_right.row() and top_left.column() <= self._target_col <= bottom_right.column():
            self.first_change_ms = (time.perf_counter() - self._armed_at) * 1000.0


def _prepare_target_order(container, clicks: int):
    app = QApplication.instance() or QApplication(sys.argv)
    adm_row = container.db_manager.fetch_one_remcard("SELECT id FROM admissions ORDER BY id DESC LIMIT 1")
    if not adm_row:
        raise RuntimeError("No admissions found in unified DB")
    admission_id = int(adm_row[0])

    doctor = DoctorMainWidget(container.patient_service, container.remcard_service)
    doctor.remcard_widget.load_patient_card(admission_id, datetime.now())
    layout = doctor.remcard_widget.layout_manager
    if hasattr(layout, "set_active_tab"):
        layout.set_active_tab("Назначения", source="benchmark")
    if hasattr(layout, "sector_2b") and hasattr(layout.sector_2b, "select_tab"):
        layout.sector_2b.select_tab("Назначения", emit=False)
    doctor.remcard_widget.on_tab_changed("Назначения")
    app.processEvents()

    ow = layout.orders_widget
    if ow.model is None:
        raise RuntimeError("Orders model is not initialized")

    created_drug_key = f"bench_click_{uuid.uuid4().hex[:8]}"
    dto = OrderDTO(
        admission_id=admission_id,
        drug_key=created_drug_key,
        latin="Bench Drug",
        type=OrderType.MEDICATION,
        status=OrderStatus.ACTIVE,
        dose_value=1.0,
        dose_unit="mg",
        frequency=1,
        specific_times=[],
        duration_min=0,
        is_committed=0,
        created_at=datetime.now(),
        comment="",
    )
    container.remcard_service.add_order(dto)
    ow._refresh_model()
    appeared_ms = _wait_for(
        lambda: any(getattr(o, "drug_key", None) == created_drug_key for o in (ow.model.orders if ow.model else [])),
        app,
        timeout_sec=2.0,
    )
    if appeared_ms is None:
        raise RuntimeError(
            f"Target order was not visible in model within timeout (drug_key={created_drug_key}, model_rows={len(ow.model.orders)})"
        )

    row_idx = next((i for i, o in enumerate(ow.model.orders) if getattr(o, "drug_key", None) == created_drug_key), None)
    if row_idx is None:
        raise RuntimeError(
            f"Target order lookup failed after visibility wait (drug_key={created_drug_key}, model_rows={len(ow.model.orders)})"
        )
    col_idx = 1
    idx = ow.model.index(row_idx, col_idx)
    if not idx.isValid():
        raise RuntimeError("Target index is invalid")

    order = ow.model.orders[row_idx]
    planned_iso = ow.model.time_slots[col_idx - 1].isoformat()

    return {
        "doctor": doctor,
        "orders_widget": ow,
        "admission_id": admission_id,
        "order_id": int(order.id),
        "row_idx": row_idx,
        "col_idx": col_idx,
        "index": idx,
        "planned_iso": planned_iso,
        "created_drug_key": created_drug_key,
        "clicks": clicks,
    }


def run_benchmark(clicks: int, max_runtime_sec: float = 90.0) -> dict:
    app = QApplication.instance() or QApplication(sys.argv)
    _configure_benchmark_env()
    container = None
    prepared = None
    started_at = time.perf_counter()

    try:
        container = bootstrap()
        prepared = _prepare_target_order(container, clicks)
        ow = prepared["orders_widget"]
        idx = prepared["index"]
        row_idx = prepared["row_idx"]
        col_idx = prepared["col_idx"]
        order_id = prepared["order_id"]
        planned_iso = prepared["planned_iso"]
        cell_key = ow._admin_cell_write_key(order_id, datetime.fromisoformat(planned_iso))

        probe = DataChangedProbe(ow.model)
        samples = Samples(ui=[], db=[])
        repeat_guard_waits: list[float] = []

        for _ in range(clicks):
            if (time.perf_counter() - started_at) > max_runtime_sec:
                raise TimeoutError(f"Benchmark exceeded max runtime ({max_runtime_sec}s)")
            app.processEvents()
            if cell_key is not None:
                guard_waited = _wait_for(
                    lambda: not ow._skip_reason_for_admin_cell_click(cell_key),
                    app,
                    timeout_sec=2.0,
                )
                if guard_waited is None:
                    raise TimeoutError("Orders cell repeat guard did not release before benchmark click")
                repeat_guard_waits.append(guard_waited)

            prev_row = container.db_manager.fetch_one_remcard(
                "SELECT id FROM administrations WHERE order_id = ? AND planned_time = ? ORDER BY id DESC LIMIT 1",
                (order_id, planned_iso),
            )
            prev_admin_id = int(prev_row["id"]) if prev_row else 0

            probe.arm(row_idx, col_idx)
            ow.on_cell_clicked(idx)

            ui_waited = _wait_for(lambda: probe.first_change_ms is not None, app, timeout_sec=1.0)
            if ui_waited is not None and probe.first_change_ms is not None:
                samples.ui.append(probe.first_change_ms)

            def _cell_db_state_changed() -> bool:
                row = container.db_manager.fetch_one_remcard(
                    "SELECT id FROM administrations WHERE order_id = ? AND planned_time = ? ORDER BY id DESC LIMIT 1",
                    (order_id, planned_iso),
                )
                if row is None:
                    return prev_admin_id != 0
                return int(row["id"]) != prev_admin_id

            db_waited = _wait_for(_cell_db_state_changed, app, timeout_sec=3.0)
            if db_waited is not None:
                samples.db.append(db_waited)

        result = {
            "admission_id": prepared["admission_id"],
            "order_id": order_id,
            "clicks": clicks,
            "ui_change_samples": len(samples.ui),
            "db_commit_samples": len(samples.db),
            "ui_change_ms_avg": round(statistics.mean(samples.ui), 3) if samples.ui else None,
            "ui_change_ms_p95": round(_percentile(samples.ui, 0.95), 3) if samples.ui else None,
            "ui_change_ms_min": round(min(samples.ui), 3) if samples.ui else None,
            "ui_change_ms_max": round(max(samples.ui), 3) if samples.ui else None,
            "db_commit_ms_avg": round(statistics.mean(samples.db), 3) if samples.db else None,
            "db_commit_ms_p95": round(_percentile(samples.db, 0.95), 3) if samples.db else None,
            "db_commit_ms_min": round(min(samples.db), 3) if samples.db else None,
            "db_commit_ms_max": round(max(samples.db), 3) if samples.db else None,
            "repeat_guard_wait_ms_avg": round(statistics.mean(repeat_guard_waits), 3) if repeat_guard_waits else None,
            "repeat_guard_wait_ms_max": round(max(repeat_guard_waits), 3) if repeat_guard_waits else None,
        }
        return result
    finally:
        try:
            if prepared and container and prepared.get("created_drug_key"):
                with container.db_manager.remcard_transaction(source="cleanup_orders_click_latency_benchmark") as cursor:
                    cursor.execute(
                        "DELETE FROM administrations WHERE order_id IN (SELECT id FROM orders WHERE drug_key = ?)",
                        (prepared["created_drug_key"],),
                    )
                    cursor.execute("DELETE FROM orders WHERE drug_key = ?", (prepared["created_drug_key"],))
        except Exception:
            pass
        try:
            if container:
                container.data_service.shutdown()
        except Exception:
            pass
        try:
            if container:
                container.db_manager.close()
        except Exception:
            pass
        try:
            if prepared and prepared.get("doctor"):
                prepared["doctor"].close()
                app.processEvents()
        except Exception:
            pass


def _benchmark_worker(queue_: mp.Queue, worker_clicks: int, worker_runtime_sec: float):
    try:
        queue_.put(
            {
                "ok": True,
                "payload": run_benchmark(clicks=worker_clicks, max_runtime_sec=worker_runtime_sec),
            }
        )
    except Exception as exc:  # noqa: BLE001
        queue_.put(
            {
                "ok": False,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
        )


def main():
    parser = argparse.ArgumentParser(description="Doctor orders click latency benchmark")
    parser.add_argument("--clicks", type=int, default=30, help="Number of click iterations")
    parser.add_argument("--max-runtime-s", type=float, default=90.0, help="Global benchmark timeout")
    parser.add_argument(
        "--hard-timeout-s",
        type=float,
        default=120.0,
        help="Parent process timeout. Child benchmark process will be terminated if exceeded.",
    )
    args = parser.parse_args()

    clicks = max(1, args.clicks)
    max_runtime_sec = max(5.0, float(args.max_runtime_s))
    hard_timeout_sec = max(max_runtime_sec + 5.0, float(args.hard_timeout_s))
    out_queue: mp.Queue = mp.Queue()

    proc = mp.Process(
        target=_benchmark_worker,
        kwargs={
            "queue_": out_queue,
            "worker_clicks": clicks,
            "worker_runtime_sec": max_runtime_sec,
        },
        daemon=False,
    )
    proc.start()
    proc.join(timeout=hard_timeout_sec)

    if proc.is_alive():
        proc.terminate()
        proc.join(timeout=2.0)
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": f"Benchmark hard-timeout exceeded ({hard_timeout_sec}s). Child process terminated.",
                    "clicks": clicks,
                    "max_runtime_s": max_runtime_sec,
                    "hard_timeout_s": hard_timeout_sec,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise SystemExit(2)

    if out_queue.empty():
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "Benchmark child exited without payload",
                    "child_exit_code": proc.exitcode,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise SystemExit(3)

    msg = out_queue.get()
    if msg.get("ok"):
        print(json.dumps(msg["payload"], ensure_ascii=False, indent=2))
        return

    print(
        json.dumps(
            {
                "ok": False,
                "error": msg.get("error"),
                "traceback": msg.get("traceback"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    raise SystemExit(1)


if __name__ == "__main__":
    mp.freeze_support()
    main()
