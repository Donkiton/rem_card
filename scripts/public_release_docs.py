"""Render the explicitly marked public documentation from verified release metadata.

The caller verifies publication and assets with GitHub. This module performs no
network calls, translates no release notes and infers no database policy.
"""

from __future__ import annotations

import argparse
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path


LANGUAGES = ("ru", "en", "de", "ja", "zh-CN")
RELEASE_START = "<!-- remcard:release:start -->"
RELEASE_END = "<!-- remcard:release:end -->"
VERSION_START = "<!-- remcard:version:start -->"
VERSION_END = "<!-- remcard:version:end -->"
VERSION_PATTERN = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
BADGE_RE = re.compile(r'(<a href="VERSION">)v' + VERSION_PATTERN + r"(</a>)")
METADATA_KEYS = {
    "schema_version", "version", "tag_name", "published_at", "release_url",
    "installer_url", "archive_url", "checksums_url", "source_commit", "changes",
}
DOCUMENTS = tuple(
    (Path("README.md" if locale == "ru" else f"README.{locale}.md"), locale, "readme")
    for locale in LANGUAGES
) + tuple(
    (Path(f"docs/guide/{kind}.{locale}.md"), locale, kind)
    for kind in ("features", "setup")
    for locale in LANGUAGES
)

LABELS = {
    "ru": (
        "Установщик Windows {version}", "Полный ZIP {version}", "SHA-256 файлов",
        "Полные изменения и требования обновления: [релиз {version}]({url}).",
    ),
    "en": (
        "Windows installer {version}", "Full ZIP {version}", "File SHA-256 checksums",
        "Read the full changes and upgrade requirements on the [release page {version}]({url}).",
    ),
    "de": (
        "Windows-Installer {version}", "Vollständiges ZIP-Paket {version}", "SHA-256-Prüfsummen",
        "Vollständige Änderungen und Update-Anforderungen: [Version {version}]({url}).",
    ),
    "ja": (
        "Windowsインストーラー {version}", "完全版ZIP {version}", "SHA-256チェックサム",
        "変更点と更新要件の全文は[リリース {version}]({url})を確認してください。",
    ),
    "zh-CN": (
        "Windows 安装程序 {version}", "完整 ZIP 安装包 {version}", "文件 SHA-256 校验值",
        "完整变更和更新要求见[版本 {version} 发布页]({url})。",
    ),
}

# General role, author and language information already present in the marked
# setup introductions is retained verbatim, independently of release notes.
SETUP_INTROS = {
    "ru": (
        "Обзор **RemCard {version}**, обновлён **{date}**. Единый `RemCard.exe` открывает выбор четырёх ролей; отдельный `RemCardUpdater.exe` устанавливает полные обновления. Номер версии исходников указан в [`VERSION`](../../VERSION), состав конкретной сборки — в её манифесте."
    ),
    "en": (
        "Overview of **RemCard {version}**, updated **{date}**. A single `RemCard.exe` opens a choice of four roles; the separate `RemCardUpdater.exe` installs full updates. The source version is recorded in [`VERSION`](../../VERSION), and each build's contents are recorded in its manifest."
    ),
    "de": (
        "Übersicht zu **RemCard {version}**, aktualisiert am **{date}**. RemCard wurde vom Anästhesisten und Intensivmediziner Vadim Bityutsky entwickelt. Die gemeinsame Anwendung `RemCard.exe` bietet die Auswahl zwischen vier Rollen; das separate Programm `RemCardUpdater.exe` installiert vollständige Updates. Die Quellcodeversion steht in [`VERSION`](../../VERSION), die Zusammensetzung einer konkreten Ausgabe in deren Manifest.\n\n"
        "**Die Sprachumschaltung betrifft die Projektdokumentation.** Die Anwendungsoberfläche, die Screenshots und der überwiegende Teil der verlinkten ausführlichen Dokumentation sind weiterhin auf Russisch."
    ),
    "ja": (
        "<strong>RemCard {version}</strong>の概要。最終更新：<strong>{date}</strong>。統合された`RemCard.exe`から4つの役割を選択でき、別プログラムの`RemCardUpdater.exe`が完全更新パッケージをインストールします。ソースのバージョン番号は[`VERSION`](../../VERSION)、各ビルドの構成はそのマニフェストに記載されています。\n\n"
        "開発者は麻酔科・集中治療医のVadim Bityutskyです。現在のユーザーインターフェースとリンク先の詳細ドキュメントは主にロシア語です。READMEの翻訳は、アプリの画面やスクリーンショットが翻訳されていることを意味しません。"
    ),
    "zh-CN": (
        "本页介绍 <strong>RemCard {version}</strong>，更新于 <strong>{date}</strong>。统一的 `RemCard.exe` 提供四种角色入口；独立的 `RemCardUpdater.exe` 用于安装完整更新。源码版本号见 [`VERSION`](../../VERSION)，具体构建的组成见其清单。\n\n"
        "RemCard 由麻醉与重症医学医师 Vadim Bityutsky 开发。应用界面和链接中的详细文档目前主要为俄语。本文档的语言切换仅改变说明文字，不会翻译应用界面或截图。"
    ),
}


