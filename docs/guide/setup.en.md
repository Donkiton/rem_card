<p align="center">
  <a href="setup.ru.md"><kbd>Русский</kbd></a>
  <a href="setup.en.md"><kbd><strong>English</strong> ✓</kbd></a>
  <a href="setup.zh-CN.md"><kbd>中文（简体）</kbd></a>
  <a href="setup.ja.md"><kbd>日本語</kbd></a>
  <a href="setup.de.md"><kbd>Deutsch</kbd></a>
</p>

<p align="center">
  <a href="../../README.en.md">← README</a> ·
  <a href="features.en.md">Features</a> ·
  <a href="architecture.en.md">Architecture</a>
</p>

# Setup, updates and reference

## Quick start from source

The project is developed and tested on **Windows with Python 3.11**. Git and Python are required; no `.env` file is needed.

```powershell
git clone https://github.com/Donkiton/rem_card.git
cd rem_card
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python run_remcard.py
```

`run_remcard.py` opens the unified application with role selection.

> [!CAUTION]
> Use a separate data folder for development and testing, rather than the production database. Locks and access checks do not replace test-environment isolation. The unified shell uses a `SessionLease` for session admission.

<details>
<summary><strong>Development database: selection and protection against accidental replacement</strong></summary>

The source version uses a local development data folder; missing structure may be created on first launch. The selected-folder list and active path are stored separately for each checkout in `.remcard/dev_database_paths.json`.

An existing selected database is opened with checks: the application must not silently create an empty database in place of a missing one. The legacy `%LOCALAPPDATA%\RemCard\dev_database_paths.json` path is used only for a one-time settings migration.

</details>

## Installed version and updates

The Windows distribution is a complete PyInstaller folder, rather than an MSI package or Docker image.

| File | Purpose |
| --- | --- |
| `RemCard.exe` | Unified application: role selection, workspaces and data-folder setup. |
| `RemCardUpdater.exe` | Utility for installing a full update. |

Run **`RemCard.exe`**, select the data folder on first launch, then choose a role. Keep the entire distribution folder, including `_internal`: the EXE alone is insufficient. `remcard_data_path.json` is stored beside the application; updates are discovered in `<data folder>\UPD\releases`.

Separate `RemCardDoctor.exe`, `RemCardNurse.exe`, OR executables and `RemCardPathSetup.exe` belong to the previous distribution model. Version 5.1 uses one entry point. The project distributes **full updates only**: `PATCH` in SemVer denotes the version level, not a separate package format.

<details>
<summary><strong>Test builds and safe production publication</strong></summary>

Build the current working tree without publishing:

```powershell
.\.venv\Scripts\python.exe scripts\build_release.py --test-worktree
```

This runs mandatory checks, PyInstaller and smoke tests for both executables. It does not change `VERSION`, `CHANGELOG.md` or `app/release_info.json`, create a commit, run `git push` or publish a package. The output is marked `TEST_WORKTREE_ONLY.txt` so it cannot accidentally be published to production.

For a production release, the version and exact commit must first be in GitHub `main`; the release manager then runs `scripts\build_release.py` with `--expected-version` and `--expected-commit`. After acceptance testing on an isolated test database, the package is transferred using the separate `scripts\publish_full_update.py` command. `ready.ok` is created last: once it appears, the version is immediately available to clients of that database. See [build and publication](../how_to_build_and_update.md) and [automatic updates](../auto_update.md).

</details>

## Shared SQLite database and data safety

The medical database is at `archiv/rao_journal.db`; reference catalogs and shared settings, including theme and background parameters, are in `settings/remcard_settings.db`. Custom background images are in `settings/backgrounds`. These paths are relative to the selected data folder. Repository JSON catalogs provide seed data and a compatibility layer, but are not the installed version's operational source of truth. See the [settings database documentation](../settings_db.md).

The shared network database follows these rules:

- `journal_mode=DELETE`, `synchronous=EXTRA`, `mmap_size=0`; WAL is prohibited for the shared database.
- Writes go through the controller, a file lock and transactions.
- A live database is backed up only through the SQLite Backup API, not by copying an open file.
- Migrations, restoration and rotation use verified backups, `quick_check` and `integrity_check`.

These measures reduce known risks; they do not automatically make an arbitrary SQLite network deployment safe. Access permissions, latency, backup and restoration require separate operational acceptance testing. See [database safety](../db_safety_contract.md) and the [source-of-truth and synchronization contract](../source_of_truth_and_sync_contract.md).

## Developer checks

Local checks and GitHub Actions use the same entry point. After installing dependencies, run these in the activated environment:

```powershell
python scripts\run_ci_checks.py core
python scripts\run_ci_checks.py ui
python scripts\run_ci_checks.py quality
python scripts\run_ci_checks.py regression
git diff --check
```

