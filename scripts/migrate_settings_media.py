from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from _local_rem_card_bootstrap import bootstrap_local_rem_card  # noqa: E402


bootstrap_local_rem_card()

from rem_card.data.settings.settings_db import SettingsDatabase  # noqa: E402
from rem_card.app.network_maintenance import find_active_network_sessions, network_maintenance_lock  # noqa: E402
from rem_card.services.settings.settings_service import SettingsService, get_settings_service  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Вынести изображения фонов и иконок из БД настроек в hash-addressed "
            "файлы и удалить неиспользуемые фоны. Запускать вне рабочей смены."
        )
    )
    parser.add_argument("--apply", action="store_true", help="Подтвердить изменение БД настроек")
    parser.add_argument(
        "--skip-vacuum",
        action="store_true",
        help="Не сжимать SQLite после удаления BLOB (размер файла не уменьшится)",
    )
    parser.add_argument("--baza-dir", help="Явный путь к папке Baza; без параметра используется текущая конфигурация")
    args = parser.parse_args()
    service = (
        SettingsService(SettingsDatabase(baza_dir=args.baza_dir))
        if args.baza_dir
        else get_settings_service()
    )
    maintenance_lock = None
    if args.apply:
        baza_dir = os.path.abspath(os.path.normpath(args.baza_dir or os.path.dirname(service.db.settings_dir)))
        session_locks_dir = os.path.join(baza_dir, "session_locks")
        active_sessions = find_active_network_sessions(session_locks_dir)
        if active_sessions:
            roles = ", ".join(sorted({item.role for item in active_sessions}))
            parser.error(f"миграция запрещена: активны сетевые сессии ({roles})")
        maintenance_lock = network_maintenance_lock(session_locks_dir)
        if not maintenance_lock.acquire(f"settings-media-migration:{os.getpid()}", "settings_media_migration"):
            parser.error("миграция запрещена: выполняется другая сетевая операция обслуживания")
        active_sessions = find_active_network_sessions(session_locks_dir)
        if active_sessions:
            maintenance_lock.release()
            roles = ", ".join(sorted({item.role for item in active_sessions}))
            parser.error(f"миграция запрещена: появилась активная сетевая сессия ({roles})")
    try:
        report = service.migrate_media_blobs_to_files(
            apply=args.apply,
            compact=bool(args.apply and not args.skip_vacuum),
        )
    finally:
        if maintenance_lock is not None:
            maintenance_lock.release()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
