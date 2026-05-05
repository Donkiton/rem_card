from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = ("app", "services", "ui")
STYLE_ROOT = Path("ui/styles")

OLD_BLUE_RE = re.compile(r"#(?:007bff|0056b3|3498db|80bdff|2196f3)\b", re.IGNORECASE)
HEX_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")
RGB_RE = re.compile(r"\brgba?\s*\(", re.IGNORECASE)
INLINE_QSS_RE = re.compile(r"\.setStyleSheet\s*\(")

LEGACY_MIGRATION_PREFIXES = (
    Path("app/main.py"),
    Path("app/updater_main.py"),
    Path("services/analytics"),
    Path("services/order_domain_service.py"),
    Path("services/prescription_engine.py"),
    Path("ui/admin_view"),
    Path("ui/analytics"),
    Path("ui/doctor_view"),
    Path("ui/main_window.py"),
    Path("ui/nurse_view"),
    Path("ui/patient_bed_management"),
    Path("ui/rem_card_sectors"),
    Path("ui/shared"),
)

MEDICAL_AND_PRINT_PREFIXES = (
    Path("services/analytics"),
    Path("ui/analytics"),
    Path("ui/rem_card_sectors/s_print"),
    Path("ui/shared/chart_widget.py"),
)

GAME_CANVAS_PREFIXES = (
    Path("ui/shared/minigames/arcade_widgets.py"),
    Path("ui/shared/minigames/snake_widget.py"),
)


def _iter_python_files() -> list[Path]:
    files: list[Path] = []
    for root_name in SCAN_ROOTS:
        root = ROOT / root_name
        if not root.exists():
            continue
        files.extend(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)
    return sorted(files)


def _rel(path: Path) -> Path:
    return path.relative_to(ROOT)


def _is_under(path: Path, prefix: Path) -> bool:
    try:
        path.relative_to(prefix)
        return True
    except ValueError:
        return path == prefix


def _is_allowed_legacy(path: Path) -> bool:
    if _is_under(path, STYLE_ROOT):
        return True
    return any(_is_under(path, prefix) for prefix in LEGACY_MIGRATION_PREFIXES)


def _is_documented_exception(path: Path) -> bool:
    return any(_is_under(path, prefix) for prefix in MEDICAL_AND_PRINT_PREFIXES + GAME_CANVAS_PREFIXES)


def _line_findings(path: Path, regex: re.Pattern[str], kind: str) -> list[str]:
    findings: list[str] = []
    text = path.read_text(encoding="utf-8", errors="ignore")
    rel = _rel(path)
    for line_no, line in enumerate(text.splitlines(), start=1):
        if regex.search(line):
            findings.append(f"{rel}:{line_no}: {kind}: {line.strip()[:180]}")
    return findings


def run(strict: bool = False) -> int:
    errors: list[str] = []
    legacy: list[str] = []

    for path in _iter_python_files():
        rel = _rel(path)

        old_blue = _line_findings(path, OLD_BLUE_RE, "старый синий цвет запрещен")
        errors.extend(old_blue)

        for finding in _line_findings(path, HEX_RE, "hex вне ui/styles"):
            if _is_under(rel, STYLE_ROOT) or _is_documented_exception(rel):
                continue
            if strict or not _is_allowed_legacy(rel):
                errors.append(finding)
            else:
                legacy.append(finding)

        for finding in _line_findings(path, RGB_RE, "rgb/rgba вне ui/styles"):
            if _is_under(rel, STYLE_ROOT) or _is_documented_exception(rel):
                continue
            if strict or not _is_allowed_legacy(rel):
                errors.append(finding)
            else:
                legacy.append(finding)

        for finding in _line_findings(path, INLINE_QSS_RE, "setStyleSheet вне ui/styles"):
            if _is_under(rel, STYLE_ROOT):
                continue
            if strict or not _is_allowed_legacy(rel):
                errors.append(finding)
            else:
                legacy.append(finding)

    if errors:
        print("Style audit failed:")
        for item in errors[:200]:
            print(f"  {item}")
        if len(errors) > 200:
            print(f"  ... еще {len(errors) - 200}")
        if legacy:
            print(f"Legacy migration findings allowed in this mode: {len(legacy)}")
        return 1

    print("Style audit passed.")
    if legacy:
        print(f"Legacy migration findings allowed: {len(legacy)}")
        print("Use --strict after migrating legacy inline styles.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка расползания дизайн-кода вне темы.")
    parser.add_argument("--strict", action="store_true", help="Запрещать все legacy inline-стили вне ui/styles.")
    args = parser.parse_args(argv)
    return run(strict=args.strict)


if __name__ == "__main__":
    raise SystemExit(main())
