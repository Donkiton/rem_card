from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
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

KIND_LABELS = {
    "hex": "hex вне ui/styles",
    "rgb": "rgb/rgba вне ui/styles",
    "qss": "setStyleSheet вне ui/styles",
    "old_blue": "старый синий цвет вне темы запрещен",
}

# Baseline текущей legacy-миграции. Обычный режим использует явные категории ниже,
# --no-new-violations запрещает увеличение счетчиков по этим файлам.
LEGACY_BASELINE_COUNTS: dict[Path, dict[str, int]] = {
    Path("app/main.py"): {"hex": 9, "qss": 1},
    Path("app/updater_main.py"): {"hex": 9, "qss": 2},
    Path("services/analytics/recovery_summary.py"): {"hex": 9},
    Path("services/order_domain_service.py"): {"hex": 2},
    Path("services/operblock_reportlab_builder.py"): {"hex": 17},
    Path("services/prescription_engine.py"): {"hex": 1},
    Path("ui/admin_view/admin_types_dict_widget.py"): {"hex": 1, "qss": 3},
    Path("ui/admin_view/background_settings_dialog.py"): {"hex": 31, "qss": 2},
    Path("ui/admin_view/db_rotation_dialog.py"): {"qss": 1},
    Path("ui/admin_view/diet_templates_widget.py"): {"hex": 1, "qss": 3},
    Path("ui/admin_view/diluents_dict_widget.py"): {"hex": 1, "qss": 3},
    Path("ui/admin_view/display_settings_dialog.py"): {"hex": 15, "qss": 2},
    Path("ui/admin_view/drugs_dict_widget.py"): {"hex": 1, "qss": 3},
    Path("ui/admin_view/emergency_password_dialog.py"): {"hex": 2, "qss": 2},
    Path("ui/admin_view/forms_dict_widget.py"): {"hex": 1, "qss": 3},
    Path("ui/admin_view/groups_dict_widget.py"): {"hex": 2, "qss": 3},
    Path("ui/admin_view/lab_analysis_catalog_widget.py"): {"hex": 17, "qss": 5},
    Path("ui/admin_view/operblock_icon_settings_dialog.py"): {"hex": 22, "qss": 1},
    Path("ui/admin_view/operblock_quick_buttons_settings_widget.py"): {"hex": 1, "qss": 2},
    Path("ui/admin_view/operblock_route_settings_widget.py"): {"hex": 2, "qss": 3},
    Path("ui/admin_view/print_settings_widget.py"): {"hex": 6, "qss": 4},
    Path("ui/admin_view/remcard_icon_settings_dialog.py"): {"hex": 12, "qss": 1},
    Path("ui/admin_view/templates_dict_widget.py"): {"hex": 6, "qss": 8, "rgb": 1},
    Path("ui/analytics/graphs_dialog.py"): {"qss": 14},
    Path("ui/analytics/operblock_statistics_dialog.py"): {"qss": 14},
    Path("ui/analytics/report_dialog.py"): {"qss": 10},
    Path("ui/analytics/statistics_dialog.py"): {"qss": 15},
    Path("ui/doctor_view/administration_dialog.py"): {"hex": 6, "qss": 4},
    Path("ui/doctor_view/archive_widget.py"): {"hex": 2, "qss": 17},
    Path("ui/doctor_view/bars_auth_dialog.py"): {"hex": 11, "qss": 9},
    Path("ui/doctor_view/card_list_widget.py"): {"hex": 7, "qss": 2},
    Path("ui/doctor_view/components/beds_selection_widget.py"): {"qss": 3},
    Path("ui/doctor_view/components/control_panel.py"): {"hex": 8, "qss": 7},
    Path("ui/doctor_view/components/sector8_panel.py"): {"qss": 9},
    Path("ui/doctor_view/orders_widget.py"): {"hex": 3, "qss": 7, "rgb": 1},
    Path("ui/doctor_view/prescription_input_widget.py"): {"hex": 2, "qss": 2},
    Path("ui/doctor_view/template_dialog.py"): {"hex": 2, "qss": 1},
    Path("ui/main_window.py"): {"hex": 7, "qss": 3},
    Path("ui/nurse_view/components/nurse_beds_selection_widget.py"): {"qss": 3},
    Path("ui/nurse_view/components/nurse_orders_widget.py"): {"qss": 3},
    Path("ui/nurse_view/components/nurse_prescription_input_widget.py"): {"hex": 2, "qss": 1},
    Path("ui/nurse_view/components/nurse_sector8_panel.py"): {"qss": 8},
    Path("ui/nurse_view/input_widgets.py"): {"hex": 1, "qss": 2},
    Path("ui/nurse_view/nurse_remcard_layout.py"): {"hex": 2, "qss": 1},
    Path("ui/nurse_view/nurse_remcard_widget.py"): {"hex": 7, "qss": 3},
    Path("ui/nurse_view/nurse_statistics_dialog.py"): {"hex": 8, "qss": 11},
    Path("ui/nurse_view/sectors/nurse_sector_2a.py"): {"qss": 4},
    Path("ui/nurse_view/sectors/nurse_sector_2b.py"): {"qss": 2},
    Path("ui/nurse_view/sectors/nurse_sector_4v.py"): {"hex": 18, "qss": 12},
    Path("ui/nurse_view/sectors/nurse_sector_7na_b.py"): {"hex": 11, "qss": 2},
    Path("ui/nurse_view/sectors/nurse_sector_dev.py"): {"hex": 10, "qss": 6},
    Path("ui/nurse_view/sectors/nurse_sector_print.py"): {"hex": 4, "qss": 6},
    Path("ui/patient_bed_management/bed_widget.py"): {"qss": 9},
    Path("ui/patient_bed_management/management_widget.py"): {"qss": 4},
    Path("ui/patient_bed_management/patient_form.py"): {"qss": 12},
    Path("ui/operblock_view/operblock_chart_widget.py"): {"hex": 37, "qss": 2},
    Path("ui/operblock_view/operblock_control_styles.py"): {"hex": 21},
    Path("ui/operblock_view/operblock_main_widget.py"): {"hex": 319, "qss": 261},
    Path("ui/patient_bed_management/side_patient_card.py"): {"hex": 1, "qss": 7},
    Path("ui/patient_bed_management/tabs/diagnosis_tab.py"): {"qss": 17},
    Path("ui/patient_bed_management/tabs/general_tab.py"): {"qss": 1},
    Path("ui/procedures/cvc_access_map_widget.py"): {"hex": 3, "qss": 2},
    Path("ui/procedures/procedure_editor_dialog.py"): {"hex": 3, "qss": 2},
    Path("ui/procedures/procedure_styles.py"): {"hex": 111, "qss": 4},
    Path("ui/procedures/procedures_list_widget.py"): {"hex": 14, "qss": 2},
    Path("ui/procedures/procedures_panel.py"): {"hex": 16, "qss": 1},
    Path("ui/rem_card_sectors/balance/balance_grid.py"): {"qss": 2},
    Path("ui/rem_card_sectors/balance/sector_2b_g.py"): {"hex": 23, "qss": 9},
    Path("ui/rem_card_sectors/balance/sector_2b_v.py"): {"hex": 25, "qss": 16},
    Path("ui/rem_card_sectors/balance/sector_2d.py"): {"hex": 26, "qss": 4},
    Path("ui/rem_card_sectors/lab_analysis_dialog.py"): {"hex": 45, "qss": 3},
    Path("ui/rem_card_sectors/outcome_dialogs.py"): {"hex": 14, "qss": 6},
    Path("ui/rem_card_sectors/s_print/builder.py"): {"hex": 8},
    Path("ui/rem_card_sectors/s_print/death_outcome.py"): {"hex": 2},
    Path("ui/rem_card_sectors/s_print/events.py"): {"hex": 1},
    Path("ui/rem_card_sectors/s_print/prescriptions.py"): {"hex": 2},
    Path("ui/rem_card_sectors/s_print/reportlab_builder.py"): {"hex": 6},
    Path("ui/rem_card_sectors/s_print/table_layout.py"): {"hex": 3},
    Path("ui/rem_card_sectors/sector_1a.py"): {"hex": 5, "qss": 3},
    Path("ui/rem_card_sectors/sector_1b.py"): {"hex": 15, "qss": 2},
    Path("ui/rem_card_sectors/sector_2a.py"): {"qss": 4},
    Path("ui/rem_card_sectors/sector_2b.py"): {"qss": 3},
    Path("ui/rem_card_sectors/sector_2g.py"): {"hex": 1, "qss": 2},
    Path("ui/rem_card_sectors/sector_2v.py"): {"hex": 1, "qss": 1},
    Path("ui/rem_card_sectors/sector_3a.py"): {"hex": 16, "qss": 8},
    Path("ui/rem_card_sectors/sector_3b.py"): {"hex": 17, "qss": 9},
    Path("ui/rem_card_sectors/sector_4.py"): {"hex": 14, "qss": 7},
    Path("ui/rem_card_sectors/sector_4_sub.py"): {"hex": 38, "qss": 24},
    Path("ui/rem_card_sectors/sector_4a.py"): {"hex": 4, "qss": 6},
    Path("ui/rem_card_sectors/sector_5.py"): {"hex": 11, "qss": 3},
    Path("ui/rem_card_sectors/sector_6.py"): {"hex": 11, "qss": 2},
    Path("ui/rem_card_sectors/sector_7bal_a.py"): {"hex": 11, "qss": 2},
    Path("ui/rem_card_sectors/sector_7bal_b.py"): {"hex": 11, "qss": 2},
    Path("ui/rem_card_sectors/sector_7na_a.py"): {"hex": 1, "qss": 3},
    Path("ui/rem_card_sectors/sector_7na_b.py"): {"hex": 11, "qss": 2},
    Path("ui/rem_card_sectors/sector_7vit_a.py"): {"hex": 11, "qss": 2},
    Path("ui/rem_card_sectors/sector_7vit_b.py"): {"hex": 24, "qss": 2},
    Path("ui/rem_card_sectors/sector_8.py"): {"hex": 2, "qss": 2},
    Path("ui/rem_card_sectors/sector_anal.py"): {"hex": 72, "qss": 3, "rgb": 1},
    Path("ui/rem_card_sectors/sector_events.py"): {"hex": 46, "qss": 21, "rgb": 1},
    Path("ui/rem_card_sectors/sector_ivl.py"): {"hex": 66, "qss": 11},
    Path("ui/rem_card_sectors/sector_print.py"): {"hex": 7, "qss": 6},
    Path("ui/rem_card_sectors/sector_w1a.py"): {"hex": 18, "qss": 7},
    Path("ui/rem_card_sectors/sector_w1b.py"): {"hex": 3, "qss": 3},
    Path("ui/rem_card_sectors/sector_w1b_nurse.py"): {"hex": 3, "qss": 3},
    Path("ui/rem_card_sectors/sector_w1c.py"): {"hex": 2, "qss": 2},
    Path("ui/shared/chart_widget.py"): {"hex": 5, "qss": 1},
    Path("ui/shared/components/diet_intake_widget.py"): {"hex": 37, "qss": 3},
    Path("ui/shared/components/nurse_order_card.py"): {"hex": 19, "qss": 7},
    Path("ui/shared/components/vital_settings_dialog.py"): {"hex": 3, "qss": 2},
    Path("ui/shared/hybrid_shift_time_picker.py"): {"hex": 18, "qss": 1},
    Path("ui/shared/emergency_dialogs.py"): {"hex": 1, "qss": 1},
    Path("ui/shared/minigames/arcade_widgets.py"): {"hex": 47, "qss": 2},
    Path("ui/shared/minigames/snake_widget.py"): {"hex": 8, "qss": 3},
    Path("ui/shared/navigation_widgets.py"): {"qss": 5},
    Path("ui/shared/vitals_widget.py"): {"hex": 6, "qss": 3},
}

