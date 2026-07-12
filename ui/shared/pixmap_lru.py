from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Hashable

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize
from PySide6.QtGui import QImage, QImageIOHandler, QImageReader, QPixmap


def decode_image_from_data(
    data: bytes | bytearray | memoryview,
    *,
    target_size: QSize | None,
    aspect_mode,
    transformation_mode,
) -> QImage:
    """Decode an image without touching GUI-thread-only QPixmap state."""
    raw = bytes(data)
    if not raw:
        return QImage()

    if target_size is not None and target_size.isValid() and not target_size.isEmpty():
        encoded = QByteArray(raw)
        buffer = QBuffer(encoded)
        if buffer.open(QIODevice.OpenModeFlag.ReadOnly):
            try:
                reader = QImageReader(buffer)
                source_size = reader.size()
                if (
                    source_size.isValid()
                    and not source_size.isEmpty()
                    and reader.supportsOption(QImageIOHandler.ImageOption.ScaledSize)
                ):
                    decoded_size = source_size.scaled(target_size, aspect_mode)
                    if decoded_size.isValid() and not decoded_size.isEmpty():
                        reader.setScaledSize(decoded_size)
                        image = reader.read()
                        if not image.isNull():
                            if image.size() != decoded_size:
                                image = image.scaled(
                                    decoded_size,
                                    aspect_mode,
                                    transformation_mode,
                                )
                            return image
            finally:
                buffer.close()

    image = QImage.fromData(raw)
    if image.isNull():
        return QImage()
    if target_size is not None and target_size.isValid() and not target_size.isEmpty():
        return image.scaled(target_size, aspect_mode, transformation_mode)
    return image


def decode_pixmap_from_data(
    data: bytes | bytearray | memoryview,
    *,
    target_size: QSize | None,
    aspect_mode,
    transformation_mode,
) -> QPixmap:
    """Decode toward the requested size and create QPixmap on the caller thread."""
    image = decode_image_from_data(
        data,
        target_size=target_size,
        aspect_mode=aspect_mode,
        transformation_mode=transformation_mode,
    )
    return QPixmap.fromImage(image) if not image.isNull() else QPixmap()


@dataclass
class _PixmapEntry:
    pixmap: QPixmap
    size_bytes: int
    expires_at: float


@dataclass
class _BlobRecordEntry:
    record: dict | None
    size_bytes: int
    expires_at: float


class BoundedPixmapCache:
    """Small process-local LRU for GUI-thread pixmaps with a memory budget."""

    def __init__(self, *, max_entries: int = 64, max_bytes: int = 32 * 1024 * 1024, ttl_sec: float = 300.0):
        self.max_entries = max(1, int(max_entries or 1))
        self.max_bytes = max(1024, int(max_bytes or 1024))
        self.ttl_sec = max(1.0, float(ttl_sec or 1.0))
        self._entries: "OrderedDict[Hashable, _PixmapEntry]" = OrderedDict()
        self._size_bytes = 0
        self._lock = threading.RLock()

    @staticmethod
    def _pixmap_size_bytes(pixmap: QPixmap) -> int:
        if pixmap.isNull():
            return 0
        depth_bytes = max(1, int(pixmap.depth() or 32) // 8)
        return max(1, int(pixmap.width()) * int(pixmap.height()) * depth_bytes)

    def get(self, key: Hashable) -> QPixmap | None:
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= now:
                self._remove_locked(key)
                return None
            self._entries.move_to_end(key)
            return QPixmap(entry.pixmap)

    def put(self, key: Hashable, pixmap: QPixmap) -> None:
        if pixmap.isNull():
            return
        stored = QPixmap(pixmap)
        size_bytes = self._pixmap_size_bytes(stored)
        if size_bytes > self.max_bytes:
            return
        with self._lock:
            self._remove_locked(key)
            self._entries[key] = _PixmapEntry(
                pixmap=stored,
                size_bytes=size_bytes,
                expires_at=time.monotonic() + self.ttl_sec,
            )
            self._size_bytes += size_bytes
            while len(self._entries) > self.max_entries or self._size_bytes > self.max_bytes:
                oldest_key = next(iter(self._entries))
                self._remove_locked(oldest_key)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._size_bytes = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def _remove_locked(self, key: Hashable) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._size_bytes = max(0, self._size_bytes - int(entry.size_bytes))


class BoundedBlobRecordCache:
    """Thread-safe LRU for icon records, bounded by encoded BLOB bytes."""

    def __init__(self, *, max_entries: int = 32, max_bytes: int = 16 * 1024 * 1024, ttl_sec: float = 300.0):
        self.max_entries = max(1, int(max_entries or 1))
        self.max_bytes = max(1, int(max_bytes or 1))
        self.ttl_sec = max(1.0, float(ttl_sec or 1.0))
        self._entries: "OrderedDict[Hashable, _BlobRecordEntry]" = OrderedDict()
        self._size_bytes = 0
        self._lock = threading.RLock()

    @staticmethod
    def _copy_record(record: dict | None) -> tuple[dict | None, int]:
        if not isinstance(record, dict):
            return None, 0
        stored = dict(record)
        blob = stored.get("image_blob")
        if isinstance(blob, memoryview):
            blob = blob.tobytes()
            stored["image_blob"] = blob
        elif isinstance(blob, bytearray):
            blob = bytes(blob)
            stored["image_blob"] = blob
        return stored, len(blob) if isinstance(blob, bytes) else 0

    def get(self, key: Hashable) -> tuple[bool, dict | None]:
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return False, None
            if entry.expires_at <= now:
                self._remove_locked(key)
                return False, None
            self._entries.move_to_end(key)
            return True, dict(entry.record) if isinstance(entry.record, dict) else None

    def put(self, key: Hashable, record: dict | None) -> bool:
        stored, size_bytes = self._copy_record(record)
        with self._lock:
            self._remove_locked(key)
            if size_bytes > self.max_bytes:
                return False
            self._entries[key] = _BlobRecordEntry(
                record=stored,
                size_bytes=size_bytes,
                expires_at=time.monotonic() + self.ttl_sec,
            )
            self._size_bytes += size_bytes
            while len(self._entries) > self.max_entries or self._size_bytes > self.max_bytes:
                oldest_key = next(iter(self._entries))
                self._remove_locked(oldest_key)
        return True

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._size_bytes = 0

    @property
    def size_bytes(self) -> int:
        with self._lock:
            return int(self._size_bytes)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def _remove_locked(self, key: Hashable) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._size_bytes = max(0, self._size_bytes - int(entry.size_bytes))
