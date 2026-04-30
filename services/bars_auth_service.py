import base64
import datetime
import json
import os
import re
import socket
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from rem_card.app.logger import logger
from rem_card.app.paths import LOCAL_APPDATA, LOGS_DIR


DEFAULT_BARS_URL = "http://10.30.30.12/"
DEFAULT_DEBUG_PORT = 9338
ISOLATED_PROFILE_DIR = os.path.join(LOCAL_APPDATA, "RemCard", "bars_browser_profile")
BARS_DIAG_PREFIX = "[BARS]"


@dataclass
class BarsAuthCheckResult:
    authorized: bool
    message: str
    url: str = ""
    title: str = ""


@dataclass
class BarsPageProbeResult:
    ok: bool
    message: str
    url: str = ""
    title: str = ""
    fields: dict[str, str] | None = None
    markers: list[str] | None = None
    text_preview: str = ""


@dataclass
class BarsPatientProbeResult:
    ok: bool
    message: str
    history_number: str = ""
    full_name: str = ""
    matched_line: str = ""
    text_preview: str = ""


@dataclass
class BarsNetworkCaptureResult:
    ok: bool
    message: str
    history_number: str = ""
    full_name: str = ""
    matched_line: str = ""
    matches: list[dict[str, str]] | None = None
    requests: list[dict[str, Any]] | None = None
    text_preview: str = ""


