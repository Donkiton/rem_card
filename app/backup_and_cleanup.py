import glob
import json
import os
import socket
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from rem_card.app.logger import logger
from rem_card.app.paths import (
    ARCHIV_DIR,
    BACKUPS_RC_DIR,
    BACKUPS_VALID_DIR,
    BACKUP_HEALTH_DIR,
    DB_LOCK_PATH,
    INVALID_BACKUPS_DIR,
    LOGS_DIR,
    REMCARD_DB_PATH,
    REPORT_DIR,
    ensure_directories,
)
from rem_card.app.sqlite_uri import build_sqlite_file_uri
from rem_card.app.sqlite_shared import (
    FileWriteLock,
    backup_meta_path,
    backup_connection,
    configure_connection,
    list_backup_candidates,
    run_quick_check,
    validate_sqlite_file,
)


# Более консервативные дефолты, чтобы рост backup-каталога оставался контролируемым
# даже без ручной настройки окружения.
BACKUP_RETENTION_DAYS = max(1, int(os.environ.get("REMCARD_BACKUP_RETENTION_DAYS", "21")))
BACKUP_MAX_COUNT = max(5, int(os.environ.get("REMCARD_BACKUP_MAX_COUNT", "21")))
BACKUP_MAX_TOTAL_BYTES = max(
    256 * 1024 * 1024,
    int(float(os.environ.get("REMCARD_BACKUP_MAX_TOTAL_GB", "1.0")) * 1024 * 1024 * 1024),
)

CHANGE_LOG_RETENTION_DAYS = max(1, int(os.environ.get("REMCARD_CHANGELOG_RETENTION_DAYS", "14")))
REPORT_RETENTION_DAYS = max(1, int(os.environ.get("REMCARD_REPORT_RETENTION_DAYS", "7")))
CHANGE_LOG_MAX_ROWS = max(10_000, int(os.environ.get("REMCARD_CHANGELOG_MAX_ROWS", "120000")))
CHANGE_LOG_PRUNE_BATCH = max(1000, int(os.environ.get("REMCARD_CHANGELOG_PRUNE_BATCH", "50000")))
CHANGE_LOG_COMPACT_MIN_FREE_BYTES = max(
    8 * 1024 * 1024,
    int(float(os.environ.get("REMCARD_CHANGELOG_COMPACT_MIN_MB", "16")) * 1024 * 1024),
)
CHANGE_LOG_COMPACT_MIN_INTERVAL_SEC = max(
    3600,
    int(float(os.environ.get("REMCARD_CHANGELOG_COMPACT_MIN_HOURS", "24")) * 3600),
)
CHANGE_LOG_COMPACT_STAMP_PATH = os.path.join(ARCHIV_DIR, ".last_change_log_compact")

LOCKED_ERROR_MARKERS = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
)
BACKUP_HEALTH_CHECK_SCAN_LIMIT = max(
    1,
    int(os.environ.get("REMCARD_BACKUP_HEALTH_CHECK_SCAN_LIMIT", "8")),
)
DAILY_BACKUP_TARGET_HOUR = min(
    23,
    max(0, int(os.environ.get("REMCARD_DAILY_BACKUP_HOUR", "3"))),
)
DAILY_BACKUP_WINDOW_HOURS = max(
    1.0,
    float(os.environ.get("REMCARD_DAILY_BACKUP_WINDOW_HOURS", "4")),
)
DAILY_BACKUP_NURSE_GRACE_MINUTES = max(
    0.0,
    float(os.environ.get("REMCARD_DAILY_BACKUP_NURSE_GRACE_MINUTES", "15")),
)
DAILY_BACKUP_RESERVATION_STALE_HOURS = max(
    1.0,
    float(os.environ.get("REMCARD_DAILY_BACKUP_RESERVATION_STALE_HOURS", "6")),
)
BACKUP_TIMEZONE_OFFSET_HOURS = float(os.environ.get("REMCARD_BACKUP_TZ_OFFSET_HOURS", "10"))
BACKUP_TIMEZONE = timezone(timedelta(hours=BACKUP_TIMEZONE_OFFSET_HOURS))


