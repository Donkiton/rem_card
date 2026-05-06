from __future__ import annotations

import os
import pathlib
import sys

from rem_card.app.logger import logger
from rem_card.ui.shared.custom_message_box import CustomMessageBox


def open_pdf_file(pdf_path, *, parent=None) -> bool:
    path = pathlib.Path(pdf_path)
    if not path.exists():
        logger.warning("[PdfOpener] PDF file not found path=%s", path)
        if parent is not None:
            CustomMessageBox.warning(parent, "PDF не найден", f"Файл отчета не найден:\n{path}")
        return False

    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices

            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                raise RuntimeError("система не приняла запрос на открытие файла")
        return True
    except Exception as exc:
        logger.exception("[PdfOpener] failed to open PDF path=%s", path)
        if parent is not None:
            CustomMessageBox.critical(parent, "Ошибка", f"Не удалось открыть PDF:\n{exc}")
        return False
