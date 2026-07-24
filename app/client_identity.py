from __future__ import annotations

import json
import os
import socket
import threading
import uuid
from pathlib import Path


CLIENT_ID_ENV = "REMCARD_CLIENT_ID"
CLIENT_ID_FILE_NAME = "client_identity.json"
_IDENTITY_LOCK = threading.Lock()
_CACHED_IDENTITY: tuple[str, str] | None = None


def _identity_root() -> Path:
    local_appdata = str(os.environ.get("LOCALAPPDATA") or "").strip()
    if local_appdata:
        return Path(local_appdata) / "RemCard"
    return Path.home() / ".remcard"


def get_client_identity_path() -> Path:
    return _identity_root() / CLIENT_ID_FILE_NAME


def _valid_client_id(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(uuid.UUID(text))
    except (ValueError, TypeError, AttributeError):
        return ""


def _read_client_id(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    return _valid_client_id(payload.get("client_id"))


def _create_client_id(path: Path, client_id: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "version": 1,
            "client_id": client_id,
            "host_at_creation": socket.gethostname(),
        },
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return _read_client_id(path)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    return client_id


def get_client_id() -> str:
    """Return a stable per-workstation UUID without touching the shared database."""
    global _CACHED_IDENTITY
    override = _valid_client_id(os.environ.get(CLIENT_ID_ENV))
    if override:
        return override

    path = get_client_identity_path()
    cache_key = os.path.normcase(os.path.abspath(str(path)))
    with _IDENTITY_LOCK:
        if _CACHED_IDENTITY is not None and _CACHED_IDENTITY[0] == cache_key:
            return _CACHED_IDENTITY[1]
        client_id = _read_client_id(path)
        if not client_id:
            client_id = _create_client_id(path, str(uuid.uuid4()))
        if not client_id:
            raise RuntimeError(f"Не удалось создать идентификатор рабочего места: {path}")
        _CACHED_IDENTITY = (cache_key, client_id)
        return client_id
