"""Bind a local replica generation to the central file that produced it."""
from __future__ import annotations

import json
import os
import uuid


def fingerprint(path: str) -> list[int]:
    info = os.stat(path)
    return [int(info.st_dev), int(info.st_ino), info.st_size, info.st_mtime_ns]


def read_baseline(local_path: str, source_identity: dict) -> dict | None:
    try:
        with open(local_path + ".source.json", encoding="utf-8") as handle:
            record = json.load(handle)
        if (record.get("format") == 1 and record.get("source") == source_identity
                and record.get("local") == fingerprint(local_path)):
            return record
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def baseline_matches(local_path: str, source_identity: dict) -> bool:
    return read_baseline(local_path, source_identity) is not None


def remember_source(local_path: str, source_identity: dict, *, source_stamp=None, state=None) -> None:
    if not source_identity:
        return
    target = local_path + ".source.json"
    temporary = target + "." + uuid.uuid4().hex + ".tmp"
    try:
        with open(temporary, "x", encoding="utf-8") as handle:
            json.dump({"format": 1, "source": source_identity,
                       "source_stamp": source_stamp, "state": state,
                       "local": fingerprint(local_path)}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            os.remove(temporary)
        except FileNotFoundError:
            pass
