from __future__ import annotations

import hashlib
import atexit
import json
import os
import pickle
import tempfile
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from rem_card.app.logger import logger
from rem_card.app.paths import LOCAL_CACHE_DIR


PERSISTENT_SNAPSHOT_CACHE_ENABLED = os.environ.get("REMCARD_PERSISTENT_SNAPSHOT_CACHE", "1") != "0"
PERSISTENT_SNAPSHOT_CACHE_MAX_FILES = max(
    20,
    int(os.environ.get("REMCARD_PERSISTENT_SNAPSHOT_CACHE_MAX_FILES", "300")),
)
PERSISTENT_SNAPSHOT_CACHE_MIN_TTL_HOURS = max(
    1.0,
    float(os.environ.get("REMCARD_PERSISTENT_SNAPSHOT_CACHE_MIN_TTL_HOURS", "24")),
)
PERSISTENT_SNAPSHOT_CACHE_MAX_PENDING_WRITES = max(
    20,
    int(os.environ.get("REMCARD_PERSISTENT_SNAPSHOT_CACHE_MAX_PENDING_WRITES", "512")),
)
PERSISTENT_SNAPSHOT_CACHE_MAX_TRACKED_HASHES = max(
    100,
    int(
        os.environ.get(
            "REMCARD_PERSISTENT_SNAPSHOT_CACHE_MAX_TRACKED_HASHES",
            str(PERSISTENT_SNAPSHOT_CACHE_MAX_FILES * 4),
        )
    ),
)
PERSISTENT_SNAPSHOT_CACHE_PRUNE_INTERVAL_SEC = max(
    5.0,
    float(os.environ.get("REMCARD_PERSISTENT_SNAPSHOT_CACHE_PRUNE_INTERVAL_SEC", "60")),
)
PERSISTENT_SNAPSHOT_CACHE_DIR = Path(LOCAL_CACHE_DIR) / "patient_snapshots"
_CACHE_LOCK = threading.RLock()
_PRUNE_LOCK = threading.Lock()
_LAST_PRUNE_MONOTONIC: dict[str, float] = {}
_MANIFEST_LOCKS_LOCK = threading.Lock()
_MANIFEST_LOCKS: dict[str, threading.RLock] = {}
_MANIFEST_FILE_NAME = "_snapshot_manifest_v1.json"
_MANIFEST_VERSION = 1
_MANIFEST_STATES: dict[str, dict[str, Any]] = {}
_MANIFEST_EPOCH_SNAPSHOTS: dict[str, tuple[dict[str, int], dict[str, int]]] = {}


def _namespace_dir(namespace: str) -> Path:
    safe_namespace = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in str(namespace or "default"))
    return PERSISTENT_SNAPSHOT_CACHE_DIR / safe_namespace


def _cache_digest(cache_key: Any) -> str:
    payload = repr(cache_key).encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()


def _cache_path(namespace: str, cache_key: Any) -> Path:
    return _namespace_dir(namespace) / f"{_cache_digest(cache_key)}.pkl"


def _writer_key(namespace: str, cache_key: Any) -> tuple[str, str]:
    return str(namespace or "default"), _cache_digest(cache_key)


def _manifest_path(namespace: str) -> Path:
    return _namespace_dir(namespace) / _MANIFEST_FILE_NAME


def _manifest_lock_path(namespace: str) -> Path:
    return _namespace_dir(namespace) / f"{_MANIFEST_FILE_NAME}.lock"


def _manifest_file_fingerprint(path: Path) -> tuple[int, int, int] | None:
    try:
        stat_result = path.stat()
    except OSError:
        return None
    return (
        int(stat_result.st_size),
        int(stat_result.st_mtime_ns),
        int(stat_result.st_ctime_ns),
    )


@contextmanager
def _manifest_cross_process_lock(namespace: str, *, timeout_sec: float = 1.0):
    """Serialize JSON manifest read/merge/write across local app processes."""
    path = _manifest_lock_path(namespace)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    acquired = False
    deadline = time.monotonic() + max(0.1, float(timeout_sec))
    try:
        if os.name == "nt":
            import msvcrt

            if path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
            while not acquired:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"manifest lock timeout: {path}")
                    time.sleep(0.01)
        else:
            import fcntl

            while not acquired:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"manifest lock timeout: {path}")
                    time.sleep(0.01)
        yield
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _manifest_state_key(namespace: str) -> str:
    return os.path.normcase(os.path.abspath(str(_namespace_dir(namespace))))


def _manifest_lock(namespace: str) -> threading.RLock:
    """Return the in-process lock for one cache namespace."""
    state_key = _manifest_state_key(namespace)
    with _MANIFEST_LOCKS_LOCK:
        lock = _MANIFEST_LOCKS.get(state_key)
        if lock is None:
            lock = threading.RLock()
            _MANIFEST_LOCKS[state_key] = lock
        return lock


def _publish_manifest_epoch_snapshot(namespace: str, state: dict[str, Any]) -> None:
    """Publish immutable-by-convention epoch maps for lock-free UI scheduling."""
    _MANIFEST_EPOCH_SNAPSHOTS[_manifest_state_key(namespace)] = (
        {str(key): int(value) for key, value in dict(state.get("key_epochs") or {}).items()},
        {
            str(key): int(value)
            for key, value in dict(state.get("admission_epochs") or {}).items()
        },
    )


def _cache_key_integer_indexes(cache_key: Any) -> dict[str, int]:
    if not isinstance(cache_key, (tuple, list)):
        return {}
    result: dict[str, int] = {}
    for index, value in enumerate(cache_key):
        if isinstance(value, bool):
            continue
        try:
            if isinstance(value, float) and not value.is_integer():
                continue
            result[str(index)] = int(value)
        except (TypeError, ValueError, OverflowError):
            continue
    return result