def _normalize_backup_role(role: str | None) -> str:
    normalized = str(role or "").strip().lower()
    if normalized in {"doctor", "врач"} or "doctor" in normalized or "врач" in normalized:
        return "doctor"
    if normalized in {"nurse", "медсестра"} or "nurse" in normalized or "мед" in normalized:
        return "nurse"
    return normalized


def _network_file_datetime(path: str) -> datetime:
    timestamp = os.path.getmtime(path)
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(BACKUP_TIMEZONE).replace(tzinfo=None)


def _network_now_from_backup_health() -> tuple[datetime, str]:
    os.makedirs(BACKUPS_VALID_DIR, exist_ok=True)
    os.makedirs(BACKUP_HEALTH_DIR, exist_ok=True)
    probe_name = f".server_time_probe_{socket.gethostname()}_{os.getpid()}.tmp"
    probe_path = os.path.join(BACKUP_HEALTH_DIR, probe_name)
    try:
        with open(probe_path, "wb") as handle:
            handle.write(f"{socket.gethostname()}:{os.getpid()}:{time.time()}".encode("ascii", errors="ignore"))
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        return _network_file_datetime(probe_path), "backup_health_mtime"
    finally:
        try:
            os.remove(probe_path)
        except FileNotFoundError:
            pass
        except OSError:
            logger.debug("Failed to remove backup time probe %s", probe_path)


def _resuscitation_date(now: datetime):
    if now.hour < 8:
        return (now - timedelta(days=1)).date()
    return now.date()


def _daily_backup_window(now: datetime) -> tuple[datetime, datetime]:
    target = now.replace(hour=DAILY_BACKUP_TARGET_HOUR, minute=0, second=0, microsecond=0)
    return target, target + timedelta(hours=DAILY_BACKUP_WINDOW_HOURS)


def _daily_backup_due(role: str, now: datetime, *, force: bool = False) -> dict:
    target, window_end = _daily_backup_window(now)
    backup_date = _resuscitation_date(target)
    backup_date_str = backup_date.strftime("%Y-%m-%d")
    base = {
        "backup_date": backup_date_str,
        "target_at": target.isoformat(sep=" "),
        "window_end_at": window_end.isoformat(sep=" "),
        "role": role,
    }
    if force:
        return {**base, "due": True, "reason": "force"}
    if role not in {"doctor", "nurse"}:
        return {**base, "due": False, "reason": "role_not_allowed"}
    if now < target:
        return {**base, "due": False, "reason": "before_night_window"}
    if now >= window_end:
        return {**base, "due": False, "reason": "after_night_window"}
    if role == "nurse":
        nurse_allowed_at = target + timedelta(minutes=DAILY_BACKUP_NURSE_GRACE_MINUTES)
        if now < nurse_allowed_at:
            return {
                **base,
                "due": False,
                "reason": "nurse_grace",
                "nurse_allowed_at": nurse_allowed_at.isoformat(sep=" "),
            }
    return {**base, "due": True, "reason": "night_window"}


def _daily_backup_paths(backup_date_str: str) -> dict[str, str]:
    db_base_name = os.path.splitext(os.path.basename(REMCARD_DB_PATH))[0]
    backup_file_name = f"{db_base_name}_{backup_date_str}.db"
    return {
        "backup": os.path.join(BACKUPS_VALID_DIR, backup_file_name),
        "done": os.path.join(BACKUP_HEALTH_DIR, f"daily_backup_{backup_date_str}.done.json"),
        "lock": os.path.join(BACKUP_HEALTH_DIR, f"daily_backup_{backup_date_str}.lock"),
        "reserved": os.path.join(BACKUP_HEALTH_DIR, f"daily_backup_{backup_date_str}.reserved.json"),
    }


