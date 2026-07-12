from __future__ import annotations

import os
import sqlite3
import threading
import time

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import QApplication, QLabel

from rem_card.data.settings.settings_db import SettingsDatabase
from rem_card.services.settings import settings_service as settings_module
from rem_card.services.settings.settings_service import OPERBLOCK_ICONS_KEY, SettingsService
from rem_card.ui.shared import operblock_icon_settings, remcard_icon_settings
from rem_card.ui.shared.async_icon_loader import get_async_icon_loader, request_async_icon
from rem_card.ui.shared.pixmap_lru import (
    BoundedBlobRecordCache,
    BoundedPixmapCache,
    decode_pixmap_from_data,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    return app


def _png_bytes(width: int = 16, height: int = 10, color: str = "#3366cc") -> bytes:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(QColor(color))
    data = QByteArray()
    buffer = QBuffer(data)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def _wait_for(qapp, predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    qapp.processEvents()
    return bool(predicate())


def test_scaled_reader_decode_keeps_requested_aspect(qapp):
    image = QImage(160, 100, QImage.Format.Format_RGB32)
    image.fill(QColor("#3366cc"))
    data = QByteArray()
    buffer = QBuffer(data)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "JPEG", 90)
    buffer.close()

    pixmap = decode_pixmap_from_data(
        bytes(data),
        target_size=QSize(16, 16),
        aspect_mode=Qt.AspectRatioMode.KeepAspectRatio,
        transformation_mode=Qt.TransformationMode.SmoothTransformation,
    )

    assert not pixmap.isNull()
    assert (pixmap.width(), pixmap.height()) == (16, 10)


def test_operblock_pixmap_cache_is_size_aware_and_invalidated(qapp, monkeypatch):
    class FakeSettingsService:
        def __init__(self):
            self.version = (1, "hash-1")
            self.metadata_calls = 0
            self.blob_calls = 0
            self.blob = _png_bytes()

        def get_operblock_icon_metadata_snapshot(self, **_kwargs):
            self.metadata_calls += 1
            return self.version, {
                "type:test": {
                    "icon_key": "type:test",
                    "category": "type",
                    "name": "Test",
                    "image_hash": self.version[1],
                    "has_image_blob": True,
                    "value": {"source_file": "test.png"},
                }
            }

        def get_operblock_icon_records(self, keys, **_kwargs):
            self.blob_calls += 1
            assert keys == ["type:test"]
            return self.version, {
                "type:test": {
                    "icon_key": "type:test",
                    "category": "type",
                    "name": "Test",
                    "image_hash": self.version[1],
                    "has_image_blob": True,
                    "image_blob": self.blob,
                    "value": {"source_file": "test.png"},
                }
            }

    fake = FakeSettingsService()
    previous = settings_module._DEFAULT_SERVICE
    settings_module._DEFAULT_SERVICE = fake
    operblock_icon_settings.invalidate_operblock_icon_cache()
    try:
        first = operblock_icon_settings.load_operblock_icon_pixmap("type:test", target_size=(8, 8))
        second = operblock_icon_settings.load_operblock_icon_pixmap("type:test", target_size=(8, 8))
        assert not first.isNull()
        assert (first.width(), first.height()) == (8, 5)
        assert second.cacheKey() == first.cacheKey()
        assert fake.metadata_calls == 1
        assert fake.blob_calls == 1

        fake.version = (2, "hash-2")
        fake.blob = _png_bytes(color="#cc6633")
        monkeypatch.setattr(operblock_icon_settings, "_ICON_VERSION_TTL_SEC", 0.0)
        updated = operblock_icon_settings.load_operblock_icon_pixmap("type:test", target_size=(6, 6))
        assert (updated.width(), updated.height()) == (6, 3)
        assert fake.metadata_calls == 2
        assert fake.blob_calls == 2

        operblock_icon_settings.invalidate_operblock_icon_cache()
        operblock_icon_settings.load_operblock_icon_pixmap("type:test", target_size=(6, 6))
        assert fake.metadata_calls == 3
        assert fake.blob_calls == 3
    finally:
        operblock_icon_settings.invalidate_operblock_icon_cache()
        settings_module._DEFAULT_SERVICE = previous


def test_bounded_pixmap_lru_evicts_oldest_entry(qapp):
    cache = BoundedPixmapCache(max_entries=2, max_bytes=1024 * 1024, ttl_sec=60)
    for key in ("one", "two", "three"):
        pixmap = QPixmap(8, 8)
        pixmap.fill(QColor("red"))
        cache.put(key, pixmap)
    assert len(cache) == 2
    assert cache.get("one") is None
    assert cache.get("two") is not None
    assert cache.get("three") is not None


def test_bounded_blob_record_lru_enforces_byte_budget():
    cache = BoundedBlobRecordCache(max_entries=4, max_bytes=6, ttl_sec=60)

    assert cache.put("one", {"icon_key": "one", "image_blob": b"1234"})
    assert cache.put("two", {"icon_key": "two", "image_blob": b"5678"})

    assert len(cache) == 1
    assert cache.size_bytes == 4
    assert cache.get("one") == (False, None)
    found, record = cache.get("two")
    assert found is True
    assert record["image_blob"] == b"5678"

    assert cache.put("too-large", {"image_blob": b"1234567"}) is False
    assert cache.size_bytes <= cache.max_bytes
    assert cache.get("too-large") == (False, None)


def test_operblock_blob_record_cache_evicts_by_encoded_bytes(qapp, monkeypatch):
    first_blob = _png_bytes(width=18, height=10, color="#3366cc")
    second_blob = _png_bytes(width=18, height=10, color="#cc6633")
    version = (1, "bounded")

    class FakeSettingsService:
        def get_operblock_icon_metadata_snapshot(self, **_kwargs):
            return version, {
                key: {
                    "icon_key": key,
                    "category": "type",
                    "image_hash": key,
                    "has_image_blob": True,
                    "value": {},
                }
                for key in ("type:first", "type:second")
            }

        def get_operblock_icon_records(self, keys, **_kwargs):
            key = keys[0]
            blob = first_blob if key == "type:first" else second_blob
            return version, {
                key: {
                    "icon_key": key,
                    "category": "type",
                    "image_hash": key,
                    "has_image_blob": True,
                    "image_blob": blob,
                    "value": {},
                }
            }

    budget = max(len(first_blob), len(second_blob))
    bounded_cache = BoundedBlobRecordCache(max_entries=8, max_bytes=budget, ttl_sec=60)
    monkeypatch.setattr(operblock_icon_settings, "_BLOB_RECORD_CACHE", bounded_cache)
    previous = settings_module._DEFAULT_SERVICE
    settings_module._DEFAULT_SERVICE = FakeSettingsService()
    operblock_icon_settings.invalidate_operblock_icon_cache()
    try:
        assert not operblock_icon_settings.load_operblock_icon_pixmap(
            "type:first", target_size=(9, 9)
        ).isNull()
        assert not operblock_icon_settings.load_operblock_icon_pixmap(
            "type:second", target_size=(9, 9)
        ).isNull()

        assert bounded_cache.size_bytes <= budget
        assert len(bounded_cache) == 1
        assert bounded_cache.get((version, "type:first")) == (False, None)
        assert bounded_cache.get((version, "type:second"))[0] is True
    finally:
        operblock_icon_settings.invalidate_operblock_icon_cache()
        settings_module._DEFAULT_SERVICE = previous


def test_remcard_pixmap_cache_is_size_aware(qapp):
    class FakeSettingsService:
        def __init__(self):
            self.version = (1, "remcard-hash")
            self.metadata_calls = 0
            self.blob_calls = 0
            self.blob = _png_bytes(width=18, height=10)

        def get_operblock_icon_metadata_snapshot(self, **kwargs):
            self.metadata_calls += 1
            assert kwargs.get("remcard_only") is True
            return self.version, {
                "remcard:test": {
                    "icon_key": "remcard:test",
                    "category": "remcard_patient",
                    "image_hash": self.version[1],
                    "has_image_blob": True,
                    "value": {},
                }
            }

        def get_operblock_icon_records(self, keys, **kwargs):
            self.blob_calls += 1
            assert keys == ["remcard:test"]
            assert kwargs.get("ensure_defaults") is False
            return self.version, {
                "remcard:test": {
                    "icon_key": "remcard:test",
                    "category": "remcard_patient",
                    "image_hash": self.version[1],
                    "has_image_blob": True,
                    "image_blob": self.blob,
                    "value": {},
                }
            }

    fake = FakeSettingsService()
    previous = settings_module._DEFAULT_SERVICE
    settings_module._DEFAULT_SERVICE = fake
    remcard_icon_settings.invalidate_remcard_icon_cache()
    try:
        first = remcard_icon_settings.load_remcard_icon_pixmap(
            "remcard:test",
            target_size=(9, 9),
        )
        second = remcard_icon_settings.load_remcard_icon_pixmap(
            "remcard:test",
            target_size=(9, 9),
        )

        assert not first.isNull()
        assert (first.width(), first.height()) == (9, 5)
        assert second.cacheKey() == first.cacheKey()
        assert fake.metadata_calls == 1
        assert fake.blob_calls == 1
    finally:
        remcard_icon_settings.invalidate_remcard_icon_cache()
        settings_module._DEFAULT_SERVICE = previous


def test_visible_operblock_icon_io_is_async_and_deduplicated(qapp):
    metadata_started = threading.Event()
    allow_metadata = threading.Event()
    worker_threads: list[str] = []

    class FakeSettingsService:
        def __init__(self):
            self.metadata_calls = 0
            self.blob_calls = 0
            self.blob = _png_bytes(width=20, height=10, color="#00aa44")

        def get_operblock_icon_metadata_snapshot(self, **_kwargs):
            self.metadata_calls += 1
            worker_threads.append(threading.current_thread().name)
            metadata_started.set()
            assert allow_metadata.wait(3.0)
            return (1, "async"), {
                "type:async": {
                    "icon_key": "type:async",
                    "category": "type",
                    "image_hash": "async",
                    "has_image_blob": True,
                    "value": {},
                }
            }

        def get_operblock_icon_records(self, keys, **_kwargs):
            self.blob_calls += 1
            worker_threads.append(threading.current_thread().name)
            return (1, "async"), {
                keys[0]: {
                    "icon_key": keys[0],
                    "category": "type",
                    "image_hash": "async",
                    "has_image_blob": True,
                    "image_blob": self.blob,
                    "value": {},
                }
            }

    fake = FakeSettingsService()
    previous = settings_module._DEFAULT_SERVICE
    settings_module._DEFAULT_SERVICE = fake
    operblock_icon_settings.invalidate_operblock_icon_cache()
    first = QLabel()
    second = QLabel()
    apply_threads: list[str] = []

    def apply_first(label, pixmap):
        apply_threads.append(threading.current_thread().name)
        label.setPixmap(pixmap)

    try:
        started = time.perf_counter()
        immediate = operblock_icon_settings.request_operblock_icon_pixmap(
            first,
            "type:async",
            target_size=(10, 10),
            apply=apply_first,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        assert immediate.isNull()
        assert elapsed_ms < 50.0
        assert metadata_started.wait(3.0)

        operblock_icon_settings.request_operblock_icon_pixmap(
            second,
            "type:async",
            target_size=(10, 10),
        )
        allow_metadata.set()
        assert _wait_for(
            qapp,
            lambda: not first.pixmap().isNull() and not second.pixmap().isNull(),
        )
        assert (first.pixmap().width(), first.pixmap().height()) == (10, 5)
        assert fake.metadata_calls == 1
        assert fake.blob_calls == 1
        assert worker_threads
        assert threading.current_thread().name not in worker_threads
        assert apply_threads == [threading.current_thread().name]
        assert get_async_icon_loader().pending_count == 0
    finally:
        allow_metadata.set()
        assert _wait_for(qapp, lambda: get_async_icon_loader().pending_count == 0)
        operblock_icon_settings.invalidate_operblock_icon_cache()
        settings_module._DEFAULT_SERVICE = previous


def test_async_icon_result_does_not_overwrite_reused_label(qapp):
    first_blob_started = threading.Event()
    allow_first_blob = threading.Event()

    class FakeSettingsService:
        def get_operblock_icon_metadata_snapshot(self, **_kwargs):
            return (1, "token"), {
                key: {
                    "icon_key": key,
                    "category": "type",
                    "image_hash": key,
                    "has_image_blob": True,
                    "value": {},
                }
                for key in ("type:first", "type:second")
            }

        def get_operblock_icon_records(self, keys, **_kwargs):
            key = keys[0]
            if key == "type:first":
                first_blob_started.set()
                assert allow_first_blob.wait(3.0)
                color = "#cc2222"
            else:
                color = "#2255cc"
            return (1, "token"), {
                key: {
                    "icon_key": key,
                    "category": "type",
                    "image_hash": key,
                    "has_image_blob": True,
                    "image_blob": _png_bytes(8, 8, color),
                    "value": {},
                }
            }

    previous = settings_module._DEFAULT_SERVICE
    settings_module._DEFAULT_SERVICE = FakeSettingsService()
    operblock_icon_settings.invalidate_operblock_icon_cache()
    label = QLabel()
    try:
        cached_second = operblock_icon_settings.load_operblock_icon_pixmap(
            "type:second",
            target_size=8,
        )
        assert not cached_second.isNull()
        operblock_icon_settings.request_operblock_icon_pixmap(label, "type:first", target_size=8)
        assert first_blob_started.wait(3.0)
        immediate_second = operblock_icon_settings.request_operblock_icon_pixmap(
            label,
            "type:second",
            target_size=8,
        )
        assert immediate_second.cacheKey() == cached_second.cacheKey()
        label.setPixmap(immediate_second)
        assert label.pixmap().toImage().pixelColor(0, 0).blue() > 150

        allow_first_blob.set()
        assert _wait_for(qapp, lambda: get_async_icon_loader().pending_count == 0)
        assert label.pixmap().toImage().pixelColor(0, 0).blue() > 150
    finally:
        allow_first_blob.set()
        assert _wait_for(qapp, lambda: get_async_icon_loader().pending_count == 0)
        operblock_icon_settings.invalidate_operblock_icon_cache()
        settings_module._DEFAULT_SERVICE = previous


def test_cold_visible_icons_share_metadata_refresh_and_bounded_workers(qapp):
    keys = tuple(f"type:cold-{index}" for index in range(12))
    lock = threading.Lock()

    class FakeSettingsService:
        def __init__(self):
            self.metadata_calls = 0
            self.blob_calls = 0
            self.worker_names: set[str] = set()

        def get_operblock_icon_metadata_snapshot(self, **_kwargs):
            with lock:
                self.metadata_calls += 1
                self.worker_names.add(threading.current_thread().name)
            time.sleep(0.04)
            return (1, "cold"), {
                key: {
                    "icon_key": key,
                    "category": "type",
                    "image_hash": key,
                    "has_image_blob": True,
                    "value": {},
                }
                for key in keys
            }

        def get_operblock_icon_records(self, requested, **_kwargs):
            key = requested[0]
            with lock:
                self.blob_calls += 1
                self.worker_names.add(threading.current_thread().name)
            return (1, "cold"), {
                key: {
                    "icon_key": key,
                    "category": "type",
                    "image_hash": key,
                    "has_image_blob": True,
                    "image_blob": _png_bytes(12, 12, "#338855"),
                    "value": {},
                }
            }

    fake = FakeSettingsService()
    previous = settings_module._DEFAULT_SERVICE
    settings_module._DEFAULT_SERVICE = fake
    operblock_icon_settings.invalidate_operblock_icon_cache()
    labels = [QLabel() for _key in keys]
    try:
        for label, key in zip(labels, keys):
            operblock_icon_settings.request_operblock_icon_pixmap(label, key, target_size=8)
        assert _wait_for(qapp, lambda: all(not label.pixmap().isNull() for label in labels), timeout=5.0)
        assert fake.metadata_calls == 1
        assert fake.blob_calls == len(keys)
        assert 1 <= len(fake.worker_names) <= 2
        assert all(name.startswith("AsyncIconWorker-") for name in fake.worker_names)
    finally:
        assert _wait_for(qapp, lambda: get_async_icon_loader().pending_count == 0)
        operblock_icon_settings.invalidate_operblock_icon_cache()
        settings_module._DEFAULT_SERVICE = previous


def test_same_icon_two_sizes_share_one_blob_query(qapp):
    blob_started = threading.Event()
    allow_blob = threading.Event()

    class FakeSettingsService:
        def __init__(self):
            self.metadata_calls = 0
            self.blob_calls = 0

        def get_operblock_icon_metadata_snapshot(self, **_kwargs):
            self.metadata_calls += 1
            return (1, "two-sizes"), {
                "type:shared": {
                    "icon_key": "type:shared",
                    "category": "type",
                    "image_hash": "shared",
                    "has_image_blob": True,
                    "value": {},
                }
            }

        def get_operblock_icon_records(self, requested, **_kwargs):
            self.blob_calls += 1
            blob_started.set()
            assert allow_blob.wait(3.0)
            return (1, "two-sizes"), {
                requested[0]: {
                    "icon_key": requested[0],
                    "category": "type",
                    "image_hash": "shared",
                    "has_image_blob": True,
                    "image_blob": _png_bytes(20, 10, "#8844cc"),
                    "value": {},
                }
            }

    fake = FakeSettingsService()
    previous = settings_module._DEFAULT_SERVICE
    settings_module._DEFAULT_SERVICE = fake
    operblock_icon_settings.invalidate_operblock_icon_cache()
    small, large = QLabel(), QLabel()
    try:
        operblock_icon_settings.request_operblock_icon_pixmap(small, "type:shared", target_size=8)
        assert blob_started.wait(3.0)
        operblock_icon_settings.request_operblock_icon_pixmap(large, "type:shared", target_size=14)
        allow_blob.set()
        assert _wait_for(qapp, lambda: not small.pixmap().isNull() and not large.pixmap().isNull())
        assert fake.metadata_calls == 1
        assert fake.blob_calls == 1
    finally:
        allow_blob.set()
        assert _wait_for(qapp, lambda: get_async_icon_loader().pending_count == 0)
        operblock_icon_settings.invalidate_operblock_icon_cache()
        settings_module._DEFAULT_SERVICE = previous


def test_invalidation_discards_prepared_old_icon_before_gui_finalize(qapp):
    prepared = {
        "image": QImage.fromData(_png_bytes(8, 8, "#cc2222")),
        "request_epoch": int(operblock_icon_settings._ICON_REQUEST_EPOCH),
        "cache_key": ("stale-finalize",),
    }
    prepared_ready = threading.Event()

    def load_prepared():
        prepared_ready.set()
        return prepared

    label = QLabel()
    request_async_icon(
        label,
        ("stale-finalize", time.monotonic_ns()),
        load_prepared,
        operblock_icon_settings._finalize_operblock_icon_image,
    )
    assert prepared_ready.wait(3.0)
    operblock_icon_settings.invalidate_operblock_icon_cache()
    assert _wait_for(qapp, lambda: get_async_icon_loader().pending_count == 0)
    assert label.pixmap().isNull()
    assert operblock_icon_settings._PIXMAP_CACHE.get(("stale-finalize",)) is None


def test_dedup_waiters_are_bounded_and_one_apply_error_does_not_block_next(qapp):
    load_started = threading.Event()
    allow_load = threading.Event()
    first = QLabel()
    error_receiver = QLabel()
    second = QLabel()
    request_key = ("generic-waiters", time.monotonic_ns())
    image = QImage.fromData(_png_bytes(8, 8, "#2288cc"))

    def load():
        load_started.set()
        assert allow_load.wait(3.0)
        return image

    request_async_icon(first, request_key, load, lambda value: QPixmap.fromImage(value))
    assert load_started.wait(3.0)
    for _index in range(30):
        request_async_icon(first, request_key, load, lambda value: QPixmap.fromImage(value))
    request_async_icon(
        error_receiver,
        request_key,
        load,
        lambda value: QPixmap.fromImage(value),
        lambda _target, _pixmap: (_ for _ in ()).throw(ValueError("apply failed")),
    )
    request_async_icon(second, request_key, load, lambda value: QPixmap.fromImage(value))
    inflight = get_async_icon_loader()._inflight[request_key]
    assert len(inflight.waiters) == 3
    allow_load.set()
    assert _wait_for(qapp, lambda: not second.pixmap().isNull())


def test_about_to_quit_clears_pixmaps_and_loader_can_be_recreated(qapp):
    pixmap = QPixmap(8, 8)
    pixmap.fill(QColor("red"))
    operblock_icon_settings._PIXMAP_CACHE.put("lifecycle", pixmap)
    remcard_icon_settings._PIXMAP_CACHE.put("lifecycle", pixmap)
    operblock_icon_settings._ensure_pixmap_cleanup_hook()
    remcard_icon_settings._ensure_pixmap_cleanup_hook()
    old_loader = get_async_icon_loader()

    operblock_icon_settings._on_pixmap_application_quit()
    remcard_icon_settings._on_pixmap_application_quit()
    old_loader.cancel_all()

    assert len(operblock_icon_settings._PIXMAP_CACHE) == 0
    assert len(remcard_icon_settings._PIXMAP_CACHE) == 0
    assert old_loader.is_active is False
    new_loader = get_async_icon_loader()
    assert new_loader is not old_loader
    assert new_loader.is_active is True


def test_icon_service_metadata_queries_and_catalog_hash_do_not_read_blob(tmp_path, monkeypatch):
    monkeypatch.setenv("REMCARD_UI_ROLE", "doctor")
    db = SettingsDatabase(baza_dir=str(tmp_path / "Baza"))
    service = SettingsService(db)
    service.ensure_ready()

    version, metadata = service.get_operblock_icon_metadata_snapshot(ensure_defaults=False)
    assert isinstance(version, tuple)
    assert all("image_blob" not in record for record in metadata.values())

    remcard_records = service.list_remcard_icons(include_blob=False)
    assert all(
        key.startswith("remcard:") or str(record.get("category") or "").startswith("remcard")
        for key, record in remcard_records.items()
    )
    assert all("image_blob" not in record for record in remcard_records.values())

    read_columns: list[tuple[str, str]] = []
    with service.db.read_connection() as conn:
        def authorizer(action, table, column, _database, _trigger):
            if action == sqlite3.SQLITE_READ:
                read_columns.append((str(table), str(column)))
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorizer)
        service._compute_catalog_hash(conn, OPERBLOCK_ICONS_KEY)
    assert ("operblock_icons", "image_blob") not in read_columns


def test_icon_upload_rejects_excessive_decoded_dimension(tmp_path, qapp):
    path = tmp_path / "too-wide.png"
    image = QImage(4097, 1, QImage.Format.Format_ARGB32)
    image.fill(QColor("blue"))
    assert image.save(os.fspath(path), "PNG")
    with pytest.raises(ValueError, match="слишком большое"):
        SettingsService._read_operblock_icon_image(os.fspath(path))
