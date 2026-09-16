"""Cross-process admission and maintenance coordination for unified entry.

The durable JSON state is useful for notification and operator display.  The
OS byte-range lock is the safety boundary: role sessions keep a shared lock
for their complete lifetime and maintenance keeps the matching exclusive lock.
Session marker files are deliberately never used as proof that a resource is
free.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import socket
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


FORMAT_VERSION = 1
_VALID_STATES = frozenset({"open", "draining", "maintenance"})
_ROLE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_LOCAL_GUARDS: dict[str, threading.RLock] = {}
_LOCAL_GUARDS_LOCK = threading.Lock()


class UnifiedAccessError(RuntimeError):
    """Base error for the unified access protocol."""


class MaintenanceStateError(UnifiedAccessError):
    """The durable maintenance state is invalid for the requested action."""


class MaintenanceConflictError(UnifiedAccessError):
    """The caller is trying to mutate a newer or differently owned operation."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _normalize_root(root: str | os.PathLike[str]) -> Path:
    raw = os.fspath(root) if root is not None else ""
    if not str(raw).strip():
        raise ValueError("A configured database root is required")
    return Path(os.path.abspath(os.path.normpath(raw)))


def _local_guard(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path))
    with _LOCAL_GUARDS_LOCK:
        return _LOCAL_GUARDS.setdefault(key, threading.RLock())


if os.name == "nt":
    from ctypes import wintypes

    class _Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", wintypes.WPARAM),
            ("InternalHigh", wintypes.WPARAM),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.LockFileEx.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    )
    _kernel32.LockFileEx.restype = wintypes.BOOL
    _kernel32.UnlockFileEx.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    )
    _kernel32.UnlockFileEx.restype = wintypes.BOOL


class _ByteLock:
    """A one-byte shared/exclusive lock backed by LockFileEx or flock."""

    def __init__(self, path: Path, *, shared: bool):
        self.path = path
        self.shared = bool(shared)
        self._fd: int | None = None
        self._overlapped: Any = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self, *, blocking: bool, create: bool = True) -> bool:
        if self.held:
            return True
        flags = os.O_RDWR | (os.O_CREAT if create else 0)
        try:
            fd = os.open(str(self.path), flags, 0o600)
        except FileNotFoundError:
            return False
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
                os.fsync(fd)
            if os.name == "nt":
                import msvcrt

                lock_flags = 0 if self.shared else 0x00000002  # LOCKFILE_EXCLUSIVE_LOCK
                if not blocking:
                    lock_flags |= 0x00000001  # LOCKFILE_FAIL_IMMEDIATELY
                overlapped = _Overlapped()
                handle = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
                if not _kernel32.LockFileEx(handle, lock_flags, 0, 1, 0, ctypes.byref(overlapped)):
                    error = ctypes.get_last_error()
                    if not blocking and error in (32, 33, 158):
                        os.close(fd)
                        return False
                    raise ctypes.WinError(error)
                self._overlapped = overlapped
            else:
                import fcntl

                operation = fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX
                if not blocking:
                    operation |= fcntl.LOCK_NB
                try:
                    fcntl.flock(fd, operation)
                except BlockingIOError:
                    os.close(fd)
                    return False
            self._fd = fd
            return True
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            if os.name == "nt":
                import msvcrt

                handle = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
                overlapped = self._overlapped or _Overlapped()
                if not _kernel32.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped)):
                    raise ctypes.WinError(ctypes.get_last_error())
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            self._overlapped = None
            os.close(fd)


class MaintenanceLease:
    """Exclusive maintenance ownership of the shared session gate."""

    def __init__(
        self,
        lock: _ByteLock,
        *,
        operation_id: str,
        generation: int,
        owner_token: str,
    ):
        self._lock = lock
        self.operation_id = operation_id
        self.generation = generation
        self.owner_token = owner_token

    @property
    def held(self) -> bool:
        return self._lock.held

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> "MaintenanceLease":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


