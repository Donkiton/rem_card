from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("REMCARD_LOCAL_FIRST_SYNC", "0")
os.environ.setdefault("REMCARD_LOCAL_OUTBOX_SYNC", "0")
os.environ.setdefault("REMCARD_CHANGELOG_LIVE_TRIM_ENABLED", "0")

from _local_rem_card_bootstrap import bootstrap_local_rem_card


bootstrap_local_rem_card()

from PySide6.QtWidgets import QApplication

from rem_card.data.dao.fluids_dao import FluidsDAO
from rem_card.data.dao.orders_dao import OrdersDAO
from rem_card.data.dao.patient_dao import PatientDAO
from rem_card.data.dao.patient_status_dao import PatientStatusDAO
from rem_card.data.dao.ventilation_dao import VentilationDAO
from rem_card.data.dao.vitals_dao import VitalsDAO
from rem_card.data.dao.db_manager import DatabaseManager
from rem_card.services.patient_status_service import PatientStatusService
from rem_card.services.read_coordinator import ReadCoordinator
from rem_card.services.remcard_facade import RemCardService
from rem_card.ui.nurse_view.sectors.nurse_sector_print import DataCollectorWorker as NurseDataCollector
from rem_card.ui.rem_card_sectors.s_print.full_report_data import collect_full_report_data
from rem_card.ui.rem_card_sectors.sector_print import DataCollectorWorker as DoctorDataCollector


DEFAULT_DB_PATH = Path(r"C:\Project\Baza_rao3_jurnal\archiv\rao_journal.db")
DEFAULT_OUTPUT_DIR = Path(r"C:\Project\Baza_rao3_jurnal\report\pdf_benchmark")


class CountingDatabaseManager(DatabaseManager):
    def __init__(self, db_path: str):
        self.fetch_all_count = 0
        self.fetch_one_count = 0
        super().__init__(db_path, db_path)
        self.reset_counts()

    def reset_counts(self) -> None:
        self.fetch_all_count = 0
        self.fetch_one_count = 0

    def fetch_all_remcard(self, query: str, params=(), *, cancel_check=None):
        self.fetch_all_count += 1
        return super().fetch_all_remcard(query, params, cancel_check=cancel_check)

    def fetch_one_remcard(self, query: str, params=()):
        self.fetch_one_count += 1
        return super().fetch_one_remcard(query, params)

    def counts(self) -> dict[str, int]:
        return {
            "fetch_all": int(self.fetch_all_count),
            "fetch_one": int(self.fetch_one_count),
        }


@dataclass(frozen=True)
class RoleSpec:
    name: str
    transform: Callable[[dict, Any, dict], dict]
    include_ventilation: bool
    config: dict[str, bool]
    unknown_patient_name: str
    missing_admission_icu_day: str | None = None


ROLE_SPECS = {
    "doctor": RoleSpec(
        name="doctor",
        transform=DoctorDataCollector.transform_data_static,
        include_ventilation=True,
        config={
            "vitals": True,
            "balance": True,
            "prescriptions": True,
            "events": True,
            "ventilation": True,
            "death_outcome": True,
            "death_protocol": True,
        },
        unknown_patient_name="Неизвестный пациент",
        missing_admission_icu_day="Неизвестно",
    ),
    "nurse": RoleSpec(
        name="nurse",
        transform=NurseDataCollector.transform_data_static,
        include_ventilation=False,
        config={
            "vitals": True,
            "balance": True,
            "prescriptions": True,
            "events": True,
            "death_outcome": True,
            "death_protocol": True,
        },
        unknown_patient_name="Неизвестный",
    ),
}


