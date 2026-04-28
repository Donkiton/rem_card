import argparse
import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)


READY_FILE_NAME = "ready.ok"
MANIFEST_FILE_NAME = "manifest.json"
LOCK_STALE_SEC = 30 * 60
ROLE_LOCK_STALE_SEC = 90
WAIT_ACTIVE_SESSIONS_TIMEOUT_SEC = 30 * 60
REQUIRED_EXES = (
    "RemCardDoctor.exe",
    "RemCardNurse.exe",
    "RemCardPathSetup.exe",
    "RemCardUpdater.exe",
)
MANAGED_ROOT_FILES = (
    "RemCard.exe",
    "RemCardDoctor.exe",
    "RemCardNurse.exe",
    "RemCardPathSetup.exe",
    "RemCardUpdater.exe",
    "VERSION",
    "CHANGELOG.md",
    "manifest.json",
)
MANAGED_ROOT_DIRS = ("_internal",)


class UpdateAlreadyRunning(RuntimeError):
    pass


class UpdateLock:
    def __init__(self, lock_path: str, payload: dict[str, Any]):
        self.lock_path = lock_path
        self.payload = dict(payload)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._acquired = False

    def acquire(self):
        os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
        self._remove_stale_if_needed()
        raw = json.dumps(self.payload, ensure_ascii=True, indent=2).encode("utf-8")
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, raw)
            finally:
                os.close(fd)
        except FileExistsError as exc:
            raise UpdateAlreadyRunning("Обновление уже выполняется на другом компьютере.") from exc

        self._acquired = True
        self._thread = threading.Thread(target=self._heartbeat, name="RemCardUpdateLock", daemon=True)
        self._thread.start()

    def release(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        if not self._acquired:
            return
        try:
            os.remove(self.lock_path)
        except FileNotFoundError:
            pass
        except Exception:
            pass
        self._acquired = False

    def _read_existing(self) -> Optional[dict[str, Any]]:
        try:
            with open(self.lock_path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            return payload if isinstance(payload, dict) else None
        except FileNotFoundError:
            return None
        except Exception:
            return {"timestamp": _safe_mtime(self.lock_path)}

    def _remove_stale_if_needed(self):
        payload = self._read_existing()
        if not payload:
            return
        ts = payload.get("timestamp")
        if isinstance(ts, (int, float)):
            age = time.time() - float(ts)
        else:
            mtime = _safe_mtime(self.lock_path)
            age = time.time() - mtime if mtime else LOCK_STALE_SEC + 1
        if age <= LOCK_STALE_SEC:
            return
        try:
            os.remove(self.lock_path)
        except FileNotFoundError:
            pass

    def _heartbeat(self):
        while not self._stop.wait(3.0):
            self.payload["timestamp"] = time.time()
            tmp_path = f"{self.lock_path}.{os.getpid()}.tmp"
            try:
                with open(tmp_path, "w", encoding="utf-8") as fh:
                    json.dump(self.payload, fh, ensure_ascii=True, indent=2)
                os.replace(tmp_path, self.lock_path)
            except Exception:
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except Exception:
                    pass


def _safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except Exception:
        return 0.0


def _now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _write_log(baza_dir: str, message: str):
    try:
        logs_dir = os.path.join(baza_dir, "logs")
        os.makedirs(logs_dir, exist_ok=True)
        path = os.path.join(logs_dir, f"updater_{datetime.now().strftime('%Y%m%d')}.log")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{_now_text()} | {message}\n")
    except Exception:
        pass


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0 or pid == os.getpid():
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except Exception:
            return False

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return int(exit_code.value) == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _wait_for_parent(pid: int, status: Callable[[str, int], None]):
    if pid <= 0:
        return
    while _is_pid_alive(pid):
        status("Ожидание закрытия запущенной программы...", 8)
        time.sleep(0.5)


def _read_json(path: str) -> Optional[dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _host_aliases() -> set[str]:
    aliases: set[str] = set()
    for value in (
        socket.gethostname(),
        socket.getfqdn(),
        os.environ.get("COMPUTERNAME"),
        os.environ.get("HOSTNAME"),
    ):
        if not value:
            continue
        host = str(value).strip().lower()
        if not host:
            continue
        aliases.add(host)
        aliases.add(host.split(".", 1)[0])
    return aliases


def _is_local_host(value: Any) -> bool:
    if not value:
        return False
    host = str(value).strip().lower()
    if not host:
        return False
    aliases = _host_aliases()
    return host in aliases or host.split(".", 1)[0] in aliases


def _role_lock_active(path: str, *, local_only: bool = True) -> bool:
    if not os.path.exists(path):
        return False
    payload = _read_json(path)
    if local_only:
        if not payload or not _is_local_host(payload.get("host")):
            return False
        try:
            pid = int(payload.get("pid") or 0)
        except Exception:
            pid = 0
        if pid > 0 and not _is_pid_alive(pid):
            return False

    ts = payload.get("timestamp") if payload else None
    if isinstance(ts, (int, float)):
        return (time.time() - float(ts)) <= ROLE_LOCK_STALE_SEC
    mtime = _safe_mtime(path)
    return bool(mtime and (time.time() - mtime) <= ROLE_LOCK_STALE_SEC)


def _wait_for_active_sessions(baza_dir: str, status: Callable[[str, int], None]):
    session_dir = os.path.join(baza_dir, "session_locks")
    lock_names = {
        "doctor.lock": "врача",
        "nurse.lock": "медсестры",
    }
    deadline = time.time() + WAIT_ACTIVE_SESSIONS_TIMEOUT_SEC
    while True:
        active = []
        for file_name, label in lock_names.items():
            if _role_lock_active(os.path.join(session_dir, file_name), local_only=True):
                active.append(label)
        if not active:
            return
        if time.time() >= deadline:
            raise RuntimeError(
                "Не удалось начать обновление: слишком долго открыты окна "
                + ", ".join(active)
                + " на этом компьютере. Закройте РЕМКАРТА и запустите программу снова."
            )
        status("Ожидание закрытия окон " + ", ".join(active) + " на этом компьютере...", 12)
        time.sleep(2.0)


def _validate_source(source_dir: str) -> dict[str, Any]:
    source = os.path.abspath(source_dir)
    if not os.path.isfile(os.path.join(source, READY_FILE_NAME)):
        raise RuntimeError("Пакет обновления еще не готов: отсутствует ready.ok.")
    manifest_path = os.path.join(source, MANIFEST_FILE_NAME)
    manifest = _read_json(manifest_path)
    if not manifest:
        raise RuntimeError("Не удалось прочитать manifest.json пакета обновления.")
    for exe_name in REQUIRED_EXES:
        if not os.path.isfile(os.path.join(source, exe_name)):
            raise RuntimeError(f"В пакете обновления отсутствует {exe_name}.")
    if not os.path.isdir(os.path.join(source, "_internal")):
        raise RuntimeError("В пакете обновления отсутствует папка _internal.")
    return manifest


def _remove_path(path: str):
    if not os.path.exists(path):
        return
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        os.remove(path)


def _retry(action: Callable[[], None], description: str, attempts: int = 50, delay_sec: float = 0.5):
    last_exc: Optional[Exception] = None
    for _ in range(attempts):
        try:
            action()
            return
        except Exception as exc:
            last_exc = exc
            time.sleep(delay_sec)
    raise RuntimeError(f"{description}: {last_exc}") from last_exc


def _copy_source_to_staging(source_dir: str, staging_dir: str):
    ignored = {READY_FILE_NAME}

    def ignore(_dir: str, names: list[str]) -> set[str]:
        return {name for name in names if name in ignored or name.endswith(".tmp")}

    _remove_path(staging_dir)
    shutil.copytree(source_dir, staging_dir, ignore=ignore)


def _replace_program_dir(
    *,
    source_dir: str,
    target_dir: str,
    status: Callable[[str, int], None],
) -> tuple[str, str]:
    source = os.path.abspath(source_dir)
    target = os.path.abspath(target_dir)
    if os.path.normcase(source) == os.path.normcase(target):
        raise RuntimeError("Источник обновления совпадает с рабочей папкой программы.")
    if not os.path.isdir(target):
        raise RuntimeError(f"Рабочая папка программы не найдена: {target}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    staging = os.path.join(target, f"__upd_new_{stamp}_{os.getpid()}")
    backup = os.path.join(target, f"__upd_old_{stamp}_{os.getpid()}")

    status("Подготовка файлов обновления...", 25)
    _copy_source_to_staging(source, staging)

    for exe_name in REQUIRED_EXES:
        if not os.path.isfile(os.path.join(staging, exe_name)):
            raise RuntimeError(f"Подготовленная сборка неполная: нет {exe_name}.")

    os.makedirs(backup, exist_ok=True)
    installed_paths: list[str] = []
    try:
        status("Резервирование старой версии...", 42)
        for name in MANAGED_ROOT_FILES:
            current = os.path.join(target, name)
            if os.path.isfile(current):
                _retry(
                    lambda current=current, name=name: shutil.move(current, os.path.join(backup, name)),
                    f"Не удалось зарезервировать {name}",
                )

        for name in MANAGED_ROOT_DIRS:
            current = os.path.join(target, name)
            if os.path.isdir(current):
                _retry(
                    lambda current=current, name=name: shutil.move(current, os.path.join(backup, name)),
                    f"Не удалось зарезервировать {name}",
                )

        status("Копирование новой версии...", 65)
        for name in MANAGED_ROOT_FILES:
            source_path = os.path.join(staging, name)
            if not os.path.isfile(source_path):
                continue
            target_path = os.path.join(target, name)
            _retry(
                lambda source_path=source_path, target_path=target_path: shutil.copy2(source_path, target_path),
                f"Не удалось скопировать {name}",
            )
            installed_paths.append(target_path)

        for name in MANAGED_ROOT_DIRS:
            source_path = os.path.join(staging, name)
            if not os.path.isdir(source_path):
                continue
            target_path = os.path.join(target, name)
            _retry(
                lambda source_path=source_path, target_path=target_path: shutil.copytree(source_path, target_path),
                f"Не удалось скопировать {name}",
            )
            installed_paths.append(target_path)

        status("Очистка временных файлов...", 92)
        try:
            shutil.rmtree(backup)
        except Exception:
            pass
        try:
            shutil.rmtree(staging)
        except Exception:
            pass
        return staging, backup
    except Exception:
        for path in reversed(installed_paths):
            try:
                _remove_path(path)
            except Exception:
                pass
        for name in MANAGED_ROOT_FILES:
            try:
                _remove_path(os.path.join(target, name))
            except Exception:
                pass
        for name in MANAGED_ROOT_DIRS:
            try:
                _remove_path(os.path.join(target, name))
            except Exception:
                pass
        for name in MANAGED_ROOT_FILES:
            saved = os.path.join(backup, name)
            if os.path.isfile(saved):
                try:
                    shutil.move(saved, os.path.join(target, name))
                except Exception:
                    pass
        for name in MANAGED_ROOT_DIRS:
            saved = os.path.join(backup, name)
            if os.path.isdir(saved):
                try:
                    shutil.move(saved, os.path.join(target, name))
                except Exception:
                    pass
        try:
            if os.path.isdir(staging):
                shutil.rmtree(staging)
        except Exception:
            pass
        raise


class UpdateWorker(QObject):
    status_changed = Signal(str, int)
    failed = Signal(str)
    succeeded = Signal(str)

    def __init__(self, args: argparse.Namespace):
        super().__init__()
        self.args = args

    @Slot()
    def run(self):
        lock = None
        try:
            source = os.path.abspath(self.args.source)
            target = os.path.abspath(self.args.target)
            baza_dir = os.path.abspath(self.args.baza_dir)
            target_version = str(self.args.target_version or "")

            self._status("Проверка пакета обновления...", 3)
            manifest = _validate_source(source)
            manifest_version = str(manifest.get("version") or "").strip()
            if target_version and manifest_version and target_version != manifest_version:
                raise RuntimeError(
                    f"Версия manifest.json ({manifest_version}) не совпадает с ожидаемой ({target_version})."
                )

            payload = {
                "timestamp": time.time(),
                "started_at": _now_text(),
                "host": socket.gethostname(),
                "pid": os.getpid(),
                "source": source,
                "target": target,
                "target_version": manifest_version or target_version,
                "launcher_host": self.args.launcher_host,
            }
            lock = UpdateLock(os.path.abspath(self.args.lock), payload)
            self._status("Получение блокировки обновления...", 5)
            lock.acquire()
            _write_log(baza_dir, f"update started source={source} target={target} version={payload['target_version']}")

            _wait_for_parent(int(self.args.parent_pid or 0), self._status)
            _wait_for_active_sessions(baza_dir, self._status)
            _replace_program_dir(source_dir=source, target_dir=target, status=self._status)

            self._status("Обновление завершено.", 100)
            _write_log(baza_dir, f"update finished version={payload['target_version']}")

            restart_exe = str(self.args.restart_exe or "").strip()
            if restart_exe:
                restart_path = os.path.join(target, restart_exe)
                if os.path.isfile(restart_path):
                    self._status("Запуск новой версии...", 100)
                    subprocess.Popen([restart_path], cwd=target)
            self.succeeded.emit(str(payload["target_version"] or ""))
        except UpdateAlreadyRunning as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            try:
                _write_log(os.path.abspath(self.args.baza_dir), f"update failed: {exc}")
            except Exception:
                pass
            self.failed.emit(str(exc))
        finally:
            if lock:
                lock.release()

    def _status(self, text: str, progress: int):
        self.status_changed.emit(text, max(0, min(100, int(progress))))


class UpdateWindow(QDialog):
    def __init__(self, args: argparse.Namespace):
        super().__init__()
        self.args = args
        self._finished = False
        self._thread: Optional[QThread] = None
        self._worker: Optional[UpdateWorker] = None
        self._setup_ui()
        app = QApplication.instance()
        if app:
            self.finished.connect(app.quit)

    def _setup_ui(self):
        self.setWindowTitle("Обновление РЕМКАРТА")
        self.setWindowFlags(
            Qt.Window
            | Qt.CustomizeWindowHint
            | Qt.WindowTitleHint
            | Qt.WindowCloseButtonHint
            | Qt.WindowStaysOnTopHint
        )
        self.setFixedWidth(460)
        self.setStyleSheet(
            """
            QDialog { background: #f4f7fb; }
            QFrame#Card {
                background: white;
                border: 1px solid #cfd8e3;
                border-radius: 8px;
            }
            QLabel#Title {
                color: #1f2937;
                font-size: 18px;
                font-weight: 700;
            }
            QLabel#Status {
                color: #374151;
                font-size: 13px;
            }
            QLabel#Hint {
                color: #6b7280;
                font-size: 12px;
            }
            QProgressBar {
                border: 1px solid #cbd5e1;
                border-radius: 4px;
                height: 18px;
                text-align: center;
                color: #111827;
                background: #eef2f7;
            }
            QProgressBar::chunk {
                background: #2f80ed;
                border-radius: 3px;
            }
            QPushButton {
                min-height: 30px;
                padding: 0 18px;
                border-radius: 4px;
                border: 1px solid #9ca3af;
                background: #ffffff;
                color: #111827;
            }
            QPushButton:hover { background: #f3f4f6; }
            """
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)

        card = QFrame(self)
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)

        self.title_label = QLabel("Обновление РЕМКАРТА")
        self.title_label.setObjectName("Title")
        self.status_label = QLabel("Подготовка...")
        self.status_label.setObjectName("Status")
        self.status_label.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.hint_label = QLabel("Не запускайте программу до завершения обновления.")
        self.hint_label.setObjectName("Hint")
        self.hint_label.setWordWrap(True)
        self.close_button = QPushButton("Закрыть")
        self.close_button.setVisible(False)
        self.close_button.clicked.connect(self.accept)

        layout.addWidget(self.title_label)
        layout.addWidget(self.status_label)
        layout.addWidget(self.progress)
        layout.addWidget(self.hint_label)
        layout.addWidget(self.close_button, 0, Qt.AlignRight)
        root.addWidget(card)

    def start(self):
        self._thread = QThread(self)
        self._worker = UpdateWorker(self.args)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.status_changed.connect(self._on_status)
        self._worker.failed.connect(self._on_failed)
        self._worker.succeeded.connect(self._on_succeeded)
        self._worker.failed.connect(self._thread.quit)
        self._worker.succeeded.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.start()

    def closeEvent(self, event):
        if self._finished:
            event.accept()
            return
        QMessageBox.warning(
            self,
            "Обновление выполняется",
            "Закрывать окно во время обновления нельзя. Дождитесь завершения процесса.",
        )
        event.ignore()

    @Slot(str, int)
    def _on_status(self, text: str, progress: int):
        self.status_label.setText(text)
        self.progress.setValue(progress)

    @Slot(str)
    def _on_failed(self, message: str):
        self._finished = True
        self.title_label.setText("Обновление не выполнено")
        self.status_label.setText(message)
        self.hint_label.setText("Старая версия программы оставлена без изменений.")
        self.close_button.setVisible(True)

    @Slot(str)
    def _on_succeeded(self, version: str):
        self._finished = True
        self.title_label.setText("Обновление завершено")
        self.status_label.setText(f"Установлена версия {version}.")
        self.hint_label.setText("Можно запускать программу.")
        self.close_button.setVisible(True)
        if str(self.args.restart_exe or "").strip():
            QTimer.singleShot(1800, self.accept)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RemCard updater")
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--baza-dir", required=True)
    parser.add_argument("--lock", required=True)
    parser.add_argument("--parent-pid", default="0")
    parser.add_argument("--current-version", default="")
    parser.add_argument("--target-version", default="")
    parser.add_argument("--restart-exe", default="")
    parser.add_argument("--launcher-host", default="")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    window = UpdateWindow(args)
    window.show()
    window.raise_()
    window.activateWindow()
    QTimer.singleShot(0, window.start)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
