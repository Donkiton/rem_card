"""Exercise the built installer against an explicitly isolated disposable fixture."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import winreg

from build_installer import validate_release_profile


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def registry_snapshot() -> dict | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall\RemCard") as key:
            result = {}
            for index in range(winreg.QueryInfoKey(key)[1]):
                name, value, kind = winreg.EnumValue(key, index)
                result[name] = [value, kind]
            return result
    except FileNotFoundError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installer", type=Path, required=True)
    parser.add_argument("--package-directory", type=Path, required=True)
    parser.add_argument("--qa-root", type=Path, required=True)
    parser.add_argument("--expected-min-client-version", required=True)
    parser.add_argument("--expected-database-upgrades", choices=("enabled", "disabled"), required=True)
    args = parser.parse_args()
    installer = args.installer.resolve()
    accepted_manifest_path = args.package_directory / "manifest.json"
    manifest = json.loads(accepted_manifest_path.read_text(encoding="utf-8"))
    profile = validate_release_profile(manifest, expected_version=manifest["version"],
        expected_min_client_version=args.expected_min_client_version,
        database_upgrades_disabled=args.expected_database_upgrades == "disabled")
    qa_root = args.qa_root.resolve()
    expected_parent = Path(os.environ["LOCALAPPDATA"]) / "Temp"
    if expected_parent.resolve() not in qa_root.parents or not qa_root.name.startswith("RemCard-installer-QA-"):
        raise RuntimeError("QA-каталог должен быть явной папкой RemCard-installer-QA-* внутри LocalAppData/Temp.")
    if qa_root.exists():
        raise RuntimeError("QA-каталог уже существует; выберите новый, чтобы не затронуть предыдущие результаты.")
    qa_root.mkdir(parents=True)
    (qa_root / "owned-fixture.json").write_text(json.dumps({"task": "RemCard installer acceptance", "installer": str(installer)}), encoding="utf-8")
    reports = qa_root / "reports"
    reports.mkdir()
    target = qa_root / "client"
    before_registry = registry_snapshot()
    started = time.monotonic()
    results = []

    def invoke(label: str, arguments: list[str], expected_exit: int = 0) -> dict:
        report = reports / (label + ".json")
        command = [str(installer), *arguments, "--report", str(report)]
        completed = subprocess.run(command, timeout=240, capture_output=True)
        if completed.returncode != expected_exit:
            raise RuntimeError(f"{label}: код {completed.returncode}, ожидался {expected_exit}; отчёт {report}")
        data = json.loads(report.read_text(encoding="utf-8"))
        if bool(data["ok"]) != (expected_exit == 0):
            raise RuntimeError(f"{label}: неверный статус отчёта")
        results.append({"test": label, "exit_code": completed.returncode, "report": str(report), "code": data.get("code")})
        print(json.dumps({"test": label, "ok": True, "report": str(report)}, ensure_ascii=False), flush=True)
        return data

    def verify_inventory(label: str) -> None:
        mismatches = []
        for entry in manifest["files"]:
            file = target / entry["path"]
            if not file.is_file() or file.stat().st_size != entry["size"] or sha256(file) != entry["sha256"]:
                mismatches.append(entry["path"])
        if mismatches:
            raise RuntimeError(label + ": inventory не совпал: " + repr(mismatches[:10]))
        if (target / "manifest.json").read_bytes() != accepted_manifest_path.read_bytes():
            raise RuntimeError(label + ": байты принятого manifest изменились")
        if (target / "ready.ok").exists():
            raise RuntimeError("ready.ok не должен попадать в установку")
        if not (target / "RemCardUninstall.exe").is_file() or not (target / "remcard_installation.json").is_file():
            raise RuntimeError("Не создана программа удаления или её метаданные")
        results.append({"test": label, "ok": True, "files": len(manifest["files"])})
        print(json.dumps({"test": label, "ok": True, "files": len(manifest["files"])}, ensure_ascii=False), flush=True)

    payload = invoke("verify-payload", ["--verify-payload"])
    if payload.get("version") != manifest["version"] or payload.get("source_commit") != manifest["source_commit"]:
        raise RuntimeError("Встроенный payload не совпадает с принятым manifest.")
    invoke("reject-drive-root", ["--install-test", "--target", "C:\\"], expected_exit=1)
    invoke("first-install", ["--install-test", "--target", str(target)])
    verify_inventory("installed-inventory")

    smoke_root = qa_root / "smoke"
    smoke_root.mkdir()
    smoke_env = dict(os.environ)
    smoke_env.update({
        "REMCARD_SMOKE_EXPECT_NO_DATABASE_UPGRADES": "1" if profile["database_upgrades_disabled"] else "0",
        "REMCARD_BAZA_DIR": str(smoke_root / "data"),
        "REMCARD_DATA_PATH_CONFIG": str(smoke_root / "remcard_data_path.json"),
        "LOCALAPPDATA": str(smoke_root / "profile" / "Local"),
        "APPDATA": str(smoke_root / "profile" / "Roaming"),
        "PROGRAMDATA": str(smoke_root / "profile" / "ProgramData"),
    })
    for filename in ("RemCard.exe", "RemCardUpdater.exe"):
        completed = subprocess.run([str(target / filename), "--compiled-smoke"], cwd=target, env=smoke_env, timeout=90, capture_output=True)
        (reports / (filename + "-smoke.log")).write_bytes(completed.stdout + completed.stderr)
        if completed.returncode:
            raise RuntimeError(f"Smoke {filename}: {completed.returncode}")
        results.append({"test": "compiled-smoke", "executable": filename, "ok": True})
        print(json.dumps({"test": "compiled-smoke", "executable": filename, "ok": True}), flush=True)

    preserved = {
        target / "remcard_data_path.json": json.dumps({"schema_version": 1, "baza_dir": str(smoke_root / "data")}, ensure_ascii=False).encode("utf-8"),
        target / "logs" / "keep-user-log.txt": b"isolated user log: keep\n",
        target / "personal-note.txt": b"unknown user-owned file: keep\n",
        qa_root / "outside-client.txt": b"outside installation: keep\n",
    }
    for path, content in preserved.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def check_preserved() -> None:
        for path, content in preserved.items():
            if not path.is_file() or path.read_bytes() != content:
                raise RuntimeError("Не сохранён пользовательский файл: " + str(path))
        if registry_snapshot() != before_registry:
            raise RuntimeError("Изолированная проверка изменила регистрацию приложения")

    invoke("reinstall-preserve", ["--install-test", "--target", str(target)])
    check_preserved()
    failure = invoke("commit-rollback", ["--install-test", "--target", str(target), "--fail-after", "0"], expected_exit=1)
    if "TEST" not in failure.get("code", ""):
        raise RuntimeError("Ошибка rollback-проверки вызвана не тестовой инъекцией")
    check_preserved()
    verify_inventory("inventory-after-rollback")
    invoke("uninstall-preserve", ["--uninstall-test", "--target", str(target)])
    check_preserved()
    if (target / "RemCard.exe").exists() or (target / "_internal").exists() or (target / "RemCardUninstall.exe").exists():
        raise RuntimeError("После удаления остались управляемые файлы программы")
    summary = {"ok": True, "installer": str(installer), "installer_sha256": sha256(installer), "qa_root": str(qa_root),
               "elapsed_seconds": round(time.monotonic() - started, 2), "tests": results,
               "user_files_preserved": True, "registry_unchanged": registry_snapshot() == before_registry,
               "release_profile": profile, "published": False}
    destination = reports / "acceptance-summary.json"
    destination.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "summary": str(destination), "elapsed_seconds": summary["elapsed_seconds"]}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
