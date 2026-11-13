from __future__ import annotations

import copy
import json
import re
import shutil
from pathlib import Path

import pytest

from scripts import public_release_docs as renderer


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def metadata() -> dict:
    prefix = "https://github.com/Donkiton/rem_card/releases"
    return {
        "schema_version": 1,
        "version": "5.1.21",
        "tag_name": "v5.1.21",
        "published_at": "2026-11-14T08:48:07Z",
        "release_url": f"{prefix}/tag/v5.1.21",
        "installer_url": f"{prefix}/download/v5.1.21/RemCard-Setup-5.1.21.exe",
        "archive_url": f"{prefix}/download/v5.1.21/RemCard_5.1.21_full.zip",
        "checksums_url": f"{prefix}/download/v5.1.21/SHA256SUMS.txt",
        "source_commit": "abc12345" * 5,
        "changes": {"ru": ["Исправлена ошибка следующего выпуска", "Уточнены правила обновления"]},
    }


@pytest.fixture
def document_root(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    for relative, _locale, _kind in renderer.DOCUMENTS:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PROJECT_ROOT / relative, target)
    (root / "docs/historical.md").write_text("Исторический выпуск 5.1.0\n", encoding="utf-8")
    (root / "VERSION").write_text("5.1.20\n", encoding="utf-8")
    return root


def snapshot(root: Path) -> dict[Path, bytes]:
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def region(text: str, kind: str) -> str:
    start, end = (
        (renderer.VERSION_START, renderer.VERSION_END)
        if kind == "features"
        else (renderer.RELEASE_START, renderer.RELEASE_END)
    )
    return text.split(start, 1)[1].split(end, 1)[0]


def unmanaged(text: str, kind: str) -> str:
    start, end = (
        (renderer.VERSION_START, renderer.VERSION_END)
        if kind == "features"
        else (renderer.RELEASE_START, renderer.RELEASE_END)
    )
    text = text.split(start, 1)[0] + start + end + text.split(end, 1)[1]
    if kind == "readme":
        text = renderer.BADGE_RE.sub(r"\g<1>v0.0.0\g<2>", text)
    return text


def test_future_release_only_changes_allowed_regions_and_paths(document_root: Path, metadata: dict):
    before = snapshot(document_root)
    changed = renderer.render_documents(document_root, metadata)
    expected = {relative for relative, _locale, _kind in renderer.DOCUMENTS}
    expected.add(Path("docs/public_release.json"))
    assert {path.relative_to(document_root) for path in changed} == expected
    after = snapshot(document_root)
    assert set(after) - set(before) == {Path("docs/public_release.json")}
    for relative, _locale, kind in renderer.DOCUMENTS:
        old, new = before[relative].decode(), after[relative].decode()
        assert unmanaged(old, kind) == unmanaged(new, kind), relative
        assert old.count("5.1.0") == new.count("5.1.0"), relative
        assert "5.1.21" in region(new, kind), relative
        if kind != "features":
            assert metadata["installer_url"] in new
            assert metadata["archive_url"] in new
            assert metadata["checksums_url"] in new
            assert metadata["release_url"] in new
    assert before[Path("VERSION")] == after[Path("VERSION")]
    assert before[Path("docs/historical.md")] == after[Path("docs/historical.md")]
    serialized = after[Path("docs/public_release.json")].decode()
    assert serialized == json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def test_identical_release_is_idempotent_without_rewriting_files(document_root: Path, metadata: dict):
    renderer.render_documents(document_root, metadata)
    before = snapshot(document_root)
    times = {path: path.stat().st_mtime_ns for path in document_root.rglob("*") if path.is_file()}
    assert renderer.render_documents(document_root, copy.deepcopy(metadata)) == []
    assert snapshot(document_root) == before
    assert all(path.stat().st_mtime_ns == stamp for path, stamp in times.items())


def test_ru_source_notes_and_foreign_fallback_never_reuse_old_database_policy(
    document_root: Path, metadata: dict
):
    renderer.render_documents(document_root, metadata)
    for relative, locale, kind in renderer.DOCUMENTS:
        body = region((document_root / relative).read_text(encoding="utf-8"), kind)
        if kind == "features":
            continue
        assert "5.1.15" not in body
        assert "5.1.20" not in body
        assert "SQLite" not in body
        assert "SMB" not in body
        assert "**29**" not in body
        if locale == "ru":
            assert all(note in body for note in metadata["changes"]["ru"])
        else:
            assert not re.search(r"[А-Яа-яЁё]", body), (relative, body)
            assert not any(note in body for note in metadata["changes"]["ru"])


def test_explicit_translations_are_used_without_inventing_other_translations(
    document_root: Path, metadata: dict
):
    metadata["changes"]["en"] = ["Fix a new release issue", "Review the updated installation requirements"]
    renderer.render_documents(document_root, metadata)
    english = (document_root / "README.en.md").read_text(encoding="utf-8")
    german = (document_root / "README.de.md").read_text(encoding="utf-8")
    assert "- Fix a new release issue" in english
    assert "Review the updated installation requirements" in english
    assert "Fix a new release issue" not in german
    assert metadata["release_url"] in german


