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

from PySide6.QtCore import QEvent, QObject, QPoint, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

try:
    from rem_card.ui.styles.theme import (
        BG_LIGHT,
        COLOR_PRIMARY,
        COLOR_PRIMARY_DARK,
        CUSTOM_DIALOG_BORDER,
        CUSTOM_DIALOG_RADIUS,
        STYLE_CUSTOM_DIALOG,
        TEXT_PRIMARY,
        TEXT_SECONDARY,
    )
except Exception:
    BG_LIGHT = "#e9ecef"
    COLOR_PRIMARY = "#007bff"
    COLOR_PRIMARY_DARK = "#0056b3"
    CUSTOM_DIALOG_BORDER = "#bdc3c7"
    CUSTOM_DIALOG_RADIUS = "5px"
    TEXT_PRIMARY = "#2c3e50"
    TEXT_SECONDARY = "#495057"
    STYLE_CUSTOM_DIALOG = f"""
        QDialog {{ background-color: transparent; }}
        QFrame#DialogMainFrame {{
            background-color: #ffffff;
            border: 1px solid {CUSTOM_DIALOG_BORDER};
            border-radius: {CUSTOM_DIALOG_RADIUS};
        }}
        QFrame#DialogTitleBar {{
            background-color: {BG_LIGHT};
            border-top-left-radius: {CUSTOM_DIALOG_RADIUS};
            border-top-right-radius: {CUSTOM_DIALOG_RADIUS};
            border-bottom: 1px solid {CUSTOM_DIALOG_BORDER};
        }}
        QLabel#DialogTitleText {{
            color: {TEXT_PRIMARY};
            font-weight: bold;
            font-size: 14px;
            padding-left: 5px;
            background-color: transparent;
        }}
        QPushButton#DialogCloseBtn {{
            background-color: transparent;
            color: {TEXT_PRIMARY};
            font-weight: bold;
            font-size: 14px;
            border: none;
            padding: 2px 10px;
            border-top-right-radius: {CUSTOM_DIALOG_RADIUS};
        }}
        QPushButton#DialogCloseBtn:hover {{
            background-color: #e74c3c;
            color: white;
        }}
        QPushButton#DialogOkBtn {{
            background-color: {BG_LIGHT};
            color: {TEXT_PRIMARY};
            font-size: 13px;
            font-weight: bold;
            padding: 6px 20px;
            border: 1px solid {CUSTOM_DIALOG_BORDER};
            border-radius: {CUSTOM_DIALOG_RADIUS};
        }}
        QPushButton#DialogOkBtn:hover {{ background-color: #d8dde2; }}
    """


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


def _remove_file_quietly(path: str):
    if not path:
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except Exception:
        pass


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
            _remove_file_quietly(str(self.args.starting_lock or ""))
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
            _remove_file_quietly(str(self.args.starting_lock or ""))
            if lock:
                lock.release()

    def _status(self, text: str, progress: int):
        self.status_changed.emit(text, max(0, min(100, int(progress))))


def _show_custom_notice(parent, title: str, message: str):
    try:
        from rem_card.ui.shared.custom_message_box import CustomMessageBox

        CustomMessageBox.warning(parent, title, message)
        return
    except Exception:
        pass

    dialog = QDialog(parent)
    dialog.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
    dialog.setAttribute(Qt.WA_TranslucentBackground)
    dialog.setModal(True)
    dialog.setFixedWidth(390)
    dialog.setStyleSheet(STYLE_CUSTOM_DIALOG)

    root = QVBoxLayout(dialog)
    root.setContentsMargins(0, 0, 0, 0)

    card = QFrame(dialog)
    card.setObjectName("DialogMainFrame")
    card_layout = QVBoxLayout(card)
    card_layout.setContentsMargins(0, 0, 0, 0)
    card_layout.setSpacing(0)

    title_bar = QFrame(card)
    title_bar.setObjectName("DialogTitleBar")
    title_bar.setFixedHeight(30)
    title_layout = QHBoxLayout(title_bar)
    title_layout.setContentsMargins(5, 0, 0, 0)
    title_layout.setSpacing(0)
    title_label = QLabel(title)
    title_label.setObjectName("DialogTitleText")
    close_button = QPushButton("✕")
    close_button.setObjectName("DialogCloseBtn")
    close_button.setFixedSize(30, 30)
    close_button.clicked.connect(dialog.reject)
    title_layout.addWidget(title_label)
    title_layout.addStretch()
    title_layout.addWidget(close_button)

    content = QFrame(card)
    content_layout = QVBoxLayout(content)
    content_layout.setContentsMargins(20, 20, 20, 20)
    content_layout.setSpacing(20)
    message_label = QLabel(message)
    message_label.setObjectName("DialogMessageText")
    message_label.setWordWrap(True)
    message_label.setMinimumWidth(250)
    message_label.setAlignment(Qt.AlignCenter)
    ok_button = QPushButton("Понятно")
    ok_button.setObjectName("DialogOkBtn")
    ok_button.clicked.connect(dialog.accept)
    content_layout.addWidget(message_label)
    content_layout.addWidget(ok_button, 0, Qt.AlignRight)

    card_layout.addWidget(title_bar)
    card_layout.addWidget(content)
    root.addWidget(card)
    dialog.exec()