PRINT_HTML_ALLOWED = frozenset(
    {
        Path("ui/rem_card_sectors/s_print/builder.py"),
        Path("ui/rem_card_sectors/s_print/death_outcome.py"),
        Path("ui/rem_card_sectors/s_print/events.py"),
        Path("ui/rem_card_sectors/s_print/prescriptions.py"),
        Path("ui/rem_card_sectors/s_print/reportlab_builder.py"),
        Path("ui/rem_card_sectors/s_print/table_layout.py"),
        Path("ui/rem_card_sectors/sector_print.py"),
        Path("ui/nurse_view/sectors/nurse_sector_print.py"),
    }
)

MEDICAL_CHART_COLORS_ALLOWED = frozenset(
    {
        Path("ui/shared/chart_widget.py"),
        Path("ui/shared/vitals_widget.py"),
        Path("ui/shared/components/vital_settings_dialog.py"),
        Path("ui/rem_card_sectors/sector_2g.py"),
        Path("ui/rem_card_sectors/sector_4_sub.py"),
        Path("ui/nurse_view/sectors/nurse_sector_4v.py"),
    }
)

GAME_CANVAS_COLORS_ALLOWED = frozenset(
    {
        Path("ui/shared/minigames/arcade_widgets.py"),
        Path("ui/shared/minigames/snake_widget.py"),
    }
)

