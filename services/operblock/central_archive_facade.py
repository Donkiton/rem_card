"""Short-lived explicit central archive calls for an offline OperBlock shell."""
from __future__ import annotations

from typing import Any


class ExplicitCentralOperBlockService:
    """Bounded central archive operations for the local OperBlock shell.

    This is intentionally not a general ``OperBlockService`` proxy: a local
    archive browser may list, view metadata and update a closed archive card,
    but cannot create, restore or delete central clinical records.
    """
    _ALLOWED_METHODS = frozenset({
        "list_archived_operation_cases",
        "list_archived_operation_cases_page",
        "get_archive_db_paths_for_period",
        "get_operation_case_form_data",
        "update_archived_operation_case_form_data",
        "list_archived_operation_case_edit_history",
    })

    def __init__(self, *, central_root: str | None = None, local_root: str | None = None):
        self.local_root = local_root
        if central_root:
            self.central_root = str(central_root)
        else:
            from rem_card.app.operblock_local_destination import get_operblock_destination
            self.central_root = str(get_operblock_destination(local_root) or "")

    def __getattr__(self, name: str):
        if name not in self._ALLOWED_METHODS:
            raise AttributeError(f"Central archive operation is not allowed: {name}")

        def invoke(*args: Any, **kwargs: Any):
            from rem_card.app.operblock_local_destination import open_central_for_operblock
            from rem_card.services.operblock_service import OperBlockService
            with open_central_for_operblock(self.central_root, local_root=self.local_root) as gateway:
                method = getattr(OperBlockService(gateway), name)
                return method(*args, **kwargs)
        return invoke