class UpdateWindow(QDialog):
    def __init__(self, args: argparse.Namespace):
        super().__init__()
        self.args = args
        self._finished = False
        self._thread: Optional[QThread] = None
        self._worker: Optional[UpdateWorker] = None
        self._is_dragging = False
        self._drag_pos = QPoint()
        self._started_at = 0.0
        self._target_progress = 1
        self._displayed_progress = 1
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(80)
        self._progress_timer.timeout.connect(self._tick_progress)
        self._setup_ui()
        app = QApplication.instance()
        if app:
            self.finished.connect(app.quit)

    def _setup_ui(self):
        self.setWindowTitle("Обновление РЕМКАРТА")
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedWidth(460)
        self.setStyleSheet(
            STYLE_CUSTOM_DIALOG
            + f"""
            QLabel#UpdateTitle {{
                color: {TEXT_PRIMARY};
                font-size: 15px;
                font-weight: bold;
                background-color: transparent;
            }}
            QLabel#UpdateStatus {{
                color: {TEXT_PRIMARY};
                font-size: 13px;
                font-weight: bold;
                background-color: transparent;
            }}
            QLabel#UpdateHint {{
                color: {TEXT_SECONDARY};
                font-size: 12px;
                background-color: transparent;
            }}
            QProgressBar#UpdateProgress {{
                border: 1px solid {CUSTOM_DIALOG_BORDER};
                border-radius: {CUSTOM_DIALOG_RADIUS};
                height: 18px;
                text-align: center;
                color: {TEXT_PRIMARY};
                background-color: {BG_LIGHT};
                font-weight: bold;
            }}
            QProgressBar#UpdateProgress::chunk {{
                background-color: {COLOR_PRIMARY};
                border-radius: {CUSTOM_DIALOG_RADIUS};
            }}
            QProgressBar#UpdateProgress::chunk:hover {{
                background-color: {COLOR_PRIMARY_DARK};
            }}
            """
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        card = QFrame(self)
        card.setObjectName("DialogMainFrame")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)

        self.title_bar = QFrame(card)
        self.title_bar.setObjectName("DialogTitleBar")
        self.title_bar.setFixedHeight(30)
        self.title_bar.installEventFilter(self)
        title_layout = QHBoxLayout(self.title_bar)
        title_layout.setContentsMargins(5, 0, 0, 0)
        title_layout.setSpacing(0)
        window_title = QLabel("Обновление РЕМКАРТА")
        window_title.setObjectName("DialogTitleText")
        self.window_close_button = QPushButton("✕")
        self.window_close_button.setObjectName("DialogCloseBtn")
        self.window_close_button.setFixedSize(30, 30)
        self.window_close_button.clicked.connect(self.close)
        title_layout.addWidget(window_title)
        title_layout.addStretch()
        title_layout.addWidget(self.window_close_button)

        content = QFrame(card)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        self.title_label = QLabel("Обновление РЕМКАРТА")
        self.title_label.setObjectName("UpdateTitle")
        self.status_label = QLabel("Подготовка...")
        self.status_label.setObjectName("UpdateStatus")
        self.status_label.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setObjectName("UpdateProgress")
        self.progress.setRange(0, 100)
        self.progress.setValue(self._displayed_progress)
        self.hint_label = QLabel("Не запускайте программу до завершения обновления.")
        self.hint_label.setObjectName("UpdateHint")
        self.hint_label.setWordWrap(True)
        self.close_button = QPushButton("Закрыть")
        self.close_button.setObjectName("DialogOkBtn")
        self.close_button.setVisible(False)
        self.close_button.clicked.connect(self.accept)

        layout.addWidget(self.title_label)
        layout.addWidget(self.status_label)
        layout.addWidget(self.progress)
        layout.addWidget(self.hint_label)
        layout.addWidget(self.close_button, 0, Qt.AlignRight)
        card_layout.addWidget(self.title_bar)
        card_layout.addWidget(content)
        root.addWidget(card)

    def start(self):
        self._started_at = time.time()
        self._progress_timer.start()
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
        _show_custom_notice(
            self,
            "Обновление выполняется",
            "Закрывать окно во время обновления нельзя. Дождитесь завершения процесса.",
        )
        event.ignore()

    def eventFilter(self, obj, event):
        if obj is getattr(self, "title_bar", None):
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._is_dragging = True
                self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                return True
            if event.type() == QEvent.MouseMove and self._is_dragging:
                self.move(event.globalPosition().toPoint() - self._drag_pos)
                return True
            if event.type() == QEvent.MouseButtonRelease:
                self._is_dragging = False
                return True
        return super().eventFilter(obj, event)

    def _tick_progress(self):
        if not self._started_at:
            return
        elapsed = max(0.0, time.time() - self._started_at)
        slow_elapsed_progress = min(96, 1 + int(elapsed / 4.5))
        desired = max(self._target_progress, slow_elapsed_progress)
        if self._finished:
            desired = self._target_progress
        desired = max(1, min(100, desired))

        if self._displayed_progress < desired:
            self._displayed_progress = min(desired, self._displayed_progress + 1)
            self.progress.setValue(self._displayed_progress)
        elif self._displayed_progress > desired:
            self._displayed_progress = desired
            self.progress.setValue(self._displayed_progress)

        if self._finished and self._displayed_progress >= desired:
            self._progress_timer.stop()

    @Slot(str, int)
    def _on_status(self, text: str, progress: int):
        self.status_label.setText(text)
        self._target_progress = max(self._target_progress, max(1, min(100, int(progress))))

    @Slot(str)
    def _on_failed(self, message: str):
        self._finished = True
        self._target_progress = max(self._displayed_progress, self._target_progress)
        self.title_label.setText("Обновление не выполнено")
        self.status_label.setText(message)
        self.hint_label.setText("Старая версия программы оставлена без изменений.")
        self.close_button.setVisible(True)

    @Slot(str)
    def _on_succeeded(self, version: str):
        self._finished = True
        self._target_progress = 100
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
    parser.add_argument("--starting-lock", default="")
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