def _manifest_epoch_key(index: int | str, value: int) -> str:
    return f"{int(index)}:{int(value)}"


def _empty_manifest_state(*, legacy_indexed: bool) -> dict[str, Any]:
    return {
        "version": _MANIFEST_VERSION,
        "legacy_indexed": bool(legacy_indexed),
        "entries": {},
        "admission_epochs": {},
        "key_epochs": {},
        "_unindexed_digests": set(),
    }


def _load_manifest_state_locked(namespace: str, *, force_reload: bool = False) -> dict[str, Any]:
    state_key = _manifest_state_key(namespace)
    cached = _MANIFEST_STATES.get(state_key)
    manifest_path = _manifest_path(namespace)
    disk_fingerprint = _manifest_file_fingerprint(manifest_path)
    if (
        isinstance(cached, dict)
        and not force_reload
        and cached.get("_manifest_fingerprint") == disk_fingerprint
    ):
        if state_key not in _MANIFEST_EPOCH_SNAPSHOTS:
            _publish_manifest_epoch_snapshot(namespace, cached)
        return cached

    namespace_dir = manifest_path.parent
    loaded: dict[str, Any] | None = None
    if manifest_path.is_file():
        try:
            with manifest_path.open("r", encoding="utf-8") as fh:
                candidate = json.load(fh)
            if isinstance(candidate, dict) and int(candidate.get("version") or 0) == _MANIFEST_VERSION:
                loaded = candidate
        except Exception as exc:
            logger.warning("[PersistentSnapshotCache] failed to read manifest %s: %s", manifest_path, exc)

    entries = dict((loaded or {}).get("entries") or {})
    epochs = dict((loaded or {}).get("admission_epochs") or {})
    key_epochs = dict((loaded or {}).get("key_epochs") or {})
    legacy_indexed = bool((loaded or {}).get("legacy_indexed", False))
    try:
        disk_digests = {
            path.stem
            for path in namespace_dir.glob("*.pkl")
            if path.is_file()
        }
    except Exception:
        disk_digests = set()
        legacy_indexed = False
    entries = {
        str(digest): entry
        for digest, entry in entries.items()
        if str(digest) in disk_digests
    }
    unindexed_digests = disk_digests.difference(entries)
    if unindexed_digests:
        # A previous process may have stopped between replacing a cache file and
        # persisting the small manifest.  A later admission invalidation must
        # therefore schedule one compatibility scan rather than trust it.
        legacy_indexed = False
    elif loaded is None and not disk_digests:
        legacy_indexed = True

    state = _empty_manifest_state(legacy_indexed=legacy_indexed)
    state["entries"] = entries
    state["admission_epochs"] = epochs
    state["key_epochs"] = key_epochs
    state["_unindexed_digests"] = set(unindexed_digests)
    state["_manifest_fingerprint"] = disk_fingerprint
    _MANIFEST_STATES[state_key] = state
    _publish_manifest_epoch_snapshot(namespace, state)
    return state


