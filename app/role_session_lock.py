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
        stale_timeout_sec: float = 45.0,
        heartbeat_sec: float = 10.0,
        logger: Optional[logging.Logger] = None,
    ):
        self.lock_path = lock_path
        self.role = role
        self.owner_id = owner_id
        self.stale_timeout_sec = stale_timeout_sec
        self.heartbeat_sec = heartbeat_sec
        self.logger = logger or logging.getLogger(__name__)

        self._token: Optional[dict[str, Any]] = None
        self._last_holder: Any = None
        self._stop_evt = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._mutex = threading.Lock()

    def _build_payload(self) -> dict[str, Any]:
        return {
            "timestamp": time.time(),
            "role": self.role,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "owner_id": self.owner_id,
            "nonce": uuid.uuid4().hex,
        }

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
    def _is_pid_alive_local(pid_value: Any) -> bool:
        try:
            pid = int(pid_value)
        except (TypeError, ValueError):
            return False
        if pid <= 0:
            return False
        if pid == os.getpid():
            return True
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
            # На Windows os.kill(pid, 0) в редких случаях может пробрасывать
            # не-OSError исключения (например, SystemError через WinAPI bridge).
            # Для lock-механизма это не критично: считаем PID "неживым",
            # чтобы не блокировать роль аварийно.
            return False
        return True

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
        if not payload:
            return True

        # Ключевой кейс: приложение аварийно закрыли на ЭТОМ же ПК.
        # В этом случае снимаем лок сразу, не дожидаясь timeout.
        holder_host = payload.get("host")
        holder_pid = payload.get("pid")
        if self._is_local_host(holder_host) and not self._is_pid_alive_local(holder_pid):
            return True

        now = time.time()
        ts = payload.get("timestamp")
        if isinstance(ts, (int, float)):
            age = now - ts
            # Timestamp в будущем (съехали часы) не должен блокировать вход навсегда.
            if age >= 0 and age > self.stale_timeout_sec:
                return True

        # Fallback по mtime lock-файла на сетевом диске.
        file_age = self._lock_file_age()
        if file_age is not None and file_age > self.stale_timeout_sec:
            return True

        return False

    def _cleanup_if_stale(self, payload: Optional[dict[str, Any]]) -> bool:
        """
        Пытается удалить stale lock-файл.
        Возвращает True, если stale-lock успешно очищен или уже отсутствует.
        Возвращает False, если lock не stale или stale, но удалить не удалось.
        """
        if not self._is_stale(payload):
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

    def is_held_by_other(self) -> bool:
        """
        Проверяет, занят ли lock другим процессом.
        Stale-lock будет очищен автоматически, если это возможно.
        """
        holder = self._read_payload()
        self._last_holder = holder
        if holder is _ROLE_LOCK_READ_UNAVAILABLE:
            return True
        if not holder:
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
            tmp_path = f"{self.lock_path}.{os.getpid()}.tmp"
            try:
                with open(tmp_path, "w", encoding="utf-8") as fh:
                    json.dump(token, fh, ensure_ascii=True)
                os.replace(tmp_path, self.lock_path)
                with self._mutex:
                    if self._token and self._token.get("nonce") == token.get("nonce"):
                        self._token = token
            except Exception as exc:
                self.logger.warning("Role lock heartbeat update failed for %s: %s", self.lock_path, exc)
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except Exception:
                    pass
                return
