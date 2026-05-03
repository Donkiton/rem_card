#!/usr/bin/env python
"""
Multi-client stress test for shared SQLite workflow.

Scenario:
- Writer process simulates doctor mutations on orders/administrations.
- Main process simulates nurse-side polling and visibility checks.
- Reports propagation latency from writer commit to reader-visible state.

Usage:
  set PYTHONPATH=C:\Project
  python C:\Project\rem_card\scripts\multi_client_stress.py --operations 200
"""

from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing as mp
import queue
import random
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
try:
    from _local_rem_card_bootstrap import bootstrap_local_rem_card

    bootstrap_local_rem_card()
except Exception:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))


def _import_runtime():
    # Delayed imports to keep spawn-friendly behavior on Windows.
    from rem_card.app.bootstrap import bootstrap
    from rem_card.data.dto.remcard_dto import AdministrationDTO, OrderDTO, OrderStatus, OrderType

    return bootstrap, AdministrationDTO, OrderDTO, OrderStatus, OrderType


def _json_iso(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _time_slots_for_shift(start: datetime, end: datetime) -> List[datetime]:
    slots: List[datetime] = []
    current = start
    while current < end:
        slots.append(current)
        current += timedelta(hours=1)
    return slots


def _fingerprint_reader_state(service, admission_id: int, shift_date: datetime) -> Tuple[str, int, int]:
    orders = service.get_orders(admission_id, shift_date, only_committed=True)
    order_payload = sorted(
        (
            int(order.id),
            str(getattr(order.status, "value", order.status)),
            int(getattr(order, "is_committed", 0)),
            str(getattr(order, "latin", "") or ""),
        )
        for order in orders
        if order and order.id is not None
    )

    order_ids = [order.id for order in orders if order and order.id is not None]
    start_dt, end_dt = service.get_day_period(shift_date)
    admin_rows = service.get_latest_administrations_for_order_ids(
        order_ids=order_ids,
        start_dt=start_dt,
        end_dt=end_dt,
        only_committed=True,
        include_deleted=False,
        include_cancelled=False,
        include_deleted_orders=False,
    )
    admin_payload = []
    for row in admin_rows:
        rd = dict(row)
        admin_payload.append(
            (
                int(rd["order_id"]),
                datetime.fromisoformat(str(rd["planned_time"]).replace(" ", "T")).isoformat(),
                str(rd.get("cell_role") or ""),
                str(rd.get("status") or ""),
                str(rd.get("comment") or ""),
            )
        )
    admin_payload.sort()

    blob = json.dumps({"orders": order_payload, "admins": admin_payload}, ensure_ascii=True, separators=(",", ":"))
    digest = hashlib.sha1(blob.encode("utf-8")).hexdigest()
    return digest, len(order_payload), len(admin_payload)


def _row_to_admin_dto(row, administration_dto_cls):
    row = dict(row)
    return administration_dto_cls(
        id=row["id"],
        order_id=row["order_id"],
        chain_id=row.get("chain_id"),
        big_chain_id=row.get("big_chain_id"),
        cell_role=row["cell_role"],
        planned_time=datetime.fromisoformat(str(row["planned_time"]).replace(" ", "T")),
        actual_time=datetime.fromisoformat(str(row["actual_time"]).replace(" ", "T")) if row.get("actual_time") else None,
        performer_id=row.get("performer_id"),
        status=row["status"],
        is_committed=row.get("is_committed", 0),
        comment=row.get("comment") or "",
        volume_ml=float(row.get("volume_ml") or 0.0),
        updated_at=row.get("updated_at"),
        last_modified_by=row.get("last_modified_by"),
    )


def _ensure_seed_order(service, admission_id: int, shift_date: datetime, rnd: random.Random, order_dto_cls, order_type_enum, order_status_enum):
    existing = service.get_orders(admission_id, shift_date, only_committed=False)
    if existing:
        return

    shift_start, shift_end = service.get_day_period(shift_date)
    now = datetime.now()
    created_at = now if shift_start <= now < shift_end else shift_start
    seed_order = order_dto_cls(
        admission_id=admission_id,
        drug_key="stress_seed",
        latin="Stress Seed",
        type=order_type_enum.MEDICATION,
        status=order_status_enum.ACTIVE,
        dose_value=1.0,
        dose_unit="mg",
        is_per_kg=False,
        frequency=1,
        specific_times=[],
        duration_min=0,
        is_committed=0,
        created_at=created_at,
        comment="",
        last_modified_by="stress_writer",
    )
    service.add_order(seed_order)
    slots = _time_slots_for_shift(shift_start, shift_end)
    planned_time = rnd.choice(slots)
    service.apply_order_left_click(seed_order, None, planned_time)
    service.finalize_order_card(admission_id)


def _writer_mutation(
    service,
    admission_id: int,
    shift_date: datetime,
    rnd: random.Random,
    administration_dto_cls,
    order_dto_cls,
    order_type_enum,
    order_status_enum,
) -> str:
    shift_start, shift_end = service.get_day_period(shift_date)
    slots = _time_slots_for_shift(shift_start, shift_end)

    doctor_orders = service.get_orders(admission_id, shift_date, only_committed=False)
    committed_orders = service.get_orders(admission_id, shift_date, only_committed=True)
    if not doctor_orders:
        _ensure_seed_order(service, admission_id, shift_date, rnd, order_dto_cls, order_type_enum, order_status_enum)
        return "seed_order"

    action = rnd.choices(
        population=["toggle_cell", "add_order", "delete_order"],
        weights=[0.55, 0.25, 0.20],
        k=1,
    )[0]

    if action == "add_order":
        now = datetime.now()
        created_at = now if shift_start <= now < shift_end else shift_start
        order_idx = rnd.randint(1000, 9999)
        new_order = order_dto_cls(
            admission_id=admission_id,
            drug_key=f"stress_{order_idx}",
            latin=f"Stress Drug {order_idx}",
            type=order_type_enum.MEDICATION,
            status=order_status_enum.ACTIVE,
            dose_value=1.0,
            dose_unit="mg",
            is_per_kg=False,
            frequency=1,
            specific_times=[],
            duration_min=0,
            is_committed=0,
            created_at=created_at,
            comment="",
            last_modified_by="stress_writer",
        )
        service.add_order(new_order)
        service.apply_order_left_click(new_order, None, rnd.choice(slots))
        service.finalize_order_card(admission_id)
        return "add_order"

    if action == "delete_order":
        if not committed_orders:
            action = "toggle_cell"
        else:
            target = rnd.choice(committed_orders)
            service.soft_delete_order_row(target.id, True)
            service.finalize_order_card(admission_id)
            return "delete_order"

    # toggle_cell
    if not doctor_orders:
        return "noop"
    target_order = rnd.choice(doctor_orders)
    target_time = rnd.choice(slots)

    latest_rows = service.get_latest_administrations(
        admission_id=admission_id,
        shift_date=shift_date,
        only_committed=False,
        include_deleted=True,
        include_cancelled=True,
        include_deleted_orders=True,
    )
    selected_row = None
    target_iso = target_time.isoformat()
    for row in latest_rows:
        rd = dict(row)
        row_time = datetime.fromisoformat(str(rd["planned_time"]).replace(" ", "T")).isoformat()
        if rd["order_id"] == target_order.id and row_time == target_iso:
            selected_row = row
            break

    admin_dto = _row_to_admin_dto(selected_row, administration_dto_cls) if selected_row else None
    service.apply_order_left_click(target_order, admin_dto, target_time)
    service.finalize_order_card(admission_id)
    return "toggle_cell"


def _writer_worker(
    admission_id: int,
    shift_iso: str,
    operations: int,
    interval_ms: int,
    seed: int,
    exact_fingerprint: bool,
    out_queue: mp.Queue,
):
    bootstrap, administration_dto_cls, order_dto_cls, order_status_enum, order_type_enum = None, None, None, None, None
    container = None
    writer_errors = 0
    writer_durations: List[float] = []
    op_counter = 0
    try:
        bootstrap, administration_dto_cls, order_dto_cls, order_status_enum, order_type_enum = _import_runtime()
        container = bootstrap()
        service = container.remcard_service
        shift_date = datetime.fromisoformat(shift_iso)
        rnd = random.Random(seed)

        for seq in range(1, operations + 1):
            start_ts = time.perf_counter()
            wall_ts = time.time()
            try:
                operation = _writer_mutation(
                    service=service,
                    admission_id=admission_id,
                    shift_date=shift_date,
                    rnd=rnd,
                    administration_dto_cls=administration_dto_cls,
                    order_dto_cls=order_dto_cls,
                    order_type_enum=order_type_enum,
                    order_status_enum=order_status_enum,
                )
                change_id = service.get_latest_change_id(admission_id)
                if exact_fingerprint:
                    fingerprint, order_count, admin_count = _fingerprint_reader_state(service, admission_id, shift_date)
                else:
                    fingerprint, order_count, admin_count = "", 0, 0
                elapsed_ms = (time.perf_counter() - start_ts) * 1000.0
                writer_durations.append(elapsed_ms)
                op_counter += 1
                out_queue.put(
                    {
                        "kind": "writer_event",
                        "seq": seq,
                        "operation": operation,
                        "change_id": int(change_id),
                        "fingerprint": fingerprint,
                        "orders": order_count,
                        "admins": admin_count,
                        "writer_wall_ts": wall_ts,
                        "writer_elapsed_ms": elapsed_ms,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                writer_errors += 1
                out_queue.put(
                    {
                        "kind": "writer_error",
                        "seq": seq,
                        "error": str(exc),
                    }
                )
            time.sleep(max(interval_ms, 0) / 1000.0)

        out_queue.put(
            {
                "kind": "writer_done",
                "operations_ok": op_counter,
                "operations_failed": writer_errors,
                "writer_avg_ms": statistics.fmean(writer_durations) if writer_durations else 0.0,
                "writer_p95_ms": statistics.quantiles(writer_durations, n=20)[18] if len(writer_durations) >= 20 else (max(writer_durations) if writer_durations else 0.0),
            }
        )
    finally:
        if container is not None:
            try:
                container.data_service.shutdown()
            except Exception:  # noqa: BLE001
                pass
            try:
                container.db_manager.close()
            except Exception:  # noqa: BLE001
                pass


@dataclass
class PendingEvent:
    seq: int
    operation: str
    change_id: int
    fingerprint: str
    writer_wall_ts: float
    added_monotonic: float


def _pick_admission_id(service, explicit_id: Optional[int], shift_date: datetime) -> int:
    if explicit_id is not None:
        return explicit_id
    active = service.get_active_patients()
    if not active:
        raise RuntimeError("No active admissions found for stress test")
    # Для стресса выбираем наиболее "лёгкую" активную госпитализацию,
    # чтобы benchmark не зависал на fingerprint/readback больших историй.
    ranked: List[Tuple[int, int]] = []
    for patient in active:
        admission_id = int(patient.id)
        try:
            orders = service.get_orders(admission_id, shift_date, only_committed=False)
            ranked.append((len(orders), admission_id))
        except Exception:  # noqa: BLE001
            ranked.append((10**9, admission_id))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return ranked[0][1]


def _percentile(values: Sequence[float], p: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    sorted_values = sorted(values)
    rank = (len(sorted_values) - 1) * p
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    if lower == upper:
        return float(sorted_values[lower])
    frac = rank - lower
    return float(sorted_values[lower] * (1.0 - frac) + sorted_values[upper] * frac)


def main():
    parser = argparse.ArgumentParser(description="Shared SQLite multi-client stress test")
    parser.add_argument("--admission-id", type=int, default=None, help="Admission id to use (default: first active)")
    parser.add_argument("--operations", type=int, default=120, help="Writer operations count")
    parser.add_argument("--writer-interval-ms", type=int, default=120, help="Delay between writer operations")
    parser.add_argument("--reader-poll-ms", type=int, default=150, help="Reader poll interval")
    parser.add_argument("--sync-timeout-s", type=float, default=5.0, help="Max wait for each event visibility")
    parser.add_argument("--max-runtime-s", type=float, default=180.0, help="Global timeout")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument(
        "--exact-fingerprint",
        action="store_true",
        help="Enable expensive full-state fingerprint checks (slower on large admissions)",
    )
    args = parser.parse_args()

    bootstrap, _, _, _, _ = _import_runtime()
    container = bootstrap()
    service = container.remcard_service
    shift_date = datetime.now()
    admission_id = _pick_admission_id(service, args.admission_id, shift_date)
    need_exact_fingerprint = bool(args.exact_fingerprint)

    event_queue: mp.Queue = mp.Queue()
    writer_proc = mp.Process(
        target=_writer_worker,
        kwargs={
            "admission_id": admission_id,
            "shift_iso": shift_date.isoformat(),
            "operations": args.operations,
            "interval_ms": args.writer_interval_ms,
            "seed": args.seed,
            "exact_fingerprint": need_exact_fingerprint,
            "out_queue": event_queue,
        },
        daemon=False,
    )
    writer_proc.start()

    pending: List[PendingEvent] = []
    latencies_ms: List[float] = []
    latencies_exact_ms: List[float] = []
    superseded_events: List[Dict] = []
    lagged_events: List[Dict] = []
    writer_errors: List[Dict] = []
    writer_summary: Dict = {}
    writer_done = False
    writer_exit_code: Optional[int] = None

    start_mono = time.monotonic()
    end_deadline = start_mono + args.max_runtime_s
    reader_change_id = service.get_latest_change_id(admission_id)
    reader_fp = ""
    if need_exact_fingerprint:
        reader_fp, _, _ = _fingerprint_reader_state(service, admission_id, shift_date)

    try:
        while time.monotonic() < end_deadline:
            # Drain writer messages.
            while True:
                try:
                    msg = event_queue.get_nowait()
                except queue.Empty:
                    break

                kind = msg.get("kind")
                if kind == "writer_event":
                    pending.append(
                        PendingEvent(
                            seq=int(msg["seq"]),
                            operation=str(msg["operation"]),
                            change_id=int(msg["change_id"]),
                            fingerprint=str(msg["fingerprint"]),
                            writer_wall_ts=float(msg["writer_wall_ts"]),
                            added_monotonic=time.monotonic(),
                        )
                    )
                elif kind == "writer_error":
                    writer_errors.append(msg)
                elif kind == "writer_done":
                    writer_summary = msg
                    writer_done = True

            if not writer_done and not writer_proc.is_alive():
                writer_exit_code = writer_proc.exitcode
                writer_done = True
                if writer_exit_code not in (0, None):
                    writer_errors.append(
                        {
                            "kind": "writer_exit",
                            "error": f"Writer process exited unexpectedly with code {writer_exit_code}",
                        }
                    )
                else:
                    writer_errors.append(
                        {
                            "kind": "writer_no_done",
                            "error": "Writer process exited without writer_done message",
                        }
                    )

            # Reader poll.
            current_change = service.get_latest_change_id(admission_id)
            if current_change != reader_change_id or pending:
                reader_change_id = current_change
                if need_exact_fingerprint and any(event.fingerprint for event in pending):
                    reader_fp, _, _ = _fingerprint_reader_state(service, admission_id, shift_date)

            now_wall = time.time()
            now_mono = time.monotonic()
            remaining: List[PendingEvent] = []
            for event in pending:
                if reader_change_id >= event.change_id:
                    latency = (now_wall - event.writer_wall_ts) * 1000.0
                    latencies_ms.append(max(latency, 0.0))
                    if event.fingerprint:
                        if reader_fp == event.fingerprint:
                            latencies_exact_ms.append(max(latency, 0.0))
                        else:
                            superseded_events.append(
                                {
                                    "seq": event.seq,
                                    "operation": event.operation,
                                    "change_id": event.change_id,
                                    "reader_change_id": reader_change_id,
                                }
                            )
                    elif need_exact_fingerprint:
                        superseded_events.append(
                            {
                                "seq": event.seq,
                                "operation": event.operation,
                                "change_id": event.change_id,
                                "reader_change_id": reader_change_id,
                            }
                        )
                    continue
                if now_mono - event.added_monotonic >= args.sync_timeout_s:
                    lagged_events.append(
                        {
                            "seq": event.seq,
                            "operation": event.operation,
                            "change_id": event.change_id,
                            "reader_change_id": reader_change_id,
                        }
                    )
                    continue
                remaining.append(event)
            pending = remaining

            if writer_done and not pending:
                break

            time.sleep(max(args.reader_poll_ms, 20) / 1000.0)

        writer_proc.join(timeout=2.0)
        if writer_proc.is_alive():
            writer_proc.terminate()
            writer_proc.join(timeout=2.0)
            writer_errors.append(
                {
                    "kind": "writer_timeout",
                    "error": "Writer process did not finish before global timeout and was terminated",
                }
            )
            writer_exit_code = writer_proc.exitcode
        elif writer_exit_code is None:
            writer_exit_code = writer_proc.exitcode

        total_written = int(writer_summary.get("operations_ok", 0)) + int(writer_summary.get("operations_failed", 0))
        total_seen = len(latencies_ms)
        total_seen_exact = len(latencies_exact_ms)
        success_rate = (total_seen / total_written * 100.0) if total_written else 0.0
        success_rate_exact = (total_seen_exact / total_written * 100.0) if total_written else 0.0
        summary = {
            "admission_id": admission_id,
            "operations_requested": args.operations,
            "operations_written_ok": int(writer_summary.get("operations_ok", 0)),
            "operations_writer_failed": int(writer_summary.get("operations_failed", 0)) + len(writer_errors),
            "operations_reader_seen_by_change": total_seen,
            "operations_reader_seen_exact": total_seen_exact,
            "operations_reader_superseded": len(superseded_events),
            "operations_reader_timed_out": len(lagged_events),
            "success_rate_by_change_percent": round(success_rate, 2),
            "success_rate_exact_percent": round(success_rate_exact, 2),
            "reader_latency_ms": {
                "min": round(min(latencies_ms), 2) if latencies_ms else 0.0,
                "p50": round(_percentile(latencies_ms, 0.50), 2),
                "p95": round(_percentile(latencies_ms, 0.95), 2),
                "max": round(max(latencies_ms), 2) if latencies_ms else 0.0,
                "avg": round(statistics.fmean(latencies_ms), 2) if latencies_ms else 0.0,
            },
            "reader_latency_exact_ms": {
                "min": round(min(latencies_exact_ms), 2) if latencies_exact_ms else 0.0,
                "p50": round(_percentile(latencies_exact_ms, 0.50), 2),
                "p95": round(_percentile(latencies_exact_ms, 0.95), 2),
                "max": round(max(latencies_exact_ms), 2) if latencies_exact_ms else 0.0,
                "avg": round(statistics.fmean(latencies_exact_ms), 2) if latencies_exact_ms else 0.0,
            },
            "writer_perf_ms": {
                "avg": round(float(writer_summary.get("writer_avg_ms", 0.0)), 2),
                "p95": round(float(writer_summary.get("writer_p95_ms", 0.0)), 2),
            },
            "writer_reported_done": bool("operations_ok" in writer_summary or "operations_failed" in writer_summary),
            "writer_exit_code": writer_exit_code,
            "superseded_events_sample": superseded_events[:10],
            "timed_out_events_sample": lagged_events[:10],
            "writer_errors_sample": writer_errors[:10],
            "finished_at": datetime.now().isoformat(),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_iso))
    finally:
        try:
            container.data_service.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            container.db_manager.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    mp.freeze_support()
    main()
