from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from rem_card.app import runtime_paths
from rem_card.ui.shared import background_settings, display_settings_storage, settings_file_io


@pytest.fixture(params=["display", "background"])
def storage_case(request, tmp_path):
    if request.param == "display":
        storage = display_settings_storage.DisplaySettingsStorage(str(tmp_path / "display.json"))
        payload = display_settings_storage.default_display_settings_payload()
        payload["active"]["doctor"]["sector8_buttons"]["visible"]["refresh"] = False
    else:
        storage = background_settings.BackgroundSettingsStorage(str(tmp_path / "background.json"))
        payload = background_settings.default_background_settings_payload()
        payload["backgrounds"].append({
            "id": "holiday", "name": "РџСЂР°Р·РґРЅРёРє", "file": "holiday.png",
            "start": "12-31", "end": "01-02", "locked": False,
        })
    return storage, payload


def test_custom_settings_roundtrip_and_complete_json(storage_case):
    storage, payload = storage_case
    storage.save(payload)
    first = storage.load()
    storage.save(first)
    assert storage.load() == first
    text = Path(storage.path).read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert isinstance(json.loads(text), dict)
    assert not Path(storage.path + ".tmp").exists()
    if "active" in first:
        assert first["active"]["doctor"]["sector8_buttons"]["visible"]["refresh"] is False
    else:
        assert any(item["name"] == "РџСЂР°Р·РґРЅРёРє" for item in first["backgrounds"])


def test_broken_settings_kept_before_default_recovery(storage_case):
    storage, _ = storage_case
    path = Path(storage.path)
    path.write_text("{broken", encoding="utf-8")
    recovered = storage.load()
    quarantined = list(path.parent.glob(path.name + ".*.broken"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "{broken"
    assert isinstance(recovered, dict)
    assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict)
    assert storage.last_error


def test_replace_failure_preserves_previous_settings(storage_case, monkeypatch):
    storage, payload = storage_case
    storage.save(payload)
    previous = Path(storage.path).read_bytes()

    def fail_replace(*args):
        raise PermissionError("target busy")

    monkeypatch.setattr(settings_file_io.os, "replace", fail_replace)
    with pytest.raises(PermissionError, match="target busy"):
        storage.save(payload)
    assert Path(storage.path).read_bytes() == previous


@pytest.mark.parametrize("layout", ["source", "onefile", "internal", "alongside"])
def test_bundled_settings_resource_layouts(storage_case, monkeypatch, tmp_path, layout):
    storage, _ = storage_case
    monkeypatch.setattr(runtime_paths, "is_compiled", lambda: layout != "source")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "RemCard.exe"))
    monkeypatch.setattr(sys, "frozen", layout == "onefile", raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "unpacked"), raising=False)
    if layout == "internal":
        (tmp_path / "_internal").mkdir()
    if layout == "source":
        assert storage._bundled_path() is None
        return
    base = tmp_path / "unpacked" if layout == "onefile" else tmp_path
    if layout == "internal":
        base /= "_internal"
    relative = (
        display_settings_storage.DISPLAY_SETTINGS_RELATIVE_PATH
        if isinstance(storage, display_settings_storage.DisplaySettingsStorage)
        else background_settings.BACKGROUND_SETTINGS_RELATIVE_PATH
    )
    assert Path(storage._bundled_path()) == base / "rem_card" / relative