class PublicReleaseError(ValueError):
    """Metadata or document boundaries do not satisfy the rendering contract."""


def validate_metadata(metadata: dict) -> datetime:
    """Validate the fixed schema and exact canonical URLs, without network I/O."""
    if not isinstance(metadata, dict) or set(metadata) != METADATA_KEYS:
        raise PublicReleaseError("Release metadata must contain exactly the schema 1 fields")
    if type(metadata["schema_version"]) is not int or metadata["schema_version"] != 1:
        raise PublicReleaseError("Unsupported metadata schema_version")
    version = metadata["version"]
    if not isinstance(version, str) or not re.fullmatch(VERSION_PATTERN, version):
        raise PublicReleaseError("Unsafe version: expected MAJOR.MINOR.PATCH")
    if metadata["tag_name"] != f"v{version}":
        raise PublicReleaseError("Release tag does not match version")
    commit = metadata["source_commit"]
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        raise PublicReleaseError("source_commit must be a full 40-character Git commit")
    timestamp = metadata["published_at"]
    if not isinstance(timestamp, str):
        raise PublicReleaseError("published_at must be an ISO-8601 timestamp with timezone")
    try:
        published = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PublicReleaseError("Invalid published_at timestamp") from exc
    if published.tzinfo is None:
        raise PublicReleaseError("published_at must include a timezone")

    repository_url = "https://github.com/Donkiton/rem_card/releases"
    download_url = f"{repository_url}/download/v{version}"
    expected_urls = {
        "release_url": f"{repository_url}/tag/v{version}",
        "installer_url": f"{download_url}/RemCard-Setup-{version}.exe",
        "archive_url": f"{download_url}/RemCard_{version}_full.zip",
        "checksums_url": f"{download_url}/SHA256SUMS.txt",
    }
    for key, expected in expected_urls.items():
        if metadata[key] != expected:
            raise PublicReleaseError(f"{key} is not the canonical URL for this release")
    changes = metadata["changes"]
    if not isinstance(changes, dict) or "ru" not in changes or set(changes) - set(LANGUAGES):
        raise PublicReleaseError("changes must contain ru and only supported locales")
    for locale, entries in changes.items():
        if not isinstance(entries, list) or not entries:
            raise PublicReleaseError(f"changes.{locale} must be a non-empty list")
        for entry in entries:
            if not isinstance(entry, str) or not entry.strip() or any(
                ord(character) < 32 for character in entry
            ):
                raise PublicReleaseError(f"changes.{locale} must contain non-empty single-line text")
    return published.astimezone(timezone.utc)


def _markdown_text(value: str) -> str:
    # Release changes are rendered as text, never as HTML, links or markers.
    escaped = html.escape(value, quote=False)
    return re.sub(r"([\\`*_\[\]{}()!#+|~])", r"\\\1", escaped)


