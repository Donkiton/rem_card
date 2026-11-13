"""Synchronize public release documents through signed, checked GitHub pull requests."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

from scripts.public_release_docs import render_documents, validate_metadata
from scripts.public_release_github import GitHubClient, create_and_merge_release_pr


APP_REPOSITORY = "Donkiton/rem_card"
SITE_REPOSITORY = "Donkiton/rem_site"
LOCALES = ("ru", "en", "de", "ja", "zh-CN")
README_FILES = ("README.md", "README.en.md", "README.de.md", "README.ja.md", "README.zh-CN.md")
DOC_PATHS = frozenset(README_FILES) | {
    f"docs/guide/{guide}.{locale}.md" for guide in ("features", "setup") for locale in LOCALES
} | {"docs/public_release.json"}
SITE_PATHS = frozenset({"content/public-release.json"})
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class ReleaseNotReady(ValueError):
    """The stable release exists but its accepted files are not all available yet."""


class SupersededRelease(ValueError):
    """A queued publication event has already been superseded by a newer stable release."""


def version_parts(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or VERSION_PATTERN.fullmatch(value) is None:
        raise ValueError("Версия релиза должна иметь формат MAJOR.MINOR.PATCH")
    return tuple(int(part) for part in value.split("."))


def latest_stable(client: GitHubClient) -> dict:
    release = client.request("GET", f"/repos/{APP_REPOSITORY}/releases/latest")
    if not isinstance(release, dict) or release.get("draft") is not False or release.get("prerelease") is not False:
        raise ValueError("Допустим только опубликованный стабильный релиз")
    tag = release.get("tag_name")
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise ValueError("Тег стабильного релиза должен начинаться с v")
    version_parts(tag[1:])
    if release.get("html_url") != f"https://github.com/{APP_REPOSITORY}/releases/tag/{tag}":
        raise ValueError("Страница релиза не соответствует проверенному репозиторию и тегу")
    return release


def download_checksums(url: str) -> str:
    request = Request(url, headers={"User-Agent": "RemCard-Public-Release-Sync", "Accept": "text/plain"})
    # Public assets are fetched without credentials, including any CDN redirect.
    with urlopen(request, timeout=45) as response:
        payload = response.read(65537)
    if len(payload) > 65536:
        raise ValueError("Файл контрольных сумм слишком велик")
    return payload.decode("utf-8-sig")


def decode_file(payload: dict) -> dict:
    if not isinstance(payload, dict) or payload.get("encoding") != "base64" or payload.get("type") != "file":
        raise ValueError("Ожидался обычный JSON-файл GitHub")
    content = payload.get("content")
    if not isinstance(content, str) or len(content) > 500000:
        raise ValueError("Некорректный размер JSON-файла GitHub")
    return json.loads(base64.b64decode(content).decode("utf-8-sig"))


def build_metadata(client: GitHubClient, release: dict, downloader=download_checksums) -> dict:
    tag = release["tag_name"]
    version = tag[1:]
    version_parts(version)
    prefix = f"https://github.com/{APP_REPOSITORY}/releases/download/{tag}/"
    filenames = {
        "installer_url": f"RemCard-Setup-{version}.exe",
        "archive_url": f"RemCard_{version}_full.zip",
        "checksums_url": "SHA256SUMS.txt",
    }
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise ReleaseNotReady("Список файлов релиза ещё не опубликован")
    selected = {}
    for field, name in filenames.items():
        matching = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == name]
        if len(matching) != 1:
            raise ReleaseNotReady(f"Ожидается единственный файл {name}")
        asset = matching[0]
        if asset.get("browser_download_url") != prefix + name:
            raise ValueError(f"Файл {name} имеет неподтверждённый URL")
        if type(asset.get("size")) is not int or asset["size"] <= 0 or asset.get("state") != "uploaded":
            raise ReleaseNotReady(f"Файл {name} ещё не загружен полностью")
        selected[field] = asset
    try:
        checksum_text = downloader(prefix + "SHA256SUMS.txt")
    except (OSError, UnicodeError) as exc:
        raise ReleaseNotReady("Файл контрольных сумм пока недоступен") from exc
    checksums = {}
    for line in checksum_text.splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?([^/\\\r\n]+)", line.strip())
        if not match:
            raise ReleaseNotReady("Контрольные суммы ещё не готовы или имеют неподдерживаемый формат")
        if match[2] in checksums:
            raise ValueError("В контрольных суммах повторяется имя файла")
        checksums[match[2]] = match[1].lower()
    for field in ("installer_url", "archive_url"):
        asset = selected[field]
        digest = checksums.get(asset["name"])
        if digest is None:
            raise ReleaseNotReady(f"Отсутствует контрольная сумма {asset['name']}")
        if asset.get("digest") != "sha256:" + digest:
            raise ReleaseNotReady(f"Контрольная сумма {asset['name']} не совпадает с GitHub asset")
    commit = client.request("GET", f"/repos/{APP_REPOSITORY}/commits/{quote(tag, safe='')}")
    source_commit = commit.get("sha") if isinstance(commit, dict) else None
    if not isinstance(source_commit, str) or SHA_PATTERN.fullmatch(source_commit) is None:
        raise ValueError("Не определён исходный коммит опубликованного тега")
    info = decode_file(client.request(
        "GET", f"/repos/{APP_REPOSITORY}/contents/app/release_info.json?ref={source_commit}",
    ))
    if not isinstance(info, dict) or info.get("version") != version or info.get("schema_version") != 1:
        raise ValueError("release_info опубликованного тега не соответствует версии релиза")
    source_changes = info.get("changes")
    if not isinstance(source_changes, list) or not source_changes:
        raise ValueError("В release_info отсутствует описание изменений")
    notes = {"ru": source_changes}
    translations = info.get("changes_i18n", {})
    if not isinstance(translations, dict):
        raise ValueError("changes_i18n должен быть словарём языков")
    for locale, changes in translations.items():
        if locale not in LOCALES:
            raise ValueError("Неизвестный язык описания релиза")
        if locale != "ru":
            notes[locale] = changes
    for locale_changes in notes.values():
        if not isinstance(locale_changes, list) or not 1 <= len(locale_changes) <= 100:
            raise ValueError("Некорректный список изменений релиза")
        if any(not isinstance(item, str) or not item.strip() or len(item) > 4000 for item in locale_changes):
            raise ValueError("Некорректная строка описания релиза")
    metadata = {
        "schema_version": 1,
        "version": version,
        "tag_name": tag,
        "published_at": release["published_at"],
        "release_url": release["html_url"],
        **{field: prefix + name for field, name in filenames.items()},
        "source_commit": source_commit,
        "changes": notes,
    }
    validate_metadata(metadata)
    return metadata


def wait_for_metadata(client: GitHubClient, *, wait_seconds=600, poll_seconds=20,
                      requested_tag=None, downloader=download_checksums,
                      clock=time.monotonic, sleeper=time.sleep) -> dict:
    deadline = clock() + wait_seconds
    while True:
        release = latest_stable(client)
        if requested_tag and release["tag_name"] != requested_tag:
            raise SupersededRelease("Заданный релиз уже не является последним стабильным; устаревшее событие пропущено")
        try:
            return build_metadata(client, release, downloader)
        except ReleaseNotReady as exc:
            if clock() >= deadline:
                raise ReleaseNotReady(f"Не удалось дождаться полного комплекта релиза: {exc}") from exc
            print(f"Ожидание файлов релиза: {exc}", flush=True)
            sleeper(min(poll_seconds, max(0, deadline - clock())))


def changed_files(root: Path, paths: frozenset[str], before: dict[str, str | None]) -> dict[str, str]:
    result = {}
    for relative in sorted(paths):
        path = root / relative
        if not path.is_file():
            raise ValueError(f"Не найден ожидаемый публичный файл: {relative}")
        value = path.read_text(encoding="utf-8")
        if value != before[relative]:
            result[relative] = value
    return result


def snapshot(root: Path, paths: frozenset[str]) -> dict[str, str | None]:
    return {
        relative: (root / relative).read_text(encoding="utf-8") if (root / relative).is_file() else None
        for relative in paths
    }


def git_head(root: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def public_process_env() -> dict[str, str]:
    """Offline renderers and public clones never receive the installation token."""
    return {
        key: value for key, value in os.environ.items()
        if not any(part in key.upper() for part in ("TOKEN", "SECRET", "PRIVATE_KEY"))
    }


def synchronize(client: GitHubClient, root: Path, metadata: dict, *, site_root: Path | None = None) -> dict:
    existing = root / "docs/public_release.json"
    if existing.exists():
        previous = json.loads(existing.read_text(encoding="utf-8"))
        if version_parts(previous["version"]) > version_parts(metadata["version"]):
            raise ValueError("Понижение опубликованной версии документации запрещено")
    before = snapshot(root, DOC_PATHS)
    base_sha = git_head(root)
    render_documents(root, metadata)
    docs = create_and_merge_release_pr(
        client, APP_REPOSITORY, f"automation/public-release-{metadata['version']}", metadata["version"],
        base_sha, changed_files(root, DOC_PATHS, before), set(DOC_PATHS),
    )
    confirmed = decode_file(client.request("GET", f"/repos/{APP_REPOSITORY}/contents/docs/public_release.json?ref=main"))
    if confirmed != metadata:
        raise ValueError("Опубликованные данные документации изменились; сайт не обновлён")
    commits = client.request("GET", f"/repos/{APP_REPOSITORY}/commits?path=docs/public_release.json&sha=main&per_page=1")
    docs_commit = commits[0].get("sha") if isinstance(commits, list) and commits else None
    if not isinstance(docs_commit, str) or SHA_PATTERN.fullmatch(docs_commit) is None:
        raise ValueError("Не определён подтверждённый коммит публичного описания")
    site_metadata = {**metadata, "documentation_commit": docs_commit}
    if site_root is not None:
        site = synchronize_site(client, site_root, site_metadata)
    else:
        with tempfile.TemporaryDirectory(prefix="remcard-public-site-") as directory:
            checkout = Path(directory) / "site"
            subprocess.run([
                "git", "clone", "--quiet", "--depth", "1", "--branch", "main",
                f"https://github.com/{SITE_REPOSITORY}.git", str(checkout),
            ], check=True, env=public_process_env())
            site = synchronize_site(client, checkout, site_metadata)
    return {"version": metadata["version"], "documentation": docs, "site": site}


def synchronize_site(client: GitHubClient, root: Path, metadata: dict) -> dict:
    before = snapshot(root, SITE_PATHS)
    base_sha = git_head(root)
    with tempfile.TemporaryDirectory(prefix="remcard-public-metadata-") as directory:
        metadata_path = Path(directory) / "release.json"
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        subprocess.run([
            "node", "scripts/sync-public-release.mjs", "--metadata", str(metadata_path),
        ], cwd=root, check=True, env=public_process_env())
    return create_and_merge_release_pr(
        client, SITE_REPOSITORY, f"automation/public-release-{metadata['version']}", metadata["version"],
        base_sha, changed_files(root, SITE_PATHS, before), set(SITE_PATHS),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Обновить README и сайт по опубликованному стабильному релизу")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--event-file", type=Path)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--metadata-output", type=Path, help="Только получить проверенные данные, без PR и записи документов")
    args = parser.parse_args()
    token = os.environ.get("PUBLIC_RELEASE_TOKEN", "")
    if not token:
        raise ValueError("Настройте PUBLIC_RELEASE_APP_ID и секрет PUBLIC_RELEASE_APP_PRIVATE_KEY в GitHub Actions")
    client = GitHubClient(token)
    requested_tag = None
    if args.event_file:
        event = json.loads(args.event_file.read_text(encoding="utf-8"))
        release = event.get("release")
        if release:
            if release.get("prerelease") or release.get("draft"):
                print("Предварительный релиз пропущен")
                return 0
            requested_tag = release.get("tag_name")
    try:
        metadata = wait_for_metadata(client, requested_tag=requested_tag)
    except SupersededRelease as exc:
        print(str(exc))
        return 0
    if args.metadata_output:
        args.metadata_output.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Проверен опубликованный релиз {metadata['version']}")
        return 0
    result = synchronize(client, args.root.resolve(), metadata)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.summary:
        lines = [f"## Публичные материалы Ремкарты {metadata['version']}", ""]
        for name, label in (("documentation", "README и руководства"), ("site", "Сайт")):
            outcome = result[name]
            lines.append(f"- {label}: {outcome.get('url') or 'уже актуальны'}")
        lines.append("\nСайт публикуется обычным GitHub Pages workflow после слияния его PR.\n")
        with args.summary.open("a", encoding="utf-8") as output:
            output.write("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
