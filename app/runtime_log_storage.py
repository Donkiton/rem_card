"""Process-owned log segments. No database, Qt or application logger imports.

The public logging APIs keep their existing payloads. This module only changes
where bytes are stored; event filtering belongs to a separate logging stage.
"""
from __future__ import annotations

import atexit
import logging
import os
import queue
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterable


DEFAULT_MAX_FILE_BYTES = 20 * 1024 * 1024
DEFAULT_LOG_QUEUE_SIZE = 4096
DEFAULT_LOG_BATCH_SIZE = 128
DEFAULT_LOG_FLUSH_TIMEOUT_MS = 500
MAX_LOG_QUEUE_SIZE = 16384
MAX_LOG_BATCH_SIZE = 1024
MAX_LOG_FLUSH_TIMEOUT_MS = 2000
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


def bounded_positive_int_setting(name: str, default: int, maximum: int) -> int:
    return min(maximum, positive_int_setting(name, default))


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

    def close(self, *, blocking: bool = True) -> None:
        if not self._lock.acquire(blocking=blocking):
            return
        try:
            if self._pid == os.getpid():
                self._finish_segment()
        finally:
            self._lock.release()


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

        self._queue_size = bounded_positive_int_setting(
            "REMCARD_LOG_QUEUE_SIZE", DEFAULT_LOG_QUEUE_SIZE, MAX_LOG_QUEUE_SIZE,
        )
        self._batch_size = bounded_positive_int_setting(
            "REMCARD_LOG_BATCH_SIZE", DEFAULT_LOG_BATCH_SIZE, MAX_LOG_BATCH_SIZE,
        )
        self._flush_timeout = (
            bounded_positive_int_setting(
                "REMCARD_LOG_FLUSH_TIMEOUT_MS", DEFAULT_LOG_FLUSH_TIMEOUT_MS,
                MAX_LOG_FLUSH_TIMEOUT_MS,
            ) / 1000.0
        )
        self._state_lock = threading.Lock()
        self._dropped = 0
        self._reported_dropped = 0
        self._write_errors = 0
        self._reported_write_errors = 0
        self._accepting = True
        self._closed_once = False
        self._pid = os.getpid()
        self._start_worker()

    def _start_worker(self) -> None:
        self._queue: queue.Queue[tuple[int, str]] = queue.Queue(maxsize=self._queue_size)
        self._idle = threading.Event()
        self._idle.set()
        self._closing = threading.Event()
        self._stopped = threading.Event()
        self._worker = threading.Thread(
            target=self._write_loop,
            name=f"RemCardLogWriter-{safe_log_prefix(self.prefix)}",
            daemon=True,
        )
        self._worker.start()

    def _reset_after_fork(self) -> None:
        """A child process cannot use the vanished worker inherited from its parent."""
        current_pid = os.getpid()
        if current_pid == self._pid:
            return
        self._pid = current_pid
        self._state_lock = threading.Lock()
        self._accepting = True
        self._closed_once = False
        self._start_worker()

    def _pending_diagnostics(self) -> tuple[list[str], tuple[int, int]]:
        with self._state_lock:
            dropped = self._dropped
            write_errors = self._write_errors
            dropped_delta = dropped - self._reported_dropped
            error_delta = write_errors - self._reported_write_errors
        messages = []
        if dropped_delta:
            messages.append(
                "Runtime log queue overflow; "
                f"dropped_records={dropped_delta} queue_capacity={self._queue_size}"
            )
        if error_delta:
            messages.append(f"Runtime log writer recovered; failed_batches={error_delta}")
        lines = []
        for message in messages:
            record = logging.LogRecord(
                "RemCard.RuntimeLog", logging.WARNING, __file__, 0, message, (), None,
                func="_write_loop",
            )
            lines.append(self.format(record) + "\n")
        return lines, (dropped, write_errors)

    def _mark_diagnostics_written(self, totals: tuple[int, int]) -> None:
        with self._state_lock:
            self._reported_dropped = max(self._reported_dropped, totals[0])
            self._reported_write_errors = max(self._reported_write_errors, totals[1])

    def _write_loop(self) -> None:
        try:
            while True:
                try:
                    first = self._queue.get(timeout=0.05)
                except queue.Empty:
                    if self._closing.is_set():
                        break
                    continue

                batch = [first]
                while len(batch) < self._batch_size:
                    try:
                        batch.append(self._queue.get_nowait())
                    except queue.Empty:
                        break
                diagnostics, totals = self._pending_diagnostics()
                lines = diagnostics + [line for _level, line in batch]
                try:
                    append_log_lines(self.directory, self.prefix, lines)
                except Exception:
                    # Logging must remain best-effort. The next successful batch
                    # records how many write attempts were lost.
                    with self._state_lock:
                        self._write_errors += 1
                else:
                    self._mark_diagnostics_written(totals)
                finally:
                    for _item in batch:
                        self._queue.task_done()
                    with self._state_lock:
                        if self._queue.empty():
                            self._idle.set()
        finally:
            self._stopped.set()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._reset_after_fork()
            item = (record.levelno, self.format(record) + "\n")
            with self._state_lock:
                if not self._accepting:
                    self._dropped += 1
                    return
                self._idle.clear()
                try:
                    self._queue.put_nowait(item)
                    return
                except queue.Full:
                    pass

                # Prefer a new warning/error over one older queued record. This
                # is still non-blocking and keeps the queue strictly bounded.
                if record.levelno >= logging.WARNING:
                    try:
                        self._queue.get_nowait()
                    except queue.Empty:
                        pass
                    else:
                        self._queue.task_done()
                        self._dropped += 1
                        try:
                            self._queue.put_nowait(item)
                            return
                        except queue.Full:
                            pass
                self._dropped += 1
                if self._queue.empty():
                    self._idle.set()
        except Exception:
            self.handleError(record)

    def flush(self) -> None:
        if self._closed_once or os.getpid() != self._pid:
            return
        self._idle.wait(timeout=self._flush_timeout)

    def close(self) -> None:
        if self._closed_once:
            return
        self._closed_once = True
        with self._state_lock:
            self._accepting = False
        deadline = time.monotonic() + self._flush_timeout
        self._idle.wait(timeout=self._flush_timeout)
        self._closing.set()
        remaining = max(0.0, deadline - time.monotonic())
        if remaining:
            self._worker.join(timeout=remaining)
        super().close()


def close_log_writers() -> None:
    # Stop the maintenance producer before sealing cached log segments.
    from rem_card.app.runtime_log_retention import _is_local_path, stop_log_maintenance

    stop_log_maintenance()
    with _WRITERS_LOCK:
        writers = list(_WRITERS.values())
        _WRITERS.clear()
    for writer in writers:
        # Never wait for the best-effort audit mirror, nor do SMB rename from
        # the exiting UI process. An unsealed segment is still valid/readable;
        # dead-PID detection makes it eligible for its normal retention later.
        if writer.managed and _is_local_path(writer.directory):
            writer.close(blocking=False)


atexit.register(close_log_writers)
