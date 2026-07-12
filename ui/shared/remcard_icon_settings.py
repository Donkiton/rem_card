from __future__ import annotations

import hashlib
import os
import threading
import time
from typing import Callable, Iterable

from PySide6.QtCore import QCoreApplication, QSize, Qt, QThread
from PySide6.QtGui import QImage, QPixmap

from rem_card.app.paths import get_icon_dir
from rem_card.services.remcard_icon_defaults import default_remcard_icon_file_for_key
from rem_card.ui.shared.async_icon_loader import begin_async_icon_request, request_async_icon
from rem_card.ui.shared.pixmap_lru import (
    BoundedBlobRecordCache,
    BoundedPixmapCache,
    decode_image_from_data,
    decode_pixmap_from_data,
)


def _float_env(name: str, default: float, *, minimum: float) -> float:
    try:
        return max(minimum, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return max(minimum, float(default))


_ICON_VERSION_TTL_SEC = _float_env("REMCARD_ICON_VERSION_TTL_SEC", 2.0, minimum=0.25)
_ICON_RECORD_CACHE: dict[str, object] = {
    "version": None,
    "checked_at": 0.0,
    "records": {},
}
_ICON_CACHE_LOCK = threading.RLock()
_METADATA_CONDITION = threading.Condition(_ICON_CACHE_LOCK)
_METADATA_LOADING = False
_BLOB_LOAD_CONDITION = threading.Condition(threading.RLock())
_BLOB_LOADING: set[tuple[object, str]] = set()
_ICON_REQUEST_EPOCH = 0
_PIXMAP_CACHE = BoundedPixmapCache(
    max_entries=48,
    max_bytes=24 * 1024 * 1024,
    ttl_sec=_float_env("REMCARD_ICON_PIXMAP_CACHE_TTL_SEC", 300.0, minimum=5.0),
)
_BLOB_RECORD_CACHE = BoundedBlobRecordCache(
    max_entries=8,
    max_bytes=8 * 1024 * 1024,
    ttl_sec=_float_env("REMCARD_ICON_BLOB_CACHE_TTL_SEC", 300.0, minimum=5.0),
)
_PIXMAP_CLEANUP_APP = None


def _clear_pixmap_cache_if_gui_thread() -> bool:
    app = QCoreApplication.instance()
    if app is None or QThread.currentThread() != app.thread():
        return False
    _PIXMAP_CACHE.clear()
    return True


def _on_pixmap_application_quit() -> None:
    global _PIXMAP_CLEANUP_APP
    _clear_pixmap_cache_if_gui_thread()
    _PIXMAP_CLEANUP_APP = None


def _ensure_pixmap_cleanup_hook() -> None:
    global _PIXMAP_CLEANUP_APP
    app = QCoreApplication.instance()
    if app is None or QThread.currentThread() != app.thread():
        return
    if _PIXMAP_CLEANUP_APP is app:
        return
    app.aboutToQuit.connect(_on_pixmap_application_quit)
    _PIXMAP_CLEANUP_APP = app


def invalidate_remcard_icon_cache() -> None:
    global _ICON_REQUEST_EPOCH
    with _ICON_CACHE_LOCK:
        _ICON_REQUEST_EPOCH += 1
        _ICON_RECORD_CACHE["version"] = None
        _ICON_RECORD_CACHE["checked_at"] = 0.0
        _ICON_RECORD_CACHE["records"] = {}
    _BLOB_RECORD_CACHE.clear()
    _clear_pixmap_cache_if_gui_thread()


def _normalized_icon_keys(icon_keys: str | Iterable[str]) -> list[str]:
    if isinstance(icon_keys, str):
        return [icon_keys] if icon_keys.strip() else []
    result: list[str] = []
    for key in icon_keys or []:
        text = str(key or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _load_icon_metadata_from_db(
    *,
    expected_epoch: int | None = None,
) -> tuple[tuple[int, str] | None, dict[str, dict]]:
    global _METADATA_LOADING, _ICON_REQUEST_EPOCH
    now = time.monotonic()
    with _METADATA_CONDITION:
        current_epoch = int(_ICON_REQUEST_EPOCH)
        if expected_epoch is not None and int(expected_epoch) != current_epoch:
            records = _ICON_RECORD_CACHE.get("records")
            return _ICON_RECORD_CACHE.get("version"), dict(records) if isinstance(records, dict) else {}
        version = _ICON_RECORD_CACHE.get("version")
        checked_at = float(_ICON_RECORD_CACHE.get("checked_at") or 0.0)
        records = _ICON_RECORD_CACHE.get("records")
        if checked_at > 0.0 and now - checked_at < _ICON_VERSION_TTL_SEC:
            return version, dict(records) if isinstance(records, dict) else {}
        if _METADATA_LOADING:
            deadline = now + 10.0
            while _METADATA_LOADING:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                _METADATA_CONDITION.wait(remaining)
            current_epoch = int(_ICON_REQUEST_EPOCH)
            records = _ICON_RECORD_CACHE.get("records")
            if expected_epoch is not None and int(expected_epoch) != current_epoch:
                return _ICON_RECORD_CACHE.get("version"), dict(records) if isinstance(records, dict) else {}
            if not _METADATA_LOADING and float(_ICON_RECORD_CACHE.get("checked_at") or 0.0) > 0.0:
                return _ICON_RECORD_CACHE.get("version"), dict(records) if isinstance(records, dict) else {}
            if _METADATA_LOADING:
                return _ICON_RECORD_CACHE.get("version"), dict(records) if isinstance(records, dict) else {}
        _METADATA_LOADING = True
        load_epoch = int(_ICON_REQUEST_EPOCH)

    try:
        from rem_card.services.settings.settings_service import get_settings_service

        version, records = get_settings_service().get_operblock_icon_metadata_snapshot(
            ensure_defaults=False,
            remcard_only=True,
        )
        records = dict(records or {})
    except Exception:
        with _METADATA_CONDITION:
            if load_epoch == int(_ICON_REQUEST_EPOCH):
                _ICON_RECORD_CACHE["checked_at"] = time.monotonic()
            _METADATA_LOADING = False
            _METADATA_CONDITION.notify_all()
            records = _ICON_RECORD_CACHE.get("records")
            return _ICON_RECORD_CACHE.get("version"), dict(records) if isinstance(records, dict) else {}

    changed = False
    with _METADATA_CONDITION:
        if load_epoch != int(_ICON_REQUEST_EPOCH):
            _METADATA_LOADING = False
            _METADATA_CONDITION.notify_all()
            current_records = _ICON_RECORD_CACHE.get("records")
            return _ICON_RECORD_CACHE.get("version"), dict(current_records) if isinstance(current_records, dict) else {}
        previous = _ICON_RECORD_CACHE.get("version")
        changed = previous is not None and previous != version
        if changed:
            _ICON_REQUEST_EPOCH += 1
        _ICON_RECORD_CACHE["version"] = version
        _ICON_RECORD_CACHE["checked_at"] = time.monotonic()
        _ICON_RECORD_CACHE["records"] = records
        _METADATA_LOADING = False
        _METADATA_CONDITION.notify_all()
    if changed:
        _BLOB_RECORD_CACHE.clear()
    return version, dict(records)


def _metadata_icon_record_from_records(normalized_keys: list[str], records: dict[str, dict]) -> dict | None:
    for key in normalized_keys:
        record = records.get(key)
        if isinstance(record, dict) and record.get("has_image_blob"):
            return record
    return None


def _metadata_icon_record(
    icon_keys: str | Iterable[str],
    *,
    expected_epoch: int | None = None,
) -> tuple[tuple[int, str] | None, dict | None]:
    version, records = _load_icon_metadata_from_db(expected_epoch=expected_epoch)
    return version, _metadata_icon_record_from_records(_normalized_icon_keys(icon_keys), records)


def _resolved_icon_record(
    icon_keys: str | Iterable[str],
    *,
    expected_epoch: int | None = None,
) -> tuple[tuple[int, str] | None, dict | None]:
    global _ICON_REQUEST_EPOCH
    version, metadata = _metadata_icon_record(icon_keys, expected_epoch=expected_epoch)
    if not metadata:
        return version, None
    icon_key = str(metadata.get("icon_key") or "").strip()
    with _ICON_CACHE_LOCK:
        if expected_epoch is not None and int(expected_epoch) != int(_ICON_REQUEST_EPOCH):
            return _ICON_RECORD_CACHE.get("version"), None
    load_key = (version, icon_key)
    with _BLOB_LOAD_CONDITION:
        found, cached = _BLOB_RECORD_CACHE.get(load_key)
        if found:
            return version, cached
        if load_key in _BLOB_LOADING:
            deadline = time.monotonic() + 10.0
            while load_key in _BLOB_LOADING:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                _BLOB_LOAD_CONDITION.wait(remaining)
            found, cached = _BLOB_RECORD_CACHE.get(load_key)
            if found:
                return version, cached
            return _ICON_RECORD_CACHE.get("version"), None
        _BLOB_LOADING.add(load_key)
    try:
        try:
            from rem_card.services.settings.settings_service import get_settings_service

            loaded_version, records = get_settings_service().get_operblock_icon_records(
                [icon_key],
                include_blob=True,
                ensure_defaults=False,
            )
            loaded = records.get(icon_key) if isinstance(records, dict) else None
        except Exception:
            return version, None
        version_mismatch = False
        with _ICON_CACHE_LOCK:
            if expected_epoch is not None and int(expected_epoch) != int(_ICON_REQUEST_EPOCH):
                return _ICON_RECORD_CACHE.get("version"), None
            current_version = _ICON_RECORD_CACHE.get("version")
            if current_version is not None and current_version != loaded_version:
                _ICON_RECORD_CACHE["checked_at"] = 0.0
                _ICON_REQUEST_EPOCH += 1
                version_mismatch = True
            else:
                cached_record = dict(loaded) if isinstance(loaded, dict) else None
                _BLOB_RECORD_CACHE.put((loaded_version, icon_key), cached_record)
        if version_mismatch:
            _BLOB_RECORD_CACHE.clear()
            return loaded_version, None
        return loaded_version, cached_record
    finally:
        with _BLOB_LOAD_CONDITION:
            _BLOB_LOADING.discard(load_key)
            _BLOB_LOAD_CONDITION.notify_all()


def remcard_icon_record(icon_keys: str | Iterable[str]) -> dict | None:
    _version, record = _resolved_icon_record(icon_keys)
    return record


def fallback_icon_path(file_name: str) -> str:
    return os.path.join(get_icon_dir(), os.path.basename(str(file_name or "").strip()))


def _target_size(value) -> QSize | None:
    if value is None:
        return None
    if isinstance(value, QSize):
        result = QSize(value)
    elif isinstance(value, int):
        result = QSize(int(value), int(value))
    else:
        try:
            width, height = value
            result = QSize(int(width), int(height))
        except Exception:
            return None
    return result if result.isValid() and not result.isEmpty() else None


def _enum_cache_key(value):
    return getattr(value, "value", value)


def _db_pixmap_cache_key(
    version,
    record: dict,
    size: QSize | None,
    aspect_mode,
    transformation_mode,
    *,
    request_epoch: int | None = None,
):
    blob = record.get("image_blob")
    image_hash = str(record.get("image_hash") or "")
    if not image_hash and isinstance(blob, (bytes, bytearray, memoryview)):
        image_hash = hashlib.sha256(bytes(blob)).hexdigest()
    return (
        "remcard-db",
        int(_ICON_REQUEST_EPOCH if request_epoch is None else request_epoch),
        version,
        str(record.get("icon_key") or ""),
        image_hash,
        None if size is None else (size.width(), size.height()),
        _enum_cache_key(aspect_mode),
        _enum_cache_key(transformation_mode),
    )


def _cached_db_pixmap(icon_keys, size: QSize | None, aspect_mode, transformation_mode):
    now = time.monotonic()
    with _ICON_CACHE_LOCK:
        version = _ICON_RECORD_CACHE.get("version")
        checked_at = float(_ICON_RECORD_CACHE.get("checked_at") or 0.0)
        records = _ICON_RECORD_CACHE.get("records")
        records = dict(records) if isinstance(records, dict) else {}
    fresh = checked_at > 0.0 and now - checked_at < _ICON_VERSION_TTL_SEC
    metadata = _metadata_icon_record_from_records(_normalized_icon_keys(icon_keys), records)
    if not metadata:
        return None, fresh, False, version
    icon_key = str(metadata.get("icon_key") or "").strip()
    found, record = _BLOB_RECORD_CACHE.get((version, icon_key))
    if not found or not isinstance(record, dict):
        return None, fresh, True, version
    return _PIXMAP_CACHE.get(
        _db_pixmap_cache_key(version, record, size, aspect_mode, transformation_mode)
    ), fresh, True, version


def _prepare_remcard_icon_image(
    icon_keys,
    size: QSize | None,
    aspect_mode,
    transformation_mode,
    request_epoch: int,
):
    with _ICON_CACHE_LOCK:
        if int(request_epoch) != int(_ICON_REQUEST_EPOCH):
            return None
    version, record = _resolved_icon_record(icon_keys, expected_epoch=request_epoch)
    if not isinstance(record, dict):
        return None
    blob = record.get("image_blob")
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        return None
    image = decode_image_from_data(
        blob,
        target_size=size,
        aspect_mode=aspect_mode,
        transformation_mode=transformation_mode,
    )
    if image.isNull():
        return None
    with _ICON_CACHE_LOCK:
        if int(request_epoch) != int(_ICON_REQUEST_EPOCH):
            return None
    return {
        "image": image,
        "request_epoch": int(request_epoch),
        "cache_key": _db_pixmap_cache_key(
            version,
            record,
            size,
            aspect_mode,
            transformation_mode,
            request_epoch=request_epoch,
        ),
    }


def _finalize_remcard_icon_image(prepared) -> QPixmap:
    if not isinstance(prepared, dict):
        return QPixmap()
    image = prepared.get("image")
    if not isinstance(image, QImage) or image.isNull():
        return QPixmap()
    with _ICON_CACHE_LOCK:
        if int(prepared.get("request_epoch", -1)) != int(_ICON_REQUEST_EPOCH):
            return QPixmap()
        pixmap = QPixmap.fromImage(image)
        if not pixmap.isNull():
            _PIXMAP_CACHE.put(prepared.get("cache_key"), pixmap)
    return pixmap


def _scaled(pixmap: QPixmap, size: QSize | None, aspect_mode, transformation_mode) -> QPixmap:
    return pixmap if size is None or pixmap.isNull() else pixmap.scaled(size, aspect_mode, transformation_mode)


def _load_file_pixmap(path: str, *, version, size: QSize | None, aspect_mode, transformation_mode) -> QPixmap:
    try:
        stat = os.stat(path)
        fingerprint = (int(stat.st_size), int(stat.st_mtime_ns))
    except OSError:
        return QPixmap()
    cache_key = (
        "remcard-file",
        version,
        os.path.normcase(os.path.abspath(path)),
        fingerprint,
        None if size is None else (size.width(), size.height()),
        _enum_cache_key(aspect_mode),
        _enum_cache_key(transformation_mode),
    )
    cached = _PIXMAP_CACHE.get(cache_key)
    if cached is not None:
        return cached
    pixmap = _scaled(QPixmap(path), size, aspect_mode, transformation_mode)
    _PIXMAP_CACHE.put(cache_key, pixmap)
    return pixmap


def load_remcard_icon_pixmap(
    icon_keys: str | Iterable[str],
    *,
    fallback_file: str = "",
    target_size=None,
    aspect_mode=Qt.AspectRatioMode.KeepAspectRatio,
    transformation_mode=Qt.TransformationMode.SmoothTransformation,
) -> QPixmap:
    _ensure_pixmap_cleanup_hook()
    size = _target_size(target_size)
    version, record = _resolved_icon_record(icon_keys)
    if record is not None:
        blob = record.get("image_blob")
        if isinstance(blob, (bytes, bytearray)):
            cache_key = _db_pixmap_cache_key(version, record, size, aspect_mode, transformation_mode)
            cached = _PIXMAP_CACHE.get(cache_key)
            if cached is not None:
                return cached
            pixmap = decode_pixmap_from_data(
                blob,
                target_size=size,
                aspect_mode=aspect_mode,
                transformation_mode=transformation_mode,
            )
            if not pixmap.isNull():
                _PIXMAP_CACHE.put(cache_key, pixmap)
                return pixmap

    keys = _normalized_icon_keys(icon_keys)
    fallback = str(fallback_file or "").strip()
    if not fallback and keys:
        fallback = default_remcard_icon_file_for_key(keys[0], "")
    if fallback:
        path = fallback_icon_path(fallback)
        return _load_file_pixmap(
            path,
            version=version,
            size=size,
            aspect_mode=aspect_mode,
            transformation_mode=transformation_mode,
        )
    return QPixmap()


def request_remcard_icon_pixmap(
    receiver,
    icon_keys: str | Iterable[str],
    *,
    fallback_file: str = "",
    target_size=None,
    aspect_mode=Qt.AspectRatioMode.KeepAspectRatio,
    transformation_mode=Qt.TransformationMode.SmoothTransformation,
    apply: Callable[[object, QPixmap], None] | None = None,
) -> QPixmap:
    """Return a local/cached image now and refresh the visible receiver off-thread."""
    _ensure_pixmap_cleanup_hook()
    receiver_token = begin_async_icon_request(receiver)
    size = _target_size(target_size)
    with _ICON_CACHE_LOCK:
        request_epoch = int(_ICON_REQUEST_EPOCH)
    cached, metadata_fresh, has_db_icon, version = _cached_db_pixmap(
        icon_keys,
        size,
        aspect_mode,
        transformation_mode,
    )
    keys = _normalized_icon_keys(icon_keys)
    fallback = str(fallback_file or "").strip()
    if not fallback and keys:
        fallback = default_remcard_icon_file_for_key(keys[0], "")
    immediate = cached
    if immediate is None and fallback:
        immediate = _load_file_pixmap(
            fallback_icon_path(fallback),
            version=version,
            size=size,
            aspect_mode=aspect_mode,
            transformation_mode=transformation_mode,
        )
    if immediate is None:
        immediate = QPixmap()

    if not metadata_fresh or (has_db_icon and cached is None):
        normalized_keys = tuple(keys)
        request_key = (
            "remcard",
            request_epoch,
            normalized_keys,
            fallback,
            None if size is None else (size.width(), size.height()),
            _enum_cache_key(aspect_mode),
            _enum_cache_key(transformation_mode),
        )
        request_async_icon(
            receiver,
            request_key,
            lambda: _prepare_remcard_icon_image(
                normalized_keys,
                size,
                aspect_mode,
                transformation_mode,
                request_epoch,
            ),
            _finalize_remcard_icon_image,
            apply,
            token=receiver_token,
        )
    return immediate


def current_remcard_icon_source(
    icon_keys: str | Iterable[str],
    *,
    fallback_file: str = "",
) -> str:
    _version, record = _metadata_icon_record(icon_keys)
    if record is not None:
        value = record.get("value") if isinstance(record.get("value"), dict) else {}
        source_file = str(value.get("source_file") or "").strip()
        return f"из БД: {source_file or record.get('image_hash') or record.get('icon_key')}"
    keys = _normalized_icon_keys(icon_keys)
    fallback = str(fallback_file or "").strip()
    if not fallback and keys:
        fallback = default_remcard_icon_file_for_key(keys[0], "")
    return fallback or "стандартная иконка"
