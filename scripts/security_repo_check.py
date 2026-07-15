from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAX_SCANNED_FILE_BYTES = 2 * 1024 * 1024

ALLOWED_DATABASES = {
    "data/mkb/mkb10.db",
}

FORBIDDEN_FILE_NAMES = {
    "crash.txt",
    "remcard_data_path.json",
    "remcard_settings.db",
    "rem_cards_data.db",
}

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    (
        "private-key",
        re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    ),
    (
        "github-token",
        re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    ),
    ("aws-access-key", re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("openai-key", re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("google-api-key", re.compile(rb"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("slack-token", re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    (
        "credential-uri",
        re.compile(
            rb"(?i)\b(?:postgres(?:ql)?|mysql|mssql|mongodb(?:\+srv)?|redis)://"
            rb"[^\s/:]+:[^\s/@]+@"
        ),
    ),
)

MARKDOWN_INTERNAL_PATH_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "internal-unc-fqdn",
        re.compile(
            r"\\\\(?!(?:server|test-fileserver)(?:\\|\.example\.test\\))"
            r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+\\",
            re.IGNORECASE,
        ),
    ),
    ("local-user-path", re.compile(r"\b[A-Z]:[\\/]Users[\\/]", re.IGNORECASE)),
    ("real-project-path", re.compile(r"\bC:[\\/]Project[\\/]", re.IGNORECASE)),
    (
        "department-drive-path",
        re.compile(r"\b[A-Z]:[\\/](?:РАО|Пациенты)(?:[\\/]|\b)", re.IGNORECASE),
    ),
)


def _git(*args: str, input_bytes: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=PROJECT_ROOT,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout


def _candidate_paths(*, staged: bool) -> list[str]:
    if staged:
        raw = _git("diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR")
    else:
        raw = _git("ls-files", "-z")
    return sorted(
        {
            item.decode("utf-8", errors="surrogateescape").replace("\\", "/")
            for item in raw.split(b"\0")
            if item
        },
        key=str.casefold,
    )


def _read_candidate(path: str, *, staged: bool) -> bytes | None:
    try:
        if staged:
            return _git("show", f":{path}")
        file_path = PROJECT_ROOT / Path(*PurePosixPath(path).parts)
        return file_path.read_bytes()
    except (OSError, subprocess.CalledProcessError):
        return None


def _forbidden_path_reason(path: str) -> str:
    normalized = path.replace("\\", "/").strip("/")
    lowered = normalized.casefold()
    parts = tuple(part.casefold() for part in PurePosixPath(normalized).parts)
    name = PurePosixPath(normalized).name.casefold()

    if lowered in ALLOWED_DATABASES:
        return ""
    if "baza_rao3_jurnal" in parts:
        return "runtime-database-directory"
    if name in FORBIDDEN_FILE_NAMES:
        return "runtime-or-local-file"
    if name.endswith(".jsonl"):
        return "runtime-audit-log"
    if name.endswith((".pem", ".pfx", ".p12", ".key")):
        return "private-key-file"
    if name.startswith("rao_journal") and name.endswith((".db", ".sqlite", ".sqlite3")):
        return "medical-database"
    if name.startswith(("remcard_outbox", "rao_journal_local_replica")) and name.endswith(
        (".db", ".sqlite", ".sqlite3")
    ):
        return "local-medical-database"
    if any(part in {"backups", "backup_health"} for part in parts) and name.endswith(
        (".db", ".sqlite", ".sqlite3")
    ):
        return "database-backup"
    return ""


def _scan_content(path: str, content: bytes) -> list[str]:
    findings: list[str] = []
    if len(content) > MAX_SCANNED_FILE_BYTES:
        return findings

    for name, pattern in SECRET_PATTERNS:
        if pattern.search(content):
            findings.append(name)

    if path.casefold().endswith(".md") and b"\0" not in content:
        text = content.decode("utf-8", errors="replace")
        for name, pattern in MARKDOWN_INTERNAL_PATH_PATTERNS:
            if pattern.search(text):
                findings.append(name)
    return findings


def run(*, staged: bool) -> int:
    findings: list[tuple[str, str]] = []
    for path in _candidate_paths(staged=staged):
        path_reason = _forbidden_path_reason(path)
        if path_reason:
            findings.append((path, path_reason))
            continue
        content = _read_candidate(path, staged=staged)
        if content is None:
            continue
        findings.extend((path, reason) for reason in _scan_content(path, content))

    if findings:
        print("Проверка безопасности репозитория обнаружила запрещённые данные:", file=sys.stderr)
        for path, reason in findings:
            print(f"  {path}: {reason}", file=sys.stderr)
        print("Удалите данные из индекса Git перед коммитом.", file=sys.stderr)
        return 1

    scope = "индекс Git" if staged else "отслеживаемые файлы"
    print(f"Проверка безопасности пройдена: {scope}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Блокирует случайную публикацию медицинских БД, runtime-файлов и секретов."
    )
    parser.add_argument("--staged", action="store_true", help="Проверить только подготовленные к коммиту файлы.")
    args = parser.parse_args(argv)
    return run(staged=bool(args.staged))


if __name__ == "__main__":
    raise SystemExit(main())