def test_note_markup_cannot_inject_html_links_or_additional_markers(document_root: Path, metadata: dict):
    metadata["changes"]["ru"] = [
        "<script>alert(1)</script> [link](https://example.test) " + renderer.RELEASE_END
    ]
    renderer.render_documents(document_root, metadata)
    text = (document_root / "README.md").read_text(encoding="utf-8")
    assert "<script>" not in text
    assert "[link](https://example.test)" not in text
    assert text.count(renderer.RELEASE_END) == 1
    assert "&lt;script&gt;" in text
    assert renderer.render_documents(document_root, metadata) == []


@pytest.mark.parametrize("broken", ["missing-start", "missing-end", "duplicate-start", "duplicate-end", "reversed"])
@pytest.mark.parametrize("relative,kind", [(Path("docs/guide/setup.zh-CN.md"), "setup"), (Path("docs/guide/features.ja.md"), "features")])
def test_bad_markers_fail_before_any_write(document_root: Path, metadata: dict, broken: str, relative: Path, kind: str):
    path = document_root / relative
    text = path.read_text(encoding="utf-8")
    start, end = (
        (renderer.VERSION_START, renderer.VERSION_END)
        if kind == "features"
        else (renderer.RELEASE_START, renderer.RELEASE_END)
    )
    if broken == "missing-start":
        text = text.replace(start, "")
    elif broken == "missing-end":
        text = text.replace(end, "")
    elif broken == "duplicate-start":
        text += "\n" + start
    elif broken == "duplicate-end":
        text += "\n" + end
    else:
        text = text.replace(start, "TEMPORARY_MARKER").replace(end, start).replace("TEMPORARY_MARKER", end)
    path.write_text(text, encoding="utf-8")
    before = snapshot(document_root)
    with pytest.raises(renderer.PublicReleaseError):
        renderer.render_documents(document_root, metadata)
    assert snapshot(document_root) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", "5.1.21/../../outside"),
        ("version", "05.1.21"),
        ("version", "5.1.21\n"),
        ("version", "5.1.21-rc1"),
        ("tag_name", "v5.1.20"),
        ("source_commit", "abc123"),
        ("schema_version", True),
        ("schema_version", 2),
        ("published_at", "2026-11-14"),
        ("published_at", "not-a-date"),
        ("installer_url", "https://example.test/RemCard-Setup-5.1.21.exe"),
        ("archive_url", "https://github.com/Donkiton/rem_card/releases/download/v5.1.20/RemCard_5.1.20_full.zip"),
        ("checksums_url", "https://github.com/Donkiton/rem_card/releases/download/v5.1.21/SHA256SUMS.txt?redirect=1"),
        ("release_url", "https://github.com/Donkiton/rem_card/releases/latest"),
        ("changes", {"en": ["No source notes"]}),
        ("changes", {"ru": []}),
        ("changes", {"ru": ["One\nTwo"]}),
        ("changes", {"ru": ["Valid"], "unsupported": ["Other"]}),
    ],
)
def test_invalid_metadata_is_rejected_without_writes(document_root: Path, metadata: dict, field: str, value):
    metadata[field] = value
    before = snapshot(document_root)
    with pytest.raises(renderer.PublicReleaseError):
        renderer.render_documents(document_root, metadata)
    assert snapshot(document_root) == before


@pytest.mark.parametrize("duplicate", [False, True])
def test_missing_or_duplicate_readme_badge_fails_before_write(document_root: Path, metadata: dict, duplicate: bool):
    path = document_root / "README.zh-CN.md"
    text = path.read_text(encoding="utf-8")
    text = text + '\n<a href="VERSION">v5.1.20</a>' if duplicate else renderer.BADGE_RE.sub("", text)
    path.write_text(text, encoding="utf-8")
    before = snapshot(document_root)
    with pytest.raises(renderer.PublicReleaseError, match="VERSION badge"):
        renderer.render_documents(document_root, metadata)
    assert snapshot(document_root) == before


def test_redirected_output_is_rejected_without_overwriting_external_file(document_root: Path, tmp_path: Path, metadata: dict):
    outside = tmp_path / "external.json"
    outside.write_text("untouched\n", encoding="utf-8")
    target = document_root / "docs/public_release.json"
    try:
        target.symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks requires an available OS permission")
    before = snapshot(document_root)
    with pytest.raises(renderer.PublicReleaseError, match="redirected"):
        renderer.render_documents(document_root, metadata)
    assert snapshot(document_root) == before
    assert outside.read_text(encoding="utf-8") == "untouched\n"


def test_output_path_error_is_detected_before_modifying_documents(document_root: Path, metadata: dict):
    (document_root / "docs/public_release.json").mkdir()
    before = snapshot(document_root)
    with pytest.raises(OSError):
        renderer.render_documents(document_root, metadata)
    assert snapshot(document_root) == before


def test_original_line_endings_survive_generated_region_replacement(document_root: Path, metadata: dict):
    for relative, _locale, _kind in renderer.DOCUMENTS:
        path = document_root / relative
        text = path.read_bytes().decode().replace("\r\n", "\n")
        path.write_bytes(text.replace("\n", "\r\n").encode())
    renderer.render_documents(document_root, metadata)
    for relative, _locale, _kind in renderer.DOCUMENTS:
        raw = (document_root / relative).read_bytes()
        assert b"\n" not in raw.replace(b"\r\n", b"")
