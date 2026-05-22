import hashlib
import os
import pickle
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

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
PERSISTENT_SNAPSHOT_CACHE_DIR = Path(LOCAL_CACHE_DIR) / "patient_snapshots"
_CACHE_LOCK = threading.RLock()


def _namespace_dir(namespace: str) -> Path:
    safe_namespace = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in str(namespace or "default"))
    return PERSISTENT_SNAPSHOT_CACHE_DIR / safe_namespace


def _cache_digest(cache_key: Any) -> str:
    payload = repr(cache_key).encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()


def _cache_path(namespace: str, cache_key: Any) -> Path:
    return _namespace_dir(namespace) / f"{_cache_digest(cache_key)}.pkl"


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


def load_snapshot(namespace: str, cache_key: Any, *, now: Optional[datetime] = None):
    if not PERSISTENT_SNAPSHOT_CACHE_ENABLED:
        return None
    with _CACHE_LOCK:
        path = _cache_path(namespace, cache_key)
        if not path.exists():
            return None
        try:
            with path.open("rb") as fh:
                payload = pickle.load(fh)
        except Exception as exc:
            logger.warning("[PersistentSnapshotCache] failed to read %s: %s", path, exc)
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            return None

        if not isinstance(payload, dict):
            logger.warning("[PersistentSnapshotCache] invalid payload type for %s: %s", path, type(payload).__name__)
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            return None

        if payload.get("cache_key") != cache_key:
            logger.warning("[PersistentSnapshotCache] cache key mismatch for %s", path)
            return None
        if _is_expired(payload.get("expires_at"), now=now):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            return None
        return payload.get("snapshot")


def delete_snapshot(namespace: str, cache_key: Any) -> bool:
    if not PERSISTENT_SNAPSHOT_CACHE_ENABLED:
        return False
    with _CACHE_LOCK:
        path = _cache_path(namespace, cache_key)
        try:
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
    removed = 0
    with _CACHE_LOCK:
        namespace_dir = _namespace_dir(namespace)
        if not namespace_dir.exists():
            return 0
        try:
            files = [path for path in namespace_dir.glob("*.pkl") if path.is_file()]
        except Exception as exc:
            logger.warning("[PersistentSnapshotCache] failed to list cache dir %s: %s", namespace_dir, exc)
            return 0

        for path in files:
            try:
                with path.open("rb") as fh:
                    payload = pickle.load(fh)
                cache_key = payload.get("cache_key") if isinstance(payload, dict) else None
                if cache_key is None or int(cache_key[admission_id_index]) != target_admission_id:
                    continue
                path.unlink(missing_ok=True)
                removed += 1
            except Exception as exc:
                logger.warning("[PersistentSnapshotCache] failed to inspect %s: %s", path, exc)
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass
    if removed:
        logger.info(
            "[PersistentSnapshotCache] deleted namespace=%s admission_id=%s entries=%s",
            namespace,
            target_admission_id,
            removed,
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
    with _CACHE_LOCK:
        namespace_dir = _namespace_dir(namespace)
        try:
            namespace_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.warning("[PersistentSnapshotCache] failed to create cache dir %s: %s", namespace_dir, exc)
            return False

        payload = {
            "cache_key": cache_key,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "expires_at": (expires_at.isoformat(timespec="seconds") if expires_at else None),
            "snapshot": snapshot,
        }
        path = _cache_path(namespace, cache_key)
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile("wb", delete=False, dir=str(namespace_dir), prefix=path.stem, suffix=".tmp") as fh:
                tmp_path = Path(fh.name)
                pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(str(tmp_path), str(path))
            prune_namespace(namespace)
            return True
        except Exception as exc:
            logger.warning("[PersistentSnapshotCache] failed to write %s: %s", path, exc)
            if tmp_path:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass
            return False


def prune_namespace(namespace: str, *, now: Optional[datetime] = None) -> None:
    if not PERSISTENT_SNAPSHOT_CACHE_ENABLED:
        return
    del now
    with _CACHE_LOCK:
        namespace_dir = _namespace_dir(namespace)
        if not namespace_dir.exists():
            return

        try:
            files = sorted(
                (path for path in namespace_dir.glob("*.pkl") if path.is_file()),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
        except Exception as exc:
            logger.warning("[PersistentSnapshotCache] failed to list cache dir %s: %s", namespace_dir, exc)
            return

        for path in files[PERSISTENT_SNAPSHOT_CACHE_MAX_FILES:]:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