TEST_FIXTURES_ALLOWED = frozenset()

LEGACY_UI_QSS_ALLOWED = frozenset(
    path
    for path, counts in LEGACY_BASELINE_COUNTS.items()
    if "qss" in counts
    and path not in GAME_CANVAS_COLORS_ALLOWED
    and path not in PRINT_HTML_ALLOWED
)

MIGRATION_TEMP_ALLOWED = frozenset(
    path
    for path in LEGACY_BASELINE_COUNTS
    if path not in LEGACY_UI_QSS_ALLOWED
    and path not in MEDICAL_CHART_COLORS_ALLOWED
    and path not in PRINT_HTML_ALLOWED
    and path not in TEST_FIXTURES_ALLOWED
)


@dataclass(frozen=True)
class Finding:
    rel: Path
    line_no: int
    kind: str
    line: str


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


def _matches(path: Path, allowed: frozenset[Path]) -> bool:
    return path in allowed


def _allowed_category(path: Path, kind: str) -> str | None:
    if _is_under(path, STYLE_ROOT):
        return "theme"
    if kind in ("hex", "rgb") and _matches(path, MEDICAL_CHART_COLORS_ALLOWED):
        return "medical-chart-colors"
    if kind in ("hex", "rgb") and _matches(path, GAME_CANVAS_COLORS_ALLOWED):
        return "game-canvas-colors"
    if kind == "qss" and _matches(path, GAME_CANVAS_COLORS_ALLOWED):
        return "game-canvas-qss"
    if _matches(path, PRINT_HTML_ALLOWED):
        return "print-html"
    if _matches(path, TEST_FIXTURES_ALLOWED):
        return "test-fixtures"
    if _matches(path, LEGACY_UI_QSS_ALLOWED):
        return "legacy-ui-qss"
    if _matches(path, MIGRATION_TEMP_ALLOWED):
        return "migration-temp"
    return None