class MaintenanceStore:
    """Durable state machine and exclusive maintenance admission."""

    def __init__(self, root: str | os.PathLike[str]):
        self.root = _normalize_root(root)
        self.control_dir = self.root / "session_locks" / "unified_access"
        self.state_path = self.control_dir / "state.json"
        self.state_guard_path = self.control_dir / "state.guard"
        self.session_gate_path = self.control_dir / "sessions.gate"
        self.sessions_dir = self.control_dir / "sessions"
        self._owner_token: str | None = None
        self._operation_id: str | None = None
        self._generation: int | None = None
        self._exclusive_lease: MaintenanceLease | None = None

    @staticmethod
    def _initial_open_state() -> dict[str, Any]:
        return {
            "format_version": FORMAT_VERSION,
            "state": "open",
            "generation": 0,
            "operation_id": None,
            "owner_token": None,
            "reason": "",
            "initiator": None,
            "published_at": None,
        }

    @staticmethod
    def _unknown(error: str) -> dict[str, Any]:
        return {
            "format_version": FORMAT_VERSION,
            "state": "unknown",
            "generation": None,
            "operation_id": None,
            "owner_token": None,
            "reason": "",
            "initiator": None,
            "published_at": None,
            "error": error,
        }

    @staticmethod
    def _validate_state(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("state is not an object")
        if payload.get("format_version") != FORMAT_VERSION:
            raise ValueError("unsupported format_version")
        state = payload.get("state")
        generation = payload.get("generation")
        if state not in _VALID_STATES:
            raise ValueError("invalid state")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ValueError("invalid generation")
        operation_id = payload.get("operation_id")
        owner_token = payload.get("owner_token")
        if state == "open":
            if operation_id is not None or owner_token is not None:
                raise ValueError("open state retains operation ownership")
        elif (
            not isinstance(operation_id, str)
            or not operation_id
            or not isinstance(owner_token, str)
            or not owner_token
        ):
            raise ValueError("active state has no operation ownership")
        return dict(payload)

    def _read_state_unlocked(self, *, allow_initial: bool = False) -> dict[str, Any]:
        if not self.root.is_dir():
            return self._unknown("root_unavailable")
        try:
            with self.state_path.open("r", encoding="utf-8") as handle:
                return self._validate_state(json.load(handle))
        except FileNotFoundError:
            if self.control_dir.exists() and not allow_initial:
                return self._unknown("state_missing_after_initialization")
            return self._initial_open_state()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return self._unknown("malformed_or_unreadable_state")

    @contextmanager
    def _state_guard(self, *, create: bool = True) -> Iterator[None]:
        if create:
            if not self.root.is_dir():
                raise MaintenanceStateError("Configured database root is unavailable")
            self.control_dir.mkdir(parents=True, exist_ok=True)
        process_guard = _local_guard(self.state_guard_path)
        with process_guard:
            lock = _ByteLock(self.state_guard_path, shared=False)
            if not lock.acquire(blocking=True, create=create):
                raise MaintenanceStateError("Maintenance state lock is unavailable")
            try:
                yield
            finally:
                lock.release()

    def read(self) -> dict[str, Any]:
        """Read a snapshot without creating control files on a pristine root."""
        if not self.root.is_dir():
            return self._unknown("root_unavailable")
        if not self.state_guard_path.is_file():
            if self.state_path.exists() or self.control_dir.exists():
                return self._unknown("state_guard_missing")
            return self._initial_open_state()
        try:
            with self._state_guard(create=False):
                return self._read_state_unlocked()
        except (OSError, UnifiedAccessError):
            return self._unknown("state_guard_unavailable")

    def _write_state_unlocked(self, payload: dict[str, Any]) -> None:
        checked = self._validate_state(payload)
        temporary = self.control_dir / f".state.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        raw = json.dumps(checked, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        fd: int | None = None
        try:
            fd = os.open(str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            written = 0
            while written < len(raw):
                count = os.write(fd, raw[written:])
                if count <= 0:
                    raise OSError("state write made no progress")
                written += count
            os.fsync(fd)
            os.close(fd)
            fd = None
            os.replace(str(temporary), str(self.state_path))
            if os.name != "nt":
                directory_fd = os.open(str(self.control_dir), os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if fd is not None:
                os.close(fd)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _require_known(state: dict[str, Any]) -> None:
        if state.get("state") == "unknown":
            raise MaintenanceStateError(f"Maintenance state is unavailable: {state.get('error')}")

    @staticmethod
    def _initiator() -> dict[str, Any]:
        return {"host": socket.gethostname(), "pid": os.getpid()}

    def begin(self, reason: str = "", *, expected_generation: int | None = None) -> dict[str, Any]:
        """Atomically publish DRAINING and reject all subsequent role entries."""
        pristine = not self.control_dir.exists()
        with self._state_guard():
            current = self._read_state_unlocked(allow_initial=pristine)
            self._require_known(current)
            if expected_generation is not None and current["generation"] != expected_generation:
                raise MaintenanceConflictError("Maintenance generation changed")
            if current["state"] != "open":
                raise MaintenanceStateError(f"Cannot begin maintenance while state is {current['state']}")
            operation_id = uuid.uuid4().hex
            owner_token = uuid.uuid4().hex
            state = {
                "format_version": FORMAT_VERSION,
                "state": "draining",
                "generation": current["generation"] + 1,
                "operation_id": operation_id,
                "owner_token": owner_token,
                "reason": str(reason or ""),
                "initiator": self._initiator(),
                "published_at": _utc_now(),
            }
            self._write_state_unlocked(state)
            self._owner_token = owner_token
            self._operation_id = operation_id
            self._generation = state["generation"]
            return dict(state)

    def _credentials(
        self,
        *,
        expected_generation: int | None,
        operation_id: str | None,
        owner_token: str | None,
    ) -> tuple[int | None, str | None, str | None]:
        return (
            self._generation if expected_generation is None else expected_generation,
            self._operation_id if operation_id is None else operation_id,
            self._owner_token if owner_token is None else owner_token,
        )

    @staticmethod
    def _check_credentials(
        state: dict[str, Any],
        expected_generation: int | None,
        operation_id: str | None,
        owner_token: str | None,
    ) -> None:
        if expected_generation is None or state["generation"] != expected_generation:
            raise MaintenanceConflictError("Maintenance generation changed or was not supplied")
        if not operation_id or state.get("operation_id") != operation_id:
            raise MaintenanceConflictError("Maintenance operation changed")
        if not owner_token or state.get("owner_token") != owner_token:
            raise MaintenanceConflictError("Maintenance operation is owned by another controller")

    def legacy_session_markers(self) -> tuple[str, ...]:
        """Return conservative legacy blockers; their age is never trusted."""
        candidates: list[Path] = []
        for directory in (self.root / "session_locks", self.root / "locks" / "sessions"):
            try:
                candidates.extend(path for path in directory.glob("*.lock") if path.is_file())
            except OSError:
                # An unreadable legacy location cannot establish that it is free.
                candidates.append(directory / "<unreadable>.lock")
        return tuple(sorted({str(path) for path in candidates}))

    def try_exclusive(
        self,
        *,
        expected_generation: int | None = None,
        operation_id: str | None = None,
        owner_token: str | None = None,
    ) -> MaintenanceLease | None:
        """Enter MAINTENANCE only when no new-protocol or legacy session blocks it."""
        if self._exclusive_lease is not None and self._exclusive_lease.held:
            return self._exclusive_lease
        expected_generation, operation_id, owner_token = self._credentials(
            expected_generation=expected_generation,
            operation_id=operation_id,
            owner_token=owner_token,
        )
        with self._state_guard():
            state = self._read_state_unlocked()
            self._require_known(state)
            if state["state"] not in ("draining", "maintenance"):
                raise MaintenanceStateError(f"Cannot acquire maintenance access while state is {state['state']}")
            self._check_credentials(state, expected_generation, operation_id, owner_token)
            if self.legacy_session_markers():
                return None
            gate = _ByteLock(self.session_gate_path, shared=False)
            if not gate.acquire(blocking=False):
                return None
            try:
                if state["state"] == "draining":
                    state = {
                        **state,
                        "state": "maintenance",
                        "generation": state["generation"] + 1,
                        "published_at": _utc_now(),
                    }
                    self._write_state_unlocked(state)
                lease = MaintenanceLease(
                    gate,
                    operation_id=str(operation_id),
                    generation=state["generation"],
                    owner_token=str(owner_token),
                )
                self._exclusive_lease = lease
                self._owner_token = str(owner_token)
                self._operation_id = str(operation_id)
                self._generation = state["generation"]
                return lease
            except Exception:
                gate.release()
                raise

    def finish(
        self,
        *,
        expected_generation: int | None = None,
        operation_id: str | None = None,
        owner_token: str | None = None,
    ) -> dict[str, Any]:
        """Cancel DRAINING or finish exclusively-held MAINTENANCE and reopen entry."""
        expected_generation, operation_id, owner_token = self._credentials(
            expected_generation=expected_generation,
            operation_id=operation_id,
            owner_token=owner_token,
        )
        with self._state_guard():
            state = self._read_state_unlocked()
            self._require_known(state)
            if state["state"] == "open":
                if expected_generation is not None and state["generation"] != expected_generation:
                    raise MaintenanceConflictError("Maintenance generation changed")
                return state
            self._check_credentials(state, expected_generation, operation_id, owner_token)
            lease = self._exclusive_lease
            if state["state"] == "maintenance":
                if (
                    lease is None
                    or not lease.held
                    or lease.operation_id != operation_id
                    or lease.owner_token != owner_token
                    or lease.generation != state["generation"]
                ):
                    raise MaintenanceConflictError("The caller does not hold exclusive maintenance access")
            reopened = {
                "format_version": FORMAT_VERSION,
                "state": "open",
                "generation": state["generation"] + 1,
                "operation_id": None,
                "owner_token": None,
                "reason": "",
                "initiator": None,
                "published_at": _utc_now(),
                "last_operation_id": state["operation_id"],
                "last_reason": state.get("reason", ""),
            }
            self._write_state_unlocked(reopened)
            # Keep the state guard until the exclusive gate is released.  A
            # new session can therefore never observe OPEN while it is blocked
            # by the just-finished maintenance owner.
            if lease is not None and lease.held:
                lease.release()
            self._exclusive_lease = None
            self._owner_token = None
            self._operation_id = None
            self._generation = reopened["generation"]
            return dict(reopened)

    def cancel(self, **credentials: Any) -> dict[str, Any]:
        """Explicit name for reopening a DRAINING operation before exclusivity."""
        return self.finish(**credentials)


class SessionLease:
    """Shared admission lease retained for the complete lifetime of one role."""

    def __init__(self, root: str | os.PathLike[str], role: str):
        self.store = MaintenanceStore(root)
        role_value = str(role or "").strip().casefold()
        if not _ROLE_RE.fullmatch(role_value):
            raise ValueError("Role must be a non-empty safe identifier")
        self.role = role_value
        self.session_id = uuid.uuid4().hex
        self.lease_token = uuid.uuid4().hex
        self.host = socket.gethostname()
        self.pid = os.getpid()
        self.acquired_at: str | None = None
        self.generation: int | None = None
        self.operation_id: str | None = None
        self.rejection_state: dict[str, Any] | None = None
        self.metadata_path = self.store.sessions_dir / f"{self.session_id}.json"
        self._gate: _ByteLock | None = None

    @property
    def held(self) -> bool:
        return self._gate is not None and self._gate.held

    @property
    def ownership(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "lease_token": self.lease_token,
            "role": self.role,
            "host": self.host,
            "pid": self.pid,
            "acquired_at": self.acquired_at,
            "generation": self.generation,
            "operation_id": self.operation_id,
            "metadata_path": str(self.metadata_path),
            "held": self.held,
        }

    @property
    def ownership_fields(self) -> dict[str, Any]:
        return self.ownership

    def _write_metadata(self) -> None:
        self.store.sessions_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.store.sessions_dir / f".{self.session_id}.{uuid.uuid4().hex}.tmp"
        payload = self.ownership
        payload.pop("metadata_path", None)
        payload.pop("held", None)
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        fd: int | None = None
        try:
            fd = os.open(str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            written = 0
            while written < len(raw):
                count = os.write(fd, raw[written:])
                if count <= 0:
                    raise OSError("session metadata write made no progress")
                written += count
            os.fsync(fd)
            os.close(fd)
            fd = None
            os.replace(str(temporary), str(self.metadata_path))
        finally:
            if fd is not None:
                os.close(fd)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def acquire(self) -> bool:
        if self.held:
            return True
        from rem_card.app.local_administrator import is_local_administrator
        administrator = is_local_administrator()
        pristine = not self.store.control_dir.exists()
        with self.store._state_guard():
            state = self.store._read_state_unlocked(allow_initial=pristine)
            self.rejection_state = None
            if state.get("state") != "open" and not (
                administrator and state.get("state") in ("draining", "maintenance")
            ):
                self.rejection_state = state
                return False
            if not self.store.state_path.exists():
                # The first mutating participant establishes generation zero.
                # Plain read() intentionally does not perform this creation.
                self.store._write_state_unlocked(state)
            gate = _ByteLock(self.store.session_gate_path, shared=True)
            if not gate.acquire(blocking=False):
                self.rejection_state = MaintenanceStore._unknown("exclusive_maintenance_lock_held")
                return False
            if state.get("state") == "maintenance":
                # Never bypass an exclusive OS lease. Re-enter draining only
                # after acquiring the shared gate under the state guard.
                state = {**state, 'state': 'draining',
                         'generation': state['generation'] + 1, 'published_at': _utc_now()}
                try:
                    self.store._write_state_unlocked(state)
                except Exception:
                    gate.release()
                    raise
            self._gate = gate
            self.acquired_at = _utc_now()
            self.generation = state["generation"]
            self.operation_id = state["operation_id"]
            try:
                self._write_metadata()
            except Exception:
                self._gate = None
                gate.release()
                self.acquired_at = None
                self.generation = None
                raise
            return True

    def release(self) -> None:
        gate = self._gate
        self._gate = None
        if gate is None:
            return
        try:
            try:
                with self.metadata_path.open("r", encoding="utf-8") as handle:
                    current = json.load(handle)
                if (
                    isinstance(current, dict)
                    and current.get("session_id") == self.session_id
                    and current.get("lease_token") == self.lease_token
                ):
                    self.metadata_path.unlink()
            except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
                # Marker loss/corruption has no bearing on the OS-lock proof.
                pass
        finally:
            gate.release()

    def __enter__(self) -> "SessionLease":
        if not self.acquire():
            state = (self.rejection_state or {}).get("state", "unknown")
            raise MaintenanceStateError(f"Role entry is blocked by maintenance state {state}")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


__all__ = [
    "FORMAT_VERSION",
    "MaintenanceConflictError",
    "MaintenanceLease",
    "MaintenanceStateError",
    "MaintenanceStore",
    "SessionLease",
    "UnifiedAccessError",
]
