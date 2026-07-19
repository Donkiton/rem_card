from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from _local_rem_card_bootstrap import bootstrap_local_rem_card  # noqa: E402


bootstrap_local_rem_card()

from rem_card.services.crash_reports import (  # noqa: E402
    ensure_shared_crash_directories,
    validate_crash_payload,
)


DEFAULT_RETENTION_DAYS = 180
MAX_REPORT_BYTES = 256 * 1024


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            if not text.endswith("\n"):
                fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)
        except Exception:
            pass


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed.replace(microsecond=0)
    except Exception:
        return None


def _read_report(path: Path) -> tuple[dict[str, Any] | None, str]:
    try:
        if path.stat().st_size > MAX_REPORT_BYTES:
            return None, "report exceeds size limit"
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"invalid JSON: {exc}"
    ok, reason = validate_crash_payload(payload)
    return (payload, "ok") if ok else (None, reason)


def _move_to_quarantine(claimed: Path, quarantine: Path, reason: str) -> None:
    quarantine.mkdir(parents=True, exist_ok=True)
    target = quarantine / f"{claimed.stem.split('.processing.', 1)[0]}.invalid.json"
    os.replace(claimed, target)
    _atomic_write_text(target.with_suffix(".reason.txt"), reason)


def _move_to_processed(claimed: Path, processed: Path, payload: dict[str, Any]) -> Path:
    occurred = _parse_time(payload.get("occurred_at")) or datetime.now()
    target_dir = processed / occurred.strftime("%Y") / occurred.strftime("%m")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{payload['id']}.json"
    os.replace(claimed, target)
    return target


def _format_stack(payload: dict[str, Any]) -> list[str]:
    frames = payload.get("frames") if isinstance(payload.get("frames"), list) else []
    if frames:
        result = []
        for frame in frames[-8:]:
            if not isinstance(frame, dict):
                continue
            result.append(
                f"  - `{frame.get('file') or 'unknown'}:{int(frame.get('line') or 0)}` — "
                f"`{frame.get('function') or 'unknown'}`"
            )
        return result
    details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
    native = details.get("native_trace") if isinstance(details.get("native_trace"), list) else []
    return [f"  - `{str(line)[:240]}`" for line in native[-8:]]


def _build_summary(payloads: list[dict[str, Any]], generated_at: datetime) -> str:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for payload in payloads:
        grouped[str(payload.get("fingerprint") or "unknown")].append(payload)

    lines = [
        "# Сводка аварий RemCard",
        "",
        f"Сформирована: {generated_at.replace(microsecond=0).isoformat(sep=' ')}",
        f"Обработано событий: {len(payloads)}",
        f"Групп ошибок: {len(grouped)}",
        "",
    ]
    ordered = sorted(
        grouped.items(),
        key=lambda item: max(str(event.get("occurred_at") or "") for event in item[1]),
        reverse=True,
    )
    for fingerprint, events in ordered:
        times = sorted(str(event.get("occurred_at") or "") for event in events)
        roles = sorted({str(event.get("role") or "unknown") for event in events})
        versions = sorted({str(event.get("app_version") or "unknown") for event in events})
        event_types = sorted({str(event.get("event_type") or "unknown") for event in events})
        example = events[-1]
        lines.extend(
            [
                f"## {fingerprint}",
                "",
                f"- Событие: {', '.join(event_types)}",
                f"- Повторений: {len(events)}",
                f"- Первое: {times[0]}",
                f"- Последнее: {times[-1]}",
                f"- Роли: {', '.join(roles)}",
                f"- Версии: {', '.join(versions)}",
                f"- Тип исключения: {example.get('exception_type') or 'не указан'}",
                "- Технический стек:",
            ]
        )
        stack_lines = _format_stack(example)
        lines.extend(stack_lines or ["  - отсутствует"])
        lines.append("")
    return "\n".join(lines)


def _cleanup_old_files(root: Path, *, cutoff: datetime) -> int:
    if not root.is_dir():
        return 0
    removed = 0
    cutoff_ts = cutoff.timestamp()
    for path in sorted(root.rglob("*"), reverse=True):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff_ts:
                path.unlink()
                removed += 1
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        except Exception:
            continue
    return removed


def process_crash_reports(
    data_root: str | os.PathLike[str],
    *,
    now: datetime | None = None,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> dict[str, Any]:
    generated_at = (now or datetime.now()).replace(microsecond=0)
    directories = ensure_shared_crash_directories(data_root)
    incoming = directories["incoming"]
    processed = directories["processed"]
    summaries = directories["summaries"]
    quarantine = directories["quarantine"]
    payloads: list[dict[str, Any]] = []
    invalid = 0

    for source in sorted(incoming.glob("*.json")):
        claimed = source.with_name(f"{source.name}.processing.{os.getpid()}")
        try:
            os.replace(source, claimed)
        except (FileNotFoundError, PermissionError):
            continue
        payload, reason = _read_report(claimed)
        if payload is None:
            _move_to_quarantine(claimed, quarantine, reason)
            invalid += 1
            continue
        _move_to_processed(claimed, processed, payload)
        payloads.append(payload)

    summary_path: Path | None = None
    if payloads:
        summary_path = summaries / f"crash-summary_{generated_at.strftime('%Y%m%d_%H%M%S')}.md"
        _atomic_write_text(summary_path, _build_summary(payloads, generated_at))

    cutoff = generated_at - timedelta(days=max(1, int(retention_days)))
    removed = _cleanup_old_files(processed, cutoff=cutoff)
    removed += _cleanup_old_files(summaries, cutoff=cutoff)
    removed += _cleanup_old_files(quarantine, cutoff=cutoff)
    return {
        "processed": len(payloads),
        "invalid": invalid,
        "removed_older_than_days": removed,
        "retention_days": max(1, int(retention_days)),
        "summary_path": str(summary_path or ""),
        "crash_root": str(directories["root"]),
    }


def _data_root_from_config(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = payload.get("baza_dir") or payload.get("path")
    if not raw:
        raise ValueError(f"В {path} не указан путь к папке данных.")
    return str(raw)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Обработать структурированные crash-отчёты RemCard.")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--data-root", help="Точный путь к выбранной папке данных RemCard.")
    target.add_argument("--config", help="Путь к remcard_data_path.json установленной программы.")
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.data_root:
        data_root = args.data_root
    elif args.config:
        data_root = _data_root_from_config(Path(args.config))
    else:
        from rem_card.app.runtime_paths import resolve_baza_dir

        data_root = resolve_baza_dir()
    result = process_crash_reports(data_root, retention_days=args.retention_days)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