def _write_manifest_state_locked(namespace: str, state: dict[str, Any]) -> bool:
    path = _manifest_path(namespace)
    tmp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            delete=False,
            dir=str(path.parent),
            prefix=path.stem,
            suffix=".tmp",
            encoding="utf-8",
        ) as fh:
            tmp_path = Path(fh.name)
            payload = {
                "version": _MANIFEST_VERSION,
                "legacy_indexed": bool(state.get("legacy_indexed", False)),
                "entries": dict(state.get("entries") or {}),
                "admission_epochs": dict(state.get("admission_epochs") or {}),
                "key_epochs": dict(state.get("key_epochs") or {}),
            }
            json.dump(payload, fh, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        os.replace(str(tmp_path), str(path))
        state["_manifest_fingerprint"] = _manifest_file_fingerprint(path)
        _publish_manifest_epoch_snapshot(namespace, state)
        return True
    except Exception as exc:
        logger.warning("[PersistentSnapshotCache] failed to write manifest %s: %s", path, exc)
        state["_manifest_fingerprint"] = ("dirty", time.monotonic_ns())
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
        return False


def _path_fingerprint(path: Path) -> dict[str, int]:
    try:
        stat = path.stat()
    except OSError:
        return {}
    return {
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "ctime_ns": int(stat.st_ctime_ns),
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
    }


def _same_path_fingerprint(path: Path, expected: dict[str, Any] | None) -> bool:
    if not expected:
        return True
    actual = _path_fingerprint(path)
    if not actual:
        return False
    for field in ("size", "mtime_ns", "ctime_ns", "device", "inode"):
        if field in expected and int(actual.get(field, -1)) != int(expected.get(field, -2)):
            return False
    return True


@dataclass(frozen=True)
class _ManifestWriteGuard:
    key_epoch: int
    indexed_epochs: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class _LegacyManifestCandidate:
    cache_key: Any
    path: Path
    fingerprint: dict[str, int]
    write_guard: _ManifestWriteGuard


def _manifest_write_guard_from_state(
    state: dict[str, Any],
    cache_key: Any,
    digest: str,
) -> _ManifestWriteGuard:
    admission_epochs = dict(state.get("admission_epochs") or {})
    indexed_epochs = tuple(
        sorted(
            (
                epoch_key,
                int(admission_epochs.get(epoch_key, 0)),
            )
            for epoch_key in (
                _manifest_epoch_key(index, value)
                for index, value in _cache_key_integer_indexes(cache_key).items()
            )
        )
    )
    return _ManifestWriteGuard(
        key_epoch=int((state.get("key_epochs") or {}).get(str(digest), 0)),
        indexed_epochs=indexed_epochs,
    )


def _manifest_write_guard_is_current_in_state(
    state: dict[str, Any],
    cache_key: Any,
    digest: str,
    write_guard: _ManifestWriteGuard,
) -> bool:
    current = _manifest_write_guard_from_state(state, cache_key, digest)
    return current == write_guard


def _manifest_cache_key_blocked_in_state(
    state: dict[str, Any],
    cache_key: Any,
    digest: str,
) -> bool:
    entry = (state.get("entries") or {}).get(str(digest))
    entry_mapping = entry if isinstance(entry, dict) else {}

    current_key_epoch = int((state.get("key_epochs") or {}).get(str(digest), 0))
    if current_key_epoch and int(entry_mapping.get("key_epoch", -1)) != current_key_epoch:
        return True

    admission_epochs = dict(state.get("admission_epochs") or {})
    entry_epochs = dict(entry_mapping.get("epochs") or {})
    for index, value in _cache_key_integer_indexes(cache_key).items():
        epoch_key = _manifest_epoch_key(index, value)
        current_epoch = int(admission_epochs.get(epoch_key, 0))
        if current_epoch and int(entry_epochs.get(epoch_key, -1)) != current_epoch:
            return True
    return False


def _capture_manifest_write_guard(
    namespace: str,
    cache_key: Any,
    digest: str,
) -> tuple[_ManifestWriteGuard, bool]:
    """Capture the persisted invalidation generations seen by a prospective write."""
    with _manifest_lock(namespace):
        state = _load_manifest_state_locked(namespace)
        return (
            _manifest_write_guard_from_state(state, cache_key, digest),
            _manifest_cache_key_blocked_in_state(state, cache_key, digest),
        )


def _try_capture_cached_manifest_write_guard(
    namespace: str,
    cache_key: Any,
    digest: str,
) -> _ManifestWriteGuard:
    """Read only the process-local guard without waiting or touching the filesystem.

    A missing local manifest state deliberately produces a zero-generation guard.
    The writer verifies it against the persisted manifest before replacing a file,
    so a process that has not observed a tombstone may lose this best-effort cache
    write but can never resurrect an invalidated snapshot.
    """
    key_epochs, admission_epochs = _MANIFEST_EPOCH_SNAPSHOTS.get(
        _manifest_state_key(namespace),
        ({}, {}),
    )
    state = {
        "key_epochs": key_epochs,
        "admission_epochs": admission_epochs,
    }
    return _manifest_write_guard_from_state(state, cache_key, digest)


def _manifest_register_in_state(
    state: dict[str, Any],
    cache_key: Any,
    path: Path,
    *,
    content_hash: str = "",
    write_guard: _ManifestWriteGuard | None = None,
) -> None:
    digest = path.stem
    indexes = _cache_key_integer_indexes(cache_key)
    effective_guard = write_guard or _manifest_write_guard_from_state(state, cache_key, digest)
    state.setdefault("entries", {})[digest] = {
        "indexes": indexes,
        "epochs": dict(effective_guard.indexed_epochs),
        "key_epoch": int(effective_guard.key_epoch),
        "fingerprint": _path_fingerprint(path),
        "content_hash": str(content_hash or ""),
    }
    unindexed_digests = state.setdefault("_unindexed_digests", set())
    if isinstance(unindexed_digests, set):
        unindexed_digests.discard(digest)
        if not unindexed_digests:
            state["legacy_indexed"] = True


def _commit_snapshot_file(
    namespace: str,
    cache_key: Any,
    tmp_path: Path,
    path: Path,
    *,
    content_hash: str,
    write_guard: _ManifestWriteGuard,
) -> bool:
    """Atomically coordinate file replacement with the cross-process manifest."""
    with _manifest_lock(namespace):
        with _manifest_cross_process_lock(namespace):
            state = _load_manifest_state_locked(namespace, force_reload=True)
            if not _manifest_write_guard_is_current_in_state(
                state,
                cache_key,
                path.stem,
                write_guard,
            ):
                return False
            with _CACHE_LOCK:
                os.replace(str(tmp_path), str(path))
            _manifest_register_in_state(
                state,
                cache_key,
                path,
                content_hash=content_hash,
                write_guard=write_guard,
            )
            if not _write_manifest_state_locked(namespace, state):
                state["legacy_indexed"] = False
                raise OSError(f"failed to persist snapshot manifest for {path}")
            return True


def _manifest_register_legacy_entries(
    namespace: str,
    items: list[_LegacyManifestCandidate],
) -> None:
    if not items:
        return
    with _manifest_lock(namespace):
        with _manifest_cross_process_lock(namespace):
            state = _load_manifest_state_locked(namespace, force_reload=True)
            entries = state.setdefault("entries", {})
            changed = False
            for item in items:
                digest = item.path.stem
                if isinstance(entries.get(digest), dict):
                    continue
                if not _same_path_fingerprint(item.path, item.fingerprint):
                    continue
                if not _manifest_write_guard_is_current_in_state(
                    state,
                    item.cache_key,
                    digest,
                    item.write_guard,
                ):
                    continue
                _manifest_register_in_state(
                    state,
                    item.cache_key,
                    item.path,
                    write_guard=item.write_guard,
                )
                changed = True
            if changed and not _write_manifest_state_locked(namespace, state):
                state["legacy_indexed"] = False


def _manifest_remove_digests(namespace: str, digests: set[str]) -> None:
    if not digests:
        return
    with _manifest_lock(namespace):
        with _manifest_cross_process_lock(namespace):
            state = _load_manifest_state_locked(namespace, force_reload=True)
            entries = state.setdefault("entries", {})
            changed = False
            for digest in digests:
                if entries.pop(str(digest), None) is not None:
                    changed = True
            if changed:
                _write_manifest_state_locked(namespace, state)


def _manifest_cache_key_blocked(namespace: str, cache_key: Any, digest: str) -> bool:
    with _manifest_lock(namespace):
        state = _load_manifest_state_locked(namespace)
        return _manifest_cache_key_blocked_in_state(state, cache_key, digest)


def _manifest_content_hash_matches(
    namespace: str,
    cache_key: Any,
    digest: str,
    content_hash: str,
) -> bool:
    with _manifest_lock(namespace):
        state = _load_manifest_state_locked(namespace)
        entry = (state.get("entries") or {}).get(str(digest))
        if not isinstance(entry, dict) or str(entry.get("content_hash") or "") != str(content_hash or ""):
            return False
        return not _manifest_cache_key_blocked_in_state(state, cache_key, digest)


def _manifest_invalidate_admission(
    namespace: str,
    admission_id: int,
    *,
    admission_id_index: int,
) -> tuple[int, dict[str, dict[str, Any]], bool]:
    target = int(admission_id)
    index_text = str(int(admission_id_index))
    epoch_key = _manifest_epoch_key(admission_id_index, target)
    with _manifest_lock(namespace):
        with _manifest_cross_process_lock(namespace):
            state = _load_manifest_state_locked(namespace, force_reload=True)
            epochs = state.setdefault("admission_epochs", {})
            epoch = int(epochs.get(epoch_key, 0)) + 1
            epochs[epoch_key] = epoch
            entries = state.setdefault("entries", {})
            removed: dict[str, dict[str, Any]] = {}
            for digest, entry in list(entries.items()):
                indexes = dict(entry.get("indexes") or {}) if isinstance(entry, dict) else {}
                try:
                    matches = int(indexes.get(index_text)) == target
                except (TypeError, ValueError):
                    matches = False
                if matches:
                    removed[str(digest)] = dict(entry)
                    entries.pop(digest, None)
            needs_legacy_scan = not bool(state.get("legacy_indexed", False))
            if not _write_manifest_state_locked(namespace, state):
                state["legacy_indexed"] = False
                raise OSError(f"failed to persist admission invalidation for {namespace}")
            return epoch, removed, needs_legacy_scan


def _manifest_complete_admission_cleanup(
    namespace: str,
    admission_id: int,
    *,
    admission_id_index: int,
    epoch: int,
    legacy_scan_completed: bool,
) -> None:
    epoch_key = _manifest_epoch_key(admission_id_index, admission_id)
    with _manifest_lock(namespace):
        with _manifest_cross_process_lock(namespace):
            state = _load_manifest_state_locked(namespace, force_reload=True)
            epochs = state.setdefault("admission_epochs", {})
            if int(epochs.get(epoch_key, 0)) == int(epoch):
                if legacy_scan_completed:
                    unindexed = state.setdefault("_unindexed_digests", set())
                    state["legacy_indexed"] = not bool(unindexed)
                # Keep the monotonic admission epoch.  Removing it after disk
                # cleanup creates an ABA window where a read that started
                # before invalidation can pass a later second check.
            _write_manifest_state_locked(namespace, state)


def _expiry_from_shift_key(shift_key: str) -> Optional[datetime]:
    try:
        shift_expiry = datetime.fromisoformat(str(shift_key)) + timedelta(days=1)
    except Exception:
        return None
    min_expiry = datetime.now() + timedelta(hours=PERSISTENT_SNAPSHOT_CACHE_MIN_TTL_HOURS)
    return max(shift_expiry, min_expiry)


def expiry_from_cache_key(cache_key: Any, *, shift_key_index: int = 2) -> Optional[datetime]:
    try:
        return _expiry_from_shift_key(cache_key[shift_key_index])
    except Exception:
        return None


def _is_expired(expires_at: Optional[str], *, now: Optional[datetime] = None) -> bool:
    if not expires_at:
        return False
    try:
        expiration = datetime.fromisoformat(str(expires_at))
    except Exception:
        return True
    return (now or datetime.now()) >= expiration


def _snapshot_payload(cache_key: Any, snapshot: Any, *, expires_at: Optional[datetime]) -> dict[str, Any]:
    return {
        "cache_key": cache_key,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "expires_at": (expires_at.isoformat(timespec="seconds") if expires_at else None),
        "snapshot": snapshot,
    }


def _serialize_snapshot(
    cache_key: Any,
    snapshot: Any,
    *,
    expires_at: Optional[datetime],
) -> tuple[bytes, str]:
    expires_text = expires_at.isoformat(timespec="seconds") if expires_at else None
    content_payload = {
        "cache_key": cache_key,
        "expires_at": expires_text,
        "snapshot": snapshot,
    }
    content_bytes = pickle.dumps(content_payload, protocol=pickle.HIGHEST_PROTOCOL)
    serialized = pickle.dumps(
        _snapshot_payload(cache_key, snapshot, expires_at=expires_at),
        protocol=pickle.HIGHEST_PROTOCOL,
    )
    return serialized, hashlib.sha256(content_bytes).hexdigest()


@dataclass(frozen=True)
class _ScheduledWrite:
    writer_key: tuple[str, str]
    namespace: str
    cache_key: Any
    path: Path
    serialized: bytes
    content_hash: str
    write_guard: _ManifestWriteGuard
    generation: int


@dataclass(frozen=True)
class _ScheduledMaintenance:
    key: tuple[Any, ...]
    callback: Callable[[], None]


class _SnapshotWriter:
    def __init__(self):
        self._condition = threading.Condition(threading.RLock())
        self._pending: "OrderedDict[tuple[str, str], _ScheduledWrite]" = OrderedDict()
        self._maintenance: "OrderedDict[tuple[Any, ...], _ScheduledMaintenance]" = OrderedDict()
        self._generation: dict[tuple[str, str], int] = {}
        self._sync_reservations: dict[tuple[str, str], tuple[str, Any, int]] = {}
        self._last_written_hash: "OrderedDict[tuple[str, str], str]" = OrderedDict()
        self._active: _ScheduledWrite | None = None
        self._active_maintenance: _ScheduledMaintenance | None = None
        self._thread: threading.Thread | None = None

    def schedule(
        self,
        namespace: str,
        cache_key: Any,
        serialized: bytes,
        content_hash: str,
        write_guard: _ManifestWriteGuard,
    ) -> bool:
        key = _writer_key(namespace, cache_key)
        path = _cache_path(namespace, cache_key)
        with self._condition:
            has_sync_reservation = key in self._sync_reservations
            pending = self._pending.get(key)
            if (
                not has_sync_reservation
                and pending is not None
                and pending.content_hash == content_hash
                and pending.write_guard == write_guard
            ):
                return True

            generation = int(self._generation.get(key, 0)) + 1
            self._generation[key] = generation
            self._sync_reservations.pop(key, None)
            job = _ScheduledWrite(
                writer_key=key,
                namespace=str(namespace or "default"),
                cache_key=cache_key,
                path=path,
                serialized=serialized,
                content_hash=content_hash,
                write_guard=write_guard,
                generation=generation,
            )
            self._pending[key] = job
            self._pending.move_to_end(key)
            while len(self._pending) > PERSISTENT_SNAPSHOT_CACHE_MAX_PENDING_WRITES:
                dropped_key, _dropped = self._pending.popitem(last=False)
                self._generation[dropped_key] = int(self._generation.get(dropped_key, 0)) + 1
                logger.debug("[PersistentSnapshotCache] dropped oldest pending write key=%s", dropped_key)
            self._ensure_thread_locked()
            self._condition.notify_all()
        return True

    def reserve_sync_write(self, namespace: str, cache_key: Any) -> tuple[tuple[str, str], int]:
        key = _writer_key(namespace, cache_key)
        with self._condition:
            generation = int(self._generation.get(key, 0)) + 1
            self._generation[key] = generation
            self._pending.pop(key, None)
            self._last_written_hash.pop(key, None)
            self._sync_reservations[key] = (str(namespace or "default"), cache_key, generation)
            self._condition.notify_all()
            return key, generation

    def mark_written(self, key: tuple[str, str], generation: int, content_hash: str) -> None:
        with self._condition:
            reservation = self._sync_reservations.get(key)
            if reservation is not None and int(reservation[2]) == int(generation):
                self._sync_reservations.pop(key, None)
            if self._generation.get(key) == generation:
                self._last_written_hash[key] = content_hash
                self._last_written_hash.move_to_end(key)
                while len(self._last_written_hash) > PERSISTENT_SNAPSHOT_CACHE_MAX_TRACKED_HASHES:
                    self._last_written_hash.popitem(last=False)
            self._condition.notify_all()

    def release_sync_write(self, key: tuple[str, str], generation: int) -> None:
        with self._condition:
            reservation = self._sync_reservations.get(key)
            if reservation is not None and int(reservation[2]) == int(generation):
                self._sync_reservations.pop(key, None)
            self._condition.notify_all()

    def commit_if_current(
        self,
        key: tuple[str, str],
        generation: int,
        callback: Callable[[], None],
    ) -> bool:
        with self._condition:
            if self._generation.get(key) != generation:
                return False
        callback()
        return self.is_generation_current(key, generation)

    def is_current(self, job: _ScheduledWrite) -> bool:
        return self.is_generation_current(job.writer_key, job.generation)

    def has_matching_written_hash(self, job: _ScheduledWrite) -> bool:
        with self._condition:
            return (
                self._generation.get(job.writer_key) == job.generation
                and self._last_written_hash.get(job.writer_key) == job.content_hash
            )

    def is_generation_current(self, key: tuple[str, str], generation: int) -> bool:
        with self._condition:
            return self._generation.get(key) == generation

    def invalidate_key(self, namespace: str, cache_key: Any) -> None:
        key = _writer_key(namespace, cache_key)
        with self._condition:
            self._generation[key] = int(self._generation.get(key, 0)) + 1
            self._pending.pop(key, None)
            self._sync_reservations.pop(key, None)
            self._last_written_hash.pop(key, None)
            self._condition.notify_all()

    def invalidate_admission(
        self,
        namespace: str,
        admission_id: int,
        *,
        admission_id_index: int,
        barrier: Callable[[], Any] | None = None,
    ) -> tuple[set[tuple[str, str]], Any]:
        target = int(admission_id)
        with self._condition:
            jobs = list(self._pending.values())
            if self._active is not None:
                jobs.append(self._active)
            invalidated_keys: set[tuple[str, str]] = set()
            for job in jobs:
                if job.namespace != str(namespace or "default"):
                    continue
                try:
                    matches = int(job.cache_key[admission_id_index]) == target
                except Exception:
                    matches = False
                if not matches:
                    continue
                key = job.writer_key
                invalidated_keys.add(key)
                self._generation[key] = int(self._generation.get(key, 0)) + 1
                self._pending.pop(key, None)
                self._last_written_hash.pop(key, None)
            for key, reservation in list(self._sync_reservations.items()):
                reservation_namespace, cache_key, _generation = reservation
                if reservation_namespace != str(namespace or "default"):
                    continue
                try:
                    matches = int(cache_key[admission_id_index]) == target
                except Exception:
                    matches = False
                if not matches:
                    continue
                invalidated_keys.add(key)
                self._generation[key] = int(self._generation.get(key, 0)) + 1
                self._sync_reservations.pop(key, None)
                self._last_written_hash.pop(key, None)
            barrier_result = barrier() if barrier is not None else None
            self._condition.notify_all()
            return invalidated_keys, barrier_result

    def schedule_maintenance(self, key: tuple[Any, ...], callback: Callable[[], None]) -> None:
        task = _ScheduledMaintenance(tuple(key), callback)
        with self._condition:
            self._maintenance[task.key] = task
            self._maintenance.move_to_end(task.key)
            self._ensure_thread_locked()
            self._condition.notify_all()

    def flush(self, timeout_sec: float | None = 5.0) -> bool:
        deadline = None if timeout_sec is None else time.monotonic() + max(0.0, float(timeout_sec))
        with self._condition:
            while (
                self._pending
                or self._maintenance
                or self._sync_reservations
                or self._active is not None
                or self._active_maintenance is not None
            ):
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def _ensure_thread_locked(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            name="PersistentSnapshotWriter",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._pending and not self._maintenance:
                    self._condition.wait()
                if self._maintenance:
                    _key, maintenance = self._maintenance.popitem(last=False)
                    self._active_maintenance = maintenance
                    job = None
                else:
                    _key, job = self._pending.popitem(last=False)
                    self._active = job
                    maintenance = None

            if maintenance is not None:
                try:
                    maintenance.callback()
                except Exception as exc:
                    logger.warning(
                        "[PersistentSnapshotCache] maintenance failed key=%s: %s",
                        maintenance.key,
                        exc,
                    )
                finally:
                    with self._condition:
                        if self._active_maintenance is maintenance:
                            self._active_maintenance = None
                        self._condition.notify_all()
                continue

            tmp_path: Path | None = None
            wrote = False
            try:
                if not self.is_current(job):
                    continue
                matching_written_hash = self.has_matching_written_hash(job)
                if (
                    matching_written_hash
                    and _manifest_content_hash_matches(
                        job.namespace,
                        job.cache_key,
                        job.writer_key[1],
                        job.content_hash,
                    )
                    and self.is_current(job)
                ):
                    self.mark_written(job.writer_key, job.generation, job.content_hash)
                    continue
                job.path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    "wb",
                    delete=False,
                    dir=str(job.path.parent),
                    prefix=job.path.stem,
                    suffix=".tmp",
                ) as fh:
                    tmp_path = Path(fh.name)
                    fh.write(job.serialized)

                committed = False

                def commit_job() -> None:
                    nonlocal committed, tmp_path
                    committed = _commit_snapshot_file(
                        job.namespace,
                        job.cache_key,
                        tmp_path,
                        job.path,
                        content_hash=job.content_hash,
                        write_guard=job.write_guard,
                    )
                    if committed:
                        tmp_path = None

                generation_current = self.commit_if_current(
                    job.writer_key,
                    job.generation,
                    commit_job,
                )
                wrote = bool(generation_current and committed)
                if wrote:
                    self.mark_written(job.writer_key, job.generation, job.content_hash)
                    _prune_namespace_if_due(job.namespace)
                elif generation_current:
                    logger.debug(
                        "[PersistentSnapshotCache] rejected stale cross-process write key=%s",
                        job.writer_key,
                    )
            except Exception as exc:
                logger.warning("[PersistentSnapshotCache] async write failed for %s: %s", job.path, exc)
            finally:
                if tmp_path is not None:
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                with self._condition:
                    if self._active is job:
                        self._active = None
                    self._condition.notify_all()


_ASYNC_WRITER = _SnapshotWriter()


def _delete_path_if_unchanged(
    namespace: str,
    path: Path,
    expected_fingerprint: dict[str, Any] | None,
) -> str:
    """Delete one stale file while holding the global cache lock only for unlink."""
    with _manifest_lock(namespace):
        with _manifest_cross_process_lock(namespace):
            state = _load_manifest_state_locked(namespace, force_reload=True)
            if isinstance((state.get("entries") or {}).get(path.stem), dict):
                return "changed"
            with _CACHE_LOCK:
                if not path.exists():
                    return "missing"
                if not _same_path_fingerprint(path, expected_fingerprint):
                    return "changed"
                try:
                    path.unlink(missing_ok=True)
                    return "deleted"
                except Exception as exc:
                    logger.warning("[PersistentSnapshotCache] failed to delete %s: %s", path, exc)
                    return "failed"


def _delete_manifest_entries_batch(
    namespace: str,
    manifest_entries: dict[str, dict[str, Any]],
) -> bool:
    if not manifest_entries:
        return True
    namespace_dir = _namespace_dir(namespace)
    cleanup_ok = True
    with _manifest_lock(namespace):
        try:
            with _manifest_cross_process_lock(namespace):
                state = _load_manifest_state_locked(namespace, force_reload=True)
                current_entries = state.get("entries") or {}
                with _CACHE_LOCK:
                    for digest, stale_entry in manifest_entries.items():
                        if isinstance(current_entries.get(str(digest)), dict):
                            continue
                        path = namespace_dir / f"{digest}.pkl"
                        if not path.exists():
                            continue
                        expected = dict(stale_entry.get("fingerprint") or {})
                        if not _same_path_fingerprint(path, expected):
                            cleanup_ok = False
                            continue
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            cleanup_ok = False
        except Exception as exc:
            logger.warning("[PersistentSnapshotCache] batch cleanup failed namespace=%s: %s", namespace, exc)
            return False
    return cleanup_ok


def _delete_invalid_snapshot_if_unchanged(
    namespace: str,
    path: Path,
    expected_fingerprint: dict[str, Any],
) -> bool:
    """Remove a corrupt/expired file without cancelling a newer replacement."""
    with _manifest_lock(namespace):
        try:
            with _manifest_cross_process_lock(namespace):
                state = _load_manifest_state_locked(namespace, force_reload=True)
                with _CACHE_LOCK:
                    if not path.exists() or not _same_path_fingerprint(path, expected_fingerprint):
                        return False
                    path.unlink(missing_ok=True)
                state.setdefault("entries", {}).pop(path.stem, None)
                unindexed = state.setdefault("_unindexed_digests", set())
                if isinstance(unindexed, set):
                    unindexed.discard(path.stem)
                    if not unindexed:
                        state["legacy_indexed"] = True
                _write_manifest_state_locked(namespace, state)
                return True
        except Exception as exc:
            logger.warning("[PersistentSnapshotCache] failed to remove invalid %s: %s", path, exc)
            return False


def _cleanup_admission_files(
    namespace: str,
    admission_id: int,
    *,
    admission_id_index: int,
    epoch: int,
    manifest_entries: dict[str, dict[str, Any]],
    scan_legacy: bool,
) -> None:
    namespace_dir = _namespace_dir(namespace)
    cleanup_ok = _delete_manifest_entries_batch(namespace, manifest_entries)

    legacy_scan_completed = not scan_legacy
    legacy_entries: list[_LegacyManifestCandidate] = []
    if scan_legacy:
        try:
            files = [path for path in namespace_dir.glob("*.pkl") if path.is_file()]
        except Exception as exc:
            logger.warning("[PersistentSnapshotCache] failed to list cache dir %s: %s", namespace_dir, exc)
            files = []
            cleanup_ok = False
        else:
            legacy_scan_completed = True

        for path in files:
            fingerprint = _path_fingerprint(path)
            try:
                with path.open("rb") as fh:
                    payload = pickle.load(fh)
                cache_key = payload.get("cache_key") if isinstance(payload, dict) else None
                if cache_key is None:
                    raise ValueError("cache_key missing")
                if int(cache_key[admission_id_index]) != int(admission_id):
                    write_guard, blocked = _capture_manifest_write_guard(
                        namespace,
                        cache_key,
                        path.stem,
                    )
                    if not blocked:
                        legacy_entries.append(
                            _LegacyManifestCandidate(
                                cache_key=cache_key,
                                path=path,
                                fingerprint=fingerprint,
                                write_guard=write_guard,
                            )
                        )
                    continue
            except Exception as exc:
                logger.warning("[PersistentSnapshotCache] failed to inspect %s: %s", path, exc)
                if _delete_path_if_unchanged(namespace, path, fingerprint) in {"changed", "failed"}:
                    cleanup_ok = False
                continue

            # A fresh store for the same digest may have completed after the
            # invalidation.  Its manifest epoch makes it authoritative.
            if not _manifest_cache_key_blocked(namespace, cache_key, path.stem):
                continue
            delete_result = _delete_path_if_unchanged(namespace, path, fingerprint)
            if delete_result == "failed":
                cleanup_ok = False
            elif delete_result == "changed" and _manifest_cache_key_blocked(
                namespace,
                cache_key,
                path.stem,
            ):
                cleanup_ok = False

    _manifest_register_legacy_entries(namespace, legacy_entries)
    if cleanup_ok:
        _manifest_complete_admission_cleanup(
            namespace,
            admission_id,
            admission_id_index=admission_id_index,
            epoch=epoch,
            legacy_scan_completed=legacy_scan_completed,
        )


def load_snapshot(namespace: str, cache_key: Any, *, now: Optional[datetime] = None):
    if not PERSISTENT_SNAPSHOT_CACHE_ENABLED:
        return None
    digest = _cache_digest(cache_key)
    if _manifest_cache_key_blocked(namespace, cache_key, digest):
        return None
    should_delete = False
    snapshot = None
    payload = None
    read_fingerprint: dict[str, int] = {}
    with _CACHE_LOCK:
        path = _namespace_dir(namespace) / f"{digest}.pkl"
        if not path.exists():
            return None
        read_fingerprint = _path_fingerprint(path)
        try:
            with path.open("rb") as fh:
                payload = pickle.load(fh)
        except Exception as exc:
            logger.warning("[PersistentSnapshotCache] failed to read %s: %s", path, exc)
            should_delete = True
        else:
            should_delete = not isinstance(payload, dict)
        if should_delete and not isinstance(payload, dict):
            logger.warning("[PersistentSnapshotCache] invalid payload type for %s: %s", path, type(payload).__name__)
        elif payload.get("cache_key") != cache_key:
            logger.warning("[PersistentSnapshotCache] cache key mismatch for %s", path)
            should_delete = True
        elif _is_expired(payload.get("expires_at"), now=now):
            should_delete = True
        else:
            snapshot = payload.get("snapshot")
    if should_delete:
        _delete_invalid_snapshot_if_unchanged(namespace, path, read_fingerprint)
        return None
    if _manifest_cache_key_blocked(namespace, cache_key, digest):
        return None
    with _CACHE_LOCK:
        if not path.exists() or not _same_path_fingerprint(path, read_fingerprint):
            return None
    return snapshot


def delete_snapshot(namespace: str, cache_key: Any) -> bool:
    if not PERSISTENT_SNAPSHOT_CACHE_ENABLED:
        return False
    _ASYNC_WRITER.invalidate_key(namespace, cache_key)
    path = _cache_path(namespace, cache_key)
    with _manifest_lock(namespace):
        try:
            with _manifest_cross_process_lock(namespace):
                state = _load_manifest_state_locked(namespace, force_reload=True)
                key_epochs = state.setdefault("key_epochs", {})
                key_epochs[path.stem] = int(key_epochs.get(path.stem, 0)) + 1
                state.setdefault("entries", {}).pop(path.stem, None)
                if not _write_manifest_state_locked(namespace, state):
                    raise OSError(f"failed to persist exact invalidation for {path}")
                with _CACHE_LOCK:
                    existed = path.exists()
                    path.unlink(missing_ok=True)
                return bool(existed)
        except Exception as exc:
            logger.warning("[PersistentSnapshotCache] failed to delete %s: %s", path, exc)
            return False


def delete_snapshots_for_admission(namespace: str, admission_id: int, *, admission_id_index: int = 1) -> int:
    if not PERSISTENT_SNAPSHOT_CACHE_ENABLED:
        return 0
    target_admission_id = int(admission_id)
    removed_keys, manifest_result = _ASYNC_WRITER.invalidate_admission(
        namespace,
        target_admission_id,
        admission_id_index=admission_id_index,
        barrier=lambda: _manifest_invalidate_admission(
            namespace,
            target_admission_id,
            admission_id_index=admission_id_index,
        ),
    )
    epoch, manifest_entries, needs_legacy_scan = manifest_result
    cleanup_key = (
        "delete-admission",
        str(namespace or "default"),
        int(admission_id_index),
        target_admission_id,
        int(epoch),
    )
    _ASYNC_WRITER.schedule_maintenance(
        cleanup_key,
        lambda: _cleanup_admission_files(
            namespace,
            target_admission_id,
            admission_id_index=admission_id_index,
            epoch=epoch,
            manifest_entries=manifest_entries,
            scan_legacy=needs_legacy_scan,
        ),
    )
    removed = len(removed_keys.union({(str(namespace or "default"), digest) for digest in manifest_entries}))
    if removed:
        logger.info(
            "[PersistentSnapshotCache] scheduled delete namespace=%s admission_id=%s entries=%s legacy_scan=%s",
            namespace,
            target_admission_id,
            removed,
            int(needs_legacy_scan),
        )
    return removed


def store_snapshot(
    namespace: str,
    cache_key: Any,
    snapshot: Any,
    *,
    expires_at: Optional[datetime] = None,
) -> bool:
    if not PERSISTENT_SNAPSHOT_CACHE_ENABLED:
        return False
    try:
        digest = _cache_digest(cache_key)
        write_guard, _blocked = _capture_manifest_write_guard(namespace, cache_key, digest)
        serialized, content_hash = _serialize_snapshot(cache_key, snapshot, expires_at=expires_at)
    except Exception as exc:
        logger.warning("[PersistentSnapshotCache] failed to serialize namespace=%s: %s", namespace, exc)
        return False
    writer_key, generation = _ASYNC_WRITER.reserve_sync_write(namespace, cache_key)
    path = _cache_path(namespace, cache_key)
    tmp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "wb",
            delete=False,
            dir=str(path.parent),
            prefix=path.stem,
            suffix=".tmp",
        ) as fh:
            tmp_path = Path(fh.name)
            fh.write(serialized)

        committed = False

        def commit_sync() -> None:
            nonlocal committed, tmp_path
            committed = _commit_snapshot_file(
                namespace,
                cache_key,
                tmp_path,
                path,
                content_hash=content_hash,
                write_guard=write_guard,
            )
            if committed:
                tmp_path = None

        generation_current = _ASYNC_WRITER.commit_if_current(writer_key, generation, commit_sync)
        if not generation_current or not committed:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
            tmp_path = None
            _ASYNC_WRITER.release_sync_write(writer_key, generation)
            logger.debug(
                "[PersistentSnapshotCache] cancelled superseded sync write namespace=%s key=%s",
                namespace,
                writer_key,
            )
            return False
    except Exception as exc:
        logger.warning("[PersistentSnapshotCache] failed to write %s: %s", path, exc)
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
        _ASYNC_WRITER.release_sync_write(writer_key, generation)
        return False
    _ASYNC_WRITER.mark_written(writer_key, generation, content_hash)
    _prune_namespace_if_due(namespace)
    return True


