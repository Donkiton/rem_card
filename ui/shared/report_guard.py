from __future__ import annotations

from datetime import datetime

from rem_card.app.logger import logger
from rem_card.ui.shared.custom_message_box import CustomMessageBox


NO_DAILY_CARD_MESSAGE = "За текущие реанимационные сутки карта не создана."


def ensure_daily_card_exists(parent, service, admission_id: int, shift_date: datetime) -> bool:
    if not service or not admission_id or not shift_date:
        return False
    if not hasattr(service, "has_card"):
        return True

    try:
        if service.has_card(admission_id, shift_date):
            return True
    except Exception as exc:
        logger.exception(
            "[ReportGuard] failed to check daily card admission_id=%s date=%s",
            admission_id,
            shift_date.isoformat() if hasattr(shift_date, "isoformat") else shift_date,
        )
        CustomMessageBox.critical(
            parent,
            "Ошибка",
            f"Не удалось проверить наличие карты за сутки:\n{exc}",
        )
        return False

    CustomMessageBox.warning(parent, "Отчет за сутки", NO_DAILY_CARD_MESSAGE)
    return False
