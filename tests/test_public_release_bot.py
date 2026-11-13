from __future__ import annotations

import base64
import copy
import json

import pytest

from scripts import public_release_bot as bot


def encoded_file(value):
    return {"type": "file", "encoding": "base64", "content": base64.b64encode(json.dumps(value).encode()).decode()}


@pytest.fixture
def release():
    version = "5.1.21"
    names = (f"RemCard-Setup-{version}.exe", f"RemCard_{version}_full.zip", "SHA256SUMS.txt")
    return {
        "tag_name": f"v{version}", "draft": False, "prerelease": False,
        "published_at": "2026-11-14T08:48:07Z",
        "html_url": f"https://github.com/Donkiton/rem_card/releases/tag/v{version}",
        "assets": [{
            "name": name, "browser_download_url": f"https://github.com/Donkiton/rem_card/releases/download/v{version}/{name}",
            "size": 200, "state": "uploaded", "digest": "sha256:" + ("a" if index == 0 else "b") * 64,
        } for index, name in enumerate(names)],
    }


@pytest.fixture
def info():
    return {"schema_version": 1, "version": "5.1.21", "date": "2026-11-14", "changes": ["Исправлена ошибка сохранения"]}


class ReadClient:
    def __init__(self, release, info):
        self.release = release
        self.info = info
        self.calls = []

    def request(self, method, path, payload=None):
        self.calls.append((method, path))
        assert method == "GET"
        if path.endswith("/releases/latest"):
            return copy.deepcopy(self.release)
        if "/commits/v" in path:
            return {"sha": "c" * 40}
        if "/contents/app/release_info.json?ref=" in path:
            return encoded_file(self.info)
        raise AssertionError(path)


def checksum_text(_url):
    return "a" * 64 + "  RemCard-Setup-5.1.21.exe\n" + "b" * 64 + "  RemCard_5.1.21_full.zip\n"


def test_metadata_comes_from_published_tag_with_ready_assets(release, info):
    client = ReadClient(release, info)
    metadata = bot.build_metadata(client, release, checksum_text)
    assert metadata["version"] == "5.1.21"
    assert metadata["changes"] == {"ru": info["changes"]}
    assert metadata["source_commit"] == "c" * 40
    assert client.calls[-1][1].endswith("ref=" + "c" * 40)
    assert not any("main" in path for _, path in client.calls)


@pytest.mark.parametrize("field", ["draft", "prerelease"])
def test_unpublished_and_preview_releases_are_rejected(release, info, field):
    release[field] = True
    with pytest.raises(ValueError, match="стабильный"):
        bot.latest_stable(ReadClient(release, info))


@pytest.mark.parametrize("tag", ["v5.1.21-beta.1", "v5.1.21;echo", "5.1.21", "v../21"])
def test_release_tag_is_data_not_a_command(release, info, tag):
    release["tag_name"] = tag
    with pytest.raises(ValueError):
        bot.latest_stable(ReadClient(release, info))


@pytest.mark.parametrize("missing", ["RemCard-Setup-5.1.21.exe", "RemCard_5.1.21_full.zip", "SHA256SUMS.txt"])
def test_partial_upload_is_not_published_to_documents(release, info, missing):
    release["assets"] = [item for item in release["assets"] if item["name"] != missing]
    client = ReadClient(release, info)
    with pytest.raises(bot.ReleaseNotReady):
        bot.build_metadata(client, release, checksum_text)
    assert client.calls == []


def test_checksum_proves_the_exact_uploaded_assets(release, info):
    release["assets"][0]["digest"] = "sha256:" + "f" * 64
    with pytest.raises(bot.ReleaseNotReady, match="не совпадает"):
        bot.build_metadata(ReadClient(release, info), release, checksum_text)


def test_foreign_asset_cannot_be_advertised(release, info):
    release["assets"][0]["browser_download_url"] = "https://example.com/RemCard-Setup-5.1.21.exe"
    with pytest.raises(ValueError, match="URL"):
        bot.build_metadata(ReadClient(release, info), release, checksum_text)


def test_wrong_tag_source_version_never_reuses_main_notes(release, info):
    info["version"] = "5.1.22"
    with pytest.raises(ValueError, match="не соответствует"):
        bot.build_metadata(ReadClient(release, info), release, checksum_text)


