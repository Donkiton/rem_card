import argparse
import json
import re
from datetime import datetime
from pathlib import Path


VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
BUMP_LEVELS = ("patch", "minor", "major")


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def version_path(root: Path) -> Path:
    return root / "VERSION"


def changelog_path(root: Path) -> Path:
    return root / "CHANGELOG.md"


def release_info_path(root: Path) -> Path:
    return root / "app" / "release_info.json"


def parse_version(value: str) -> tuple[int, int, int]:
    text = str(value or "").strip()
    if not VERSION_RE.match(text):
        raise ValueError(f"Version must use MAJOR.MINOR.PATCH, got: {value!r}")
    major, minor, patch = text.split(".")
    return int(major), int(minor), int(patch)


def read_version(root: Path) -> str:
    path = version_path(root)
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        text = "1.0.0"
    parse_version(text)
    return text


def bump_version(current: str, level: str) -> str:
    major, minor, patch = parse_version(current)
    if level == "patch":
        patch += 1
    elif level == "minor":
        minor += 1
        patch = 0
    elif level == "major":
        major += 1
        minor = 0
        patch = 0
    else:
        raise ValueError(f"Unknown bump level: {level}")
    return f"{major}.{minor}.{patch}"


def split_changes(raw: str) -> list[str]:
    normalized = str(raw or "").replace("\r\n", "\n").replace("\r", "\n")
    parts: list[str] = []
    for line in normalized.split("\n"):
        for item in line.split(";"):
            text = item.strip().lstrip("-").strip()
            if text:
                parts.append(text)
    return parts


def default_changelog() -> str:
    return (
        "# Журнал изменений РЕМКАРТА\n\n"
        "Формат версии: `MAJOR.MINOR.PATCH`.\n\n"
        "- `PATCH`: небольшие исправления и точечные изменения.\n"
        "- `MINOR`: крупное обновление или заметная новая функция.\n"
        "- `MAJOR`: очень большое обновление или несовместимое изменение.\n"
    )


def build_changelog_entry(version: str, date_text: str, changes: list[str]) -> str:
    bullets = "\n".join(f"- {change}" for change in changes)
    return f"## {version} - {date_text}\n\n{bullets}\n"


def update_changelog(root: Path, version: str, date_text: str, changes: list[str]) -> None:
    path = changelog_path(root)
    text = path.read_text(encoding="utf-8") if path.exists() else default_changelog()
    if re.search(rf"^##\s+{re.escape(version)}\s+-", text, flags=re.MULTILINE):
        raise RuntimeError(f"CHANGELOG.md already contains version {version}")

    lines = text.rstrip().splitlines()
    insert_at = len(lines)
    for index, line in enumerate(lines):
        if line.startswith("## "):
            insert_at = index
            break

    header = "\n".join(lines[:insert_at]).rstrip()
    tail = "\n".join(lines[insert_at:]).strip()
    entry = build_changelog_entry(version, date_text, changes).rstrip()

    if tail:
        new_text = f"{header}\n\n{entry}\n\n{tail}\n"
    else:
        new_text = f"{header}\n\n{entry}\n"
    path.write_text(new_text, encoding="utf-8")


def find_changelog_entry(root: Path, version: str) -> tuple[str, list[str]]:
    path = changelog_path(root)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    heading_re = re.compile(rf"^##\s+{re.escape(version)}\s+-\s+(.+?)\s*$")

    for index, line in enumerate(lines):
        match = heading_re.match(line)
        if not match:
            continue

        date_text = match.group(1).strip()
        changes: list[str] = []
        for item in lines[index + 1 :]:
            if item.startswith("## "):
                break
            stripped = item.strip()
            if stripped.startswith("- "):
                changes.append(stripped[2:].strip())
        if not changes:
            raise RuntimeError(f"CHANGELOG.md entry for {version} has no change bullets")
        return date_text, changes

    raise RuntimeError(f"CHANGELOG.md does not contain entry for current version {version}")


def write_release_info(root: Path, version: str, date_text: str, changes: list[str]) -> None:
    path = release_info_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "version": version,
        "date": date_text,
        "changes": changes,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sync_release_info(root: Path) -> None:
    version = read_version(root)
    date_text, changes = find_changelog_entry(root, version)
    write_release_info(root, version, date_text, changes)
    print(f"release_info.json synced for {version}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Обновляет VERSION, CHANGELOG.md и app/release_info.json."
    )
    parser.add_argument(
        "items",
        nargs="*",
        help="patch/minor/major и описание. При --set указывается только описание.",
    )
    parser.add_argument("--set", dest="set_version", help="Задать точную версию MAJOR.MINOR.PATCH")
    parser.add_argument("--sync-only", action="store_true", help="Только пересобрать app/release_info.json")
    args = parser.parse_args()

    root = project_root()

    if args.sync_only:
        sync_release_info(root)
        return 0

    current = read_version(root)
    if args.set_version:
        parse_version(args.set_version)
        next_version = args.set_version
        change_items = args.items
    elif args.items:
        level = args.items[0]
        if level not in BUMP_LEVELS:
            parser.error("Первый аргумент должен быть patch, minor или major")
        next_version = bump_version(current, level)
        change_items = args.items[1:]
    else:
        parser.error("Укажите patch/minor/major или --set 1.2.3")

    changes = split_changes(" ".join(change_items))
    if not changes:
        parser.error("Укажите, что изменено. Например: patch \"Исправлена печать карты\"")

    date_text = datetime.now().strftime("%Y-%m-%d")
    version_path(root).write_text(next_version + "\n", encoding="utf-8")
    update_changelog(root, next_version, date_text, changes)
    write_release_info(root, next_version, date_text, changes)

    print(f"Version updated: {current} -> {next_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