def _safe_marker_token(value: object, *, limit: int = 80) -> str:
    text = str(value or "unknown").strip().lower()
    chars = []
    for ch in text:
        if ch.isalnum() or ch in {"-", "_"}:
            chars.append(ch)
        elif ch in {" ", ".", ":", "/", "\\"}:
            chars.append("_")
    token = "".join(chars).strip("_")
    return (token or "unknown")[:limit]


def _daily_backup_report_marker_path(result: dict) -> str:
    backup_date = _safe_marker_token(result.get("backup_date") or datetime.now().strftime("%Y-%m-%d"))
    status = _safe_marker_token(result.get("status"))
    reason = _safe_marker_token(result.get("reason") or result.get("error"))
    return os.path.join(BACKUP_HEALTH_DIR, f"daily_backup_report_{backup_date}_{status}_{reason}.json")


def _write_json_atomic(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = os.path.join(
        os.path.dirname(path),
        f".{os.path.basename(path)}.{socket.gethostname()}_{os.getpid()}.tmp",
    )
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _read_json_file(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _reservation_is_stale(path: str, now: datetime) -> bool:
    try:
        mtime = _network_file_datetime(path)
    except OSError:
        return True
    return (now - mtime) > timedelta(hours=DAILY_BACKUP_RESERVATION_STALE_HOURS)


def _reserve_daily_backup(paths: dict[str, str], *, role: str, now: datetime, time_source: str) -> dict | None:
    if os.path.exists(paths["backup"]) or os.path.exists(paths["done"]):
        return None
    os.makedirs(os.path.dirname(paths["lock"]), exist_ok=True)
    payload = {
        "role": role,
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "reserved_at": now.isoformat(sep=" "),
        "time_source": time_source,
    }
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    while True:
        try:
            fd = os.open(paths["lock"], os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, raw)
            finally:
                os.close(fd)
            _write_json_atomic(paths["reserved"], payload)
            return payload
        except FileExistsError:
            if _reservation_is_stale(paths["lock"], now):
                try:
                    os.remove(paths["lock"])
                    logger.warning("Removed stale daily backup reservation lock: %s", paths["lock"])
                    continue
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    logger.warning("Daily backup reservation is busy and cannot be cleaned: %s", exc)
            return None


def _release_daily_backup_reservation(paths: dict[str, str], reservation: dict | None) -> None:
    if not reservation:
        return
    for path in (paths.get("reserved"), paths.get("lock")):
        if not path:
            continue
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Failed to remove daily backup reservation file %s: %s", path, exc)


def _mark_daily_backup_done(paths: dict[str, str], *, role: str, now: datetime, backup_path: str, status: str) -> None:
    _write_json_atomic(
        paths["done"],
        {
            "status": status,
            "role": role,
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "completed_at": now.isoformat(sep=" "),
            "backup_path": backup_path,
        },
    )


def _should_report_daily_backup_not_created(result: dict) -> bool:
    status = str(result.get("status") or "").strip().lower()
    reason = str(result.get("reason") or "").strip().lower()
    if status in {"missing_db", "primary_db_unavailable", "error"}:
        return True
    if status == "skipped" and reason == "after_night_window":
        return True
    return False


def _daily_backup_reason_text(result: dict) -> str:
    status = str(result.get("status") or "").strip()
    reason = str(result.get("reason") or "").strip()
    error = str(result.get("error") or "").strip()
    labels = {
        "after_night_window": "ночное окно резервного копирования уже прошло",
        "primary_db_missing": "основная база не найдена",
        "primary_db_quick_check_failed": "основная база не прошла быструю проверку SQLite",
        "primary_db_unavailable": "основная база недоступна для чтения",
        "exception": "ошибка во время фонового обслуживания",
    }
    text = labels.get(reason) or labels.get(status) or reason or status or "причина не указана"
    if error:
        text = f"{text}: {error}"
    detail = str(result.get("health_detail") or "").strip()
    if detail and detail not in text:
        text = f"{text}; детали: {detail}"
    return text


def _submit_daily_backup_not_created_report(result: dict) -> None:
    marker_path = _daily_backup_report_marker_path(result)
    os.makedirs(os.path.dirname(marker_path), exist_ok=True)
    marker_payload = {
        "status": "pending",
        "created_at": datetime.now().replace(microsecond=0).isoformat(sep=" "),
        "result": result,
    }
    raw_marker = json.dumps(marker_payload, ensure_ascii=False, indent=2).encode("utf-8")
    try:
        fd = os.open(marker_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, raw_marker)
        finally:
            os.close(fd)
    except FileExistsError:
        return

    try:
        from rem_card.services.user_reports import REPORT_TYPE_PROBLEM, UserReportsService

        reason_text = _daily_backup_reason_text(result)
        text = "\n".join(
            [
                "Автоматический ночной бекап основной базы не был создан.",
                "",
                f"Причина: {reason_text}",
                f"Дата бекапа: {result.get('backup_date') or ''}",
                f"Статус: {result.get('status') or ''}",
                f"Роль: {result.get('role') or ''}",
                f"Компьютер: {socket.gethostname()}",
                f"Время проверки: {result.get('now') or ''}",
                f"Источник времени: {result.get('time_source') or ''}",
                f"Основная база: {REMCARD_DB_PATH}",
                f"Ожидаемый файл бекапа: {result.get('backup_path') or ''}",
                "",
                "Пользователю сообщение не показывалось. Это системный репорт для контроля резервного копирования.",
            ]
        )
        report = UserReportsService().submit_report(
            report_type=REPORT_TYPE_PROBLEM,
            text=text,
            role="system",
            created_at=datetime.now().replace(microsecond=0),
            extra_context={
                "automatic": True,
                "source": "daily_backup",
                "daily_backup_result": result,
            },
        )
        _write_json_atomic(
            marker_path,
            {
                "status": "reported",
                "reported_at": datetime.now().replace(microsecond=0).isoformat(sep=" "),
                "report_dir": str(report.directory),
                "result": result,
            },
        )
        logger.warning("Daily backup was not created; auto report submitted: %s", report.directory)
    except Exception as exc:
        try:
            os.remove(marker_path)
        except Exception:
            pass
        logger.warning("Failed to submit daily backup missing report: %s", exc, exc_info=True)


def _maybe_submit_daily_backup_not_created_report(result: dict) -> None:
    if not _should_report_daily_backup_not_created(result):
        return
    try:
        _submit_daily_backup_not_created_report(dict(result))
    except Exception as exc:
        logger.warning("Daily backup missing report skipped: %s", exc, exc_info=True)


def _network_now_or_local() -> tuple[datetime, str]:
    try:
        return _network_now_from_backup_health()
    except Exception as exc:
        logger.warning("Failed to read network backup time, using local clock: %s", exc)
        return datetime.now(), "local_clock_fallback"


def perform_daily_backup_and_cleanup(role: str | None = None, *, force: bool = False) -> dict:
    result: dict = {}
    try:
        ensure_directories()

        now, time_source = _network_now_or_local()
        backup_role = _normalize_backup_role(role or os.environ.get("REMCARD_UI_ROLE"))
        due = _daily_backup_due(backup_role, now, force=force)
        result = {
            "status": "skipped",
            "time_source": time_source,
            "now": now.isoformat(sep=" "),
            **due,
        }

        backup_date_str = str(due["backup_date"])
        paths = _daily_backup_paths(backup_date_str)
        backup_file_path = paths["backup"]
        result = {**result, "backup_path": backup_file_path}

        if os.path.exists(backup_file_path) or os.path.exists(paths["done"]):
            logger.info("Backup for %s already exists, skipping backup.", backup_date_str)
            return {**result, "status": "already_done", "backup_path": backup_file_path}

        if not due.get("due"):
            logger.info("Daily backup skipped: %s", result)
            _maybe_submit_daily_backup_not_created_report(result)
            return result

        primary_health = _primary_db_health_status(REMCARD_DB_PATH)
        if not primary_health.get("ok"):
            failed_result = {
                **result,
                "status": str(primary_health.get("status") or "primary_db_unavailable"),
                "due_reason": result.get("reason"),
                "reason": str(primary_health.get("reason") or "primary_db_unavailable"),
                "health_detail": str(primary_health.get("detail") or ""),
                "backup_path": backup_file_path,
            }
            logger.warning("Daily backup skipped: primary DB is not healthy: %s", failed_result)
            _maybe_submit_daily_backup_not_created_report(failed_result)
            return failed_result

        reservation = _reserve_daily_backup(paths, role=backup_role, now=now, time_source=time_source)
        if reservation is None:
            logger.info("Daily backup skipped: backup for %s is already reserved or done.", backup_date_str)
            return {**result, "status": "reserved_or_done", "backup_path": backup_file_path}

        try:
            _create_safe_sqlite_backup(REMCARD_DB_PATH, backup_file_path)
            logger.info("Daily backup created via SQLite backup API: %s", backup_file_path)
            _mark_daily_backup_done(
                paths,
                role=backup_role,
                now=now,
                backup_path=backup_file_path,
                status="created",
            )

            prune_stats = _prune_change_log_and_maybe_compact(REMCARD_DB_PATH, now)

            # Cleanup old backups by age + enforce hard count/size caps only after a fresh
            # validated backup exists for the current night.
            if _can_cleanup_old_backups(REMCARD_DB_PATH, BACKUPS_RC_DIR):
                cutoff_30_days = now - timedelta(days=BACKUP_RETENTION_DAYS)
                _cleanup_old_files(BACKUPS_RC_DIR, "*.db", cutoff_30_days, "backup")
                _cleanup_old_files(BACKUPS_VALID_DIR, "*.db", cutoff_30_days, "validated backup")
                _cleanup_old_files(BACKUPS_VALID_DIR, "*.meta.json", cutoff_30_days, "validated backup metadata")
                _enforce_backup_limits(BACKUPS_RC_DIR)
                _enforce_backup_limits(BACKUPS_VALID_DIR)
            else:
                logger.warning(
                    "Backup cleanup skipped: no healthy backup source is available yet. "
                    "Old backups are preserved to reduce recovery risk."
                )

            # Cleanup old reports (> 1 week)
            cutoff_report_days = now - timedelta(days=REPORT_RETENTION_DAYS)
            _cleanup_old_report_files(REPORT_DIR, cutoff_report_days)

            # Cleanup old local runtime logs (> 30 days)
            cutoff_30_days = now - timedelta(days=30)
            _cleanup_old_files(LOGS_DIR, "*.log", cutoff_30_days, "local log")

            if prune_stats and prune_stats.get("deleted_rows", 0) > 0:
                logger.info(
                    "Change-log maintenance: deleted=%s, before=%s, after=%s, compacted=%s",
                    prune_stats.get("deleted_rows"),
                    prune_stats.get("rows_before"),
                    prune_stats.get("rows_after"),
                    prune_stats.get("compacted"),
                )

            return {**result, "status": "created", "backup_path": backup_file_path}
        finally:
            _release_daily_backup_reservation(paths, reservation)

    except Exception as exc:
        logger.error("Error during backup and cleanup: %s", exc, exc_info=True)
        error_result = {
            **result,
            "status": "error",
            "reason": "exception",
            "error": str(exc),
        }
        _maybe_submit_daily_backup_not_created_report(error_result)
        return error_result


def _primary_db_health_status(db_path: str) -> dict:
    if not os.path.exists(db_path):
        return {
            "ok": False,
            "status": "missing_db",
            "reason": "primary_db_missing",
            "detail": f"Database file not found: {db_path}",
        }

    conn = None
    try:
        uri = build_sqlite_file_uri(db_path, mode="ro")
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=4.0)
        configure_connection(conn, readonly=True)
        ok, result = run_quick_check(conn)
        if ok:
            return {"ok": True, "status": "ok", "reason": "quick_check_ok", "detail": str(result or "ok")}
        return {
            "ok": False,
            "status": "primary_db_unavailable",
            "reason": "primary_db_quick_check_failed",
            "detail": str(result or "quick_check failed"),
        }
    except Exception as exc:
        logger.warning("Primary DB health check failed for daily backup: %s", exc)
        return {
            "ok": False,
            "status": "primary_db_unavailable",
            "reason": "primary_db_unavailable",
            "detail": str(exc),
        }
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _is_primary_db_healthy(db_path: str) -> bool:
    return bool(_primary_db_health_status(db_path).get("ok"))


def _has_healthy_backup(backup_dir: str) -> bool:
    candidates = list_backup_candidates(backup_dir=backup_dir)
    if not candidates:
        return False

    for backup_path in candidates[:BACKUP_HEALTH_CHECK_SCAN_LIMIT]:
        ok, _reason = validate_sqlite_file(backup_path)
        if ok:
            return True
    return False


def _can_cleanup_old_backups(db_path: str, backup_dir: str) -> bool:
    if not _is_primary_db_healthy(db_path):
        return False
    return _has_healthy_backup(backup_dir)


def create_manual_primary_db_backup(source: str = "manual_primary_db_backup") -> str:
    ensure_directories()
    health = _primary_db_health_status(REMCARD_DB_PATH)
    if not health.get("ok"):
        reason = str(health.get("detail") or health.get("reason") or "основная база недоступна")
        raise RuntimeError(f"Основная база недоступна для безопасного бекапа: {reason}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    db_base_name = os.path.splitext(os.path.basename(REMCARD_DB_PATH))[0]
    backup_path = os.path.join(BACKUPS_VALID_DIR, f"{db_base_name}_manual_{stamp}.db")
    created_path = _create_safe_sqlite_backup(REMCARD_DB_PATH, backup_path, source=source, lock_wait_sec=20.0)
    _annotate_primary_backup_meta(
        created_path,
        backup_kind="primary_manual",
        source=source,
    )
    logger.info("Manual primary DB backup created: %s", created_path)
    return created_path


def _annotate_primary_backup_meta(db_path: str, *, backup_kind: str, source: str) -> None:
    meta_path = backup_meta_path(db_path)
    try:
        with open(meta_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            payload = {}
    except FileNotFoundError:
        payload = {}
    except Exception as exc:
        logger.warning("Failed to read primary backup metadata %s: %s", meta_path, exc)
        payload = {}
    payload.update(
        {
            "backup_kind": backup_kind,
            "source": source,
            "source_db_path": REMCARD_DB_PATH,
        }
    )
    try:
        _write_json_atomic(meta_path, payload)
    except Exception as exc:
        logger.warning("Failed to annotate primary backup metadata %s: %s", meta_path, exc)


def _create_safe_sqlite_backup(
    db_path: str,
    backup_file_path: str,
    *,
    source: str = "daily_backup",
    lock_wait_sec: float = 60.0,
):
    os.makedirs(os.path.dirname(backup_file_path), exist_ok=True)
    uri = build_sqlite_file_uri(db_path, mode="ro")
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=5.0)
    try:
        configure_connection(conn, readonly=True)
        return backup_connection(
            conn,
            backup_file_path,
            invalid_dir=INVALID_BACKUPS_DIR,
            logger=logger,
            lock_path=DB_LOCK_PATH,
            source=source,
            lock_wait_sec=lock_wait_sec,
        )
    finally:
        conn.close()


def _cleanup_old_files(directory, pattern, cutoff_date, file_type):
    if not os.path.exists(directory):
        return

    search_pattern = os.path.join(directory, pattern)
    for filepath in glob.glob(search_pattern):
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
            if mtime < cutoff_date:
                os.remove(filepath)
                logger.info("Deleted old %s: %s", file_type, filepath)
        except Exception as exc:
            logger.error("Failed to delete old %s %s: %s", file_type, filepath, exc)


def _cleanup_old_report_files(directory, cutoff_date):
    if not os.path.exists(directory):
        return

    search_pattern = os.path.join(directory, "*")
    for filepath in glob.glob(search_pattern):
        if not os.path.isfile(filepath):
            continue
        try:
            created_at = datetime.fromtimestamp(_get_report_creation_timestamp(filepath))
            if created_at < cutoff_date:
                os.remove(filepath)
                logger.info("Deleted old report: %s", filepath)
        except Exception as exc:
            logger.error("Failed to delete old report %s: %s", filepath, exc)


def _get_report_creation_timestamp(filepath: str) -> float:
    try:
        created_at = os.path.getctime(filepath)
    except OSError:
        return os.path.getmtime(filepath)

    if os.name == "nt":
        return created_at

    try:
        return min(created_at, os.path.getmtime(filepath))
    except OSError:
        return created_at


def _enforce_backup_limits(backup_dir: str):
    if not os.path.isdir(backup_dir):
        return

    files = [
        os.path.join(backup_dir, name)
        for name in os.listdir(backup_dir)
        if name.lower().endswith(".db") and os.path.isfile(os.path.join(backup_dir, name))
    ]
    files.sort(key=os.path.getmtime, reverse=True)

    # Hard limit by count
    for old_path in files[BACKUP_MAX_COUNT:]:
        try:
            _remove_backup_with_meta(old_path)
            logger.info("Deleted backup by count-limit: %s", old_path)
        except Exception as exc:
            logger.warning("Failed to delete backup by count-limit %s: %s", old_path, exc)

    # Recompute after count cleanup
    files = [
        os.path.join(backup_dir, name)
        for name in os.listdir(backup_dir)
        if name.lower().endswith(".db") and os.path.isfile(os.path.join(backup_dir, name))
    ]
    files.sort(key=os.path.getmtime, reverse=True)

    total_size = sum(os.path.getsize(path) for path in files)
    if total_size <= BACKUP_MAX_TOTAL_BYTES:
        return

    for old_path in reversed(files):
        if total_size <= BACKUP_MAX_TOTAL_BYTES:
            break
        try:
            size = os.path.getsize(old_path)
            _remove_backup_with_meta(old_path)
            total_size -= size
            logger.info("Deleted backup by size-limit: %s", old_path)
        except Exception as exc:
            logger.warning("Failed to delete backup by size-limit %s: %s", old_path, exc)


def _remove_backup_with_meta(db_path: str):
    os.remove(db_path)
    meta_path = f"{db_path}.meta.json"
    if os.path.exists(meta_path):
        os.remove(meta_path)


def _prune_change_log_and_maybe_compact(db_path: str, now: datetime):
    if not os.path.exists(db_path):
        return None

    lock = FileWriteLock(DB_LOCK_PATH, stale_timeout_sec=10 * 60, logger=logger)
    if not lock.acquire(owner_id=f"{os.getpid()}:backup_cleanup", source="change_log_cleanup"):
        logger.warning("Change-log maintenance skipped: db.lock is busy.")
        return None

    conn = None
    deleted_rows = 0
    rows_before = 0
    rows_after = 0
    compacted = False

    try:
        conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None, timeout=2.0)
        configure_connection(conn)

        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='change_log'"
        ).fetchone()
        if not exists:
            return None

        rows_before = int(conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0] or 0)
        if rows_before <= 0:
            return {
                "rows_before": 0,
                "rows_after": 0,
                "deleted_rows": 0,
                "compacted": False,
            }

        cutoff_by_age = 0
        if CHANGE_LOG_RETENTION_DAYS > 0:
            cutoff_dt = now - timedelta(days=CHANGE_LOG_RETENTION_DAYS)
            cutoff_str = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")
            cutoff_by_age = int(
                conn.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM change_log WHERE DATETIME(changed_at) < DATETIME(?)",
                    (cutoff_str,),
                ).fetchone()[0]
                or 0
            )

        cutoff_by_count = 0
        if CHANGE_LOG_MAX_ROWS > 0 and rows_before > CHANGE_LOG_MAX_ROWS:
            overflow = rows_before - CHANGE_LOG_MAX_ROWS
            row = conn.execute(
                "SELECT id FROM change_log ORDER BY id ASC LIMIT 1 OFFSET ?",
                (max(0, overflow - 1),),
            ).fetchone()
            cutoff_by_count = int(row[0]) if row and row[0] is not None else 0

        cutoff_id = max(cutoff_by_age, cutoff_by_count)
        if cutoff_id <= 0:
            rows_after = rows_before
            return {
                "rows_before": rows_before,
                "rows_after": rows_after,
                "deleted_rows": 0,
                "compacted": False,
            }

        conn.execute("BEGIN IMMEDIATE")
        while True:
            cursor = conn.execute(
                """
                DELETE FROM change_log
                WHERE id IN (
                    SELECT id FROM change_log
                    WHERE id <= ?
                    ORDER BY id ASC
                    LIMIT ?
                )
                """,
                (cutoff_id, CHANGE_LOG_PRUNE_BATCH),
            )
            changed = int(cursor.rowcount or 0)
            if changed <= 0:
                break
            deleted_rows += changed
            if changed < CHANGE_LOG_PRUNE_BATCH:
                break
        conn.execute("COMMIT")

        rows_after = int(conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0] or 0)

        if deleted_rows > 0:
            compacted = _maybe_compact_db(conn, db_path, now)

        return {
            "rows_before": rows_before,
            "rows_after": rows_after,
            "deleted_rows": deleted_rows,
            "compacted": compacted,
        }
    except sqlite3.OperationalError as exc:
        if conn and conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
        if _is_locked_error(exc):
            logger.warning("Change-log maintenance skipped due to DB lock: %s", exc)
            return None
        raise
    finally:
        if conn:
            conn.close()
        lock.release()


