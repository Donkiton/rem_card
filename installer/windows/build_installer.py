"""Build the local branded Windows installer from an already accepted full ZIP.

No publication, git changes, application rebuild, database access or production UPD writes.
The verified full archive stays byte-for-byte unchanged inside the installer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import zipfile

MAGIC = b"RCSETUP_PAYLOAD1"
SOURCE = Path(__file__).resolve().parent
ROOT = SOURCE.parents[1]
FRAMEWORK = Path("C:/Windows/Microsoft.NET/Framework64/v4.0.30319")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_assets(destination: Path) -> dict[str, Path]:
    from PIL import Image

    destination.mkdir(parents=True, exist_ok=True)
    assets = {
        "RemCard.logo.png": ROOT / "icon/remcardicon.png",
        "RemCard.LICENSE": ROOT / "LICENSE",
    }
    for mode in ("dark", "light"):
        original = ROOT / ("icon/unified_welcome_background_v3.png" if mode == "dark" else "icon/unified_welcome_background_light_v3.png")
        output = destination / f"background-{mode}.jpg"
        with Image.open(original) as picture:
            picture.thumbnail((1400, 900))
            picture.convert("RGB").save(output, "JPEG", quality=88, optimize=True)
        # Resource logical names stay stable; WPF decodes the actual JPEG format.
        assets[f"RemCard.background-{mode}.png"] = output
    if (SOURCE / "InstallerWindow.xaml").exists():
        assets["RemCard.ui.xaml"] = SOURCE / "InstallerWindow.xaml"
    return assets


def compile_stub(output: Path, assets: dict[str, Path], version: str, source_commit: str, build_dir: Path) -> None:
    compiler = FRAMEWORK / "csc.exe"
    if not compiler.is_file():
        raise RuntimeError("Не найден встроенный компилятор .NET Framework.")
    references = [
        FRAMEWORK / "System.dll", FRAMEWORK / "System.Core.dll", FRAMEWORK / "System.Xaml.dll",
        FRAMEWORK / "System.IO.Compression.dll", FRAMEWORK / "System.Web.Extensions.dll",
        FRAMEWORK / "WPF/WindowsBase.dll", FRAMEWORK / "WPF/PresentationCore.dll",
        FRAMEWORK / "WPF/PresentationFramework.dll",
    ]
    manifest = build_dir / "installer.manifest"
    manifest.write_text('''<?xml version="1.0" encoding="utf-8"?>
<assembly manifestVersion="1.0" xmlns="urn:schemas-microsoft-com:asm.v1">
  <assemblyIdentity version="1.0.0.0" name="RemCard.Setup" type="win32"/>
  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3"><security><requestedPrivileges><requestedExecutionLevel level="asInvoker" uiAccess="false"/></requestedPrivileges></security></trustInfo>
  <compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1"><application><supportedOS Id="{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}"/></application></compatibility>
  <application xmlns="urn:schemas-microsoft-com:asm.v3"><windowsSettings><dpiAware xmlns="http://schemas.microsoft.com/SMI/2005/WindowsSettings">true/pm</dpiAware><longPathAware xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">true</longPathAware></windowsSettings></application>
</assembly>
''', encoding="utf-8")
    info = build_dir / "AssemblyInfo.cs"
    info.write_text(
        'using System.Reflection;\nusing System.Runtime.Versioning;\n'
        '[assembly: TargetFramework(".NETFramework,Version=v4.8", FrameworkDisplayName=".NET Framework 4.8")]\n'
        '[assembly: AssemblyTitle("Ремкарта — мастер установки")]\n'
        '[assembly: AssemblyProduct("Ремкарта")]\n'
        '[assembly: AssemblyCompany("Битюцкий Вадим Алексеевич")]\n'
        f'[assembly: AssemblyVersion("{version}.0")]\n'
        f'[assembly: AssemblyFileVersion("{version}.0")]\n'
        f'[assembly: AssemblyInformationalVersion("{version}+{source_commit}")]\n', encoding="utf-8")
    sources = [SOURCE / name for name in ("InstallerText.cs", "Contracts.cs", "PayloadArchive.cs", "InstallerEngine.cs", "EcgMonitor.cs", "InstallerWindow.cs", "App.cs")]
    missing = [str(path) for path in sources if not path.is_file()]
    if missing:
        raise RuntimeError("Ещё не готовы исходники установщика: " + ", ".join(missing))
    command = [str(compiler), "/nologo", "/target:winexe", "/platform:x64", "/optimize+", "/codepage:65001",
               "/out:" + str(output), "/win32manifest:" + str(manifest),
               "/win32icon:" + str(ROOT / "icon/remcardicon.ico")]
    command += ["/reference:" + str(path) for path in references]
    command += ["/resource:" + str(path) + "," + logical for logical, path in assets.items()]
    command += [str(path) for path in sources] + [str(info)]
    completed = subprocess.run(command, text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if completed.stdout:
        print(completed.stdout, end="", flush=True)
    if completed.returncode:
        raise RuntimeError("Компиляция установщика завершилась ошибкой.")


def validate_release_profile(manifest: dict, *, expected_version: str,
                             expected_min_client_version: str,
                             database_upgrades_disabled: bool) -> dict:
    """Check the explicitly accepted profile without changing the manifest."""
    if manifest.get("package_type") != "full" or manifest.get("schema_version") != 2:
        raise RuntimeError("Ожидался принятый полный релиз schema 2.")
    if manifest.get("version") != expected_version:
        raise RuntimeError("Версия payload не совпадает с VERSION исходников установщика.")
    if not re.fullmatch(r"\d+\.\d+\.\d+", expected_min_client_version):
        raise RuntimeError("Ожидалась точная минимальная версия клиента MAJOR.MINOR.PATCH.")
    if manifest.get("min_client_version") != expected_min_client_version:
        raise RuntimeError("Минимальный клиент manifest не совпадает с принятым профилем.")
    if type(manifest.get("database_upgrades_disabled")) is not bool or manifest["database_upgrades_disabled"] != database_upgrades_disabled:
        raise RuntimeError("Профиль миграций manifest не совпадает с принятым профилем.")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", str(manifest.get("source_commit", ""))):
        raise RuntimeError("В принятом manifest отсутствует точный source_commit.")
    return {"min_client_version": expected_min_client_version,
            "database_upgrades_disabled": database_upgrades_disabled}


def build(package: Path, output_dir: Path, expected_sha256: str, *,
          expected_min_client_version: str, database_upgrades_disabled: bool) -> Path:
    package = package.resolve()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    actual_hash = sha256(package)
    if actual_hash.lower() != expected_sha256.lower():
        raise RuntimeError("SHA-256 исходного принятого ZIP не совпадает. Сборка остановлена.")
    with zipfile.ZipFile(package) as archive:
        manifest_bytes = archive.read("manifest.json")
        manifest = json.loads(manifest_bytes)
        version = manifest["version"]
        source_commit = manifest["source_commit"]
        profile = validate_release_profile(manifest,
            expected_version=(ROOT / "VERSION").read_text(encoding="utf-8").strip(),
            expected_min_client_version=expected_min_client_version,
            database_upgrades_disabled=database_upgrades_disabled)
        files = [{"path": item["path"], "size": item["size"], "sha256": item["sha256"]} for item in manifest["files"]]
        if not files or len({str(item["path"]).casefold() for item in files}) != len(files):
            raise RuntimeError("Inventory пуст или содержит повторяющиеся имена.")
        files.append({"path": "manifest.json", "size": len(manifest_bytes), "sha256": hashlib.sha256(manifest_bytes).hexdigest()})
        names = [item.filename for item in archive.infolist() if not item.is_dir()]
        if len({name.casefold() for name in names}) != len(names):
            raise RuntimeError("В ZIP повторяется имя файла.")
        actual_names = set(names)
        expected_names = {item["path"] for item in files} | {"ready.ok"}
        if actual_names != expected_names:
            raise RuntimeError("Состав ZIP отличается от принятого full-manifest.")
        for item in files:
            path = item["path"]
            if not isinstance(path, str) or "\\" in path or ":" in path or path.startswith("/") or any(part in {"", ".", ".."} for part in path.split("/")):
                raise RuntimeError("Небезопасный путь в inventory ZIP.")
            if type(item["size"]) is not int or item["size"] < 0 or not re.fullmatch(r"[0-9a-f]{64}", str(item["sha256"])):
                raise RuntimeError("Некорректный размер или SHA-256 inventory ZIP.")
            if archive.getinfo(path).external_attr >> 16 & 0xF000 == 0xA000:
                raise RuntimeError("В ZIP обнаружена ссылка вместо файла.")
            if archive.getinfo(item["path"]).file_size != item["size"]:
                raise RuntimeError("Размер файла в ZIP не совпадает: " + item["path"])
            with archive.open(path) as entry:
                digest = hashlib.sha256()
                for chunk in iter(lambda: entry.read(1024 * 1024), b""):
                    digest.update(chunk)
                if digest.hexdigest() != item["sha256"]:
                    raise RuntimeError("SHA-256 файла в ZIP не совпадает: " + path)
        archive.read("ready.ok")  # Read the last entry too, including its CRC.
    metadata = json.dumps({"format": 1, "app": "RemCard", "version": version, "source_commit": source_commit, "files": files}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assets = prepare_assets(output_dir / "assets")
    uninstaller = output_dir / "RemCardUninstall.exe"
    print("Компилируем небольшой фирменный деинсталлятор…", flush=True)
    compile_stub(uninstaller, assets, version, source_commit, output_dir)
    assets["RemCard.uninstaller.exe"] = uninstaller
    stub = output_dir / "RemCardSetup.stub.exe"
    print("Компилируем мастер установки…", flush=True)
    compile_stub(stub, assets, version, source_commit, output_dir)
    result = output_dir / f"RemCard-Setup-{version}.exe"
    temporary = result.with_suffix(".exe.building")
    print("Присоединяем принятый ZIP без изменения его байтов…", flush=True)
    with temporary.open("wb") as output:
        with stub.open("rb") as source:
            shutil.copyfileobj(source, output)
        offset = output.tell()
        with package.open("rb") as source:
            shutil.copyfileobj(source, output, length=1024 * 1024)
        output.write(metadata)
        output.write(MAGIC)
        output.write(struct.pack("<qqq", offset, package.stat().st_size, len(metadata)))
        output.write(bytes.fromhex(actual_hash))
        output.write(hashlib.sha256(metadata).digest())
    temporary.replace(result)
    report = {"version": version, "source_commit": source_commit, "package": str(package), "package_sha256": actual_hash,
              "installer": str(result), "installer_sha256": sha256(result), "installer_bytes": result.stat().st_size,
              "uninstaller_bytes": uninstaller.stat().st_size, "payload_files": len(files),
              "unpacked_bytes": sum(item["size"] for item in files), "release_profile": profile,
              "languages": ["ru", "en"], "default_language": "ru", "published": False}
    (output_dir / "build-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--expected-min-client-version", required=True)
    parser.add_argument("--expected-database-upgrades", choices=("enabled", "disabled"), required=True)
    args = parser.parse_args()
    build(args.package, args.output_dir, args.expected_sha256,
          expected_min_client_version=args.expected_min_client_version,
          database_upgrades_disabled=args.expected_database_upgrades == "disabled")
    return 0


if __name__ == "__main__":
    sys.exit(main())