class BarsAuthService:
    """
    Открывает БАРС в обычном профиле Яндекс-Браузера и проверяет авторизацию.

    Важно: сервис не хранит пароль/PIN/сертификаты и не выполняет вход вместо
    пользователя. Он только открывает БАРС и читает признаки уже открытой сессии.
    """

    SUCCESS_MARKERS = (
        "пользователь:",
        "кабинет:",
        "отделение сотрудника",
        "специальность сотрудника",
        "регистратура",
        "рабочий лист",
        "рабочие места",
        "заказ исследований",
        "аптека/склад",
    )

    def __init__(
        self,
        bars_url: Optional[str] = None,
        browser_path: Optional[str] = None,
        profile_dir: Optional[str] = None,
        debug_port: Optional[int] = None,
    ):
        self.bars_url = (bars_url or os.environ.get("REMCARD_BARS_URL") or DEFAULT_BARS_URL).strip()
        self.browser_path = browser_path or self._find_yandex_browser()
        self.profile_dir = profile_dir or os.environ.get("REMCARD_BARS_BROWSER_PROFILE_DIR") or self._default_profile_dir()
        self._use_user_data_dir = bool(profile_dir) or os.environ.get("REMCARD_BARS_USE_USER_DATA_DIR") == "1"
        self._enable_devtools = os.environ.get("REMCARD_BARS_DISABLE_DEVTOOLS") != "1"
        self.debug_port = int(os.environ.get("REMCARD_BARS_DEBUG_PORT") or debug_port or DEFAULT_DEBUG_PORT)
        self._process: Optional[subprocess.Popen] = None
        self._last_authorized = False
        self._last_message = "Авторизация не проверялась"
        self._diag_path = os.path.join(LOGS_DIR, f"bars_debug_{datetime.datetime.now().strftime('%Y%m%d')}.log")
        self._diag(
            "service_init",
            bars_url=self.bars_url,
            browser_path=self.browser_path,
            profile_dir=self.profile_dir,
            use_user_data_dir=self._use_user_data_dir,
            enable_devtools=self._enable_devtools,
            launch_mode="explicit_user_data_dir" if self._use_user_data_dir else "system_default_profile",
            debug_port=self.debug_port,
            running_browser_pids=self._running_yandex_pids(),
        )

    @property
    def last_authorized(self) -> bool:
        return self._last_authorized

    @property
    def last_message(self) -> str:
        return self._last_message

    def open_auth_window(self) -> BarsAuthCheckResult:
        devtools_before = self._enable_devtools and self._devtools_available(self.debug_port)
        self._diag(
            "open_auth_window_start",
            browser_path=self.browser_path,
            profile_dir=self.profile_dir,
            use_user_data_dir=self._use_user_data_dir,
            enable_devtools=self._enable_devtools,
            launch_mode="explicit_user_data_dir" if self._use_user_data_dir else "system_default_profile",
            debug_port=self.debug_port,
            devtools_before=devtools_before,
            devtools_version=self._get_devtools_version() if devtools_before else {},
            running_browser_pids=self._running_yandex_pids(),
        )
        if not self.browser_path or not os.path.exists(self.browser_path):
            message = "Яндекс-Браузер не найден"
            self._last_message = message
            self._diag("open_auth_window_browser_missing", level="warning")
            return BarsAuthCheckResult(False, message)

        if devtools_before:
            existing_page = self._find_bars_debug_page()
            if existing_page:
                self._activate_debug_page(existing_page)
                page_url = str(existing_page.get("url") or "")
                title = str(existing_page.get("title") or "")
                text = self._read_page_text(existing_page)
                if self._looks_like_bars_work_screen(text) or self._looks_like_hospitalization_journal(text):
                    result = BarsAuthCheckResult(True, "Авторизация БАРС уже пройдена", url=page_url, title=title)
                    self._apply_check_result(result)
                    self._diag("open_auth_window_reused_existing_authorized", url=page_url, title=title)
                    return result

                self._last_message = "Вкладка БАРС уже открыта. Завершите вход и выбор кабинета в этом окне."
                self._diag("open_auth_window_reused_existing_page", url=page_url, title=title)
                return BarsAuthCheckResult(False, self._last_message, url=page_url, title=title)

            opened_page = self._open_bars_tab_via_devtools()
            if opened_page:
                self._diag("open_auth_window_reused_devtools", opened_page=opened_page)
                self._last_message = "Окно БАРС открыто через уже доступный DevTools-порт"
                return BarsAuthCheckResult(False, self._last_message)
            self._diag(
                "open_auth_window_devtools_reuse_failed",
                level="warning",
                debug_port=self.debug_port,
                pages=self._summarize_pages(self._get_debug_pages(log_failures=False)),
            )

        self.debug_port = self._resolve_debug_port(self.debug_port)

        try:
            self._start_browser_process(background=False, event_prefix="open_auth_window")
        except Exception as exc:
            message = f"Не удалось открыть Яндекс-Браузер: {exc}"
            self._last_message = message
            self._diag("open_auth_window_failed", level="warning", error=repr(exc))
            return BarsAuthCheckResult(False, message)

        devtools_after = self._enable_devtools and self._wait_for_devtools(timeout_sec=4.0)
        self._diag(
            "open_auth_window_done",
            devtools_after=devtools_after,
            devtools_version=self._get_devtools_version() if devtools_after else {},
            pages_count=len(self._get_debug_pages(log_failures=False)) if devtools_after else 0,
            running_browser_pids=self._running_yandex_pids(),
        )
        self._last_message = "Окно БАРС открыто в профиле Яндекс-Браузера, ожидается вход"
        return BarsAuthCheckResult(False, self._last_message)

    def prepare_background_session(self):
        self._diag("prepare_background_session_start", debug_port=self.debug_port)
        page = self._ensure_bars_debug_page(background=False, allow_open=False)
        minimized = self.minimize_bars_windows() if page else 0
        self._diag(
            "prepare_background_session_done",
            page_found=bool(page),
            minimized_windows=minimized,
            pages=self._summarize_pages(self._get_debug_pages(log_failures=False)),
        )

    def minimize_bars_windows(self) -> int:
        if os.name != "nt":
            return 0
        try:
            import ctypes
        except Exception:
            return 0

        user32 = ctypes.windll.user32
        markers = (
            "медицинская информационная система",
            "журнал госпитализации",
            "лечащий врач",
            "рабочий лист",
            "запись в регистратуру",
            "10.30.30.12",
            "барс",
        )
        minimized = 0

        def callback(hwnd, _lparam):
            nonlocal minimized
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = str(buffer.value or "").lower()
            if any(marker in title for marker in markers):
                user32.ShowWindow(hwnd, 6)
                minimized += 1
            return True

        enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(callback)
        try:
            user32.EnumWindows(enum_proc, 0)
        except Exception as exc:
            self._diag("minimize_bars_windows_failed", level="warning", error=repr(exc))
            return minimized

        if minimized:
            self._diag("minimize_bars_windows_done", minimized_windows=minimized)
        return minimized

    def mark_authorized_manually(self) -> BarsAuthCheckResult:
        self._last_authorized = True
        self._last_message = "Авторизация подтверждена пользователем"
        self._diag("mark_authorized_manually")
        return BarsAuthCheckResult(True, self._last_message)

    def check_authorized(self) -> BarsAuthCheckResult:
        self._diag("check_authorized_start", debug_port=self.debug_port)
        if not self._enable_devtools:
            result = BarsAuthCheckResult(False, "БАРС открыт в обычном профиле; автоматическая проверка страницы отключена")
            self._apply_check_result(result)
            self._diag("check_authorized_devtools_disabled", message=result.message)
            return result
        pages = self._get_debug_pages()
        if not pages:
            result = BarsAuthCheckResult(False, "Окно БАРС еще не готово")
            self._apply_check_result(result)
            self._diag("check_authorized_no_pages", message=result.message)
            return result

        best_result = BarsAuthCheckResult(False, "Авторизация не определена")
        self._diag("check_authorized_pages", pages=self._summarize_pages(pages))
        for page in pages:
            page_url = str(page.get("url") or "")
            title = str(page.get("title") or "")
            if not self._looks_like_bars_page(page_url, title):
                continue

            text = self._read_page_text(page) or f"{title}\n{page_url}"
            marker_count = self._count_success_markers(text)
            self._diag(
                "check_authorized_page_text",
                url=page_url,
                title=title,
                text_len=len(text),
                marker_count=marker_count,
            )
            if self._looks_like_bars_work_screen(text):
                result = BarsAuthCheckResult(
                    True,
                    "Авторизация БАРС пройдена",
                    url=page_url,
                    title=title,
                )
                self._apply_check_result(result)
                self._diag("check_authorized_success", url=page_url, title=title)
                return result

            best_result = BarsAuthCheckResult(
                False,
                "Ожидается завершение входа и выбор кабинета",
                url=page_url,
                title=title,
            )

        self._apply_check_result(best_result)
        self._diag(
            "check_authorized_not_ready",
            message=best_result.message,
            url=best_result.url,
            title=best_result.title,
        )
        return best_result

    def probe_current_page(self) -> BarsPageProbeResult:
        self._diag("probe_current_page_start")
        if not self._enable_devtools:
            return BarsPageProbeResult(
                False,
                "DevTools-чтение отключено настройкой REMCARD_BARS_DISABLE_DEVTOOLS=1.",
            )
        pages = self._get_debug_pages()
        if not pages:
            self._diag("probe_current_page_no_pages", level="warning", running_browser_pids=self._running_yandex_pids())
            return BarsPageProbeResult(
                False,
                "Не удалось подключиться к открытому Яндекс-Браузеру. "
                "Если браузер уже был открыт до РЕМКАРТЫ, закройте все окна Яндекс-Браузера и откройте БАРС кнопкой снова.",
            )

        for page in pages:
            page_url = str(page.get("url") or "")
            title = str(page.get("title") or "")
            if not self._looks_like_bars_page(page_url, title):
                continue

            text = self._read_page_text(page)
            if not text:
                self._diag("probe_current_page_no_text", level="warning", url=page_url, title=title)
                return BarsPageProbeResult(
                    False,
                    "Страница БАРС найдена, но текст прочитать не удалось",
                    url=page_url,
                    title=title,
                )

            fields = self._extract_dashboard_fields(text)
            markers = [marker for marker in self.SUCCESS_MARKERS if marker in text.lower()]
            preview = self._compact_text_preview(text)
            self._diag(
                "probe_current_page_success",
                url=page_url,
                title=title,
                text_len=len(text),
                markers=markers,
                fields=fields,
            )
            return BarsPageProbeResult(
                True,
                f"Страница прочитана: {len(text)} символов, признаков рабочего экрана: {len(markers)}",
                url=page_url,
                title=title,
                fields=fields,
                markers=markers,
                text_preview=preview,
            )

        self._diag("probe_current_page_bars_tab_not_found", pages=self._summarize_pages(pages))
        return BarsPageProbeResult(False, "Открытая вкладка БАРС не найдена")

    def probe_patient_by_history(self, history_number: str) -> BarsPatientProbeResult:
        history_number = " ".join(str(history_number or "").split())
        self._diag("probe_patient_start", history_number=history_number)
        if not history_number:
            self._diag("probe_patient_empty_history", level="warning")
            return BarsPatientProbeResult(False, "Введите номер истории")
        if not self._enable_devtools:
            self._diag("probe_patient_devtools_disabled", history_number=history_number)
            return BarsPatientProbeResult(
                False,
                "DevTools-чтение отключено настройкой REMCARD_BARS_DISABLE_DEVTOOLS=1.",
                history_number=history_number,
            )

        page = self._ensure_bars_debug_page(background=True, allow_open=False)
        if not page:
            self._diag(
                "probe_patient_no_bars_page",
                level="warning",
                debug_port=self.debug_port,
                devtools_available=self._devtools_available(self.debug_port),
                running_browser_pids=self._running_yandex_pids(),
                pages=self._summarize_pages(self._get_debug_pages(log_failures=False)),
            )
            return BarsPatientProbeResult(
                False,
                "Служебная сессия БАРС недоступна. Нажмите БАРС и пройдите авторизацию заново; после входа окно можно свернуть.",
                history_number=history_number,
            )

        ready_text = self._wait_for_bars_ready(page, timeout_sec=4.0)
        if not self._looks_like_bars_work_screen(ready_text) and "журнал госпитализации" not in ready_text.lower():
            self._diag(
                "probe_patient_page_not_authorized",
                level="warning",
                text_preview=self._compact_text_preview(ready_text),
            )
            return BarsPatientProbeResult(
                False,
                "БАРС открылся, но рабочий экран не найден. Вероятно, сессия истекла или кабинет не выбран.",
                history_number=history_number,
                text_preview=self._compact_text_preview(ready_text),
            )

        steps: list[str] = []
        steps.extend(self._open_hospitalization_journal(page))
        steps.append(self._fill_history_search(page, history_number))
        time.sleep(0.4)
        steps.append(self._click_visible_text(page, ["Найти", "Поиск", "Искать"]))
        time.sleep(2.0)
        self._diag("probe_patient_steps_done", steps=steps)

        text = self._read_page_text_with_retry(page, attempts=3, delay_sec=0.8)
        if not text:
            self._diag("probe_patient_no_text_after_steps", level="warning", steps=steps)
            return BarsPatientProbeResult(
                False,
                "Сценарий выполнен, но текст страницы прочитать не удалось",
                history_number=history_number,
                text_preview="; ".join(step for step in steps if step),
            )

        full_name, matched_line, preview = self._extract_patient_line(text, history_number)
        if full_name:
            self._diag(
                "probe_patient_success",
                history_number=history_number,
                full_name=full_name,
                matched_line=matched_line,
            )
            return BarsPatientProbeResult(
                True,
                "Пациент найден на странице БАРС",
                history_number=history_number,
                full_name=full_name,
                matched_line=matched_line,
                text_preview=preview,
            )

        self._diag(
            "probe_patient_not_found",
            level="warning",
            history_number=history_number,
            text_len=len(text),
            matched_line=matched_line,
            preview=preview or self._compact_text_preview(text),
        )
        return BarsPatientProbeResult(
            False,
            "ФИО по этому номеру истории не найдено в прочитанном тексте страницы",
            history_number=history_number,
            matched_line=matched_line,
            text_preview=preview or self._compact_text_preview(text),
        )

    def capture_patient_search_requests(self, history_number: str) -> BarsNetworkCaptureResult:
        history_number = " ".join(str(history_number or "").split())
        self._diag("capture_patient_requests_start", history_number=history_number)
        if not history_number:
            return BarsNetworkCaptureResult(False, "Введите номер истории")
        if not self._enable_devtools:
            return BarsNetworkCaptureResult(
                False,
                "DevTools-чтение отключено настройкой REMCARD_BARS_DISABLE_DEVTOOLS=1.",
                history_number=history_number,
            )

        page = self._ensure_bars_debug_page(background=True, allow_open=False)
        if not page:
            return BarsNetworkCaptureResult(
                False,
                "Вкладка БАРС недоступна для перехвата запросов. Нажмите БАРС и завершите вход; поиск не будет открывать вкладку сам.",
                history_number=history_number,
            )

        ready_text = self._wait_for_bars_ready(page, timeout_sec=4.0)
        if not self._looks_like_bars_work_screen(ready_text) and "журнал госпитализации" not in ready_text.lower():
            return BarsNetworkCaptureResult(
                False,
                "БАРС открыт, но рабочий экран не найден. Сначала завершите вход и выбор кабинета.",
                history_number=history_number,
                text_preview=self._compact_text_preview(ready_text),
            )

        install_result = self._install_network_capture(page)
        self._clear_network_capture(page)

        steps = [install_result]
        steps.extend(self._open_hospitalization_journal(page))
        steps.append(self._fill_history_search(page, history_number))
        time.sleep(0.4)
        steps.append(self._click_visible_text(page, ["Найти", "Поиск", "Искать"]))
        time.sleep(2.5)

        captured = self._get_network_capture(page)
        requests = self._summarize_captured_requests(captured, history_number)
        matches = self._extract_patient_matches_from_requests(captured, history_number)
        text = self._read_page_text_with_retry(page, attempts=3, delay_sec=0.8)
        full_name, matched_line, preview = self._extract_patient_line(text, history_number)
        if matches and not full_name:
            full_name = matches[0].get("full_name", "")
        if matches and not matched_line:
            matched_line = matches[0].get("history_number", "")

        request_with_history = next((item for item in requests if item.get("contains_history")), None)
        ok = bool(matches or request_with_history or full_name)
        self._diag(
            "capture_patient_requests_done",
            history_number=history_number,
            ok=ok,
            full_name=full_name,
            matched_line=matched_line,
            matches=matches[:20],
            requests_count=len(requests),
            requests=[
                {
                    "kind": item.get("kind"),
                    "method": item.get("method"),
                    "url": item.get("url"),
                    "status": item.get("status"),
                    "request_len": item.get("request_len"),
                    "response_len": item.get("response_len"),
                    "contains_history": item.get("contains_history"),
                    "contains_full_name": item.get("contains_full_name"),
                }
                for item in requests[:20]
            ],
            steps=steps,
        )

        if ok:
            message = f"Найдено совпадений: {len(matches)}" if matches else "Перехват выполнен: пациент найден"
        else:
            message = "Перехват выполнен, но запроса/ответа с этим номером истории пока не видно"
        return BarsNetworkCaptureResult(
            ok,
            message,
            history_number=history_number,
            full_name=full_name,
            matched_line=matched_line,
            matches=matches,
            requests=requests,
            text_preview=preview or "; ".join(steps),
        )

    def _apply_check_result(self, result: BarsAuthCheckResult):
        self._last_authorized = bool(result.authorized)
        self._last_message = result.message

    def _find_yandex_browser(self) -> Optional[str]:
        env_path = os.environ.get("REMCARD_YANDEX_BROWSER")
        candidates = [
            env_path,
            os.path.join(LOCAL_APPDATA, "Yandex", "YandexBrowser", "Application", "browser.exe"),
            os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), "Yandex", "YandexBrowser", "Application", "browser.exe"),
            os.path.join(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"), "Yandex", "YandexBrowser", "Application", "browser.exe"),
        ]
        for candidate in candidates:
            if candidate and os.path.exists(candidate):
                return candidate
        return None

    def _default_profile_dir(self) -> str:
        if os.environ.get("REMCARD_BARS_ISOLATED_PROFILE") == "1":
            return ISOLATED_PROFILE_DIR

        existing_profile = os.path.join(LOCAL_APPDATA, "Yandex", "YandexBrowser", "User Data")
        if os.path.isdir(existing_profile):
            return existing_profile
        return ISOLATED_PROFILE_DIR

    def _build_browser_args(self, background: bool = False) -> list[str]:
        args = [self.browser_path]
        if self._use_user_data_dir:
            os.makedirs(self.profile_dir, exist_ok=True)
            args.append(f"--user-data-dir={self.profile_dir}")
        if self._enable_devtools:
            args.extend(
                [
                    f"--remote-debugging-port={self.debug_port}",
                    "--remote-debugging-address=127.0.0.1",
                ]
            )
        if background:
            args.extend(
                [
                    "--start-minimized",
                    "--window-position=-32000,-32000",
                    "--window-size=1200,900",
                ]
            )
        args.append(self.bars_url)
        return args

    def _start_browser_process(self, background: bool, event_prefix: str) -> subprocess.Popen:
        if self._enable_devtools and self._running_yandex_pids() and not self._devtools_available(self.debug_port):
            self._diag(
                f"{event_prefix}_browser_already_running",
                level="warning",
                message=(
                    "Yandex Browser is already running; Chromium may ignore "
                    "remote-debugging flags for the existing default profile."
                ),
                running_browser_pids=self._running_yandex_pids(),
            )

        args = self._build_browser_args(background=background)
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self._process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
        )
        self._diag(
            f"{event_prefix}_process_started",
            pid=getattr(self._process, "pid", None),
            profile_dir=self.profile_dir,
            use_user_data_dir=self._use_user_data_dir,
            enable_devtools=self._enable_devtools,
            background=background,
            args=args,
            debug_port=self.debug_port,
        )
        return self._process

    def _resolve_debug_port(self, preferred_port: int) -> int:
        if self._enable_devtools and self._devtools_available(preferred_port):
            self._diag("debug_port_existing_devtools", port=preferred_port)
            return preferred_port
        if self._is_port_free(preferred_port):
            self._diag("debug_port_free", port=preferred_port)
            return preferred_port
        for port in range(preferred_port + 1, preferred_port + 20):
            if (self._enable_devtools and self._devtools_available(port)) or self._is_port_free(port):
                self._diag("debug_port_fallback", preferred_port=preferred_port, selected_port=port)
                return port
        self._diag("debug_port_unresolved", level="warning", preferred_port=preferred_port)
        return preferred_port

    def _ensure_bars_debug_page(self, background: bool = False, allow_open: bool = True) -> Optional[dict[str, Any]]:
        page = self._find_bars_debug_page()
        if page:
            if background:
                self.minimize_bars_windows()
            return page

        if not allow_open:
            self._diag(
                "ensure_bars_debug_page_missing_no_open",
                level="warning",
                background=background,
                debug_port=self.debug_port,
                devtools_available=self._devtools_available(self.debug_port),
                pages=self._summarize_pages(self._get_debug_pages(log_failures=False)),
            )
            return None

        if not self._enable_devtools or not self.browser_path or not os.path.exists(self.browser_path):
            self._diag(
                "ensure_bars_debug_page_unavailable",
                level="warning",
                enable_devtools=self._enable_devtools,
                browser_path=self.browser_path,
            )
            return None

        if not self._devtools_available(self.debug_port):
            self.debug_port = self._resolve_debug_port(self.debug_port)
            try:
                self._start_browser_process(background=background, event_prefix="ensure_bars_debug_page")
            except Exception as exc:
                self._diag("ensure_bars_debug_page_launch_failed", level="warning", error=repr(exc))
                return None
            self._wait_for_devtools(timeout_sec=5.0)

        if self._devtools_available(self.debug_port):
            opened_page = self._open_bars_tab_via_devtools()
            if opened_page:
                self._diag("ensure_bars_debug_page_opened_tab", background=background, opened_page=opened_page)
                time.sleep(1.0)

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            page = self._find_bars_debug_page()
            if page:
                if background:
                    self.minimize_bars_windows()
                return page
            time.sleep(0.35)

        self._diag(
            "ensure_bars_debug_page_not_found",
            level="warning",
            debug_port=self.debug_port,
            devtools_available=self._devtools_available(self.debug_port),
            pages=self._summarize_pages(self._get_debug_pages(log_failures=False)),
        )
        return None

    def _wait_for_bars_ready(self, page: dict[str, Any], timeout_sec: float = 4.0) -> str:
        deadline = time.monotonic() + max(0.2, float(timeout_sec))
        last_text = ""
        while time.monotonic() < deadline:
            last_text = self._read_page_text(page)
            normalized = last_text.lower()
            if self._looks_like_bars_work_screen(last_text) or "журнал госпитализации" in normalized:
                return last_text
            time.sleep(0.35)
        return last_text

    def _devtools_available(self, port: int) -> bool:
        try:
            with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=0.35) as response:
                return response.status == 200
        except Exception:
            return False

    def _get_devtools_version(self) -> dict[str, Any]:
        try:
            with urlopen(f"http://127.0.0.1:{self.debug_port}/json/version", timeout=0.7) as response:
                payload = response.read().decode("utf-8", errors="replace")
            data = json.loads(payload)
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            self._diag(
                "devtools_version_unavailable",
                level="warning",
                debug_port=self.debug_port,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return {}

    def _open_bars_tab_via_devtools(self) -> dict[str, Any]:
        encoded_url = quote(self.bars_url, safe="")
        endpoint = f"http://127.0.0.1:{self.debug_port}/json/new?{encoded_url}"
        for method in ("PUT", "GET"):
            try:
                request = Request(endpoint, method=method)
                with urlopen(request, timeout=1.5) as response:
                    payload = response.read().decode("utf-8", errors="replace")
                data = json.loads(payload)
                return data if isinstance(data, dict) else {}
            except Exception as exc:
                self._diag(
                    "open_bars_tab_via_devtools_failed",
                    level="warning",
                    method=method,
                    endpoint=endpoint,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
        return {}

    def _activate_debug_page(self, page: dict[str, Any]) -> bool:
        page_id = str(page.get("id") or "")
        if not page_id:
            return False
        endpoint = f"http://127.0.0.1:{self.debug_port}/json/activate/{quote(page_id, safe='')}"
        try:
            request = Request(endpoint, method="GET")
            with urlopen(request, timeout=0.8) as response:
                ok = 200 <= response.status < 300
            self._diag(
                "activate_debug_page_done",
                page=self._summarize_pages([page])[0],
                ok=ok,
                log_to_main=False,
            )
            return ok
        except Exception as exc:
            self._diag(
                "activate_debug_page_failed",
                level="warning",
                page=self._summarize_pages([page])[0],
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return False

    @staticmethod
    def _is_port_free(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", int(port)))
                return True
            except OSError:
                return False

    def _wait_for_devtools(self, timeout_sec: float) -> bool:
        deadline = time.monotonic() + max(0.1, float(timeout_sec))
        while time.monotonic() < deadline:
            if self._devtools_available(self.debug_port):
                return True
            time.sleep(0.25)
        return False

    def _get_debug_pages(self, log_failures: bool = True) -> list[dict[str, Any]]:
        try:
            with urlopen(f"http://127.0.0.1:{self.debug_port}/json/list", timeout=0.7) as response:
                payload = response.read().decode("utf-8", errors="replace")
            data = json.loads(payload)
            pages = data if isinstance(data, list) else []
            self._diag("debug_pages_loaded", pages_count=len(pages), log_to_main=False)
            return pages
        except Exception as exc:
            if log_failures:
                self._diag(
                    "debug_pages_unavailable",
                    level="warning",
                    debug_port=self.debug_port,
                    error_type=type(exc).__name__,
                    error=str(exc),
                    running_browser_pids=self._running_yandex_pids(),
                )
            return []

    def _find_bars_debug_page(self) -> Optional[dict[str, Any]]:
        best_page: Optional[dict[str, Any]] = None
        best_score = -1
        for page in self._get_debug_pages():
            if str(page.get("type") or "") != "page":
                continue
            page_url = str(page.get("url") or "")
            title = str(page.get("title") or "")
            if not self._looks_like_bars_page(page_url, title):
                continue

            score = 10
            if "журнал госпитализации" in title.lower():
                score += 100
            text = self._read_page_text(page) if page.get("webSocketDebuggerUrl") else ""
            if self._looks_like_hospitalization_journal(text):
                score += 120
            elif self._looks_like_bars_work_screen(text):
                score += 70
            elif text:
                score += min(len(text), 1000) // 100
            if score > best_score:
                best_page = page
                best_score = score
        if best_page:
            self._diag("find_bars_debug_page_selected", score=best_score, page=self._summarize_pages([best_page])[0])
        return best_page

    def _looks_like_bars_page(self, page_url: str, title: str) -> bool:
        bars_host = urlparse(self.bars_url).netloc.lower()
        page_host = urlparse(page_url).netloc.lower()
        if bars_host and bars_host == page_host:
            return True
        text = f"{page_url}\n{title}".lower()
        return "медицинская информационная система" in text or "10.30.30.12" in text

    def _count_success_markers(self, text: str) -> int:
        normalized = str(text or "").lower()
        return sum(1 for marker in self.SUCCESS_MARKERS if marker in normalized)

    def _looks_like_bars_work_screen(self, text: str) -> bool:
        normalized = str(text or "").lower()
        nav_markers = (
            "регистратура",
            "рабочий лист",
            "рабочие места",
            "список направлений",
            "заказ исследований",
        )
        return self._count_success_markers(text) >= 3 and any(marker in normalized for marker in nav_markers)

    def _extract_dashboard_fields(self, text: str) -> dict[str, str]:
        result: dict[str, str] = {}
        labels = (
            "Пользователь:",
            "ЛПУ:",
            "Отделение:",
            "Кабинет:",
            "Отделение сотрудника:",
            "Специальность сотрудника:",
            "Системное время:",
        )
        lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
        for line in lines:
            normalized = line.lower()
            for label in labels:
                if normalized.startswith(label.lower()):
                    result[label.rstrip(":")] = self._trim_value(line[len(label):].strip())

        visible_sections = [
            "Регистратура",
            "Рабочий лист",
            "Рабочие места",
            "Список направлений",
            "Заказ исследований",
            "Аптека/Склад",
            "Учет",
            "Отчеты",
            "Аналитика",
            "Словари",
            "Настройки",
        ]
        found_sections = [section for section in visible_sections if section.lower() in text.lower()]
        if found_sections:
            result["Разделы"] = ", ".join(found_sections)
        return result

    @staticmethod
    def _trim_value(value: str, max_len: int = 180) -> str:
        value = " ".join(str(value or "").split())
        if len(value) <= max_len:
            return value
        return value[: max_len - 3] + "..."

    def _compact_text_preview(self, text: str, max_lines: int = 12) -> str:
        lines = []
        seen = set()
        for raw_line in str(text or "").splitlines():
            line = self._trim_value(raw_line.strip(), max_len=220)
            if not line or line in seen:
                continue
            seen.add(line)
            lines.append(line)
            if len(lines) >= max_lines:
                break
        return "\n".join(lines)

    def _open_hospitalization_journal(self, page: dict[str, Any]) -> list[str]:
        text = self._read_page_text_with_retry(page, attempts=2, delay_sec=0.3)
        if self._looks_like_hospitalization_journal(text):
            self._diag("open_hospitalization_journal_already_open")
            return ["Журнал госпитализации уже открыт"]

        steps: list[str] = []
        steps.append(self._hover_visible_text(page, ["Регистратура"]))
        time.sleep(0.35)
        if not self._page_contains_text(page, ["Приемный покой", "Приёмный покой"]):
            steps.append(self._click_visible_text(page, ["Регистратура"]))
            time.sleep(0.45)

        steps.append(self._hover_visible_text(page, ["Приемный покой", "Приёмный покой"]))
        time.sleep(0.45)
        if not self._page_contains_text(page, ["Журнал госпитализации"]):
            steps.append(self._click_visible_text(page, ["Приемный покой", "Приёмный покой"]))
            time.sleep(0.45)
            steps.append(self._hover_visible_text(page, ["Приемный покой", "Приёмный покой"]))
            time.sleep(0.45)

        steps.append(self._click_visible_text(page, ["Журнал госпитализации"]))
        self._wait_for_hospitalization_journal(page, timeout_sec=4.0)
        return steps

    def _wait_for_hospitalization_journal(self, page: dict[str, Any], timeout_sec: float) -> str:
        deadline = time.monotonic() + max(0.2, float(timeout_sec))
        last_text = ""
        while time.monotonic() < deadline:
            last_text = self._read_page_text_with_retry(page, attempts=1, delay_sec=0)
            if self._looks_like_hospitalization_journal(last_text):
                self._diag("open_hospitalization_journal_ready", text_len=len(last_text))
                return last_text
            time.sleep(0.35)
        self._diag(
            "open_hospitalization_journal_not_ready",
            level="warning",
            text_preview=self._compact_text_preview(last_text),
        )
        return last_text

    @staticmethod
    def _looks_like_hospitalization_journal(text: str) -> bool:
        normalized = str(text or "").lower()
        return (
            "журнал госпитализации" in normalized
            and "№ иб" in normalized
            and "пациент" in normalized
        )

    def _page_contains_text(self, page: dict[str, Any], labels: list[str]) -> bool:
        labels_json = json.dumps(labels, ensure_ascii=False)
        expression = f"""
(() => {{
  const labels = {labels_json}.map(x => String(x).toLowerCase());
  const body = document.body ? String(document.body.innerText || document.body.textContent || '').toLowerCase() : '';
  return labels.some(label => body.includes(label));
}})()
"""
        return self._evaluate_page_expression(page, expression).lower() == "true"

    def _install_network_capture(self, page: dict[str, Any]) -> str:
        expression = """
(() => {
  const serializeBody = (body) => {
    try {
      if (body == null) return '';
      if (typeof body === 'string') return body;
      if (body instanceof URLSearchParams) return body.toString();
      if (body instanceof FormData) {
        return Array.from(body.entries()).map(([k, v]) => `${k}=${String(v)}`).join('&');
      }
      if (body instanceof Blob) return `[Blob ${body.size}]`;
      return Object.prototype.toString.call(body);
    } catch (_) {
      return '[unavailable]';
    }
  };
  const absoluteUrl = (win, url) => {
    try { return new URL(String(url || ''), win.location.href).href; } catch (_) { return String(url || ''); }
  };
  const push = (win, item) => {
    try {
      if (!win.__remcardBarsCapturedRequests) win.__remcardBarsCapturedRequests = [];
      win.__remcardBarsCapturedRequests.push(item);
      if (win.__remcardBarsCapturedRequests.length > 200) win.__remcardBarsCapturedRequests.shift();
    } catch (_) {}
  };
  const patchWindow = (win) => {
    try {
      if (!win || !win.XMLHttpRequest || win.__remcardBarsCaptureVersion === 2) return false;
      win.__remcardBarsCaptureInstalled = true;
      win.__remcardBarsCaptureVersion = 2;
      win.__remcardBarsCapturedRequests = [];

      const xhrProto = win.XMLHttpRequest.prototype;
      const originalOpen = xhrProto.open;
      const originalSend = xhrProto.send;
      xhrProto.open = function(method, url) {
        this.__remcardBarsRequestInfo = {
          kind: 'xhr',
          method: String(method || 'GET').toUpperCase(),
          url: absoluteUrl(win, url)
        };
        return originalOpen.apply(this, arguments);
      };
      xhrProto.send = function(body) {
        const started = Date.now();
        const info = this.__remcardBarsRequestInfo || {kind: 'xhr', method: 'GET', url: ''};
        const requestBody = serializeBody(body);
        try {
          this.addEventListener('loadend', () => {
            let responseText = '';
            try {
              if (!this.responseType || this.responseType === 'text') responseText = String(this.responseText || '');
            } catch (_) {}
            push(win, {
              kind: 'xhr',
              method: info.method,
              url: info.url || String(this.responseURL || ''),
              body: requestBody.slice(0, 4000),
              status: this.status || 0,
              response_url: String(this.responseURL || ''),
              response_len: responseText.length,
              response_preview: responseText.slice(0, 250000),
              duration_ms: Date.now() - started
            });
          });
        } catch (_) {}
        return originalSend.apply(this, arguments);
      };

      if (win.fetch) {
        const originalFetch = win.fetch;
        win.fetch = function(input, init) {
          const started = Date.now();
          const method = String((init && init.method) || (input && input.method) || 'GET').toUpperCase();
          const url = absoluteUrl(win, (typeof input === 'string') ? input : (input && input.url));
          const requestBody = serializeBody(init && init.body);
          return originalFetch.apply(this, arguments).then((response) => {
            try {
              response.clone().text().then((text) => {
                push(win, {
                  kind: 'fetch',
                  method,
                  url,
                  body: requestBody.slice(0, 4000),
                  status: response.status || 0,
                  response_url: String(response.url || ''),
                  response_len: String(text || '').length,
                  response_preview: String(text || '').slice(0, 250000),
                  duration_ms: Date.now() - started
                });
              }).catch(() => {
                push(win, {kind: 'fetch', method, url, body: requestBody.slice(0, 4000), status: response.status || 0, response_len: 0, response_preview: '', duration_ms: Date.now() - started});
              });
            } catch (_) {}
            return response;
          });
        };
      }
      return true;
    } catch (_) {
      return false;
    }
  };
  let patched = 0;
  const walk = (win) => {
    try {
      if (patchWindow(win)) patched += 1;
      const frames = win.document ? win.document.querySelectorAll('iframe,frame') : [];
      for (const frame of frames) {
        try { walk(frame.contentWindow); } catch (_) {}
      }
    } catch (_) {}
  };
  walk(window);
  return JSON.stringify({ok: true, patched});
})()
"""
        payload = self._evaluate_json_expression(page, expression)
        self._diag("install_network_capture", patched=payload.get("patched"))
        return f"Перехватчик запросов установлен: {payload.get('patched') or 0}"

    def _clear_network_capture(self, page: dict[str, Any]):
        expression = """
(() => {
  let cleared = 0;
  const walk = (win) => {
    try {
      if (win.__remcardBarsCapturedRequests) {
        win.__remcardBarsCapturedRequests = [];
        cleared += 1;
      }
      const frames = win.document ? win.document.querySelectorAll('iframe,frame') : [];
      for (const frame of frames) {
        try { walk(frame.contentWindow); } catch (_) {}
      }
    } catch (_) {}
  };
  walk(window);
  return JSON.stringify({ok: true, cleared});
})()
"""
        payload = self._evaluate_json_expression(page, expression)
        self._diag("clear_network_capture", cleared=payload.get("cleared"))

    def _get_network_capture(self, page: dict[str, Any]) -> list[dict[str, Any]]:
        expression = """
(() => {
  const requests = [];
  const walk = (win) => {
    try {
      if (Array.isArray(win.__remcardBarsCapturedRequests)) {
        for (const item of win.__remcardBarsCapturedRequests) requests.push(item);
      }
      const frames = win.document ? win.document.querySelectorAll('iframe,frame') : [];
      for (const frame of frames) {
        try { walk(frame.contentWindow); } catch (_) {}
      }
    } catch (_) {}
  };
  walk(window);
  return JSON.stringify({ok: true, requests});
})()
"""
        payload = self._evaluate_json_expression(page, expression)
        requests = payload.get("requests")
        return requests if isinstance(requests, list) else []

    def _summarize_captured_requests(
        self,
        requests: list[dict[str, Any]],
        history_number: str,
    ) -> list[dict[str, Any]]:
        normalized_history = self._normalize_history(history_number)
        result: list[dict[str, Any]] = []
        for item in requests:
            if not isinstance(item, dict):
                continue
            body = str(item.get("body") or "")
            response_preview = str(item.get("response_preview") or "")
            url = str(item.get("url") or item.get("response_url") or "")
            combined = self._normalize_history("\n".join([url, body, response_preview]))
            full_name = self._guess_full_name(response_preview)
            contains_history = bool(normalized_history and normalized_history in combined)
            result.append(
                {
                    "kind": str(item.get("kind") or ""),
                    "method": str(item.get("method") or ""),
                    "url": self._trim_value(url, max_len=500),
                    "status": item.get("status") or 0,
                    "request_len": len(body),
                    "request_preview": self._trim_value(body, max_len=350),
                    "response_len": item.get("response_len") or len(response_preview),
                    "response_preview": self._trim_value(response_preview, max_len=500),
                    "duration_ms": item.get("duration_ms") or 0,
                    "contains_history": contains_history,
                    "contains_full_name": bool(full_name and full_name in response_preview),
                }
            )
        result.sort(
            key=lambda value: (
                1 if value.get("contains_history") else 0,
                int(value.get("response_len") or 0),
            ),
            reverse=True,
        )
        return result

    def _extract_patient_matches_from_requests(
        self,
        requests: list[dict[str, Any]],
        history_fragment: str,
    ) -> list[dict[str, str]]:
        normalized_fragment = self._normalize_history_fragment(history_fragment)
        if not normalized_fragment:
            return []

        matches: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in requests:
            if not isinstance(item, dict):
                continue
            response_text = str(item.get("response_preview") or "")
            if "<DataSet" not in response_text or "<row>" not in response_text:
                continue
            for row in self._parse_dataset_rows(response_text):
                history_number = self._trim_value(row.get("DEPBED") or row.get("DEP_BED") or "", max_len=300)
                full_name = self._trim_value(row.get("PATIENT") or row.get("PATIENT_ACTUAL") or "", max_len=220)
                if not history_number or not full_name:
                    continue
                normalized_history = self._normalize_history_fragment(history_number)
                if normalized_fragment not in normalized_history:
                    continue
                key = (history_number, full_name)
                if key in seen:
                    continue
                seen.add(key)
                matches.append(
                    {
                        "history_number": history_number,
                        "full_name": full_name,
                        "birthdate": self._trim_value(row.get("PATIENT_BIRTHDATE") or "", max_len=40),
                        "diagnosis": self._trim_value(
                            row.get("DIAGNOSIS_FROM") or row.get("HOSP_MKB") or "",
                            max_len=220,
                        ),
                    }
                )
        matches.sort(key=lambda item: item.get("history_number", ""))
        return matches

    def _parse_dataset_rows(self, xml_text: str) -> list[dict[str, str]]:
        try:
            root = ET.fromstring(str(xml_text or "").strip())
        except Exception as exc:
            self._diag("parse_dataset_rows_failed", level="warning", error_type=type(exc).__name__, error=str(exc))
            return []

        rows: list[dict[str, str]] = []
        for row_node in root.findall(".//row"):
            row: dict[str, str] = {}
            for child in list(row_node):
                tag = str(child.tag or "").strip()
                if not tag:
                    continue
                row[tag] = self._trim_value(child.text or "", max_len=1000)
            if row:
                rows.append(row)
        return rows

    @staticmethod
    def _normalize_history_fragment(value: str) -> str:
        return re.sub(r"\s+", "", str(value or "").lower())

    def _hover_visible_text(self, page: dict[str, Any], labels: list[str]) -> str:
        payload = self._act_on_visible_text(page, labels, action="hover")
        if payload.get("ok"):
            text = self._trim_value(str(payload.get("text") or ""), max_len=180)
            self._diag(
                "hover_visible_text_success",
                labels=labels,
                hovered_text=text,
                hovered_text_len=payload.get("text_len") or len(text),
                score=payload.get("score"),
                tag=payload.get("tag"),
                target_tag=payload.get("target_tag"),
            )
            return f"Наведено: {text}"
        self._diag("hover_visible_text_not_found", level="warning", labels=labels)
        return f"Не найдено для наведения: {', '.join(labels)}"

    def _click_visible_text(self, page: dict[str, Any], labels: list[str]) -> str:
        payload = self._act_on_visible_text(page, labels, action="click")
        if payload.get("ok"):
            clicked_text = str(payload.get("text") or "")
            clicked_preview = self._trim_value(clicked_text, max_len=180)
            self._diag(
                "click_visible_text_success",
                labels=labels,
                clicked_text=clicked_preview,
                clicked_text_len=payload.get("text_len") or len(clicked_text),
                score=payload.get("score"),
                tag=payload.get("tag"),
                target_tag=payload.get("target_tag"),
            )
            return f"Нажато: {clicked_preview}"
        self._diag("click_visible_text_not_found", level="warning", labels=labels)
        return f"Не найдено: {', '.join(labels)}"

    def _act_on_visible_text(self, page: dict[str, Any], labels: list[str], action: str) -> dict[str, Any]:
        labels_json = json.dumps(labels, ensure_ascii=False)
        action_json = json.dumps(action)
        expression = f"""
(() => {{
  const labels = {labels_json}.map(x => String(x).toLowerCase());
  const action = {action_json};
  const docs = [];
  const walk = (win) => {{
    try {{
      if (!win || !win.document) return;
      docs.push(win.document);
      for (const frame of win.document.querySelectorAll('iframe,frame')) {{
        try {{ walk(frame.contentWindow); }} catch (_) {{}}
      }}
    }} catch (_) {{}}
  }};
  walk(window);
  const visible = (el) => {{
    const style = el.ownerDocument.defaultView.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  }};
  const norm = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
  const ownText = (el) => norm(Array.from(el.childNodes)
    .filter(node => node.nodeType === Node.TEXT_NODE)
    .map(node => node.textContent || '')
    .join(' '));
  const elementText = (el) => norm(el.innerText || el.value || el.textContent || '');
  const hasChildWithLabel = (el, label) => Array.from(el.children || []).some(child => {{
    try {{
      return visible(child) && elementText(child).toLowerCase().includes(label);
    }} catch (_) {{
      return false;
    }}
  }});
  const clickableTarget = (el) => {{
    return el.closest('button,a,input,[role="button"],[role="menuitem"],[onclick],[tabindex]') || el;
  }};
  const clickElement = (el) => {{
    const target = clickableTarget(el);
    target.scrollIntoView({{block: 'center', inline: 'center'}});
    const win = target.ownerDocument.defaultView;
    for (const type of ['mousemove', 'mouseover', 'mouseenter']) {{
      target.dispatchEvent(new win.MouseEvent(type, {{bubbles: true, cancelable: true, view: win}}));
    }}
    if (action === 'click') {{
      for (const type of ['mousedown', 'mouseup', 'click']) {{
        target.dispatchEvent(new win.MouseEvent(type, {{bubbles: true, cancelable: true, view: win}}));
      }}
      if (typeof target.click === 'function') target.click();
    }}
    return target;
  }};
  const candidates = [];
  for (const doc of docs) {{
    const elements = Array.from(doc.querySelectorAll('button,a,input,span,div,label,li,[role="button"],[role="menuitem"],[onclick],[tabindex]'));
    for (const el of elements) {{
      if (!visible(el)) continue;
      const tag = String(el.tagName || '').toLowerCase();
      const style = el.ownerDocument.defaultView.getComputedStyle(el);
      const hasActionMarker = Boolean(el.getAttribute('role') || el.getAttribute('onclick') || el.getAttribute('tabindex'));
      if (tag === 'div' && el.children.length > 8 && !hasActionMarker && style.cursor !== 'pointer') continue;
      const text = elementText(el);
      if (text.length > 260 && !labels.some(label => text.toLowerCase().startsWith(label))) continue;
      const normalized = text.toLowerCase();
      if (!normalized) continue;
      const own = ownText(el).toLowerCase();
      for (const label of labels) {{
        if (!normalized.includes(label)) continue;
        let score = 0;
        if (normalized === label) score += 1000;
        else if (own === label) score += 900;
        else if (normalized.startsWith(label)) score += 500;
        else score += 200;
        if (['button', 'a', 'input'].includes(tag)) score += 120;
        if (hasActionMarker) score += 80;
        if (style.cursor === 'pointer') score += 50;
        if (text.length <= label.length + 8) score += 120;
        if (hasChildWithLabel(el, label) && own !== label) score -= 450;
        score -= Math.min(text.length, 500) / 4;
        candidates.push({{el, label, text, score, tag}});
      }}
    }}
  }}
  candidates.sort((a, b) => b.score - a.score);
  if (!candidates.length) return JSON.stringify({{ok: false}});
  const best = candidates[0];
  const target = clickElement(best.el);
  return JSON.stringify({{
    ok: true,
    text: best.text,
    text_len: best.text.length,
    score: best.score,
    tag: best.tag,
    target_tag: String(target.tagName || '').toLowerCase()
  }});
}})()
"""
        return self._evaluate_json_expression(page, expression)

    def _fill_history_search(self, page: dict[str, Any], history_number: str) -> str:
        history_json = json.dumps(history_number, ensure_ascii=False)
        expression = f"""
(() => {{
  const history = {history_json};
  const docs = [];
  const walk = (win) => {{
    try {{
      if (!win || !win.document) return;
      docs.push(win.document);
      for (const frame of win.document.querySelectorAll('iframe,frame')) {{
        try {{ walk(frame.contentWindow); }} catch (_) {{}}
      }}
    }} catch (_) {{}}
  }};
  walk(window);
  const visible = (el) => {{
    const style = el.ownerDocument.defaultView.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  }};
  const scoreInput = (el) => {{
    const attrs = [
      el.name, el.id, el.placeholder, el.title, el.getAttribute('aria-label'),
      el.closest('td,tr,div,fieldset') ? el.closest('td,tr,div,fieldset').innerText : ''
    ].map(x => String(x || '').toLowerCase()).join(' ');
    let score = 0;
    for (const marker of ['истор', 'иб', 'номер', 'госпитал']) {{
      if (attrs.includes(marker)) score += 10;
    }}
    if ((el.type || '').toLowerCase() === 'text') score += 2;
    return score;
  }};
  let best = null;
  let bestScore = -1;
  for (const doc of docs) {{
    const inputs = Array.from(doc.querySelectorAll('input:not([type=hidden]):not([disabled]), textarea:not([disabled])')).filter(visible);
    for (const input of inputs) {{
      const score = scoreInput(input);
      if (score > bestScore) {{
        best = input;
        bestScore = score;
      }}
    }}
  }}
  if (!best) return JSON.stringify({{ok: false, reason: 'visible input not found'}});
  best.focus();
  best.value = history;
  best.dispatchEvent(new Event('input', {{bubbles: true}}));
  best.dispatchEvent(new Event('change', {{bubbles: true}}));
  best.dispatchEvent(new KeyboardEvent('keydown', {{bubbles: true, key: 'Enter', code: 'Enter'}}));
  best.dispatchEvent(new KeyboardEvent('keyup', {{bubbles: true, key: 'Enter', code: 'Enter'}}));
  return JSON.stringify({{ok: true, score: bestScore, name: best.name || best.id || best.placeholder || ''}});
}})()
"""
        payload = self._evaluate_json_expression(page, expression)
        if payload.get("ok"):
            self._diag(
                "fill_history_search_success",
                history_number=history_number,
                input_name=payload.get("name"),
                score=payload.get("score"),
            )
            return f"Номер истории введен в поле поиска: {payload.get('name') or 'без имени'}"
        self._diag(
            "fill_history_search_failed",
            level="warning",
            history_number=history_number,
            reason=payload.get("reason"),
        )
        return f"Поле поиска истории не найдено: {payload.get('reason') or ''}".strip()

    def _evaluate_json_expression(self, page: dict[str, Any], expression: str) -> dict[str, Any]:
        raw = self._evaluate_page_expression(page, expression)
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _evaluate_page_expression(self, page: dict[str, Any], expression: str) -> str:
        ws_url = str(page.get("webSocketDebuggerUrl") or "")
        if not ws_url:
            return ""
        payload = {
            "id": int(time.time() * 1000) % 1000000,
            "method": "Runtime.evaluate",
            "params": {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": False,
            },
        }
        try:
            return self._cdp_runtime_evaluate(ws_url, payload)
        except Exception as exc:
            self._diag(
                "cdp_expression_failed",
                level="warning",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return ""

    def _extract_patient_line(self, text: str, history_number: str) -> tuple[str, str, str]:
        lines = [self._trim_value(line.strip(), max_len=500) for line in str(text or "").splitlines()]
        lines = [line for line in lines if line]
        normalized_history = self._normalize_history(history_number)
        matched_index = next(
            (
                index
                for index, line in enumerate(lines)
                if normalized_history and normalized_history in self._normalize_history(line)
            ),
            None,
        )
        if matched_index is None:
            return "", "", self._compact_text_preview(text)

        matched_line = lines[matched_index]
        context_lines = lines[max(0, matched_index - 3) : matched_index + 12]
        preview = "\n".join(context_lines)

        full_name = self._guess_full_name(matched_line)
        if not full_name:
            for candidate in lines[matched_index + 1 : matched_index + 6]:
                full_name = self._guess_full_name(candidate)
                if full_name:
                    break
        if not full_name:
            full_name = self._guess_full_name(preview)
        return full_name, matched_line, preview

    @staticmethod
    def _normalize_history(value: str) -> str:
        return re.sub(r"\s+", "", str(value or "").lower())

    @staticmethod
    def _guess_full_name(text: str) -> str:
        if not text:
            return ""
        ignored = {
            "Медицинская",
            "Информационная",
            "Система",
            "Регистратура",
            "Госпитализации",
            "Пользователь",
            "Отделение",
            "Кабинет",
            "Системное",
        }
        pattern = re.compile(r"\b([А-ЯЁ][А-ЯЁа-яё-]{2,})\s+([А-ЯЁ][А-ЯЁа-яё-]{2,})(?:\s+([А-ЯЁ][А-ЯЁа-яё-]{2,}))?\b")
        for match in pattern.finditer(text):
            parts = [part for part in match.groups() if part]
            if any(part in ignored for part in parts):
                continue
            if len(parts) >= 2:
                return " ".join(parts)
        return ""

    def _read_page_text(self, page: dict[str, Any]) -> str:
        ws_url = str(page.get("webSocketDebuggerUrl") or "")
        if not ws_url:
            return ""

        expression = (
            "(() => {"
            "const body = document.body ? document.body.innerText : '';"
            "return [document.title, location.href, body].join('\\n');"
            "})()"
        )
        return self._evaluate_page_expression(page, expression)

    def _read_page_text_with_retry(self, page: dict[str, Any], attempts: int = 3, delay_sec: float = 0.5) -> str:
        last_text = ""
        for index in range(max(1, int(attempts))):
            last_text = self._read_page_text(page)
            if last_text:
                return last_text
            if index < attempts - 1 and delay_sec > 0:
                time.sleep(delay_sec)
        return last_text

    def _running_yandex_pids(self) -> list[int]:
        try:
            output = subprocess.check_output(
                [
                    "tasklist",
                    "/FI",
                    "IMAGENAME eq browser.exe",
                    "/FO",
                    "CSV",
                    "/NH",
                ],
                text=True,
                encoding="cp866",
                errors="replace",
                timeout=1.5,
            )
        except Exception as exc:
            return []

        pids: list[int] = []
        for line in output.splitlines():
            line = line.strip()
            if not line or line.upper().startswith("INFO:"):
                continue
            try:
                parts = next(csv_row for csv_row in [line.split('","')])
                if len(parts) >= 2:
                    pid_text = parts[1].strip('"')
                    pids.append(int(pid_text))
            except Exception:
                continue
        return pids

    @staticmethod
    def _summarize_pages(pages: list[dict[str, Any]]) -> list[dict[str, str]]:
        summary = []
        for page in pages:
            summary.append(
                {
                    "url": str(page.get("url") or "")[:220],
                    "title": str(page.get("title") or "")[:160],
                    "type": str(page.get("type") or ""),
                    "has_ws": "1" if page.get("webSocketDebuggerUrl") else "0",
                }
            )
        return summary

    def _diag(self, event: str, level: str = "info", log_to_main: bool = True, **fields):
        payload = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "event": event,
            **fields,
        }
        line = json.dumps(payload, ensure_ascii=False, default=str)
        try:
            os.makedirs(os.path.dirname(self._diag_path), exist_ok=True)
            with open(self._diag_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass

        if not log_to_main:
            return
        message = f"{BARS_DIAG_PREFIX} {event}: {line}"
        if level == "warning":
            logger.warning(message)
        elif level == "error":
            logger.error(message)
        else:
            logger.info(message)

    def _cdp_runtime_evaluate(self, ws_url: str, payload: dict[str, Any]) -> str:
        parsed = urlparse(ws_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or self.debug_port
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        with socket.create_connection((host, port), timeout=2.0) as sock:
            sock.settimeout(5.0)
            self._websocket_handshake(sock, host, port, path)
            self._websocket_send_text(sock, json.dumps(payload, ensure_ascii=False))

            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                frame = self._websocket_read_frame(sock)
                if frame is None:
                    continue
                opcode, data = frame
                if opcode == 8:
                    break
                if opcode != 1:
                    continue
                response = json.loads(data.decode("utf-8", errors="replace"))
                if response.get("id") != payload["id"]:
                    continue
                result = response.get("result", {}).get("result", {})
                value = result.get("value")
                return str(value or "")

        return ""

    def _websocket_handshake(self, sock: socket.socket, host: str, port: int, path: str):
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        sock.sendall(request.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response and len(response) < 8192:
            chunk = sock.recv(1024)
            if not chunk:
                break
            response += chunk
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise RuntimeError("DevTools WebSocket handshake failed")

    @staticmethod
    def _websocket_send_text(sock: socket.socket, text: str):
        payload = text.encode("utf-8")
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.append(0x80 | 126)
            header.extend(length.to_bytes(2, "big"))
        else:
            header.append(0x80 | 127)
            header.extend(length.to_bytes(8, "big"))
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        sock.sendall(bytes(header) + mask + masked)

    @staticmethod
    def _websocket_read_exact(sock: socket.socket, size: int) -> bytes:
        chunks = []
        remaining = size
        while remaining > 0:
            chunk = sock.recv(remaining)
            if not chunk:
                raise RuntimeError("Unexpected WebSocket EOF")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _websocket_read_frame(self, sock: socket.socket) -> Optional[tuple[int, bytes]]:
        header = self._websocket_read_exact(sock, 2)
        opcode = header[0] & 0x0F
        masked = bool(header[1] & 0x80)
        length = header[1] & 0x7F
        if length == 126:
            length = int.from_bytes(self._websocket_read_exact(sock, 2), "big")
        elif length == 127:
            length = int.from_bytes(self._websocket_read_exact(sock, 8), "big")

        mask = self._websocket_read_exact(sock, 4) if masked else b""
        payload = self._websocket_read_exact(sock, length) if length else b""
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return opcode, payload
