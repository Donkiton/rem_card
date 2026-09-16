"""Opt-in maintenance access for this Windows profile, installation and version."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import uuid

from rem_card.app.version import APP_VERSION


def _identity():
    location = Path(sys.executable) if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1]
    return {'host': socket.gethostname().casefold(),
            'installation': os.path.normcase(str(location.resolve())), 'version': APP_VERSION}


def _path():
    identity = _identity()
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode('utf-8')).hexdigest()
    base = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')
    return base / 'rem_card' / 'maintenance_access' / (key + '.json')


def is_local_administrator():
    try:
        value = json.loads(_path().read_text(encoding='utf-8'))
        return value == {**_identity(), 'administrator': True}
    except (OSError, ValueError, TypeError):
        return False


def set_local_administrator(enabled):
    path = _path()
    if not enabled:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump({**_identity(), 'administrator': True}, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