def schedule_store_snapshot(
    namespace: str,
    cache_key: Any,
    snapshot: Any,
    *,
    expires_at: Optional[datetime] = None,
) -> bool:
    """Serialize now and persist later; repeated writes for one key are coalesced."""
    if not PERSISTENT_SNAPSHOT_CACHE_ENABLED:
        return False
    try:
        digest = _cache_digest(cache_key)
        write_guard = _try_capture_cached_manifest_write_guard(namespace, cache_key, digest)
        serialized, content_hash = _serialize_snapshot(cache_key, snapshot, expires_at=expires_at)
    except Exception as exc:
        logger.warning("[PersistentSnapshotCache] failed to schedule namespace=%s: %s", namespace, exc)
        return False
    return _ASYNC_WRITER.schedule(
        namespace,
        cache_key,
        serialized,
        content_hash,
        write_guard,
    )


def flush(timeout_sec: float | None = 5.0) -> bool:
    """Wait until all scheduled cache writes and their maintenance are complete."""
    return _ASYNC_WRITER.flush(timeout_sec)


def _prune_namespace_if_due(namespace: str) -> None:
    now_monotonic = time.monotonic()
    with _PRUNE_LOCK:
        last_run = float(_LAST_PRUNE_MONOTONIC.get(str(namespace), 0.0))
        if now_monotonic - last_run < PERSISTENT_SNAPSHOT_CACHE_PRUNE_INTERVAL_SEC:
            return
        _LAST_PRUNE_MONOTONIC[str(namespace)] = now_monotonic
    prune_namespace(namespace)


