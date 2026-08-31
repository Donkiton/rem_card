"""Process-owned log segments. No database, Qt or application logger imports.

The public logging APIs keep their existing payloads. This module only changes
where bytes are stored; event filtering belongs to a separate logging stage.
"""
from __future__ import annotations

import atexit
import logging
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterable


DEFAULT_MAX_FILE_BYTES = 20 * 1024 * 1024
KNOWN_TEXT_PREFIXES = (
    "rem_card", "doctor", "nurse", "nurse_emergency", "operblock",
    "operblock_emergency", "operblock_planned", "path_setup", "startup",
    "updater", "log_maintenance",
)
_WRITERS_LOCK = threading.RLock()
_WRITERS: dict[tuple[int, str, str, str], "LogSegmentWriter"] = {}


def storage_enabled() -> bool:
    return os.environ.get("REMCARD_LOG_STORAGE_ENABLED", "1") != "0"


def positive_int_setting(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except (ValueError, TypeError):
        return default


def safe_log_prefix(prefix: str) -> str:
    prefix = re.sub(r"[^a-z0-9_-]", "_", str(prefix).lower())[:64].strip("_")
    if prefix in (*KNOWN_TEXT_PREFIXES, "metrics", "audit"):
        return prefix
    return "runtime_" + (prefix or "rem_card")


def _request_cleanup(directory: Path) -> None:
    from rem_card.app.runtime_log_retention import request_log_cleanup

    request_log_cleanup(str(directory), rollover=True)


class LogSegmentWriter:
    """Only this object/process ever appends to its randomly named segments.

    An active filename protects the current segment from other cleaners. A
    successful rename publishes a closed, immutable segment. Handles are closed
    after each batch, so readers, the updater and Windows shutdown are not held
    hostage by a cached writer. An entire JSON line is never split or truncated.
    """

    def __init__(self, directory: str, prefix: str, extension: str, *, managed: bool = True):
        self.directory = Path(directory).absolute()
        self.prefix = safe_log_prefix(prefix)
        self.extension = extension
        if extension not in {"log", "jsonl"}:
            raise ValueError("Unsupported runtime log extension")
        self.managed = managed
        self._lock = threading.RLock()
        self._pid = os.getpid()
        self._session = uuid.uuid4().hex
        self._part = 0
        self._day = ""
        self._size = 0
        self.path: Path | None = None

    def _finish_segment(self) -> None:
        if self.path is not None:
            closed = self.path.with_name(self.path.name.replace("_active.", "_closed."))
            try:
                self.path.rename(closed)
            except OSError:
                # A reader can deny rename on Windows. Never reuse or truncate
                # that segment; its active name stays protected until PID exit.
                pass
        self.path = None
        self._size = 0

    def _new_segment(self, day: str) -> None:
        self._finish_segment()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._part += 1
        self._day = day
        name = (
            f"{self.prefix}_{day}_p{self._pid}_s{self._session}"
            f"_{self._part:06d}_active.{self.extension}"
        )
        path = self.directory / name
        # Exclusive creation also refuses a pre-existing link at this name.
        with path.open("xb"):
            pass
        self.path = path
        if self.managed:
            _request_cleanup(self.directory)

    def write(self, lines: Iterable[str]) -> Path | None:
        with self._lock:
            if self._pid != os.getpid():
                # A fork must not append to or rename its parent's segment.
                self._pid = os.getpid()
                self._session = uuid.uuid4().hex
                self.path = None
                self._size = 0
            limit = positive_int_setting("REMCARD_LOG_MAX_FILE_BYTES", DEFAULT_MAX_FILE_BYTES)
            batch = bytearray()
            for line in lines:
                raw = str(line).encode("utf-8")
                day = datetime.now().strftime("%Y%m%d")
                if self.path is None or day != self._day or (
                    self._size + len(batch) > 0 and self._size + len(batch) + len(raw) > limit
                ):
                    self._append(batch)
                    batch.clear()
                    self._new_segment(day)
                batch.extend(raw)
            self._append(batch)
            return self.path

    def _append(self, raw: bytes | bytearray) -> None:
        if not raw or self.path is None:
            return
        try:
            with self.path.open("ab") as stream:
                stream.write(raw)
            self._size += len(raw)
        except OSError:
            # A partial disk-full write must not be followed by another JSON
            # record in the same segment. Keep the forensic fragment as-is.
            self._finish_segment()
            raise

    def close(self) -> None:
        with self._lock:
            if self._pid == os.getpid():
                self._finish_segment()


def append_log_lines(
    directory: str, prefix: str, lines: Iterable[str], *, extension: str = "log",
    managed: bool = True,
) -> Path | None:
    if not storage_enabled():
        directory_path = Path(directory)
        directory_path.mkdir(parents=True, exist_ok=True)
        name = "startup.log" if prefix == "startup" else f"{safe_log_prefix(prefix)}_{datetime.now():%Y%m%d}.{extension}"
        path = directory_path / name
        with path.open("a", encoding="utf-8") as stream:
            stream.writelines(lines)
        return path
    key = (os.getpid(), os.path.normcase(os.path.abspath(directory)), prefix, extension)
    with _WRITERS_LOCK:
        writer = _WRITERS.get(key)
        if writer is None:
            writer = LogSegmentWriter(directory, prefix, extension, managed=managed)
            _WRITERS[key] = writer
    return writer.write(lines)


class RuntimeLogHandler(logging.Handler):
    def __init__(self, directory: str, prefix: str):
        super().__init__()
        self.directory = directory
        self.prefix = prefix
        # Verify the destination during logger setup so fallback still works.
        probe = LogSegmentWriter(directory, prefix, "log", managed=False)
        probe.write([""])
        probe_path = probe.path
        if probe_path is not None:
            probe_path.unlink()
        probe.path = None
        _request_cleanup(Path(directory))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            append_log_lines(self.directory, self.prefix, [self.format(record) + "\n"])
        except Exception:
            self.handleError(record)


def close_log_writers() -> None:
    # Stop the maintenance producer before sealing cached log segments.
    from rem_card.app.runtime_log_retention import stop_log_maintenance

    stop_log_maintenance()
    with _WRITERS_LOCK:
        writers = list(_WRITERS.values())
        _WRITERS.clear()
    for writer in writers:
        writer.close()


atexit.register(close_log_writers)