`core` checks logic and data, `ui` checks the Qt interface, `quality` checks code quality and architectural constraints, and `regression` runs the full safety-scenario registry. In CI the groups run independently; the final `tests` status checks their success and report completeness. Pytest modules are assigned in [`tests/groups.json`](../../tests/groups.json) without omissions or duplicates.

`python scripts\run_ci_checks.py all` runs the entire pytest suite in one process to check shared isolation; it does not replace the separate `quality` and `regression` runs. The runner assigns temporary database and QSettings paths, saving reports in `tmp/ci-reports/<group>`. Standard `python -m pytest tests -q` is also available for direct testing. See [testing and CI](../testing.md).

Network test rigs, restore drills, emergency mode and offline mode read or modify the selected data folder. Run them only against a deliberately selected test database, following the [acceptance procedure](../operational_acceptance.md) and [backup verification guide](../backup_restore_drill.md).


## Release context

The application interface, screenshots and linked technical documentation are primarily in Russian.

Overview of **RemCard 5.1.10**, updated **October 22, 2026**. A single `RemCard.exe` opens a choice of four roles; the separate `RemCardUpdater.exe` installs full updates. The source version is recorded in [`VERSION`](../../VERSION), and each build's contents are recorded in its manifest.

In [release 5.1.10](https://github.com/Donkiton/rem_card/releases/tag/v5.1.10), small changes can reach the local replica as deltas even when the change history is large. Network reads of patient status run outside the nurse interface; late responses cannot replace newer data. The release also refines safe retries when saving initial status and loading the nightly backup after the shared database lock is released. Doctor–nurse collaboration checks are documented in the [synchronization validation report](../sync_responsiveness_validation.md); replica rules and measurement limits are described in the [replica contract](../source_of_truth_and_sync_contract.md) and [benchmark methodology](../replica_delta_benchmark.md).

The packaged 5.1.10 build disables both shared-database migrations and any increase in the minimum required client version. It is an **optional update** for compatible workstations: there is no need to update every computer at the same time. The initial full snapshot still depends on network speed and database size.

> [!WARNING]
> **RemCard is not a medical device and does not replace clinical protocols, clinical decisions or responsible professional oversight.** Real-world use requires independent technical and clinical review, acceptance testing in the intended environment, and a backup workflow.

## Documentation, status and contact

- [Documentation index](../README.md) — current procedures and historical materials, primarily in Russian.
- [Interactive architecture map](https://donkiton.github.io/rem_card/codemap/codemap.html) — modules, dependencies, tests and end-to-end flows linked to source.
- [Project website](https://donkiton.github.io/rem_site/#english) and [releases](https://github.com/Donkiton/rem_card/releases).
- [Changelog](../../CHANGELOG.md) and [`VERSION`](../../VERSION) — changes and the current version.
- [Testing and CI](../testing.md) — test groups, isolation, completeness checks and reports.
- [Burn calculator](../burn_infusion_calculator.md) and [analytics contract](../analytics_clinical_contract.md) — calculations, data sources and limitations.
- [Versioning](../versioning.md), [automatic updates](../auto_update.md) and [builds](../how_to_build_and_update.md) — the full-release process.
- [Database safety contract](../db_safety_contract.md), [source of truth and synchronization](../source_of_truth_and_sync_contract.md), [operational acceptance](../operational_acceptance.md) and [emergency runbook](../emergency_runbook.md) — safe operation.

RemCard is an experimental project under active development, built by anaesthesiologist-intensivist Vadim Bityutsky. Tests, static checks, backups and protective mechanisms do not replace independent technical, information-security and clinical audits.

For bug reports and suggestions about architecture, performance and safe operation, contact [menfise@mail.ru](mailto:menfise@mail.ru) with the subject `РЕМ КАРТА`.

## License and responsibility

RemCard's concept, architecture and original source code were created by its developer, Vadim Alekseevich Bityutsky (Битюцкий Вадим Алексеевич; GitHub: [DonKiton](https://github.com/DonKiton)).

RemCard is distributed under the [RemCard Proprietary Source License 1.1](../../LICENSE), which permits non-commercial use by individuals. An individual user or developer may view, download, copy, run, study and modify the code for free, and share original or modified versions without receiving money or other commercial benefit. Distribution must retain the license and author attribution, identify changes, provide source code and clearly mark modified versions as unofficial.

Any use by an organization or on behalf of an employer, client, business, clinic or institution requires the copyright holder's prior written permission. Without a separate agreement, sale and resale, paid deployment, installation, integration, modification, support, training, consulting, hosting, SaaS, subscriptions, advertising and other monetization are prohibited. These restrictions apply to all derivative versions: changing the font, interface, features, name or any other part of the application does not grant the right to earn money from it.

Public availability and forks on GitHub do not grant commercial rights. The software is provided “as is” and used at the user's own responsibility. Third-party libraries and dependencies retain their own licenses. Under the OSI classification this is a source-available license, rather than an open-source license, because commercial use is restricted. If this summary differs from the full license text, [LICENSE](../../LICENSE) takes precedence.
