"""Preview/apply an explicit local log cleanup; never inspect the medical DB."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from _local_rem_card_bootstrap import bootstrap_local_rem_card

bootstrap_local_rem_card()

from rem_card.app.runtime_log_retention import apply_log_cleanup, plan_log_cleanup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Предпросмотр и очистка только технических логов RemCard")
    parser.add_argument("--logs-dir", required=True, help="Точный локальный каталог логов установки")
    parser.add_argument("--include-legacy", action="store_true", help="Дополнительно проверить старые AppData-логи")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--preview", type=Path, help="Сохранить список кандидатов без удаления")
    action.add_argument("--apply", type=Path, help="Удалить только всё ещё допустимые файлы из сохранённого списка")
    args = parser.parse_args(argv)
    roots = [str(Path(args.logs_dir).absolute())]
    if args.include_legacy:
        from rem_card.app.runtime_paths import get_legacy_runtime_logs_dir

        roots.append(get_legacy_runtime_logs_dir())
    if args.preview:
        result = plan_log_cleanup(roots)
        # Refuse overwrite: a prior reviewed plan is an independent artifact.
        with args.preview.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
    else:
        with args.apply.open(encoding="utf-8") as stream:
            plan = json.load(stream)
        if plan.get("schema_version") != 1:
            parser.error("Неподдерживаемая версия плана очистки")
        if plan.get("roots") != roots:
            parser.error("Каталоги должны совпадать с просмотренным планом очистки")
        result = apply_log_cleanup(plan, roots)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 1 if result.get("failed_files") else 0


if __name__ == "__main__":
    raise SystemExit(main())
