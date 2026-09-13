"""Build a private manual-test sandbox from the current worktree, never publish."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def prepare_environment(work: Path) -> dict[str, str]:
    from app.isolated_test_runtime import user_profile_environment

    env = dict(os.environ)
    for key in list(env):
        if key.startswith("REMCARD_"):
            env.pop(key, None)
    # Native helper tools (e.g. Poppler) can put a different ICU ABI on PATH.
    # PyInstaller must resolve Windows/Qt DLLs from this Python and Windows,
    # never from an unrelated application's dependency directory.
    windows = Path(env.get("SystemRoot", r"C:\Windows"))
    native_paths = [
        Path(sys.executable).parent, Path(sys.base_prefix),
        Path(sys.base_prefix) / "DLLs", windows / "System32", windows,
        windows / "System32" / "Wbem",
    ]
    git = shutil.which("git", path=env.get("PATH", ""))
    if git:
        native_paths.append(Path(git).parent)
    env["PATH"] = os.pathsep.join(dict.fromkeys(str(path) for path in native_paths))
    env.update({
        **user_profile_environment(work / "build-state" / "userprofile"),
        "PYTHONUTF8": "1",
        "REMCARD_BAZA_DIR": str(work / "seed"),
        "REMCARD_SETTINGS_RELEASE_SOURCE_BAZA": str(work / "seed"),
        "REMCARD_LOCAL_LOGS_DIR": str(work / "build-state" / "logs"),
        "REMCARD_CI_SETTINGS_DIR": str(work / "build-state" / "qt"),
        "LOCALAPPDATA": str(work / "build-state" / "localappdata"),
        "APPDATA": str(work / "build-state" / "appdata"),
        "ProgramData": str(work / "build-state" / "programdata"),
        "REMCARD_ISOLATED_TEST_BUILD": "1",
    })
    return env


def create_seed(root: Path) -> None:
    from _local_rem_card_bootstrap import bootstrap_local_rem_card

    bootstrap_local_rem_card()
    from rem_card.app.runtime_paths import create_baza_structure_and_db
    from rem_card.app.emergency_password import set_emergency_password
    from rem_card.data.settings.settings_db import SettingsDatabase
    from rem_card.services.settings.settings_service import SettingsService

    ok, message = create_baza_structure_and_db(str(root))
    if not ok:
        raise RuntimeError(message)
    service = SettingsService(SettingsDatabase(baza_dir=str(root)))
    service.ensure_ready()
    set_emergency_password("test-2026", service, changed_by_user="test-bundle-builder")
    now = datetime.now().replace(microsecond=0).isoformat()
    with sqlite3.connect(str(root / "archiv" / "rao_journal.db")) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO patients (id, full_name, admission_uid, birth_date, last_name, first_name, middle_name) "
            "VALUES (1, 'Тестов Пациент Учебный', 'REM-TEST-ONLY-1', '1980-01-01', 'Тестов', 'Пациент', 'Учебный')"
        )
        conn.execute(
            "INSERT INTO admissions (id, patient_id, bed_number, history_number, admission_datetime, "
            "patient_age, patient_gender, diagnosis_text) VALUES (1, 1, 1, 'ТЕСТ-001', ?, 46, 'Мужской', "
            "'Учебный пациент. Не является медицинскими данными.')", (now,)
        )
        conn.execute(
            "INSERT OR REPLACE INTO beds (bed_number, status, current_admission_id) VALUES (1, 'OCCUPIED', 1)"
        )
        conn.execute(
            "INSERT INTO patient_status_events (admission_id, status, start_time, created_by) "
            "VALUES (1, 'ACTIVE', ?, 'test-bundle-builder')", (now,)
        )
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Synthetic seed integrity check failed")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("Synthetic seed foreign keys invalid")


def build_bundle(destination: Path) -> Path:
    destination = destination.resolve()
    if destination.exists():
        raise RuntimeError(f"Папка результата уже существует: {destination}")
    (ROOT / "tmp").mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="emergency-test-build-", dir=ROOT / "tmp"))
    print(f"Build workspace: {work}", flush=True)
    env = prepare_environment(work)
    os.environ.clear()
    os.environ.update(env)
    from app.isolated_test_runtime import isolate_qsettings

    Path(env["USERPROFILE"]).mkdir(parents=True, exist_ok=True)
    isolate_qsettings(env["REMCARD_CI_SETTINGS_DIR"])
    create_seed(work / "seed")
    # Official builder removes only these known generated directories.
    for name in ("build", "dist"):
        path = ROOT / name
        if path.resolve().parent != ROOT.resolve() or path.is_symlink():
            raise RuntimeError(f"Unsafe generated artifact directory: {path}")
    log_path = work / "build.log"
    print(f"Build log: {log_path}", flush=True)
    with log_path.open("w", encoding="utf-8") as log:
        subprocess.run(
            [sys.executable, "scripts/build_release.py", "--test-worktree", "--progress-json"],
            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, check=True,
        )
    destination.mkdir(parents=True)
    shutil.copytree(ROOT / "dist" / "Prog", destination / "Prog")
    shutil.copytree(work / "seed", destination / "Database")
    (destination / "TEST_SANDBOX.json").write_text(json.dumps({
        "schema_version": 1, "purpose": "RemCard emergency sandbox",
    }, indent=2), encoding="utf-8")
    for filename in ("START_TEST.cmd", "Test-RemCard.ps1", "README_RU.txt"):
        source = ROOT / "scripts" / "test_bundle" / filename
        content = source.read_text(encoding="utf-8-sig")
        encoding = "utf-8-sig" if filename.endswith(".ps1") else "utf-8"
        (destination / filename).write_text(content, encoding=encoding, newline="\r\n")
    (destination / "BUILD_INFO.txt").write_text(
        "Тестовая сборка текущего рабочего дерева. Не для обновления рабочей программы.\n"
        f"Дата: {datetime.now().isoformat(timespec='seconds')}\n"
        "База создана с нуля. Пациент Тестов Пациент Учебный, история ТЕСТ-001 — вымышленные данные.\n"
        "Аварийный пароль этой тестовой базы: test-2026\n"
        "Частота обновления резервной копии в тестовой сборке: 30 секунд, ожидание простоя: 5 секунд.\n",
        encoding="utf-8",
    )
    print(f"Bundle ready: {destination}", flush=True)
    return destination


def package_bundle(source: Path, output: Path) -> Path:
    """Preserve the database's empty required directories in the delivered ZIP."""
    from app.isolated_test_runtime import sandbox_paths

    source, output = source.resolve(), output.resolve()
    sandbox_paths(source)
    if output.is_relative_to(source):
        raise RuntimeError("ZIP должен находиться вне папки сборки.")
    if (source / "State").exists():
        raise RuntimeError("Упаковывать можно только сборку без состояния проверок.")
    partial = output.with_suffix(output.suffix + ".partial")
    if output.exists() or partial.exists():
        raise FileExistsError(f"Архив уже существует: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    prefix = "RemCard-Emergency-Test/"
    expected = set()
    with zipfile.ZipFile(partial, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(source.rglob("*")):
            name = prefix + path.relative_to(source).as_posix()
            if path.is_dir():
                name += "/"
            archive.write(path, name)
            expected.add(name)
    with zipfile.ZipFile(partial) as archive:
        if set(archive.namelist()) != expected or archive.testzip() is not None:
            raise RuntimeError("Проверка содержимого ZIP не пройдена.")
    partial.rename(output)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--archive", type=Path, help="Упаковать результат в ZIP с сохранением пустых папок")
    args = parser.parse_args()
    bundle = build_bundle(args.destination)
    if args.archive:
        print(f"Archive ready: {package_bundle(bundle, args.archive)}", flush=True)
