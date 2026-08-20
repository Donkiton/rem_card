"""
Backward-compatible re-export for legacy imports.
Canonical implementation lives in ui/shared/orders_model.py.
"""

from ..shared.orders_model import OrdersModel

__all__ = ["OrdersModel"]
