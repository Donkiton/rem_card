#!/usr/bin/env python
"""
Static safety guardrails for shared SQLite architecture.

The checks are intentionally narrow: they block known dangerous patterns while
allowing established infrastructure code such as sqlite_shared.py and startup
recovery internals.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
try:
    from _local_rem_card_bootstrap import bootstrap_local_rem_card

    bootstrap_local_rem_card()
except Exception:
    pass


WRITE_SQL_RE = re.compile(r"\.execute\(\s*(?:[rubfRUBF]*)(['\"]{1,3})\s*(INSERT|UPDATE|DELETE|BEGIN|COMMIT|ALTER|DROP|CREATE)\b", re.IGNORECASE)
PRIVATE_SERVICE_RE = re.compile(r"\b(?:service|remcard_service)\._[A-Za-z]\w*")
COPY_LIVE_DB_RE = re.compile(r"shutil\.copy(?:2|file)?\s*\(.*(?:REMCARD_DB_PATH|JOURNAL_DB_PATH|self\.db_path|\bdb_path\b)", re.IGNORECASE)

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "venv", "build", "dist"}
LIVE_DB_COPY_ALLOWLIST = {
    "app/sqlite_shared.py",
    "app/startup_db_guard.py",
    "app/paths.py",
    "app/updater_main.py",
    "scripts/regression_safety_checks.py",
}


def _rel(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()


def _is_skipped(path: Path) -> bool:
    return any(part in SKIP_DIRS for part in path.parts)


def _python_files(*roots: str) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        base = PROJECT_ROOT / root
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if not _is_skipped(path):
                files.append(path)
    return files


def _line_findings(path: Path, pattern: re.Pattern[str], message: str) -> list[dict[str, Any]]:
    findings = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    for line_no, line in enumerate(lines, start=1):
        if pattern.search(line):
            findings.append({"path": _rel(path), "line": line_no, "message": message, "snippet": line.strip()})
    return findings


def _check_ui_forbidden_patterns() -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    for path in _python_files("ui"):
        findings.extend(_line_findings(path, re.compile(r"\bsqlite3\.connect\s*\("), "UI must not open SQLite connections"))
        findings.extend(_line_findings(path, WRITE_SQL_RE, "UI must not execute write/DDL SQL directly"))
        findings.extend(_line_findings(path, re.compile(r"\bDataService\.run_write\b|\brun_write\s*\("), "UI must enqueue writes, not call run_write directly"))
        findings.extend(_line_findings(path, PRIVATE_SERVICE_RE, "UI must not access private service fields"))
    return {"name": "ui_forbidden_patterns", "ok": not findings, "findings": findings}


def _check_network_sqlite_profile() -> dict[str, Any]:
    try:
        from rem_card.app.sqlite_shared import _resolve_sqlite_profile_settings

        settings = _resolve_sqlite_profile_settings("network")
    except Exception as exc:
        return {"name": "network_sqlite_profile", "ok": False, "error": str(exc)}

    expected = {"journal_mode": "DELETE", "synchronous": "EXTRA", "mmap_mb": 0}
    mismatches = {key: {"expected": value, "actual": settings.get(key)} for key, value in expected.items() if settings.get(key) != value}
    return {
        "name": "network_sqlite_profile",
        "ok": not mismatches,
        "settings": settings,
        "mismatches": mismatches,
    }


def _check_live_db_copy_patterns() -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    for path in _python_files("app", "data", "services", "scripts"):
        rel_path = _rel(path)
        if rel_path in LIVE_DB_COPY_ALLOWLIST:
            continue
        findings.extend(_line_findings(path, COPY_LIVE_DB_RE, "Live DB copies must use SQLite Backup API"))
    return {"name": "no_live_db_copy_bypass", "ok": not findings, "findings": findings}


def _check_required_contract_files() -> dict[str, Any]:
    required = [
        "docs/db_safety_contract.md",
        "app/schema_migration_guard.py",
        "scripts/network_acceptance_runner.py",
        "scripts/restore_drill.py",
    ]
    missing = [path for path in required if not (PROJECT_ROOT / path).is_file()]
    return {"name": "required_safety_artifacts", "ok": not missing, "missing": missing}


def _check_backup_api_guard() -> dict[str, Any]:
    guard_path = PROJECT_ROOT / "app" / "schema_migration_guard.py"
    text = guard_path.read_text(encoding="utf-8") if guard_path.exists() else ""
    ok = "backup_connection(" in text and "invalid_dir=" in text and "_acquire_file_lock" in text
    return {"name": "migration_uses_backup_api", "ok": ok}


def _check_recovery_lock_guard() -> dict[str, Any]:
    recovery_path = PROJECT_ROOT / "app" / "startup_db_guard.py"
    text = recovery_path.read_text(encoding="utf-8") if recovery_path.exists() else ""
    ok = "recover_shared_db_with_locks" in text and "recovery.lock" in text and "_active_other_role_locks" in text
    return {"name": "recovery_lock_guard_present", "ok": ok}


def _check_settings_db_guardrails() -> dict[str, Any]:
    findings: list[str] = []
    paths_text = (PROJECT_ROOT / "app" / "settings_db_paths.py").read_text(encoding="utf-8")
    db_text = (PROJECT_ROOT / "data" / "settings" / "settings_db.py").read_text(encoding="utf-8")
    runtime_paths_text = (PROJECT_ROOT / "app" / "runtime_paths.py").read_text(encoding="utf-8")
    app_paths_text = (PROJECT_ROOT / "app" / "paths.py").read_text(encoding="utf-8")

    if "SETTINGS_DIR_NAME = \"settings\"" not in paths_text:
        findings.append("settings DB path must use <BAZA_DIR>/settings")
    if "\"archiv\"" in paths_text:
        findings.append("settings DB path module must not route settings DB through archiv")
    for token in ("profile=\"network\"", "SQLiteWriteController", "get_settings_lock_path"):
        if token not in db_text:
            findings.append(f"settings DB safety token missing: {token}")
    if "profile=\"wal\"" in db_text.lower() or "journal_mode = WAL" in db_text:
        findings.append("settings DB must not enable WAL")
    if "settings_local_db_used=false" not in db_text:
        findings.append("startup log must explicitly report no local settings DB")
    sync_start = runtime_paths_text.find("def sync_external_settings_from_bundle")
    sync_body = runtime_paths_text[sync_start: runtime_paths_text.find("\ndef ", sync_start + 1)]
    if "return 0" not in sync_body or "_copy_file_atomic(" in sync_body:
        findings.append("compiled startup must not copy runtime settings JSON next to exe")
    ensure_start = app_paths_text.find("def ensure_external_dictionaries_initialized")
    ensure_body = app_paths_text[ensure_start: app_paths_text.find("\ndef ", ensure_start + 1)]
    if "_copy_missing_json_files" in ensure_body or "os.makedirs(target_dir" in ensure_body:
        findings.append("compiled startup must not create external dictionary JSON files")
    return {"name": "settings_db_guardrails", "ok": not findings, "findings": findings}


def _check_settings_runtime_catalog_boundaries() -> dict[str, Any]:
    findings: list[str] = []
    sources = {
        "services/prescription_engine.py": (PROJECT_ROOT / "services" / "prescription_engine.py").read_text(encoding="utf-8"),
        "services/order_domain_service.py": (PROJECT_ROOT / "services" / "order_domain_service.py").read_text(encoding="utf-8"),
        "services/lab_analysis_catalog_service.py": (PROJECT_ROOT / "services" / "lab_analysis_catalog_service.py").read_text(encoding="utf-8"),
        "services/diet_service.py": (PROJECT_ROOT / "services" / "diet_service.py").read_text(encoding="utf-8"),
        "services/doctor_list_service.py": (PROJECT_ROOT / "services" / "doctor_list_service.py").read_text(encoding="utf-8"),
    }
    for token in ("user_overrides.json", ".seed.json", "json.load"):
        if token in sources["services/prescription_engine.py"]:
            findings.append(f"PrescriptionEngine runtime still references {token}")
    if ".seed.json" in sources["services/order_domain_service.py"] or "json.load" in sources["services/order_domain_service.py"]:
        findings.append("OrderDomainService must read drug/group priority from settings DB, not seed JSON")
    if "self.file_store = file_store or" in sources["services/lab_analysis_catalog_service.py"]:
        findings.append("LabAnalysisCatalogService default must not create JSON file store")
    if "self.file_store = file_store or" in sources["services/diet_service.py"]:
        findings.append("DietTemplateService default must not create JSON file store")
    if "self.path = path or" in sources["services/doctor_list_service.py"]:
        findings.append("DoctorListStore default must not point at JSON runtime storage")
    return {"name": "settings_runtime_catalog_boundaries", "ok": not findings, "findings": findings}


def main() -> int:
    started = time.perf_counter()
    checks = [
        _check_ui_forbidden_patterns(),
        _check_network_sqlite_profile(),
        _check_live_db_copy_patterns(),
        _check_required_contract_files(),
        _check_backup_api_guard(),
        _check_recovery_lock_guard(),
        _check_settings_db_guardrails(),
        _check_settings_runtime_catalog_boundaries(),
    ]
    failed = [check for check in checks if not check.get("ok")]
    report = {
        "status": "failed" if failed else "passed",
        "checks_total": len(checks),
        "checks_passed": len(checks) - len(failed),
        "checks_failed": len(failed),
        "duration_sec": round(time.perf_counter() - started, 3),
        "checks": checks,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