def _release_body(metadata: dict, locale: str) -> str:
    version = metadata["version"]
    installer, archive, checksums, full_changes = LABELS[locale]
    downloads = " · ".join(
        f"[{label.format(version=version)}]({metadata[key]})"
        for label, key in (
            (installer, "installer_url"), (archive, "archive_url"), (checksums, "checksums_url")
        )
    )
    entries = metadata["changes"].get(locale, [])
    summary = "\n".join(f"- {_markdown_text(entry)}" for entry in entries)
    requirements = full_changes.format(version=version, url=metadata["release_url"])
    return "\n\n".join(part for part in (downloads, summary, requirements) if part)


def _replace_region(text: str, start: str, end: str, body: str, *, inline: bool) -> str:
    if text.count(start) != 1 or text.count(end) != 1:
        raise PublicReleaseError(f"Expected exactly one {start} / {end} marker pair")
    start_index = text.index(start) + len(start)
    end_index = text.index(end)
    if end_index < start_index:
        raise PublicReleaseError("Release markers are reversed")
    if not inline:
        if text[:start_index - len(start)].split("\n")[-1].strip():
            raise PublicReleaseError("Block start marker must be on its own line")
        if text[end_index + len(end):].split("\n")[0].strip():
            raise PublicReleaseError("Block end marker must be on its own line")
        if text[start_index:].split("\n")[0].strip():
            raise PublicReleaseError("Block start marker must be on its own line")
        if text[:end_index].split("\n")[-1].strip():
            raise PublicReleaseError("Block end marker must be on its own line")
        newline = "\r\n" if "\r\n" in text else "\n"
        body = newline + body.replace("\n", newline) + newline
    return text[:start_index] + body + text[end_index:]


def _allowed_path(root: Path, relative: Path) -> Path:
    path = root / relative
    if path.resolve() != path:
        raise PublicReleaseError(f"Refusing redirected output path: {relative}")
    return path


def render_documents(root: Path, metadata: dict) -> list[Path]:
    """Render the 15 allowed Markdown files and JSON; return only changed paths.

    All metadata, input files, markers and output paths are checked before the
    first write. No Git operations, application files or historical docs change.
    An unchanged second invocation returns an empty list.
    """
    published = validate_metadata(metadata)
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise PublicReleaseError("Document root must be an existing directory")
    planned: list[tuple[Path, bytes]] = []
    for relative, locale, kind in DOCUMENTS:
        path = _allowed_path(root, relative)
        text = path.read_bytes().decode("utf-8")
        if kind == "features":
            updated = _replace_region(
                text, VERSION_START, VERSION_END, metadata["version"], inline=True
            )
        else:
            body = _release_body(metadata, locale)
            if kind == "setup":
                intro = SETUP_INTROS[locale].format(
                    version=metadata["version"], date=published.date().isoformat()
                )
                body = intro + "\n\n" + body
            updated = _replace_region(text, RELEASE_START, RELEASE_END, body, inline=False)
            if kind == "readme":
                if len(BADGE_RE.findall(updated)) != 1:
                    raise PublicReleaseError(f"Expected one bounded VERSION badge in {relative}")
                updated = BADGE_RE.sub(
                    lambda match: match[1] + "v" + metadata["version"] + match[2], updated
                )
        planned.append((path, updated.encode("utf-8")))
    metadata_path = _allowed_path(root, Path("docs/public_release.json"))
    serialized = json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    planned.append((metadata_path, serialized.encode("utf-8")))

    changed = [
        (path, content)
        for path, content in planned
        if not path.exists() or path.read_bytes() != content
    ]
    for path, content in changed:
        path.write_bytes(content)
    return [path for path, _content in changed]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--metadata", type=Path, required=True)
    args = parser.parse_args()
    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    for path in render_documents(args.root, metadata):
        print(path)


if __name__ == "__main__":
    main()
