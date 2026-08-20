from __future__ import annotations

import threading
from typing import Any

from rem_card.services.operblock_anesthesia_types import load_operblock_anesthesia_types
from rem_card.services.operblock_team import (
    load_operblock_anesthesiologists,
    load_operblock_anesthetists,
)
from rem_card.services.settings.settings_service import (
    DOCTORS_KEY,
    OPERBLOCK_SETTINGS_KEY,
    get_settings_service,
)


_CACHE_LOCK = threading.RLock()
_CACHE_KEY: tuple[int, str, int, str] | None = None
_CACHE_VALUE: dict[str, Any] | None = None


def _copy_options(options: dict[str, Any]) -> dict[str, Any]:
    return {
        "anesthesia_types": [dict(item or {}) for item in options.get("anesthesia_types") or []],
        "anesthesiologists": list(options.get("anesthesiologists") or []),
        "anesthetists": list(options.get("anesthetists") or []),
    }


def invalidate_start_anesthesia_options_cache() -> None:
    global _CACHE_KEY, _CACHE_VALUE
    with _CACHE_LOCK:
        _CACHE_KEY = None
        _CACHE_VALUE = None


def load_start_anesthesia_options() -> dict[str, Any]:
    """Load dialog catalogs, reusing them while settings versions stay current."""
    global _CACHE_KEY, _CACHE_VALUE
    settings_service = get_settings_service()
    operblock_version, operblock_hash = settings_service.get_catalog_version(OPERBLOCK_SETTINGS_KEY)
    doctors_version, doctors_hash = settings_service.get_catalog_version(DOCTORS_KEY)
    cache_key = (
        int(operblock_version or 0),
        str(operblock_hash or ""),
        int(doctors_version or 0),
        str(doctors_hash or ""),
    )

    with _CACHE_LOCK:
        if _CACHE_KEY == cache_key and _CACHE_VALUE is not None:
            return _copy_options(_CACHE_VALUE)

    options = {
        "anesthesia_types": load_operblock_anesthesia_types(),
        "anesthesiologists": load_operblock_anesthesiologists(),
        "anesthetists": load_operblock_anesthetists(),
    }
    cached_options = _copy_options(options)
    with _CACHE_LOCK:
        _CACHE_KEY = cache_key
        _CACHE_VALUE = cached_options
    return _copy_options(cached_options)