def _median(values: list[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    quantiles = statistics.quantiles(values, n=20, method="inclusive")
    return float(quantiles[18])


def _round_ms(value: float) -> float:
    return round(float(value), 3)


def _safe_patient_name(data: list[dict] | dict | None) -> str:
    if isinstance(data, list) and data:
        raw = data[0].get("patient_name")
    elif isinstance(data, dict):
        raw = data.get("patient_name")
    else:
        raw = "unknown"
    safe = str(raw or "unknown").strip().replace(" ", "_").replace("/", "_").replace("\\", "_")
    return safe or "unknown"


def _pdf_info(path: Path) -> dict[str, Any]:
    info: dict[str, Any] = {"size_bytes": path.stat().st_size if path.exists() else 0}
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        info["pages"] = len(reader.pages)
        first_pages_text = "\n".join((page.extract_text() or "") for page in reader.pages[:2])
        full_text = "\n".join((page.extract_text() or "") for page in reader.pages)
        info["first_pages_text_chars"] = len(first_pages_text)
        info["full_text_chars"] = len(full_text)
        info["has_card_title"] = "РЕАНИМАЦИОННАЯ КАРТА" in full_text
        info["section_counts"] = {
            "card_title": full_text.count("РЕАНИМАЦИОННАЯ КАРТА"),
            "vitals": full_text.count("ТАБЛИЦА ПОКАЗАТЕЛЕЙ"),
            "prescriptions": full_text.count("ЛИСТ НАЗНАЧЕНИЙ"),
            "balance_in": full_text.count("ПОЧАСОВОЕ ВВЕДЕНИЕ"),
            "balance_out": full_text.count("ПОЧАСОВОЕ ВЫВЕДЕНИЕ"),
            "movement": full_text.count("ДВИЖЕНИЕ"),
            "ventilation": full_text.count("ИСТОРИЯ СОБЫТИЙ ИВЛ"),
            "death_outcome": full_text.count("ИСХОД: СМЕРТЬ"),
            "death_protocol": full_text.count("ПРОТОКОЛ УСТАНОВЛЕНИЯ СМЕРТИ ЧЕЛОВЕКА"),
        }
    except Exception as exc:
        info["pages"] = None
        info["text_check_error"] = str(exc)
    return info


def create_service(db_path: Path) -> tuple[RemCardService, CountingDatabaseManager]:
    db = CountingDatabaseManager(str(db_path))
    patient_dao = PatientDAO(db)
    vitals_dao = VitalsDAO(db)
    fluids_dao = FluidsDAO(db)
    orders_dao = OrdersDAO(db)
    ventilation_dao = VentilationDAO(db)
    status_dao = PatientStatusDAO(db)
    status_service = PatientStatusService(status_dao, data_service=None, ventilation_dao=ventilation_dao)
    service = RemCardService(
        vitals_dao,
        fluids_dao,
        orders_dao,
        ventilation_dao,
        patient_dao,
        status_service=status_service,
        data_service=None,
    )
    service.status_service = status_service
    service.read_coordinator = ReadCoordinator(service)
    service.read_mode = "benchmark"
    service.source_db_path = str(db_path)
    return service, db


def collect_report(service: RemCardService, db: CountingDatabaseManager, admission_id: int, spec: RoleSpec) -> tuple[list[dict], dict]:
    dates = service.get_all_card_dates(admission_id)
    db.reset_counts()
    started = time.perf_counter()
    data = collect_full_report_data(
        service,
        admission_id,
        dates,
        spec.config,
        spec.transform,
        include_ventilation=spec.include_ventilation,
        unknown_patient_name=spec.unknown_patient_name,
        unknown_icu_day="?",
        missing_admission_icu_day=spec.missing_admission_icu_day,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return data, {
        "elapsed_ms": elapsed_ms,
        "day_count": len(dates),
        "result_count": len(data),
        "queries": db.counts(),
    }


def build_pdf(data: list[dict], config: dict[str, bool], output_path: Path) -> dict[str, Any]:
    from rem_card.ui.rem_card_sectors.s_print.builder import ReportBuilder

    started = time.perf_counter()
    ReportBuilder.build_pdf(data, config, output_path)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    info = _pdf_info(output_path)
    info["elapsed_ms"] = elapsed_ms
    return info


def run_case(
    *,
    db_path: Path,
    output_dir: Path,
    admission_id: int,
    role: str,
    iterations: int,
    keep_pdfs: bool,
) -> dict[str, Any]:
    spec = ROLE_SPECS[role]
    output_dir.mkdir(parents=True, exist_ok=True)
    collect_samples: list[dict[str, Any]] = []
    pdf_samples: list[dict[str, Any]] = []
    last_data: list[dict] | None = None
    service, db = create_service(db_path)
    try:
        for idx in range(iterations):
            data, collect_result = collect_report(service, db, admission_id, spec)
            last_data = data
            collect_samples.append(collect_result)

            patient_name = _safe_patient_name(data)
            pdf_path = output_dir / f"bench_{role}_adm{admission_id}_{patient_name}_{idx + 1}.pdf"
            pdf_result = build_pdf(data, spec.config, pdf_path)
            pdf_result["path"] = str(pdf_path)
            pdf_samples.append(pdf_result)
            if not keep_pdfs and idx + 1 < iterations:
                try:
                    pdf_path.unlink(missing_ok=True)
                except Exception:
                    pass
    finally:
        db.close()

    collect_ms = [_round_ms(item["elapsed_ms"]) for item in collect_samples]
    pdf_ms = [_round_ms(item["elapsed_ms"]) for item in pdf_samples]
    query_samples = [item["queries"] for item in collect_samples]
    last_pdf = pdf_samples[-1] if pdf_samples else {}
    return {
        "admission_id": admission_id,
        "role": role,
        "iterations": iterations,
        "days": collect_samples[-1]["day_count"] if collect_samples else 0,
        "patient_name": _safe_patient_name(last_data),
        "collect_ms": {
            "samples": collect_ms,
            "median": _round_ms(_median(collect_ms)),
            "p95": _round_ms(_p95(collect_ms)),
        },
        "pdf_ms": {
            "samples": pdf_ms,
            "median": _round_ms(_median(pdf_ms)),
            "p95": _round_ms(_p95(pdf_ms)),
        },
        "total_ms": {
            "samples": [_round_ms(c + p) for c, p in zip(collect_ms, pdf_ms)],
            "median": _round_ms(_median([c + p for c, p in zip(collect_ms, pdf_ms)])),
        },
        "queries": query_samples[-1] if query_samples else {},
        "last_pdf": last_pdf,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark full PDF report data collection and rendering.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--admissions", type=int, nargs="+", default=[3, 8])
    parser.add_argument("--roles", choices=sorted(ROLE_SPECS), nargs="+", default=["doctor", "nurse"])
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--keep-pdfs", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.db.exists():
        raise FileNotFoundError(args.db)
    iterations = max(1, int(args.iterations))
    app = QApplication.instance() or QApplication([])

    results = []
    for admission_id in args.admissions:
        for role in args.roles:
            case = run_case(
                db_path=args.db,
                output_dir=args.output_dir,
                admission_id=int(admission_id),
                role=role,
                iterations=iterations,
                keep_pdfs=bool(args.keep_pdfs),
            )
            results.append(case)
            print(
                "adm={admission_id} role={role} days={days} "
                "collect_med={collect:.1f}ms pdf_med={pdf:.1f}ms total_med={total:.1f}ms "
                "queries={queries} pages={pages} size={size}".format(
                    admission_id=case["admission_id"],
                    role=case["role"],
                    days=case["days"],
                    collect=case["collect_ms"]["median"],
                    pdf=case["pdf_ms"]["median"],
                    total=case["total_ms"]["median"],
                    queries=case["queries"],
                    pages=case["last_pdf"].get("pages"),
                    size=case["last_pdf"].get("size_bytes"),
                )
            )

    payload = {
        "db": str(args.db),
        "output_dir": str(args.output_dir),
        "iterations": iterations,
        "results": results,
    }
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    app.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
