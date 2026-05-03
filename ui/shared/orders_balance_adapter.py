from __future__ import annotations

from copy import copy
from datetime import datetime


_INACTIVE_STATUSES = {"deleted", "cancelled"}
_NURSE_MARKS = {"nurse_executed", "nurse_not_executed"}


def _status_value(value) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip().lower()


def _same_shift(model, shift_date) -> bool:
    model_shift = getattr(model, "shift_date", None)
    if model_shift is None or shift_date is None:
        return False

    service = getattr(model, "service", None)
    if service is not None and hasattr(service, "get_day_period"):
        try:
            model_start, _ = service.get_day_period(model_shift)
            target_start, _ = service.get_day_period(shift_date)
            return model_start == target_start
        except Exception:
            pass

    if model_shift == shift_date:
        return True
    try:
        return model_shift.date() == shift_date.date()
    except Exception:
        return False


def _has_pending_mark(model) -> bool:
    for admin in getattr(model, "admin_map", {}).values():
        if admin is not None and hasattr(admin, "_pending_mark"):
            return True
    return False


def _should_use_local_orders(widget, model, *, tab_active: bool) -> bool:
    if tab_active:
        return True

    has_drafts = getattr(widget, "has_drafts", None)
    if callable(has_drafts):
        try:
            if bool(has_drafts()):
                return True
        except Exception:
            pass

    try:
        if int(getattr(widget, "_pending_admin_write_count", 0) or 0) > 0:
            return True
    except Exception:
        pass

    return _has_pending_mark(model)


def _copy_admin_for_balance(admin):
    admin_copy = copy(admin)
    if hasattr(admin, "_pending_mark"):
        mark = getattr(admin, "_pending_mark", "") or ""
        admin_copy.comment = mark
        admin_copy.actual_time = datetime.now() if mark else None
    return admin_copy


def _parse_datetime(value):
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def _current_orders_mark_overrides(current_orders_widget):
    if current_orders_widget is None:
        return None

    overrides = {}
    get_pending = getattr(current_orders_widget, "_get_pending_mark", None)
    for admin_id in list(getattr(current_orders_widget, "_pending_marks", {}) or {}):
        pending = get_pending(admin_id) if callable(get_pending) else None
        if not pending:
            continue
        overrides[int(admin_id)] = {
            "comment": str(pending.get("mark") or ""),
            "actual_time": _parse_datetime(pending.get("actual_time")),
        }

    if overrides:
        return overrides
    return None


def apply_current_order_mark_overrides(
    orders,
    current_orders_widget,
    admission_id,
    shift_date,
):
    try:
        if int(getattr(current_orders_widget, "admission_id", 0) or 0) != int(admission_id or 0):
            return None
    except Exception:
        return None
    if not _same_shift(current_orders_widget, shift_date):
        return None

    overrides = _current_orders_mark_overrides(current_orders_widget)
    if not overrides:
        return None

    patched_orders = []
    for order in orders or []:
        order_copy = copy(order)
        patched_admins = []
        for admin in getattr(order, "administrations", []) or []:
            admin_copy = copy(admin)
            try:
                override = overrides.get(int(getattr(admin, "id", 0) or 0))
            except Exception:
                override = None
            if override is not None:
                mark = str(override.get("comment") or "")
                admin_copy.comment = mark if mark in _NURSE_MARKS else ""
                admin_copy.actual_time = override.get("actual_time") if admin_copy.comment else None
            patched_admins.append(admin_copy)
        order_copy.administrations = patched_admins
        patched_orders.append(order_copy)

    return patched_orders


def build_balance_orders_from_orders_widget(
    orders_widget,
    admission_id,
    shift_date,
    *,
    tab_active: bool = False,
):
    """Return local order copies when the orders widget has unsaved/pending balance state."""
    if orders_widget is None:
        return None
    model = getattr(orders_widget, "model", None)
    if model is None:
        return None

    try:
        if int(getattr(model, "admission_id", 0) or 0) != int(admission_id or 0):
            return None
    except Exception:
        return None
    if not _same_shift(model, shift_date):
        return None
    if not _should_use_local_orders(orders_widget, model, tab_active=bool(tab_active)):
        return None

    admins_by_order_id = {}
    for key, admin in getattr(model, "admin_map", {}).items():
        if admin is None:
            continue
        if _status_value(getattr(admin, "status", "")) in _INACTIVE_STATUSES:
            continue
        order_id = getattr(admin, "order_id", None)
        if order_id is None and isinstance(key, tuple) and key:
            order_id = key[0]
        if order_id is None:
            continue
        admins_by_order_id.setdefault(order_id, []).append(_copy_admin_for_balance(admin))

    balance_orders = []
    for order in getattr(model, "orders", []) or []:
        if order is None:
            continue
        if getattr(order, "_pending_delete", False):
            continue
        if _status_value(getattr(order, "status", "")) in _INACTIVE_STATUSES:
            continue
        order_id = getattr(order, "id", None)
        order_copy = copy(order)
        order_copy.administrations = list(admins_by_order_id.get(order_id, []))
        balance_orders.append(order_copy)

    return balance_orders
