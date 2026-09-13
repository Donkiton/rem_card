import json
import logging
import os
import socket
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Optional


_ROLE_LOCK_READ_UNAVAILABLE = object()


class RoleSessionLock:
    """
    Сетевой lock роли (doctor/nurse/add_patient) на общей папке.
    Держится heartbeat'ом и автоматически считается stale при обрыве процесса.
    """

    def __init__(
        self,
        lock_path: str,
        role: str,
        owner_id: str,
        *,
        owner_role: str | None = None,
        stale_timeout_sec: float = 45.0,
        heartbeat_sec: float = 10.0,
        logger: Optional[logging.Logger] = None,
    ):
        self.lock_path = lock_path
        self.role = role
        self.owner_id = owner_id
        self.owner_role = str(owner_role or "").strip().casefold()
        self.stale_timeout_sec = stale_timeout_sec
        self.heartbeat_sec = heartbeat_sec
        self.logger = logger or logging.getLogger(__name__)

        self._token: Optional[dict[str, Any]] = None
        self._last_holder: Any = None
        self._stop_evt = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._mutex = threading.Lock()

    def _build_payload(self) -> dict[str, Any]:
        payload = {
            "timestamp": time.time(),
            "role": self.role,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "owner_id": self.owner_id,
            "nonce": uuid.uuid4().hex,
        }
        if self.owner_role:
            payload["owner_role"] = self.owner_role
        return payload

    def _read_payload(self):
        try:
            with open(self.lock_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return None
        except Exception as exc:
            self.logger.warning("Failed to read role lock %s: %s", self.lock_path, exc)
            return _ROLE_LOCK_READ_UNAVAILABLE

    @staticmethod
    def _host_aliases() -> set[str]:
        aliases: set[str] = set()
        candidates = [
            socket.gethostname(),
            socket.getfqdn(),
            os.environ.get("COMPUTERNAME"),
            os.environ.get("HOSTNAME"),
        ]
        for name in candidates:
            if not name:
                continue
            norm = str(name).strip().lower()
            if not norm:
                continue
            aliases.add(norm)
            aliases.add(norm.split(".")[0])
        return aliases

    def _is_local_host(self, host_value: Any) -> bool:
        if not host_value:
            return False
        host = str(host_value).strip().lower()
        if not host:
            return False
        aliases = self._host_aliases()
        return host in aliases or host.split(".")[0] in aliases

    @staticmethod
    def _is_pid_alive_local(pid_value: Any) -> Optional[bool]:
        """Safely reports local process liveness; ``None`` means unknown."""
        try:
            pid = int(pid_value)
        except (TypeError, ValueError):
            return None
        if pid <= 0:
            return None
        if pid == os.getpid():
            return True
        if os.name == "nt":
            try:
                import ctypes
                from ctypes import wintypes

                process_query_limited_information = 0x1000
                still_active = 259
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
                kernel32.OpenProcess.restype = wintypes.HANDLE
                kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
                kernel32.GetExitCodeProcess.restype = wintypes.BOOL
                kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
                kernel32.CloseHandle.restype = wintypes.BOOL

                handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
                if not handle:
                    error_code = ctypes.get_last_error()
                    if error_code == 87:  # ERROR_INVALID_PARAMETER: PID does not exist.
                        return False
                    if error_code == 5:  # ERROR_ACCESS_DENIED: process exists but cannot be queried.
                        return True
                    return None
                try:
                    exit_code = wintypes.DWORD()
                    if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                        return None
                    return int(exit_code.value) == still_active
                finally:
                    kernel32.CloseHandle(handle)
            except Exception:
                # An unavailable/failed query must keep the lock rather than
                # risk deleting a marker owned by a live process.
                return None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # Процесс есть, но не хватает прав на сигнал.
            return True
        except OSError:
            return False
        except Exception:
            return None
        return True

    def _lock_file_signature(self) -> Optional[tuple[int, int, int, int]]:
        try:
            stat = os.stat(self.lock_path)
            return (
                int(stat.st_dev),
                int(stat.st_ino),
                int(stat.st_size),
                int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
            )
        except FileNotFoundError:
            return None
        except Exception:
            return None

    def _lock_file_age(self) -> Optional[float]:
        try:
            return max(0.0, time.time() - os.path.getmtime(self.lock_path))
        except FileNotFoundError:
            return None
        except Exception:
            return None

    @staticmethod
    def _payload_age_sec(payload: Optional[dict[str, Any]]) -> Optional[float]:
        if not isinstance(payload, dict):
            return None
        ts = payload.get("timestamp")
        if not isinstance(ts, (int, float)):
            return None
        return max(0.0, time.time() - float(ts))

    @staticmethod
    def _payload_summary(payload: Optional[dict[str, Any]]) -> str:
        if not isinstance(payload, dict):
            return "empty"
        host = payload.get("host", "?")
        pid = payload.get("pid", "?")
        owner_id = payload.get("owner_id", "?")
        role = payload.get("role", "?")
        return f"role={role}, host={host}, pid={pid}, owner_id={owner_id}"

    def _is_stale(self, payload: Optional[dict[str, Any]]) -> bool:
        if payload is _ROLE_LOCK_READ_UNAVAILABLE:
            return False
        if payload is None:
            return True
        if not isinstance(payload, dict) or not payload:
            return False

        # Ключевой кейс: приложение аварийно закрыли на ЭТОМ же ПК.
        # В этом случае снимаем лок сразу, не дожидаясь timeout.
        holder_host = payload.get("host")
        holder_pid = payload.get("pid")
        if self._is_local_host(holder_host):
            pid_alive = self._is_pid_alive_local(holder_pid)
            if pid_alive is True:
                return False
            if pid_alive is False:
                return True
            return False

        file_age = self._lock_file_age()
        if file_age is not None and file_age <= self.stale_timeout_sec:
            return False

        payload_age = self._payload_age_sec(payload)
        if payload_age is not None and payload_age <= self.stale_timeout_sec:
            return False
        if payload_age is not None and payload_age > self.stale_timeout_sec:
            return True
        if file_age is not None and file_age > self.stale_timeout_sec:
            return True

        return False

    def _cleanup_if_stale(self, payload: Optional[dict[str, Any]]) -> bool:
        """
        Пытается удалить stale lock-файл.
        Возвращает True, если stale-lock успешно очищен или уже отсутствует.
        Возвращает False, если lock не stale или stale, но удалить не удалось.
        """
        if payload is None:
            return not os.path.exists(self.lock_path)
        if not isinstance(payload, dict) or not payload:
            return False
        expected_nonce = str(payload.get("nonce") or "").strip()
        if not expected_nonce:
            return False
        signature_before = self._lock_file_signature()
        if signature_before is None or not self._is_stale(payload):
            return False
        current = self._read_payload()
        signature_after = self._lock_file_signature()
        if (
            not isinstance(current, dict)
            or str(current.get("nonce") or "").strip() != expected_nonce
            or signature_after != signature_before
        ):
            return False
        try:
            os.remove(self.lock_path)
            self.logger.warning(
                "Removed stale role lock: %s holder=(%s) age_sec=%.1f file_age_sec=%.1f",
                self.lock_path,
                self._payload_summary(payload),
                self._payload_age_sec(payload) or -1.0,
                self._lock_file_age() or -1.0,
            )
            return True
        except FileNotFoundError:
            return True
        except Exception as exc:
            self.logger.warning("Failed to remove stale role lock %s: %s", self.lock_path, exc)
            return False

    def ownership_context(self) -> Optional[dict[str, str]]:
        """Возвращает точный идентификатор удерживаемого этим объектом lock'а."""
        with self._mutex:
            token = dict(self._token) if self._token else None
        nonce = str((token or {}).get("nonce") or "").strip()
        if not nonce:
            return None
        return {
            "path": os.path.abspath(self.lock_path),
            "nonce": nonce,
            "role": str(self.role or ""),
        }

    def is_held_by_other(self, *, ignored_nonce: str = "") -> bool:
        """
        Проверяет, занят ли lock другим процессом.
        Stale-lock будет очищен автоматически, если это возможно.
        """
        holder = self._read_payload()
        self._last_holder = holder
        if holder is _ROLE_LOCK_READ_UNAVAILABLE:
            return True
        if holder is None:
            return False
        if not isinstance(holder, dict) or not holder:
            return True

        if ignored_nonce and str(holder.get("nonce") or "") == str(ignored_nonce):
            return False

        # Наш же lock (по nonce) — не считаем занятым "другим".
        with self._mutex:
            token = dict(self._token) if self._token else None
        if token and holder.get("nonce") == token.get("nonce"):
            return False
        if holder.get("owner_id") == self.owner_id:
            return False

        # Если stale удалось почистить — lock свободен.
        if self._cleanup_if_stale(holder):
            return False

        # Lock существует и не принадлежит текущему владельцу.
        return True

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)

        while True:
            payload = self._build_payload()
            raw = json.dumps(payload, ensure_ascii=True).encode("utf-8")
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, raw)
                finally:
                    os.close(fd)

                with self._mutex:
                    self._token = payload
                    self._last_holder = None
                self._start_heartbeat()
                return True
            except FileExistsError:
                holder = self._read_payload()
                self._last_holder = holder
                if self._cleanup_if_stale(holder):
                    continue
                if self._is_stale(holder):
                    # stale-lock обнаружен, но не удалось удалить
                    return False
                return False

    def release(self):
        with self._mutex:
            token = self._token
            self._token = None
            self._stop_evt.set()
            thread = self._heartbeat_thread
            self._heartbeat_thread = None

        if thread and thread.is_alive():
            thread.join(timeout=1.0)

        if not token:
            return

        current = self._read_payload()
        if current is _ROLE_LOCK_READ_UNAVAILABLE:
            return
        if current and current.get("nonce") != token.get("nonce"):
            return

        try:
            os.remove(self.lock_path)
        except FileNotFoundError:
            pass
        except Exception as exc:
            self.logger.warning("Failed to release role lock %s: %s", self.lock_path, exc)

    def refresh(self) -> bool:
        """
        Обновляет mtime удерживаемого lock и перезапускает heartbeat, если он
        остановился после временной недоступности сетевой папки.
        """
        with self._mutex:
            token = dict(self._token) if self._token else None
        if not token:
            return False

        current = self._read_payload()
        if current is _ROLE_LOCK_READ_UNAVAILABLE or not current:
            return False
        if current.get("nonce") != token.get("nonce"):
            return False

        token["timestamp"] = time.time()
        try:
            os.utime(self.lock_path, None)
            with self._mutex:
                if self._token and self._token.get("nonce") == token.get("nonce"):
                    self._token = token
            self._start_heartbeat()
            return True
        except Exception as exc:
            self.logger.warning("Role lock refresh failed for %s: %s", self.lock_path, exc)
            return False

    def describe_holder(self) -> str:
        holder = self._last_holder or self._read_payload()
        if holder is _ROLE_LOCK_READ_UNAVAILABLE:
            return "lock-файл временно недоступен для чтения"
        if not holder:
            return "неизвестный владелец"

        host = holder.get("host", "?")
        pid = holder.get("pid", "?")
        ts = holder.get("timestamp")
        ts_human = "неизвестно"
        if isinstance(ts, (int, float)):
            ts_human = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        return f"host={host}, pid={pid}, время={ts_human}"

    def holder_owner_role(self) -> str | None:
        """Возвращает роль владельца lock, если она известна."""
        holder = self._last_holder or self._read_payload()
        if not isinstance(holder, dict):
            return None

        owner_role = str(holder.get("owner_role") or "").strip().casefold()
        if owner_role:
            return owner_role

        # Lock старой версии не содержит owner_role. Для формы добавления
        # пациента сохраняем понятное предупреждение, извлекая роль из owner_id.
        owner_id = str(holder.get("owner_id") or "").strip().casefold()
        if owner_id.endswith(":doctor_add_patient"):
            return "doctor"
        if owner_id.endswith(":nurse_add_patient"):
            return "nurse"
        return None

    def _start_heartbeat(self):
        with self._mutex:
            if self._heartbeat_thread and self._heartbeat_thread.is_alive():
                return
            self._stop_evt.clear()
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_worker,
                name=f"RoleLockHeartbeat:{self.role}",
                daemon=True,
            )
            self._heartbeat_thread.start()

    def _heartbeat_worker(self):
        while not self._stop_evt.wait(self.heartbeat_sec):
            with self._mutex:
                token = dict(self._token) if self._token else None
            if not token:
                return

            current = self._read_payload()
            if current is _ROLE_LOCK_READ_UNAVAILABLE:
                return
            if not current:
                return
            if current.get("nonce") != token.get("nonce"):
                return

            token["timestamp"] = time.time()
            try:
                # На сетевых SMB-папках периодический os.replace() lock-файла
                # может завершить процесс native access violation без Python
                # исключения. Для heartbeat достаточно обновлять mtime: nonce и
                # владелец остаются в исходном JSON, а stale-проверка учитывает
                # свежий mtime файла.
                os.utime(self.lock_path, None)
                with self._mutex:
                    if self._token and self._token.get("nonce") == token.get("nonce"):
                        self._token = token
            except Exception as exc:
                self.logger.warning("Role lock heartbeat update failed for %s: %s", self.lock_path, exc)
                return
