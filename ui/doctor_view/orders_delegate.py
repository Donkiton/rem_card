"""
Backward-compatible re-export for legacy imports.
Canonical implementation lives in ui/shared/orders_delegate.py.
"""

from ..shared.orders_delegate import OrdersDelegate

__all__ = ["OrdersDelegate"]