def _line_findings(path: Path, regex: re.Pattern[str], kind: str) -> list[Finding]:
    findings: list[Finding] = []
    text = path.read_text(encoding="utf-8", errors="ignore")
    rel = _rel(path)
    for line_no, line in enumerate(text.splitlines(), start=1):
        if regex.search(line):
            findings.append(Finding(rel=rel, line_no=line_no, kind=kind, line=line.strip()[:180]))
    return findings


def _collect_findings() -> tuple[list[Finding], list[Finding]]:
    old_blue: list[Finding] = []
    style_findings: list[Finding] = []

    for path in _iter_python_files():
        rel = _rel(path)
        if _is_under(rel, STYLE_ROOT):
            continue
        old_blue.extend(_line_findings(path, OLD_BLUE_RE, "old_blue"))
        style_findings.extend(_line_findings(path, HEX_RE, "hex"))
        style_findings.extend(_line_findings(path, RGB_RE, "rgb"))
        style_findings.extend(_line_findings(path, INLINE_QSS_RE, "qss"))

    return old_blue, style_findings


def _format_finding(finding: Finding) -> str:
    return f"{finding.rel}:{finding.line_no}: {KIND_LABELS[finding.kind]}: {finding.line}"


def _print_report(title: str, errors: list[str], legacy_count: int = 0) -> int:
    if errors:
        print(f"{title}:")
        for item in errors[:200]:
            print(f"  {item}")
        if len(errors) > 200:
            print(f"  ... еще {len(errors) - 200}")
        if legacy_count:
            print(f"Legacy migration findings allowed in this mode: {legacy_count}")
        return 1

    print("Style audit passed.")
    if legacy_count:
        print(f"Legacy migration findings allowed: {legacy_count}")
        print("Use --strict after migrating legacy inline styles.")
    return 0


def _baseline_counter() -> Counter[tuple[Path, str]]:
    counter: Counter[tuple[Path, str]] = Counter()
    for path, counts in LEGACY_BASELINE_COUNTS.items():
        for kind, count in counts.items():
            counter[(path, kind)] = count
    return counter


def _current_counter(findings: list[Finding]) -> Counter[tuple[Path, str]]:
    return Counter((finding.rel, finding.kind) for finding in findings)


def run(strict: bool = False, no_new_violations: bool = False) -> int:
    old_blue, style_findings = _collect_findings()

    if strict:
        errors = [_format_finding(finding) for finding in old_blue + style_findings]
        return _print_report("Style audit failed", errors)

    if no_new_violations:
        errors = [_format_finding(finding) for finding in old_blue]
        baseline = _baseline_counter()
        current = _current_counter(style_findings)
        for key, current_count in sorted(current.items(), key=lambda item: (str(item[0][0]), item[0][1])):
            baseline_count = baseline.get(key, 0)
            if current_count > baseline_count:
                path, kind = key
                errors.append(
                    f"{path}: {KIND_LABELS[kind]}: текущих {current_count}, baseline {baseline_count}, "
                    f"новых {current_count - baseline_count}"
                )
        return _print_report("Style audit no-new-violations failed", errors)

    errors = [_format_finding(finding) for finding in old_blue]
    legacy_count = 0
    for finding in style_findings:
        category = _allowed_category(finding.rel, finding.kind)
        if category:
            legacy_count += 1
        else:
            errors.append(_format_finding(finding))

    return _print_report("Style audit failed", errors, legacy_count=legacy_count)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка расползания дизайн-кода вне темы.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--strict", action="store_true", help="Показать все inline-стили вне ui/styles.")
    mode.add_argument(
        "--no-new-violations",
        action="store_true",
        help="Запретить новые inline-стили вне ui/styles сверх текущего baseline.",
    )
    args = parser.parse_args(argv)
    return run(strict=args.strict, no_new_violations=args.no_new_violations)


if __name__ == "__main__":
    raise SystemExit(main())
