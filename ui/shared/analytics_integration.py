from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Callable

from rem_card.app.logger import logger


class _AnalyticsConnectionAdapter:
    def __init__(self, conn, *, db_path: str, owns_connection: bool):
        self._conn = conn
        self.db_path = db_path
        self._owns_connection = owns_connection

    def get_connection(self):
        return self._conn

    def close_connection(self):
        if self._owns_connection and self._conn is not None:
            self._conn.close()
        self._conn = None


def open_analytics_report_dialog(
    parent=None,
    *,
    remcard_service=None,
    db_manager=None,
    start_dt: str | None = None,
    end_dt: str | None = None,
    show_graph_button: bool = True,
    db_paths: list[str] | None = None,
):
    base_db_manager = _get_base_db_manager(remcard_service=remcard_service, db_manager=db_manager)
    analytics_manager, cleanup = _resolve_analytics_manager(
        base_db_manager,
        start_dt=start_dt,
        end_dt=end_dt,
        db_paths=db_paths,
    )

    try:
        if not show_graph_button:
            from rem_card.ui.analytics.statistics_dialog import StatisticsDialog

            now = datetime.now()
            start_dt = start_dt or (now - timedelta(days=30)).strftime("%Y-%m-%d 00:00:00")
            end_dt = end_dt or now.strftime("%Y-%m-%d 23:59:59")
            dialog = StatisticsDialog(
                analytics_manager,
                start_date_str=start_dt,
                end_date_str=end_dt,
                parent=parent,
            )
        else:
            from rem_card.ui.analytics.report_dialog import ReportDialog

            dialog = ReportDialog(
                analytics_manager,
                parent,
                start_dt=start_dt,
                end_dt=end_dt,
                show_graph_button=show_graph_button,
            )
        dialog.exec()
    finally:
        if cleanup:
            cleanup()


def open_analytics_graphs_dialog(
    parent=None,
    *,
    remcard_service=None,
    db_manager=None,
    start_dt: str | None = None,
    end_dt: str | None = None,
    db_paths: list[str] | None = None,
):
    now = datetime.now()
    if not start_dt:
        start_dt = (now - timedelta(days=30)).strftime("%Y-%m-%d 00:00:00")
    if not end_dt:
        end_dt = now.strftime("%Y-%m-%d 23:59:59")

    base_db_manager = _get_base_db_manager(remcard_service=remcard_service, db_manager=db_manager)
    analytics_manager, cleanup = _resolve_analytics_manager(
        base_db_manager,
        start_dt=start_dt,
        end_dt=end_dt,
        db_paths=db_paths,
    )

    from rem_card.ui.analytics.graphs_dialog import GraphsDialog

    try:
        dialog = GraphsDialog(analytics_manager, start_dt, end_dt, parent)
        dialog.exec()
    finally:
        if cleanup:
            cleanup()


def _get_base_db_manager(*, remcard_service=None, db_manager=None):
    if db_manager is not None:
        return db_manager

    candidates = [
        ("orders_dao", "db"),
        ("patient_dao", "db"),
        ("vitals_dao", "db"),
        ("data_service", "db"),
    ]
    for outer_attr, inner_attr in candidates:
        owner = getattr(remcard_service, outer_attr, None)
        candidate = getattr(owner, inner_attr, None)
        if candidate is not None:
            return candidate

    candidate = getattr(remcard_service, "db_manager", None)
    if candidate is not None:
        return candidate

    raise ValueError("Не найден менеджер базы данных для аналитики.")


def _resolve_analytics_manager(
    base_db_manager,
    *,
    start_dt: str | None,
    end_dt: str | None,
    db_paths: list[str] | None,
) -> tuple[object, Callable[[], None] | None]:
    normalized = _normalize_db_paths(db_paths or [])
    if not normalized:
        return _create_live_analytics_manager(base_db_manager)

    base_path = _get_db_path(base_db_manager)
    if len(normalized) == 1 and base_path and os.path.normcase(normalized[0]) == os.path.normcase(base_path):
        return _create_live_analytics_manager(base_db_manager)

    try:
        from rem_card.services.analytics.multi_db_analytics import create_multi_db_analytics_manager

        merged_manager = create_multi_db_analytics_manager(
            normalized,
            start_dt=start_dt,
            end_dt=end_dt,
        )
        return merged_manager, merged_manager.close_connection
    except Exception as exc:
        logger.warning("Failed to build multi-DB analytics manager, fallback to primary DB: %s", exc)
        return _create_live_analytics_manager(base_db_manager)


def _create_live_analytics_manager(base_db_manager) -> tuple[object, Callable[[], None] | None]:
    if hasattr(base_db_manager, "get_connection"):
        return base_db_manager, None

    if hasattr(base_db_manager, "_open_readonly_central_connection"):
        conn = base_db_manager._open_readonly_central_connection()
        adapter = _AnalyticsConnectionAdapter(
            conn,
            db_path=_get_db_path(base_db_manager) or "readonly_analytics",
            owns_connection=True,
        )
        return adapter, adapter.close_connection

    db_path = _get_db_path(base_db_manager)
    if db_path and os.path.isfile(db_path):
        from rem_card.services.analytics.multi_db_analytics import create_readonly_analytics_manager

        manager = create_readonly_analytics_manager(db_path)
        return manager, manager.close_connection

    conn = getattr(base_db_manager, "_remcard_conn", None)
    if conn is not None:
        adapter = _AnalyticsConnectionAdapter(conn, db_path="live_connection", owns_connection=False)
        return adapter, None

    raise ValueError("База данных недоступна для аналитики.")


def _normalize_db_paths(db_paths: list[str]) -> list[str]:
    normalized = []
    seen = set()
    for raw in db_paths:
        if not raw:
            continue
        abs_path = os.path.abspath(str(raw))
        if not os.path.isfile(abs_path):
            continue
        key = os.path.normcase(abs_path)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(abs_path)
    return normalized


def _get_db_path(db_manager) -> str:
    for attr in ("db_path", "remcard_db_path", "journal_db_path"):
        value = getattr(db_manager, attr, None)
        if value:
            return os.path.abspath(str(value))
    return ""
