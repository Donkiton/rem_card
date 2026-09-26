"""Offline AntV infographic rendering through an isolated helper process."""

from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Mapping, Sequence


_WORKER_FLAG = "--remcard-infographic-worker"
_WORKER_TIMEOUT_SECONDS = 30.0
_MAX_ITEMS = 6
_DEFAULT_COLORS = ("#1783FF", "#00C9C9", "#F0884D", "#7863FF")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def render_infographic(
    items: Sequence[Mapping[str, object]],
    *,
    title: str,
    colors: Sequence[str],
) -> str | None:
    """Render one to six scalar cards to a full-resolution temporary PNG.

    The Qt/Chromium runtime stays in a short-lived child process so callers
    can safely invoke this function from report worker threads. Any validation,
    startup, rendering, or export failure returns ``None`` for the existing
    chart fallback.
    """

    job_directory = None
    output_path = ""
    succeeded = False
    try:
        payload = _normalize_payload(items, title=title, colors=colors)
        if payload is None:
            return None
        job_directory = tempfile.TemporaryDirectory(
            prefix="remcard_infographic_", ignore_cleanup_errors=True
        )
        input_path = str(Path(job_directory.name) / "request.json")
        output_path = _reserve_temp_path("graph_", ".png")
        Path(input_path).write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        if not _invoke_worker(input_path, output_path):
            return None
        output = Path(output_path)
        if output.stat().st_size < len(_PNG_SIGNATURE):
            return None
        with output.open("rb") as stream:
            if stream.read(len(_PNG_SIGNATURE)) != _PNG_SIGNATURE:
                return None
        if not _is_valid_png(output_path):
            return None
        succeeded = True
        return str(output.resolve())
    except (OSError, OverflowError, TypeError, ValueError):
        return None
    finally:
        if job_directory is not None:
            job_directory.cleanup()
        if output_path and not succeeded:
            _unlink_quietly(output_path)


def _normalize_payload(
    items: Sequence[Mapping[str, object]],
    *,
    title: str,
    colors: Sequence[str],
) -> dict[str, object] | None:
    try:
        source_items = list(items)
    except TypeError:
        return None
    if not 1 <= len(source_items) <= _MAX_ITEMS:
        return None

    normalized_items: list[dict[str, object]] = []
    for source in source_items:
        if not isinstance(source, Mapping):
            return None
        label = _clean_text(source.get("label"), maximum=100)
        value = source.get("value")
        if not label or isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            numeric = float(value)
        except (OverflowError, TypeError, ValueError):
            return None
        if not math.isfinite(numeric):
            return None
        numeric = round(numeric, 2)
        normalized_value: int | float = int(numeric) if numeric.is_integer() else numeric
        unit = _clean_text(source.get("unit"), maximum=40)
        caption = _clean_text(source.get("caption"), maximum=240)
        if unit is None or caption is None:
            return None
        description = " · ".join(part for part in (unit, caption) if part)
        item: dict[str, object] = {"label": label, "value": normalized_value}
        if description:
            description_lines = textwrap.wrap(
                description,
                width=55,
                break_long_words=False,
                break_on_hyphens=False,
            )
            if len(description_lines) > 2:
                return None
            item["desc"] = "\n".join(description_lines)
        normalized_items.append(item)

    try:
        normalized_colors = [
            str(color).strip()
            for color in colors or ()
            if _is_hex_color(str(color).strip())
        ]
    except TypeError:
        return None
    if not normalized_colors:
        normalized_colors = list(_DEFAULT_COLORS)

    clean_title = _clean_text(title, maximum=180)
    if clean_title is None:
        return None
    clean_title = clean_title or "Показатели"
    if any(len(word) > 45 for word in clean_title.split()):
        return None
    title_lines = textwrap.wrap(
        clean_title,
        width=45,
        break_long_words=False,
        break_on_hyphens=False,
    )
    if len(title_lines) > 3:
        return None
    wrapped_title = "\n".join(title_lines)
    return {
        "title": wrapped_title,
        "colors": normalized_colors[:_MAX_ITEMS],
        "items": normalized_items,
    }


def _clean_text(value: object, *, maximum: int) -> str | None:
    if value is None:
        return ""
    cleaned = " ".join(str(value).replace("\x00", " ").split())
    return cleaned if len(cleaned) <= maximum else None


def _is_hex_color(value: str) -> bool:
    if len(value) not in (4, 7) or not value.startswith("#"):
        return False
    return all(character in "0123456789abcdefABCDEF" for character in value[1:])


def _reserve_temp_path(prefix: str, suffix: str) -> str:
    descriptor, path = tempfile.mkstemp(prefix=prefix, suffix=suffix)
    os.close(descriptor)
    os.unlink(path)
    return path


def _worker_command(input_path: str, output_path: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, _WORKER_FLAG, input_path, output_path]
    checkout_root = Path(__file__).resolve().parents[2]
    return [
        sys.executable,
        str(checkout_root / "run_remcard.py"),
        _WORKER_FLAG,
        input_path,
        output_path,
    ]


def _invoke_worker(input_path: str, output_path: str) -> bool:
    command = _worker_command(input_path, output_path)
    environment = dict(os.environ)
    environment.setdefault("QT_QPA_PLATFORM", "offscreen")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        environment.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
    chromium_flags = environment.get("QTWEBENGINE_CHROMIUM_FLAGS", "").strip()
    required_flags = (
        "--disable-gpu",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-sync",
        "--no-first-run",
    )
    environment["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(
        [chromium_flags, *required_flags]
    ).strip()

    kwargs: dict[str, object] = {
        "cwd": str(Path(__file__).resolve().parents[2]),
        "env": environment,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
        startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
        if startupinfo_cls is not None:
            startupinfo = startupinfo_cls()
            startupinfo.dwFlags |= int(getattr(subprocess, "STARTF_USESHOWWINDOW", 0) or 0)
            startupinfo.wShowWindow = int(getattr(subprocess, "SW_HIDE", 0) or 0)
            kwargs["startupinfo"] = startupinfo
    else:
        kwargs["start_new_session"] = True

    process = None
    try:
        process = subprocess.Popen(command, **kwargs)
        return process.wait(timeout=_WORKER_TIMEOUT_SECONDS) == 0
    except (OSError, subprocess.TimeoutExpired):
        if process is not None:
            _terminate_process_tree(process)
        return False
    finally:
        if process is not None and process.poll() is None:
            _terminate_process_tree(process)


def _terminate_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0),
            )
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _is_valid_png(path: str) -> bool:
    try:
        from PIL import Image, ImageChops

        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            image.load()
            if image.width <= 0 or image.height <= 0 or image.getbbox() is None:
                return False
            rgb = image.convert("RGB")
            background = Image.new("RGB", rgb.size, rgb.getpixel((0, 0)))
            return ImageChops.difference(rgb, background).getbbox() is not None
    except (OSError, ValueError):
        return False


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


__all__ = ["render_infographic"]