def test_explicit_translations_are_optional_and_version_bound(release, info):
    info["changes_i18n"] = {"en": ["Fixed a save error."]}
    result = bot.build_metadata(ReadClient(release, info), release, checksum_text)
    assert result["changes"]["en"] == ["Fixed a save error."]
    assert "de" not in result["changes"]
    info["changes_i18n"]["fr"] = ["Unsupported language"]
    with pytest.raises(ValueError, match="язык"):
        bot.build_metadata(ReadClient(release, info), release, checksum_text)


def test_invalid_publication_date_rejected_before_any_write(release, info):
    release["published_at"] = "tomorrow"
    with pytest.raises(ValueError):
        bot.build_metadata(ReadClient(release, info), release, checksum_text)


def test_readiness_retry_does_not_publish_until_checksums_match(release, info):
    elapsed = [0]
    attempts = []

    def downloader(url):
        attempts.append(url)
        if len(attempts) == 1:
            raise OSError("CDN not ready")
        return checksum_text(url)

    result = bot.wait_for_metadata(
        ReadClient(release, info), wait_seconds=30, poll_seconds=5,
        downloader=downloader, clock=lambda: elapsed[0], sleeper=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
    )
    assert result["version"] == "5.1.21"
    assert len(attempts) == 2 and elapsed[0] == 5


def test_incomplete_release_times_out_without_writes(release, info):
    release["assets"] = []
    elapsed = [0]
    with pytest.raises(bot.ReleaseNotReady, match="Не удалось дождаться"):
        bot.wait_for_metadata(
            ReadClient(release, info), wait_seconds=10, poll_seconds=5,
            downloader=checksum_text, clock=lambda: elapsed[0], sleeper=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
        )
    assert elapsed[0] == 10


def test_superseded_event_cannot_downgrade_public_release(release, info):
    with pytest.raises(bot.SupersededRelease):
        bot.wait_for_metadata(ReadClient(release, info), requested_tag="v5.1.20", downloader=checksum_text)


def test_offline_children_do_not_receive_access_tokens(monkeypatch):
    monkeypatch.setenv("PUBLIC_RELEASE_TOKEN", "secret-value")
    monkeypatch.setenv("ACTIONS_RUNTIME_TOKEN", "another-secret")
    monkeypatch.setenv("PATH", "tools")
    environment = bot.public_process_env()
    assert "PUBLIC_RELEASE_TOKEN" not in environment
    assert "ACTIONS_RUNTIME_TOKEN" not in environment
    assert environment["PATH"] == "tools"


def test_documentation_is_confirmed_before_site_update(tmp_path, monkeypatch, release, info):
    metadata = bot.build_metadata(ReadClient(release, info), release, checksum_text)
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(bot, "DOC_PATHS", frozenset({"docs/public_release.json"}))
    monkeypatch.setattr(bot, "git_head", lambda _root: "d" * 40)

    def render(path, value):
        target = path / "docs/public_release.json"
        target.parent.mkdir()
        target.write_text(json.dumps(value), encoding="utf-8")

    monkeypatch.setattr(bot, "render_documents", render)
    operations = []
    monkeypatch.setattr(bot, "create_and_merge_release_pr", lambda *args: operations.append("docs-merged") or {"changed": True})

    class ConfirmClient:
        def request(self, _method, path, payload=None):
            if "/contents/docs/public_release.json" in path:
                operations.append("docs-confirmed")
                return encoded_file(metadata)
            return [{"sha": "e" * 40}]

    def sync_site(_client, _root, value):
        operations.append("site")
        assert value["documentation_commit"] == "e" * 40
        return {"changed": True}

    monkeypatch.setattr(bot, "synchronize_site", sync_site)
    bot.synchronize(ConfirmClient(), root, metadata, site_root=tmp_path / "site")
    assert operations == ["docs-merged", "docs-confirmed", "site"]


def test_newer_documentation_stops_entire_pipeline(tmp_path, monkeypatch, release, info):
    metadata = bot.build_metadata(ReadClient(release, info), release, checksum_text)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/public_release.json").write_text(json.dumps({"version": "5.1.22"}), encoding="utf-8")
    monkeypatch.setattr(bot, "render_documents", lambda *args: pytest.fail("Renderer must not run"))
    with pytest.raises(ValueError, match="Понижение"):
        bot.synchronize(ReadClient(release, info), tmp_path, metadata)