def _maybe_compact_db(conn: sqlite3.Connection, db_path: str, now: datetime) -> bool:
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0] or 0)
    freelist_count = int(conn.execute("PRAGMA freelist_count").fetchone()[0] or 0)
    free_bytes = page_size * freelist_count

    if free_bytes < CHANGE_LOG_COMPACT_MIN_FREE_BYTES:
        return False
    if not _compact_due(now):
        return False

    try:
        before_size = os.path.getsize(db_path)
        conn.execute("VACUUM")
        after_size = os.path.getsize(db_path)
        _mark_compact_ts(now)
        logger.info(
            "SQLite compacted after change-log prune: before=%s MB, after=%s MB",
            round(before_size / (1024 * 1024), 2),
            round(after_size / (1024 * 1024), 2),
        )
        return True
    except sqlite3.OperationalError as exc:
        if _is_locked_error(exc):
            logger.warning("SQLite compact skipped due to lock: %s", exc)
            return False
        logger.warning("SQLite compact failed: %s", exc)
        return False


def _compact_due(now: datetime) -> bool:
    try:
        if not os.path.exists(CHANGE_LOG_COMPACT_STAMP_PATH):
            return True
        with open(CHANGE_LOG_COMPACT_STAMP_PATH, "r", encoding="utf-8") as fh:
            raw = fh.read().strip()
        last_ts = float(raw)
        return (time.time() - last_ts) >= CHANGE_LOG_COMPACT_MIN_INTERVAL_SEC
    except Exception:
        return True


def _mark_compact_ts(now: datetime):
    try:
        with open(CHANGE_LOG_COMPACT_STAMP_PATH, "w", encoding="utf-8") as fh:
            fh.write(str(time.time()))
    except Exception as exc:
        logger.debug("Failed to persist compact timestamp: %s", exc)


def _is_locked_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in LOCKED_ERROR_MARKERS)
