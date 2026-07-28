from __future__ import annotations

import hashlib
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from rem_card.app.local_metrics import record_metric


_CACHE_SEMAPHORE = threading.BoundedSemaphore(2)
_PENDING_LOCK = threading.Lock()
_PENDING_TARGETS: set[str] = set()
_TARGET_LOCKS_GUARD = threading.Lock()
_TARGET_LOCKS: dict[str, threading.Lock] = {}


def _local_appdata_dir() -> str:
    root = os.environ.get("LOCALAPPDATA")
    if root:
        return os.path.abspath(os.path.normpath(root))
    return os.path.join(os.path.expanduser("~"), "AppData", "Local")


def media_cache_root() -> str:
    override = str(os.environ.get("REMCARD_MEDIA_CACHE_DIR") or "").strip()
    if override:
        return os.path.abspath(os.path.normpath(override))
    return os.path.join(_local_appdata_dir(), "RemCard", "media_cache")


def settings_cache_namespace(settings_db_path: str) -> str:
    normalized = os.path.normcase(
        os.path.abspath(os.path.normpath(str(settings_db_path or "")))
    )
    return hashlib.sha256(normalized.encode("utf-8", errors="surrogatepass")).hexdigest()[:24]


def _safe_kind(kind: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(kind or "")).strip("._")
    return cleaned[:48] or "media"


def _safe_hash(image_hash: str) -> str:
    normalized = str(image_hash or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        return ""
    return normalized


def _safe_extension(source_path: str) -> str:
    extension = Path(str(source_path or "")).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", extension):
        return ".bin"
    return extension


def media_cache_path(
    *,
    settings_db_path: str,
    kind: str,
    image_hash: str,
    source_path: str,
) -> str:
    digest = _safe_hash(image_hash)
    if not digest:
        return ""
    return os.path.join(
        media_cache_root(),
        settings_cache_namespace(settings_db_path),
        _safe_kind(kind),
        f"{digest}{_safe_extension(source_path)}",
    )


def _cached_file_is_usable(
    path: str,
    *,
    expected_size: int | None,
) -> bool:
    try:
        stat_result = os.stat(path)
    except OSError:
        return False
    return (
        stat_result.st_size > 0
        and (
            expected_size in (None, 0)
            or int(stat_result.st_size) == int(expected_size)
        )
    )


def _target_lock(path: str) -> threading.Lock:
    key = os.path.normcase(os.path.abspath(path))
    with _TARGET_LOCKS_GUARD:
        lock = _TARGET_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _TARGET_LOCKS[key] = lock
        return lock


def materialize_media_cache(
    *,
    source_path: str,
    settings_db_path: str,
    kind: str,
    image_hash: str,
    expected_size: int | None = None,
) -> str:
    digest = _safe_hash(image_hash)
    target_path = media_cache_path(
        settings_db_path=settings_db_path,
        kind=kind,
        image_hash=digest,
        source_path=source_path,
    )
    if not target_path:
        return ""
    if _cached_file_is_usable(target_path, expected_size=expected_size):
        return target_path

    started = time.perf_counter()
    lock = _target_lock(target_path)
    with lock:
        if _cached_file_is_usable(target_path, expected_size=expected_size):
            return target_path
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        temp_path = f"{target_path}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        hasher = hashlib.sha256()
        copied = 0
        try:
            with open(source_path, "rb") as source, open(temp_path, "xb") as target:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    target.write(chunk)
                    hasher.update(chunk)
                    copied += len(chunk)
                target.flush()
                os.fsync(target.fileno())
            if hasher.hexdigest() != digest:
                raise ValueError("Хеш сетевого медиафайла не совпадает с БД настроек.")
            if expected_size not in (None, 0) and copied != int(expected_size):
                raise ValueError("Размер сетевого медиафайла не совпадает с БД настроек.")
            os.replace(temp_path, target_path)
            record_metric(
                "settings_media_cache_fill_ms",
                round((time.perf_counter() - started) * 1000.0, 3),
                kind=_safe_kind(kind),
                size_bytes=copied,
            )
            return target_path
        except Exception as exc:
            record_metric(
                "settings_media_cache_fill_failed",
                1,
                kind=_safe_kind(kind),
                error_class=type(exc).__name__,
            )
            return ""
        finally:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass


def schedule_media_cache_fill(
    *,
    source_path: str,
    settings_db_path: str,
    kind: str,
    image_hash: str,
    expected_size: int | None = None,
    on_ready: Callable[[str], None] | None = None,
) -> str:
    target_path = media_cache_path(
        settings_db_path=settings_db_path,
        kind=kind,
        image_hash=image_hash,
        source_path=source_path,
    )
    if not target_path:
        return ""
    if _cached_file_is_usable(target_path, expected_size=expected_size):
        return target_path

    key = os.path.normcase(os.path.abspath(target_path))
    with _PENDING_LOCK:
        if key in _PENDING_TARGETS:
            return ""
        _PENDING_TARGETS.add(key)

    def run() -> None:
        try:
            with _CACHE_SEMAPHORE:
                ready_path = materialize_media_cache(
                    source_path=source_path,
                    settings_db_path=settings_db_path,
                    kind=kind,
                    image_hash=image_hash,
                    expected_size=expected_size,
                )
            if ready_path and on_ready is not None:
                try:
                    on_ready(ready_path)
                except Exception:
                    pass
        finally:
            with _PENDING_LOCK:
                _PENDING_TARGETS.discard(key)

    threading.Thread(
        target=run,
        name=f"RemCardMediaCache-{os.path.basename(target_path)[:12]}",
        daemon=True,
    ).start()
    return ""