def prune_namespace(namespace: str, *, now: Optional[datetime] = None) -> None:
    if not PERSISTENT_SNAPSHOT_CACHE_ENABLED:
        return
    del now
    namespace_dir = _namespace_dir(namespace)
    if not namespace_dir.exists():
        return
    with _manifest_lock(namespace):
        try:
            with _manifest_cross_process_lock(namespace):
                state = _load_manifest_state_locked(namespace, force_reload=True)
                with _CACHE_LOCK:
                    files = sorted(
                        (path for path in namespace_dir.glob("*.pkl") if path.is_file()),
                        key=lambda item: item.stat().st_mtime,
                        reverse=True,
                    )
                    removed_digests: set[str] = set()
                    for path in files[PERSISTENT_SNAPSHOT_CACHE_MAX_FILES:]:
                        try:
                            path.unlink(missing_ok=True)
                            removed_digests.add(path.stem)
                        except Exception:
                            pass
                entries = state.setdefault("entries", {})
                for digest in removed_digests:
                    entries.pop(digest, None)
                if removed_digests:
                    _write_manifest_state_locked(namespace, state)
        except Exception as exc:
            logger.warning("[PersistentSnapshotCache] failed to prune cache dir %s: %s", namespace_dir, exc)


atexit.register(flush, 1.0)
