"""Render isolated RU/EN previews and verify language switching without installing."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import time
import zipfile

from build_installer import MAGIC, ROOT, compile_stub, prepare_assets
from verify_installer import registry_snapshot


def build_preview_fixture(directory: Path) -> Path:
    """A clearly named UI-only payload; never a distributable application build."""
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assets = prepare_assets(directory / "assets")
    uninstaller = directory / "RemCardUninstall.exe"
    compile_stub(uninstaller, assets, version, "0" * 40, directory)
    assets["RemCard.uninstaller.exe"] = uninstaller
    stub = directory / "PreviewOnly.stub.exe"
    compile_stub(stub, assets, version, "0" * 40, directory)
    contents = {"RemCard.exe": b"UI PREVIEW ONLY - NOT AN APPLICATION\n",
                "RemCardUpdater.exe": b"UI PREVIEW ONLY - NOT AN UPDATER\n",
                "_internal/preview-only.txt": b"Disposable UI fixture. Do not distribute.\n"}
    inventory = [{"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                 for name, data in contents.items()]
    manifest = {"schema_version": 2, "app": "rem_card", "package_type": "full", "version": version,
                "source_commit": "0" * 40, "min_client_version": version,
                "database_upgrades_disabled": False, "files": inventory}
    contents["manifest.json"] = json.dumps(manifest).encode("utf-8")
    inventory.append({"path": "manifest.json", "size": len(contents["manifest.json"]),
                      "sha256": hashlib.sha256(contents["manifest.json"]).hexdigest()})
    package = directory / "PREVIEW-ONLY.zip"
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in contents.items():
            archive.writestr(name, data)
        archive.writestr("ready.ok", "PREVIEW ONLY\n")
    metadata = json.dumps({"format": 1, "app": "RemCard", "version": version,
                           "source_commit": "0" * 40, "files": inventory}, separators=(",", ":")).encode()
    payload = package.read_bytes()
    result = directory / "RemCard-PREVIEW-ONLY.exe"
    with result.open("wb") as output:
        output.write(stub.read_bytes())
        offset = output.tell()
        output.write(payload)
        output.write(metadata)
        output.write(MAGIC)
        output.write(struct.pack("<qqq", offset, len(payload), len(metadata)))
        output.write(hashlib.sha256(payload).digest())
        output.write(hashlib.sha256(metadata).digest())
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--installer", type=Path)
    source.add_argument("--build-preview-fixture", action="store_true")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    temp = (Path(os.environ["LOCALAPPDATA"]) / "Temp").resolve()
    if temp not in output.parents or not output.name.startswith("RemCard-installer-QA-"):
        raise RuntimeError("Use a new RemCard-installer-QA-* directory below LocalAppData/Temp.")
    output.mkdir(parents=True, exist_ok=False)
    installer = build_preview_fixture(output) if args.build_preview_fixture else args.installer.resolve()
    before = registry_snapshot()
    target = output / "preview-target" / ("Long application folder " * 5).strip()
    results = []
    started = time.monotonic()

    def invoke(label: str, arguments: list[str]) -> dict:
        report = output / (label + ".json")
        completed = subprocess.run([str(installer), "--ui-smoke", "--target", str(target),
                                    "--auto-close-ms", "25000", "--report", str(report), *arguments],
                                   timeout=35, capture_output=True)
        if completed.returncode != 0 or not report.exists():
            raise RuntimeError(f"{label}: preview failed ({completed.returncode})")
        data = json.loads(report.read_text(encoding="utf-8"))
        if not data.get("ok") or data.get("layout_issues"):
            raise RuntimeError(f"{label}: {data}")
        results.append({"test": label, "ok": True, "report": str(report)})
        return data

    for language in ("ru", "en"):
        for preview in ("welcome", "license", "location", "review", "progress", "error", "uninstall"):
            name = f"{language}-{preview}"
            data = invoke(name, ["--language", language, "--preview", preview,
                                 "--render-preview", str(output / (name + ".png"))])
            if data.get("language") != language:
                raise RuntimeError("The preview language did not match --language.")
        state = invoke(language + "-state-switch", ["--language", language, "--language-state-smoke"])
        if state.get("license_sha256") != hashlib.sha256((ROOT / "LICENSE").read_bytes()).hexdigest():
            raise RuntimeError("The embedded LICENSE bytes differ from the repository LICENSE.")
    default = invoke("default-language", ["--preview", "welcome", "--render-preview", str(output / "default-language.png")])
    if default.get("language") != "ru":
        raise RuntimeError("Russian must be the default language.")
    if target.exists() or registry_snapshot() != before:
        raise RuntimeError("UI preview changed the installation target or registry.")
    summary = {"ok": True, "ui_fixture_only": args.build_preview_fixture,
               "installer": str(installer), "elapsed_seconds": round(time.monotonic() - started, 2),
               "registry_unchanged": True, "target_not_created": True, "tests": results}
    (output / "ui-acceptance-summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
