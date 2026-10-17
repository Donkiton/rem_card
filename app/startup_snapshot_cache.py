"""User-bound persistent cache for a previously validated startup snapshot.

The cache is only a local baseline for the killable startup worker.  A caller
must still prove the live source admission before and after using it.  The
manifest is protected with Windows DPAPI so editing a JSON file cannot forge a
new admission or extend the original full-check deadline.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import json
import os
import shutil
import stat
import tempfile
import time
from typing import Any


_FORMAT = 1
_CACHE_DIR = "startup_snapshot_cache"
_DB_FILE = "validated.db"
_MANIFEST_FILE = "manifest.dpapi"
_MAX_MANIFEST_BYTES = 1024 * 1024
_DPAPI_ENTROPY = b"RemCard startup validated snapshot cache v1"


class _DataBlob(ctypes.Structure):
    _fields_ = (("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte)))


def _blob(data: bytes) -> tuple[_DataBlob, Any]:
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _crypt(data: bytes, *, protect: bool) -> bytes | None:
    """Return DPAPI output, or None when trustworthy sealing is unavailable."""
    if os.name != "nt" or not data:
        return None
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
        kernel32.LocalFree.restype = wintypes.HLOCAL
        source, source_buffer = _blob(data)
        entropy, entropy_buffer = _blob(_DPAPI_ENTROPY)
        output = _DataBlob()
        # Keep ctypes buffers alive across the native call.
        _ = source_buffer, entropy_buffer
        if protect:
            function = crypt32.CryptProtectData
            function.argtypes = (
                ctypes.POINTER(_DataBlob), wintypes.LPCWSTR,
                ctypes.POINTER(_DataBlob), wintypes.LPVOID, wintypes.LPVOID,
                wintypes.DWORD, ctypes.POINTER(_DataBlob),
            )
            function.restype = wintypes.BOOL
            ok = function(ctypes.byref(source), None, ctypes.byref(entropy), None, None,
                          0x1, ctypes.byref(output))
        else:
            function = crypt32.CryptUnprotectData
            function.argtypes = (
                ctypes.POINTER(_DataBlob), ctypes.POINTER(wintypes.LPWSTR),
                ctypes.POINTER(_DataBlob), wintypes.LPVOID, wintypes.LPVOID,
                wintypes.DWORD, ctypes.POINTER(_DataBlob),
            )
            function.restype = wintypes.BOOL
            description = wintypes.LPWSTR()
            ok = function(ctypes.byref(source), ctypes.byref(description), ctypes.byref(entropy),
                          None, None, 0x1, ctypes.byref(output))
            if description:
                kernel32.LocalFree(ctypes.cast(description, wintypes.HLOCAL))
        if not ok or not output.pbData:
            return None
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            kernel32.LocalFree(ctypes.cast(output.pbData, wintypes.HLOCAL))
    except (AttributeError, OSError, ValueError):
        return None


def _seal(data: bytes) -> bytes | None:
    return _crypt(data, protect=True)


def _unseal(data: bytes) -> bytes | None:
    return _crypt(data, protect=False)


def _source_name(source_path: str) -> tuple[str, str]:
    normalized = os.path.normcase(os.path.abspath(source_path))
    return normalized, hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def cache_paths(source_path: str) -> tuple[str, str]:
    from rem_card.app.runtime_paths import get_local_cache_dir
    _, name = _source_name(source_path)
    folder = os.path.abspath(os.path.join(get_local_cache_dir(), _CACHE_DIR, name))
    return os.path.join(folder, _DB_FILE), os.path.join(folder, _MANIFEST_FILE)


def _digest(path: str) -> str:
    result = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def _regular_local_file(path: str) -> bool:
    try:
        info = os.lstat(path)
        return (stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)
                and not getattr(info, "st_file_attributes", 0) & 0x400)
    except OSError:
        return False


def _read_sealed(path: str) -> tuple[dict, bytes] | None:
    if not _regular_local_file(path):
        return None
    try:
        if os.path.getsize(path) > _MAX_MANIFEST_BYTES:
            return None
        with open(path, "rb") as handle:
            sealed = handle.read(_MAX_MANIFEST_BYTES + 1)
        plain = _unseal(sealed)
        if plain is None:
            return None
        value = json.loads(plain.decode("utf-8"))
        return (value, sealed) if isinstance(value, dict) else None
    except (OSError, UnicodeError, ValueError, TypeError):
        return None


def load(source_path: str, *, ttl_sec: float, now_epoch: float | None = None,
         destination_path: str | None = None) -> dict | None:
    """Load and authenticate cache metadata without trusting it for admission."""
    db_path, manifest_path = cache_paths(source_path)
    loaded = _read_sealed(manifest_path)
    if loaded is None or not _regular_local_file(db_path):
        return None
    manifest, sealed = loaded
    normalized, _ = _source_name(source_path)
    try:
        checked = float(manifest["full_checked_at_epoch"])
        age = (time.time() if now_epoch is None else float(now_epoch)) - checked
        valid = (
            manifest.get("format") == _FORMAT
            and manifest.get("source_path") == normalized
            and isinstance(manifest.get("admission"), dict)
            and isinstance(manifest.get("state"), dict)
            and isinstance(manifest.get("schema_digest"), str)
            and (manifest.get("source_stamp") is None
                 or isinstance(manifest.get("source_stamp"), dict))
            and isinstance(manifest.get("sha256"), str)
            and int(manifest.get("size_bytes")) == os.path.getsize(db_path)
            and 0 <= age < float(ttl_sec)
            and _digest(db_path) == manifest["sha256"]
        )
        if not valid:
            return None
        result_path = db_path
        if destination_path:
            os.makedirs(os.path.dirname(os.path.abspath(destination_path)), exist_ok=True)
            shutil.copyfile(db_path, destination_path)
            with open(destination_path, "r+b") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            if (_digest(destination_path) != manifest["sha256"]
                    or os.path.getsize(destination_path) != int(manifest["size_bytes"])):
                os.remove(destination_path)
                return None
            result_path = os.path.abspath(destination_path)
        # Atomic cache replacement racing validation/materialization must not
        # mix a DB generation with another generation's sealed receipt.
        with open(manifest_path, "rb") as handle:
            if handle.read(_MAX_MANIFEST_BYTES + 1) != sealed:
                if destination_path:
                    os.remove(destination_path)
                return None
    except (KeyError, OSError, TypeError, ValueError, OverflowError):
        if destination_path:
            try:
                os.remove(destination_path)
            except OSError:
                pass
        return None
    return {**manifest, "path": result_path}


def store(source_path: str, snapshot_path: str, *, admission: dict, state: dict,
          schema_digest: str, source_stamp: dict | None,
          full_checked_at_epoch: float) -> bool:
    """Atomically publish DB bytes followed by their user-bound manifest."""
    db_path, manifest_path = cache_paths(source_path)
    folder = os.path.dirname(db_path)
    db_temp = manifest_temp = ""
    try:
        os.makedirs(folder, exist_ok=True)
        descriptor, db_temp = tempfile.mkstemp(prefix="validated.", suffix=".tmp", dir=folder)
        os.close(descriptor)
        shutil.copyfile(snapshot_path, db_temp)
        with open(db_temp, "r+b") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        digest = _digest(db_temp)
        normalized, _ = _source_name(source_path)
        manifest = {
            "format": _FORMAT,
            "source_path": normalized,
            "admission": admission,
            "state": state,
            "schema_digest": str(schema_digest),
            "source_stamp": source_stamp if isinstance(source_stamp, dict) else None,
            "full_checked_at_epoch": float(full_checked_at_epoch),
            "size_bytes": os.path.getsize(db_temp),
            "sha256": digest,
        }
        plain = json.dumps(manifest, ensure_ascii=True, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
        sealed = _seal(plain)
        if sealed is None:
            return False
        descriptor, manifest_temp = tempfile.mkstemp(prefix="manifest.", suffix=".tmp", dir=folder)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(sealed)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(db_temp, db_path)
        db_temp = ""
        os.replace(manifest_temp, manifest_path)
        manifest_temp = ""
        return True
    except (OSError, TypeError, ValueError, OverflowError):
        return False
    finally:
        for path in (db_temp, manifest_temp):
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
