import argparse
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from bump_version import (
    BUMP_LEVELS,
    bump_version,
    parse_version,
    read_version,
    update_changelog,
    version_path,
    write_release_info,
)


RELEASE_LEVELS = ("auto", *BUMP_LEVELS)
VERSIONED_FILES = ("VERSION", "CHANGELOG.md", "app/release_info.json")


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def run(
    args: list[str],
    *,
    cwd: Path,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd),
        check=check,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def git_output(root: Path, args: list[str]) -> str:
    result = run(["git", *args], cwd=root, capture=True)
    return str(result.stdout or "").strip()


def ensure_git_repo(root: Path) -> None:
    try:
        inside = git_output(root, ["rev-parse", "--is-inside-work-tree"])
    except Exception as exc:
        raise RuntimeError("Команда должна запускаться внутри git-репозитория.") from exc
    if inside.lower() != "true":
        raise RuntimeError("Команда должна запускаться внутри git-репозитория.")


def ensure_clean_tree(root: Path) -> None:
    status = git_output(root, ["status", "--porcelain"])
    if status:
        raise RuntimeError(
            "Рабочее дерево не чистое. Сначала закоммитьте изменения, затем запускайте релизную сборку.\n\n"
            + status
        )


def latest_version_commit(root: Path) -> str:
    commit = git_output(root, ["log", "-1", "--format=%H", "--", "VERSION"])
    if not commit:
        raise RuntimeError("Не удалось найти последний коммит, где менялся VERSION.")
    return commit


def collect_commit_subjects(root: Path, since_commit: str) -> list[str]:
    raw = git_output(root, ["log", "--reverse", "--format=%s", f"{since_commit}..HEAD"])
    subjects: list[str] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        subject = normalize_subject(line)
        if not subject:
            continue
        key = subject.casefold()
        if key in seen:
            continue
        seen.add(key)
        subjects.append(subject)
    return subjects


def normalize_subject(value: str) -> str:
    subject = str(value or "").strip()
    if not subject:
        return ""
    if subject.lower().startswith("merge "):
        return ""
    if re.match(r"^(release|version|bump version)\b", subject, flags=re.IGNORECASE):
        return ""
    return subject


def detect_level(subjects: list[str]) -> str:
    joined = "\n".join(subjects).casefold()
    if re.search(r"(^|\W)(breaking|major|несовмест|ломающ)", joined):
        return "major"

    minor_patterns = (
        r"^feat(\(.+?\))?!?:",
        r"^feature(\(.+?\))?!?:",
        r"^add\b",
        r"^implement\b",
        r"^introduce\b",
        r"^добав",
        r"^реализ",
        r"^нов",
    )
    for subject in subjects:
        text = subject.casefold()
        if any(re.search(pattern, text) for pattern in minor_patterns):
            return "minor"
    return "patch"


def build_changelog_changes(subjects: list[str], manual_changes: list[str]) -> list[str]:
    changes: list[str] = []
    seen: set[str] = set()
    for item in [*subjects, *manual_changes]:
        text = str(item or "").strip().lstrip("-").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        changes.append(text)
    return changes


def update_release_files(root: Path, level: str, changes: list[str], set_version: str | None = None) -> tuple[str, str]:
    current = read_version(root)
    if set_version:
        parse_version(set_version)
        next_version = set_version
    else:
        next_version = bump_version(current, level)

    date_text = datetime.now().strftime("%Y-%m-%d")
    version_path(root).write_text(next_version + "\n", encoding="utf-8")
    update_changelog(root, next_version, date_text, changes)
    write_release_info(root, next_version, date_text, changes)
    return current, next_version


def run_build(root: Path) -> None:
    run([sys.executable, "-m", "PyInstaller", "RemCard.spec"], cwd=root)


def commit_release(root: Path, version: str) -> None:
    run(["git", "add", *VERSIONED_FILES], cwd=root)
    run(["git", "commit", "-m", f"release {version}"], cwd=root)


def push_current_branch(root: Path) -> None:
    branch = git_output(root, ["branch", "--show-current"])
    if not branch:
        raise RuntimeError("Не удалось определить текущую ветку для push.")
    run(["git", "push", "origin", branch], cwd=root)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Автоматически поднимает версию, собирает CHANGELOG.md из git-коммитов "
            "после прошлого релиза и запускает PyInstaller."
        )
    )
    parser.add_argument(
        "level",
        nargs="?",
        choices=RELEASE_LEVELS,
        default="auto",
        help="auto, patch, minor или major. По умолчанию auto.",
    )
    parser.add_argument("--set", dest="set_version", help="Задать точную версию MAJOR.MINOR.PATCH")
    parser.add_argument(
        "--change",
        action="append",
        default=[],
        help="Добавить пункт в changelog вручную. Можно указать несколько раз.",
    )
    parser.add_argument("--skip-build", action="store_true", help="Только обновить версию и changelog, без PyInstaller.")
    parser.add_argument("--no-commit", action="store_true", help="Не создавать release-коммит после сборки.")
    parser.add_argument("--push", action="store_true", help="После release-коммита отправить текущую ветку в origin.")
    parser.add_argument("--allow-empty", action="store_true", help="Разрешить релиз без новых git-коммитов.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.push and args.no_commit:
        raise SystemExit("--push нельзя использовать вместе с --no-commit")

    root = project_root()
    ensure_git_repo(root)
    ensure_clean_tree(root)

    previous_release_commit = latest_version_commit(root)
    subjects = collect_commit_subjects(root, previous_release_commit)
    changes = build_changelog_changes(subjects, args.change)
    if not changes and not args.allow_empty:
        raise RuntimeError(
            "Нет новых git-коммитов после прошлого релиза. "
            "Сделайте коммит с изменениями или добавьте --change \"Описание\"."
        )
    if not changes:
        changes = ["Техническая пересборка без изменений в коде"]

    level = detect_level(changes) if args.level == "auto" else args.level
    current, next_version = update_release_files(root, level, changes, set_version=args.set_version)
    print(f"Version updated: {current} -> {next_version} ({level})")
    print("Changelog:")
    for change in changes:
        print(f"  - {change}")

    if not args.skip_build:
        run_build(root)

    if args.no_commit:
        print("Release files are not committed because --no-commit was used.")
    else:
        commit_release(root, next_version)
        if args.push:
            push_current_branch(root)

    print("Release build completed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        raise SystemExit(1)
